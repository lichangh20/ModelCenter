#! /bin/bash

MASTER_ADDR=localhost
MASTER_PORT=12345
NNODES=1
NODE_RANK=0
GPUS_PER_NODE=4

DISTRIBUTED_ARGS="--nproc_per_node $GPUS_PER_NODE \
                  --nnodes $NNODES \
                  --node_rank $NODE_RANK \
                  --master_addr $MASTER_ADDR \
                  --master_port $MASTER_PORT"

BASE_PATH="/home/bingxing2/home/scx6117/Lab/ModelCenter"
VERSION="base"
DATASET="RTE"

OPTS=""
OPTS+=" --dataset ${DATASET}"
OPTS+=" --base-path ${BASE_PATH}"
OPTS+=" --model-config gpt2-${VERSION}"
OPTS+=" --batch-size 60"
OPTS+=" --train-iters 800"
OPTS+=" --save-iters 1000"
OPTS+=" --max-decoder-length 512"
OPTS+=" --save ${BASE_PATH}/results"
OPTS+=" --save-name finetune-gpt2-ckpt"
OPTS+=" --lr 0.00005"
OPTS+=" --inspect-iters 100"
OPTS+=" --warmup-iters 100"
OPTS+=" --lr-decay-style constant"
OPTS+=" --weight-decay 1e-3"
OPTS+=" --clip-grad 10.0"
OPTS+=" --loss-scale 128"
# OPTS+=" --load ${BASE_PATH}/results/GPT2-${VERSION}.pt"

CMD="python3 -m torch.distributed.launch ${DISTRIBUTED_ARGS} ${BASE_PATH}/examples/gpt2/finetune_gpt2.py ${OPTS}"
echo ${CMD}

${CMD} 2>&1 | tee ${BASE_PATH}/logs/gpt2_superglue/finetune-gpt2-${VERSION}-${DATASET}.log
