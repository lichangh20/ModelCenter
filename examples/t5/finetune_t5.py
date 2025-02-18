import time
import random
import os
import csv

import torch
import numpy as np
from sklearn.metrics import accuracy_score, f1_score

import bmtrain as bmt

from model_center import get_args
from model_center.model import T5, T5Config
from model_center.tokenizer import T5Tokenizer
from model_center.dataset.t5dataset import DATASET
from model_center.utils import print_inspect
from model_center.dataset import DistributedDataLoader


def get_tokenizer(args):
    tokenizer = T5Tokenizer.from_pretrained(args.model_config)
    return tokenizer

def get_model(args):
    # model = T5.from_pretrained(args.model_config)
    config = T5Config.from_pretrained(args.model_config)
    model = T5(config)
    bmt.init_parameters(model)
    return model

def get_optimizer(args, model):
    optimizer = bmt.optim.AdamOffloadOptimizer(model.parameters(), weight_decay=args.weight_decay)
    return optimizer

def get_learning_rate_scheduler(args, optimizer):
    if args.lr_decay_iters is None:
        args.lr_decay_iters = args.train_iters * args.epochs
    if args.lr_decay_style == "noam":
        lr_scheduler = bmt.lr_scheduler.Noam(optimizer, 
                                            start_lr = args.lr,
                                            warmup_iter = args.warmup_iters, 
                                            end_iter = args.lr_decay_iters,
                                            num_iter = args.start_step)
    elif args.lr_decay_style == "constant":
        lr_scheduler = bmt.lr_scheduler.NoDecay(optimizer, 
                                            start_lr = args.lr,
                                            warmup_iter = args.warmup_iters, 
                                            end_iter = -1,
                                            num_iter = args.start_step)
    elif args.lr_decay_style == "linear":
        lr_scheduler = bmt.lr_scheduler.Linear(optimizer, 
                                            start_lr = args.lr,
                                            warmup_iter = args.warmup_iters, 
                                            end_iter = args.lr_decay_iters,
                                            num_iter = args.start_step)
    elif args.lr_decay_style == "exponential":
        lr_scheduler = bmt.lr_scheduler.Exponential(optimizer, 
                                            start_lr = args.lr,
                                            warmup_iter = args.warmup_iters, 
                                            end_iter = args.lr_decay_iters,
                                            num_iter = args.start_step)
    elif args.lr_decay_style == "cosine":
        lr_scheduler = bmt.lr_scheduler.Cosine(optimizer, 
                                            start_lr = args.lr,
                                            warmup_iter = args.warmup_iters, 
                                            end_iter = args.lr_decay_iters,
                                            num_iter = args.start_step)
    else:
        raise ValueError(f"lr_scheduler of type {args.lr_decay_style} is not supported yet.")

    return lr_scheduler

def setup_model_and_optimizer(args):
    # get the tokenizer
    tokenizer = get_tokenizer(args)
    # get the model
    model = get_model(args)
    bmt.synchronize()
    # get the optimizer and lr_scheduler
    optimizer = get_optimizer(args, model)
    lr_scheduler = get_learning_rate_scheduler(args, optimizer)
    bmt.synchronize()
    # get the memory usage
    bmt.print_rank("Model mem\n", torch.cuda.memory_summary())
    bmt.synchronize()
    return tokenizer, model, optimizer, lr_scheduler

def initialize():
    # get arguments
    args = get_args()
    # init bmt 
    bmt.init_distributed(seed = args.seed)
    # init save folder
    if args.save != None:
        os.makedirs(args.save, exist_ok=True)
    return args


def prepare_dataset(args, tokenizer, base_path, dataset_name, rank, world_size):
    splits = ['train', 'dev', 'test']
    dataset = {}
    for split in splits:
        dataset[split] = DATASET[dataset_name](base_path, split, rank, world_size, tokenizer, args.max_encoder_length, args.max_decoder_length)
    verbalizer = torch.LongTensor(DATASET[dataset_name].get_verbalizer(tokenizer)).cuda()
    return dataset, verbalizer


