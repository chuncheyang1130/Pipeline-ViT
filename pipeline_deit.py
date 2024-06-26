# Copyright (c) Meta Platforms, Inc. and affiliates
# test
import torch
from torch import nn
from typing import Any
import time
import numpy as np
from tqdm.auto import tqdm
from collections import defaultdict
from transformers import DeiTImageProcessor, DeiTForImageClassification, ViTForImageClassification, DeiTModel
import timm
import pippy
from pippy.IR import annotate_split_points, Pipe, PipeSplitWrapper

from util import *
from quant_util import *


import os
import copy

from PIL import Image
import requests
from accelerate import Accelerator
import torch.distributed.rpc as rpc
import torch.profiler as profiler
import logging

import argparse

# parallel-scp -r -A -h ~/hosts.txt ~/Pipeline-ViT/ ~/
# torchrun   --nnodes=2   --nproc-per-node=1   --node-rank=0   --master-addr=192.168.1.102   --master-port=50000   pipeline_deit.py

def set_split_point(model, nproc):

    if nproc == 2:
        annotate_split_points(model, {'blocks.5': PipeSplitWrapper.SplitPoint.END})
    elif nproc == 4:
        annotate_split_points(model, {'blocks.2': PipeSplitWrapper.SplitPoint.END, 
                                      'blocks.5': PipeSplitWrapper.SplitPoint.END, 
                                      'blocks.8': PipeSplitWrapper.SplitPoint.END})
    elif nproc == 6:
        annotate_split_points(model, {'blocks.1': PipeSplitWrapper.SplitPoint.END, 
                                      'blocks.3': PipeSplitWrapper.SplitPoint.END, 
                                      'blocks.5': PipeSplitWrapper.SplitPoint.END, 
                                      'blocks.7': PipeSplitWrapper.SplitPoint.END, 
                                      'blocks.9': PipeSplitWrapper.SplitPoint.END})

    # model.print_readable()

    return model


def run_serial(model, imgs):

    result = None

    # for i in tqdm(range(num_iter)):
    for img in tqdm(imgs):
        
        if result == None:
            output = model(img)
            result = output
        else:
            output = model(img)
            result = torch.cat((result, output), dim=0)


