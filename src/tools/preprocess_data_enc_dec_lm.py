# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Processing data for pretraining."""

import argparse
import json
import multiprocessing
import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__),
                                             os.path.pardir)))
import time
import math
import torch
import torch.nn.functional as F
from tqdm import tqdm
import random
import numpy as np

from tokenization_enc_dec import EncDecTokenizer
from data import indexed_dataset

# from ray.util.multiprocessing.pool import Pool

random.seed(233)
np.random.seed(233)
g = torch.manual_seed(233)
torch.cuda.manual_seed_all(233)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

hash = {}
for i in [0,1,2,3,4,5]:
    for j in [0,1,2,3,4,5]:
        hash[(i,j)] = 0.0

def check(x, y):
    global hash
    a = [4,16,32,64,128,256]
    flag_1 = -1
    length = len(x)
    for index, i in enumerate(a):
        if length < i:
            flag_1 = index
            break
    flag_2 = -1
    length = len(y)
    for index, i in enumerate(a):
        if length < i:
            flag_2 = index
            break
    hash[(flag_1, flag_2)] += 1.0

class Encoder(object):
    def __init__(self, args):
        self.args = args

    def initializer(self):
        # Use Encoder class as a container for global data
        Encoder.tokenizer = EncDecTokenizer(os.path.join(self.args.tokenizer_path, 'vocab.txt'))

    def encode(self, line):
        # end with <eod>
        if len(line) > 5000000:
            return None, None, 0

        data = line.strip()
        data = data.replace("<n>", "\n")
        doc_ids = Encoder.tokenizer.encode(data)
        if len(doc_ids) < 12:
            return None, None, 0
        doc_ids.append(Encoder.tokenizer.eod_id)

        contexts = []
        labels = []

        i = 0
        while i < len(doc_ids):
            tmp = (int)(random.random() * 100)
            if tmp % 8 <= 3:
                tmp = 32
            elif tmp % 8 <= 5:
                tmp = 64
            elif tmp % 8 == 6:
                tmp = 128
            elif tmp % 8 == 7:
                tmp = 256
            else:
                assert 1>0
            piece = doc_ids[i:i+tmp-1+255]
            if len(piece) < 12:
                break
            context = piece[:tmp-1]
            label = piece[tmp-1:]

            if len(label) < 63:
                length = len(piece)
                if length < 16:
                    context = piece[:-7]
                    label = piece[-7:]
                elif length < 24:
                    context = piece[:7]
                    label = piece[7:]
                elif length < 32:
                    context = piece[:-15]
                    label = piece[-15:]
                elif length < 48:
                    context = piece[:15]
                    label = piece[15:]
                elif length < 64:
                    context = piece[:-31]
                    label = piece[-31:]
                elif length < 96:
                    context = piece[:31]
                    label = piece[31:]
                elif  length < 128:
                    context = piece[:-63]
                    label = piece[-63:]
                elif  length < 192:
                    context = piece[:63]
                    label = piece[63:]
                elif  length < 256:
                    context = piece[:-127]
                    label = piece[-127:]
                elif  length < 384:
                    context = piece[:127]
                    label = piece[127:]
                elif  length < 512:
                    context = piece[:-255]
                    label = piece[-255:]
            # print ("==========================")
            # print(Encoder.tokenizer.decode(context))
            # print("====>", Encoder.tokenizer.decode(label))
            # print ("--------------------------")
            # print()
            # print (len(context), len(label))
            assert (len(context) > 4  and len(context) < 256), (len(context), len(label), tmp) 
            assert (len(label) > 4 and len(label) < 256),  (len(context), len(label), tmp) 
            contexts.append(context)
            labels.append(label)
            i += (tmp - 1)
        return contexts, labels, len(line)


def get_args():
    parser = argparse.ArgumentParser()
    group = parser.add_argument_group(title='input data')
    group.add_argument('--input', default="/mnt/sfs_turbo/hx/CPM-2.1/raw_data/rmzb_baidu_baike.txt", type=str, help='Path to input TXT')
    
    group = parser.add_argument_group(title='tokenizer')
    group.add_argument('--tokenizer_path', default="/mnt/sfs_turbo/hx/CPM-2.1/bpe_cn", type=str, help='Path of tokenizer')

    group = parser.add_argument_group(title='output data')
    group.add_argument("--output_path", default="/mnt/sfs_turbo/hx/CPM-2.1/pretrain_data/", type=str)
    group.add_argument('--output_prefix', default="rmzb_baidu_baike_new_lm", type=str,
                       help='Path to binary output file without suffix')
    group.add_argument('--dataset_impl', type=str, default='mmap',
                       choices=['lazy', 'cached', 'mmap'])

    group = parser.add_argument_group(title='runtime')
    group.add_argument('--workers', type=int, default=32,
                       help='Number of worker processes to launch')
    group.add_argument('--log_interval', type=int, default=10000,
                       help='Interval between progress updates')

    args = parser.parse_args()
    args.keep_empty = False

    args.rank = 0
    args.make_vocab_size_divisible_by = 128

    return args

def main():
    args = get_args()
    startup_start = time.time()

    print("Opening", args.input)
    fin = open(args.input, 'r', encoding='utf-8')

    encoder = Encoder(args)
    tokenizer = EncDecTokenizer(os.path.join(args.tokenizer_path, 'vocab.txt'))
    # pool = Pool(args.workers, initializer=encoder.initializer)
    pool = multiprocessing.Pool(args.workers, initializer=encoder.initializer)
    
    # use the tokenizer to encode the sentences
    encoded_docs = pool.imap_unordered(encoder.encode, fin, 10)

    level = "document"

    print(f"Vocab size: {tokenizer.vocab_size}")
    print(f"Output prefix: {args.output_prefix}")
    context_bin_file = os.path.join(args.output_path, "{}_{}_context.bin".format(args.output_prefix, level))
    context_idx_file = os.path.join(args.output_path,  "{}_{}_context.idx".format(args.output_prefix, level))
    target_bin_file = os.path.join(args.output_path,  "{}_{}_target.bin".format(args.output_prefix, level))
    target_idx_file = os.path.join(args.output_path,  "{}_{}_target.idx".format(args.output_prefix, level))
    
    builder_context = indexed_dataset.make_builder(context_bin_file, impl=args.dataset_impl, dtype=np.uint16)
    builder_target = indexed_dataset.make_builder(target_bin_file, impl=args.dataset_impl, dtype=np.uint16)

    startup_end = time.time()
    proc_start = time.time()
    total_bytes_processed = 0
    print("Time to startup:", startup_end - startup_start)

    # sentinel_idx = tokenizer.vocab_size # start from the last token of the tokenizer
    print("tokenizer vocab size:", tokenizer.vocab_size)
    for i, (pair_ids, label_ids, bytes_processed) in enumerate(encoded_docs, start=1):
        if pair_ids is None or label_ids is None:
            continue
        total_bytes_processed += bytes_processed

        for pids, lids in zip(pair_ids, label_ids):
            builder_context.add_item(torch.IntTensor(pids))
            builder_target.add_item(torch.IntTensor(lids))
        
        if i % args.log_interval == 0:
            current = time.time()
            elapsed = current - proc_start
            mbs = total_bytes_processed / elapsed / 1024 / 1024
            print(f"Processed {i} documents",
                  f"({i/elapsed} docs/s, {mbs} MB/s).",
                  file=sys.stderr)

    builder_context.finalize(context_idx_file)
    builder_target.finalize(target_idx_file)

    pool.close()

if __name__ == '__main__':
    main()