def finetune(args, tokenizer, model, optimizer, lr_scheduler, dataset, verbalizer):
    output_dir = '../result/{}/{}/'\
        .format(args.model_config, args.dataset_name)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    if args.local_rank == 0:
        with open(os.path.join(output_dir, "token.txt"), "w") as f:
            time_tuple = time.localtime(time.time())
            print('Time {}/{:02d}/{:02d} {:02d}:{:02d}:{:02d}:'
                .format(time_tuple[0], time_tuple[1], time_tuple[2], time_tuple[3],
                        time_tuple[4], time_tuple[5]), file=f)
    loss_func = bmt.loss.FusedCrossEntropy(ignore_index=-100)

    optim_manager = bmt.optim.OptimManager(loss_scale=args.loss_scale, loss_scale_steps=100)
    optim_manager.add_optimizer(optimizer, lr_scheduler)

    # print_inspect(model, '*')

    bmt.print_rank(verbalizer)

    for epoch in range(20):
        dataloader = {
            "train": DistributedDataLoader(dataset['train'], batch_size=args.batch_size, shuffle=True),
            "dev": DistributedDataLoader(dataset['dev'], batch_size=args.batch_size, shuffle=False),
        }

        model.train()
        epoch_token_num = 0
        epoch_time = 0
        if args.local_rank == 0:
            with open(os.path.join(output_dir, "token.txt"), "a") as f:
                print("Epoch {}:".format(epoch+1), file=f)
        for it, data in enumerate(dataloader['train']):
            enc_input = data["enc_input"]
            enc_length = data["enc_length"]
            dec_input = data["dec_input"]
            dec_length = data["dec_length"]
            targets = data["targets"]
            index = data["index"]
            batch_token_num = enc_input.numel() + dec_input.numel()
            epoch_token_num += batch_token_num
            
            torch.cuda.synchronize()
            st_time = time.time()

            logits = model(enc_input, enc_length, dec_input, dec_length, output_logits=True).logits
            logits = logits.index_select(dim=-1, index=verbalizer)
            logits = logits[torch.where(index==1)]

            loss = loss_func(logits, targets)
            global_loss = bmt.sum_loss(loss).item()

            optim_manager.zero_grad()

            optim_manager.backward(loss)
            grad_norm = optim_manager.clip_grad_norm(optimizer.param_groups, args.clip_grad, norm_type = 2)

            optim_manager.step()
            
            torch.cuda.synchronize()
            elapsed_time = time.time() - st_time
            epoch_time += elapsed_time

            bmt.print_rank(
                "train | epoch {:3d} | Iter: {:6d}/{:6d} | loss: {:.4f} | lr: {:.4e}, scale: {:10.4f} | grad_norm: {:.4f} | time: {:.3f} | tokens/s: {:.1f}".format(
                    epoch,
                    it,
                    len(dataloader["train"]),
                    global_loss,
                    lr_scheduler.current_lr,
                    int(optim_manager.loss_scale),
                    grad_norm,
                    elapsed_time,
                    batch_token_num * 8/ elapsed_time,
                )
            )
            if args.local_rank == 0:
                with open(os.path.join(output_dir, "token.txt"), "a") as f:
                    print("    iter {}: {:.1f} token/s".format(it, batch_token_num * 8 / elapsed_time), file=f)
            # if it % args.inspect_iters == 0: print_inspect(model, "*")
            # if args.save != None and it % args.save_iters == 0:
            #     bmt.save(model, os.path.join(args.save, args.save_name+("-%d.pt" % it)))
        if args.local_rank == 0:
            with open(os.path.join(output_dir, "token.txt"), "a") as f:
                print("    batch {}: {:.1f} token/s".format(epoch+1, epoch_token_num * 8 / epoch_time), file=f)
        model.eval()
        with torch.no_grad():
            for split in ['dev']:
                pd = []
                gt = []
                for it, data in enumerate(dataloader[split]):
                    enc_input = data["enc_input"]
                    enc_length = data["enc_length"]
                    dec_input = data["dec_input"]
                    dec_length = data["dec_length"]
                    targets = data["targets"]
                    index = data["index"]

                    logits = model(enc_input, enc_length, dec_input, dec_length, output_logits=True).logits
                    logits = logits.index_select(dim=-1, index=verbalizer)
                    logits = logits[torch.where(index==1)]
                    logits = logits.argmax(dim=-1)
                
                    pd.extend(logits.cpu().tolist())
                    gt.extend(targets.cpu().tolist())

                    bmt.print_rank(
                        "{} | epoch {:3d} | Iter: {:6d}/{:6d} |".format(
                            split,
                            epoch,
                            it,
                            len(dataloader[split]),
                        )
                    )
                pd = bmt.gather_result(torch.tensor(pd).int()).cpu().tolist()
                gt = bmt.gather_result(torch.tensor(gt).int()).cpu().tolist()
                bmt.print_rank(pd)
                bmt.print_rank(gt)
                
                bmt.print_rank(f"{split} epoch {epoch}:")
                if args.dataset_name in ["BoolQ", "CB", "COPA", "RTE", "WiC", "WSC"]:
                    acc = accuracy_score(gt, pd)
                    bmt.print_rank(f"accuracy: {acc*100:.2f}")
                if args.dataset_name in ["CB"]:
                    f1 = f1_score(gt, pd, average="macro")
                    bmt.print_rank(f"Average F1: {f1*100:.2f}")


def main():
    args = initialize()
    tokenizer, model, optimizer, lr_scheduler = setup_model_and_optimizer(args)
    dataset, verbalizer = prepare_dataset(
        args,
        tokenizer,
        f"{args.base_path}/down_data/superglue/",
        args.dataset_name,
        bmt.rank(), bmt.world_size(),
    )
    finetune(args, tokenizer, model, optimizer, lr_scheduler, dataset, verbalizer)

if __name__ == "__main__":
    main()