def run_pipeline(stage, rank, world_size, imgs=None):

    if rank == 0:
        stage(imgs)
    elif rank == world_size-1:
        output = stage()
    else:
        stage()
    
    if rank == world_size-1:
        return output
    else:
        return None
    

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chunk_size', type=int, default=1)
    parser.add_argument('--num_threads', type=int, default=1)
    parser.add_argument('--num_interop_threads', type=int, default=4)
    args = parser.parse_args()


    # MODEL_NAME = "facebook/deit-small-distilled-patch16-224"
    # MODEL_NAME = "facebook/deit-small-patch16-224"
    # MODEL_NAME = "facebook/deit-tiny-distilled-patch16-224"
    # MODEL_NAME = "facebook/deit-tiny-patch16-224"

    DEVICE = "cpu"
    torch.set_num_threads(args.num_threads)
    torch.set_num_interop_threads(args.num_interop_threads)
    print(f'intra op threads num: {torch.get_num_threads()} | inter op threads num: {torch.get_num_interop_threads()}')

    WARMUP = 0
    NUM_TEST = 1
    # NUM_IMGS = 200

    # MINI_BATCH_SIZE = 2
    MINI_BATCH_SIZE = args.chunk_size
    NUM_CHUNKS = 200

    SERIAL_BATCH_SIZE = MINI_BATCH_SIZE
    # PIPELINE_BATCH_SIZE = NUM_CHUNKS * MINI_BATCH_SIZE
    NUM_IMGS = NUM_CHUNKS * MINI_BATCH_SIZE
    # INPUT_PER_ITER = 4

    torch.manual_seed(0)
        
    import os
    rank = int(os.environ["RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    os.environ["TP_SOCKET_IFNAME"]="eth0" 
    os.environ["GLOO_SOCKET_IFNAME"]="eth0"
    os.environ["GLOO_TIMEOUT_SECONDS"] = "3600"

    import torch.distributed as dist
    dist.init_process_group(backend='gloo', rank=rank, world_size=world_size)

    # import torch.distributed.rpc as rpc
    # rpc.init_rpc(f"worker{rank}", rank=rank, world_size=world_size, backend=rpc.BackendType.TENSORPIPE)

    # rpc.init_rpc(f"worker{rank}", rank=rank, world_size=world_size, backend=rpc.BackendType.TENSORPIPE)
    # rpc.init_rpc(
    #     f"worker{rank}", 
    #     rank=rank, 
    #     world_size=world_size, 
    #     rpc_backend_options=rpc.TensorPipeRpcBackendOptions(
    #         rpc_timeout=500
    #     )
    # )

    print(f"**************** My Rank: {rank} ****************")
    print(f'RANK:{os.environ["RANK"]}')
    print(f'LOCAL_RANK:{os.environ["LOCAL_RANK"]}')
    print(f'WORLD_SIZE:{os.environ["WORLD_SIZE"]}')
    print(f'LOCAL_WORLD_SIZE:{os.environ["LOCAL_WORLD_SIZE"]}')
    print(f'intra op threads num: {torch.get_num_threads()} | inter op threads num: {torch.get_num_interop_threads()}')

    print()

    # from pippy.microbatch import TensorChunkSpec

    # args_chunk_spec: Any = (TensorChunkSpec(0),)
    # kwargs_chunk_spec: Any = {}
    # output_chunk_spec: Any = TensorChunkSpec(0)


    serial_input = torch.randn(NUM_CHUNKS, SERIAL_BATCH_SIZE, 3, 224, 224)
    pipeline_input = torch.randn(NUM_IMGS, 3, 224, 224)
    # imgs = torch.randn(NUM_IMGS, 3, 224, 224)

    train_loader, test_loader, nb_classes = prepare_data(SERIAL_BATCH_SIZE, data='cifar-100')
    # train_loader, test_loader, nb_classes = prepare_data(SERIAL_BATCH_SIZE, data='cifar-10')

    model = torch.load("./0.9099_deit3_small_patch16_224.pth", map_location='cpu')
    # model = torch.load("./mobilenet_0.9.pth", map_location='cpu')

    model = set_split_point(model, world_size)


    dist.barrier()

    # With NO split policy
    pipe = Pipe.from_tracing(model, NUM_CHUNKS, example_args=(pipeline_input, ))
    # With split policy
    # split_policy = pippy.split_into_equal_size(world_size)
    # pipe = Pipe.from_tracing(model, NUM_CHUNKS, example_args=(pipeline_input, ), split_policy=split_policy)
    # print(pipe)

    nstages = len(list(pipe.split_gm.children()))
    if rank == 0:

        print(" Original module params ".center(80, "*"))
        params = sum(p.numel() for p in model.state_dict().values())   
        print(f"Original module params: {params / (10 ** 6)}M params")

        for i, sm in enumerate(pipe.split_gm.children()):
            params = sum(p.numel() for p in sm.state_dict().values())
            print(f"Pipeline Stage {i} params: {params / (10 ** 6)}M params")


    from pippy.PipelineStage import PipelineStage
    stage = PipelineStage(pipe, rank, DEVICE)

    # for i, sm in enumerate(pipe.split_gm.children()):
    #     if rank == i:
    #         sm.print_readable()
        
    dist.barrier()

    '''
    Running Pipeline
    '''

    fps_list = []
        
    print("Running Pipeline...")
    with torch.no_grad():

        # for i in tqdm(range(1, NUM_TEST+WARMUP+1)):
        for i in range(1, NUM_TEST+WARMUP+1):
            
            '''
            To be fair, all threads has to be on same point
            '''

            dist.barrier()

            start_time = time.perf_counter()
            pipeline_output = run_pipeline(stage=stage, imgs=pipeline_input, rank=rank, world_size=world_size)
            end_time = time.perf_counter()

            elapsed_time = torch.tensor(end_time - start_time)
            print(f"Rank {rank} Elapsed Time: {elapsed_time.item()}")

            dist.barrier()

            dist.reduce(elapsed_time, dst=world_size-1, op=torch.distributed.ReduceOp.MAX)

            if rank == world_size-1:
                print(f"Reduced Elapsed Time: {elapsed_time.item()}")

            if i <= WARMUP:
                continue

            if rank == world_size - 1:
                fps = NUM_IMGS / elapsed_time.item()
                fps_list.append(fps)


    if rank == world_size - 1:
        print('Throughput with %d pipeline stages (mini batch size = %d): %.4f fps'%(world_size, MINI_BATCH_SIZE, np.mean(fps_list)), end='\n\n')
        pipeline_fps = np.mean(fps_list)
        # torch.testing.assert_close(pipeline_output, reference_output)

        # print(" Pipeline parallel model ran successfully! ".center(80, "*"))


    # '''
    # Running Serial
    # '''

    fps_list = []

    print("Running Serial...")

    with torch.no_grad():
        for i in range(1, NUM_TEST+WARMUP+1):
            
            # tmp_imgs = torch.unsqueeze(imgs, dim=1)

            start_time = time.perf_counter()
            reference_output = run_serial(model=model, imgs=serial_input)
            end_time = time.perf_counter()

            elapsed_time = torch.tensor(end_time - start_time)

            print(f"Rank {rank} Elapsed Time: {elapsed_time.item()}")

            dist.barrier()

            dist.reduce(elapsed_time, dst=world_size-1, op=torch.distributed.ReduceOp.MAX)
            
            if i <= WARMUP:
                continue

            if rank == world_size - 1:
                fps = NUM_IMGS / elapsed_time.item()
                fps_list.append(fps)

        if rank == world_size - 1:
            print('Throughput without pipeline (mini batch size = %d): %.4f fps'%(MINI_BATCH_SIZE, np.mean(fps_list)), end='\n\n')
            serial_fps = np.mean(fps_list)

            print(f'speed up: {pipeline_fps/serial_fps}')

    dist.destroy_process_group()
    # rpc.shutdown()

if __name__ == "__main__":
    main()