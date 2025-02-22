import argparse
import gc
import json
import time
from collections import defaultdict
from functools import reduce

import torch
from param_bench.train.compute.python.tools.eg_replay_utils import (
    has_backward_parent,
    is_backward_aten,
)
from param_bench.train.compute.python.tools.execution_graph import NodeType
from torch.profiler import ExecutionGraphObserver

from ..lib import pytorch as lib_pytorch
from ..lib.init_helper import load_modules
from ..workloads import pytorch as workloads_pytorch
from .eg_replay_utils import (
    build_fbgemm_func,
    build_torchscript_func,
    fbgemm_input_args_indices,
    generate_fbgemm_tensors,
    get_input_tensors,
    get_output_tensors,
    is_fbgemm_backward,
    is_fbgemm_forward,
    is_fbgemm_forward_unweighted,
    is_qualified,
    is_tensor,
    is_tensor_list,
    TORCH_DTYPES_BYTES,
    TORCH_DTYPES_RNG,
    trace_handler,
)
from .execution_graph import ExecutionGraph


class ExgrReplayManager:
    def __init__(self, exgr, args):
        with open(exgr, 'r') as f:
            self.exgr = ExecutionGraph(json.load(f))
        self.numWarmupIters = args.warmup
        self.numIters = args.iter
        self.profile_replay = args.profile_replay
        self.profile_memory = args.profile_memory

        # Permanent
        self.tensor_registry_permanent = {}
        self.dependency_permanent = defaultdict(int)
        self.sorted_nodes = []
        self.funcs = {}
        # Mark some intermediate tensors (output of operators) as unchangeable
        self.unchangeable_intermediate_tensors = set()
        # Unique tensors in execution graph identified by [tensor_id, storage_id, offset, num_elem, elem_bytes]
        self.original_unique_tensors = set()
        # Number Unique tensors in replay since unique tensors in eg may have multiple shapes and to accommodate that
        # in replay we treat tensors with same identifier but different shapes as different tensors
        self.replay_unique_tensor_num = 0
        # Map unique tensor with the node id of its operation in eg to unique tensors in replay. We assume
        # the shape of a tensor for an operation keeps the same (e.g., a tensor can be both input and output)
        self.tensors_mapping = {}
        # Dict that stores the shape of each unique tensor in replay
        self.replay_tensors_shapes = {}
        # Dict that stores the shapes of a tensor that has appeared, for the convenience of quickly determining whether
        # to create a unique tensor in replay if the identifier is same but shape is different
        self.tensor_shapes = defaultdict(set)
        # Mark those tensors that occur first as an input in the original run as needing to be instantiated in replay
        # at the very beginning
        self.instantiate = set()
        # Tensors that should be instantiated on cpu, e.g., input of aten::pin_memory and aten::to
        self.cpu_tensor = set()
        # Temporary
        self.tensor_registry = {}
        # Skip the node if their names contain any of the following strings.
        self.skip_node_names = ["DataLoader", "aten::set_"]

        if self.profile_memory:
            self.current_allocated_mem = 0
            self.current_reserved_mem = 0
            self.op_allocated_mem = {}
            self.op_reserved_mem = {}

        self.cuda = torch.device('cuda:0')

        self.fbgemm_backward_ops = []

        self.additional_tensors = set()
        self.top_tensors = {}
        self.additional_tensors_size = 0


    def reset_registry(self):
        self.tensor_registry = {k: (None if v is None else (v if k in self.cpu_tensor else v.cuda(self.cuda))) for k, v in self.tensor_registry_permanent.items()}
        gc.collect()
        torch.cuda.empty_cache()


    def extract_subgraph(self, root):
        """
            return: all nodes in the subgraph, in the order of node ID
        """
        def _dfs_traverse(root):
            for child in root.children:
                try:
                    if any(x in child.name for x in self.skip_node_names):
                        continue

                    if is_qualified(child):
                        self.sorted_nodes.append(child)

                        self.top_tensors[child] = set()
                        for _, t_id, _ in get_input_tensors(child):
                            self.top_tensors[child].add(t_id)
                        for _, t_id, _ in get_output_tensors(child):
                            self.top_tensors[child].add(t_id)

                        # Tensors dependency
                        for _, t_id, _ in get_input_tensors(child):
                            self.dependency_permanent[t_id] += 1

                        # Build aten funcs
                        func, output_count = self.build_func(child)
                        self.funcs[child.id] = (func, output_count)
                    else:
                        _dfs_traverse(child)
                except Exception as e:
                    print(f"Graph parse error: {e}, node id: {child.id}")
                    exit(1)

        _dfs_traverse(root)
        self.sorted_nodes = sorted(self.sorted_nodes, key=lambda x: x.id)
        print("#Operations to execute: ", len(self.sorted_nodes))


    def analyze_subgraph(self, root):
        def _bfs_traverse(node):
            for child in node.children:
                if any(x in child.name for x in self.skip_node_names):
                    continue

                if is_backward_aten(child) or has_backward_parent(child):
                    continue
                else:
                    if child not in self.sorted_nodes and child.type == NodeType.OPERATOR:
                        node = child.parent
                        while (node not in self.sorted_nodes):
                            node = node.parent
                        for data_type, t_id, shape in get_output_tensors(child):
                            if t_id not in self.top_tensors[node] and \
                                t_id in self.dependency_permanent and t_id not in self.additional_tensors:
                                self.additional_tensors.add(t_id)
                                if shape:
                                    self.additional_tensors_size += reduce(lambda x,y:x*y, shape) * TORCH_DTYPES_BYTES[data_type.lstrip('Tensor(').rstrip(')')]
                    _bfs_traverse(child)

        _bfs_traverse(root)
        print(f"Additional allocated {len(self.additional_tensors)} tensors with total size of {self.additional_tensors_size/1024/1024}MB")


    def analyze_tensors(self):
        def add_unique_tensor(node_id, t_id, shape):
            # If we did not see this tensor before, add it as a unique tensor
            if t_id not in self.original_unique_tensors:
                self.original_unique_tensors.add(t_id)
                self.replay_unique_tensor_num += 1
                self.tensors_mapping[(node_id, t_id)] = self.replay_unique_tensor_num
                self.replay_tensors_shapes[self.tensors_mapping[(node_id, t_id)]] = shape
                self.tensor_shapes[t_id].add((self.tensors_mapping[(node_id, t_id)], tuple(shape)))
                return

            # If we saw this tensor before but with a different shape, add it as a unique tensor
            for (relay_t_id, pre_shape) in self.tensor_shapes[t_id]:
                if tuple(shape) == pre_shape:
                    self.tensors_mapping[(node_id, t_id)] = relay_t_id
                    return

            self.replay_unique_tensor_num += 1
            self.tensors_mapping[(node_id, t_id)] = self.replay_unique_tensor_num
            self.replay_tensors_shapes[self.tensors_mapping[(node_id, t_id)]] = shape
            self.tensor_shapes[t_id].add((self.tensors_mapping[(node_id, t_id)], tuple(shape)))


        for node in self.sorted_nodes:
            for _, t_id, shape in get_input_tensors(node):
                if t_id in self.dependency_permanent.keys():
                    add_unique_tensor(node.id, t_id, shape)

            for _, t_id, shape in get_output_tensors(node):
                if t_id in self.dependency_permanent.keys():
                    add_unique_tensor(node.id, t_id, shape)

        # Simulate the execution progress and record the output tensors we have seen so far
        output_set = set()
        for node in self.sorted_nodes:
            for _, t_id, _ in get_input_tensors(node):
                if t_id in self.dependency_permanent.keys() and self.tensors_mapping[(node.id, t_id)] not in output_set:
                    self.instantiate.add(self.tensors_mapping[(node.id, t_id)])

            for _, t_id, _ in get_output_tensors(node):
                if t_id in self.dependency_permanent.keys():
                    output_set.add(self.tensors_mapping[(node.id, t_id)])


    def allocate_tensors(self):
        # Instantiation of tensors:
        for node in self.sorted_nodes:
            if is_fbgemm_forward(node):
                input_args, _ = generate_fbgemm_tensors(node)
            for idx, (data_type, t_id, shape) in enumerate(get_input_tensors(node)):
                replay_t_id = self.tensors_mapping[(node.id, t_id)]
                if t_id in self.dependency_permanent.keys() and \
                        replay_t_id not in self.tensor_registry_permanent.keys() and \
                        (node.name == "aten::embedding_bag" or node.name == "fbgemm::split_embedding_codegen_lookup_sgd_function" \
                        or replay_t_id in self.instantiate):
                    try:
                        if is_fbgemm_forward(node):
                            self.tensor_registry_permanent[replay_t_id] = input_args[idx]
                            if node.name == "fbgemm::split_embedding_codegen_lookup_sgd_function":
                                self.unchangeable_intermediate_tensors.add(replay_t_id)
                        else:
                            dtype, rng = TORCH_DTYPES_RNG[data_type.lstrip('Tensor(').rstrip(')')]
                            self.tensor_registry_permanent[replay_t_id] = rng(shape).to(dtype)
                            if node.name == "aten::embedding_bag":
                                self.unchangeable_intermediate_tensors.add(replay_t_id)
                            if node.name == "aten::pin_memory" and idx == 0:
                                self.cpu_tensor.add(replay_t_id)
                    except KeyError:
                        if data_type != 'Tensor(nullptr (uninitialized))':
                            print("KeyError: ", node.id, t_id, data_type)
                        self.tensor_registry_permanent[replay_t_id] = None

            ######
            # Workaround to match offsets for embedding table
            # Currently assume a uniform distribution
            if node.name == "aten::embedding_bag":
                indices_tensor_shape = node.input_shapes[1][0]
                offsets_tensor_shape = node.input_shapes[2][0]
                nnz = indices_tensor_shape / offsets_tensor_shape
                for i in range(offsets_tensor_shape):
                   self.tensor_registry_permanent[self.tensors_mapping[(node.id, node.inputs[2])]][i] = i * nnz
            ######


    def build_func(self, node):
        if is_fbgemm_forward(node):
            func, output_count = build_fbgemm_func(node)
            self.fbgemm_backward_ops.append(func.backward)
            return func.forward, output_count
        elif is_fbgemm_backward(node):
            assert self.fbgemm_backward_ops
            return self.fbgemm_backward_ops.pop(-1), len(node.output_types)
        return build_torchscript_func(node)


    def preprocess_graph(self):
        nodes = self.exgr.get_nodes(clean=True)
        root_node = nodes[1] # 1-base

        self.extract_subgraph(root_node)
        self.analyze_subgraph(root_node)

        self.analyze_tensors()

        tensor_with_multiple_shape_count = 0
        for tensor in self.tensor_shapes:
            if len(self.tensor_shapes[tensor]) != 1:
                tensor_with_multiple_shape_count += len(self.tensor_shapes[tensor])
        print(f"Tensor count with same identifier but different shapes:{tensor_with_multiple_shape_count}, total tensor: {len(self.tensor_shapes)}")

        self.allocate_tensors()
        self.reset_registry()


    def get_inputs(self, node):
        try:
            if is_fbgemm_forward(node):
                idx_list = fbgemm_input_args_indices(node)
                inputs = [self.tensor_registry[self.tensors_mapping[(node.id, tuple(node.inputs[idx]))]] for idx in idx_list]
                if is_fbgemm_forward_unweighted(node):
                    inputs.append(None)
            else:
                inputs = []
                for idx, item in enumerate(node.inputs):
                    if is_tensor(node, idx):
                        inputs.append(self.tensor_registry[self.tensors_mapping[(node.id, tuple(item))]])
                    elif is_tensor_list(node, idx):
                        inputs.append([self.tensor_registry[self.tensors_mapping[(node.id, tuple(t_id))]] for t_id in item])
                    elif item == '<None>' or item == '<Generator>':
                        inputs.append(None)
                    elif item == 'inf' or item == '-inf':
                        inputs.append(float(item))
                    else:
                        inputs.append(item)
            return inputs
        except Exception as e:
            print("Inputs error: ", e, node.id)


    def run_op(self, node):
        func, output_count = self.funcs[node.id]
        if not func:
            return
        inputs = self.get_inputs(node)

        ######
        # Workaround to eliminate the "strides() called on undefined Tensor" error
        if node.name == "aten::convolution_backward":
            inputs[-1] = [True, True, True]
        ######

        # Workaround to handle tensors are on different devices
        if node.name == "aten::mul":
            if inputs[0].is_cuda ^ inputs[1].is_cuda:
                if inputs[0].is_cuda:
                    inputs[1] = inputs[1].to(self.cuda)
                else:
                    inputs[1] = inputs[1].to('cpu')

        try:
            outputs = []
            if output_count == 0:
                func(*inputs)
            else:
                if output_count == 1:
                    tmp = (func(*inputs),)
                else:
                    tmp = func(*inputs)
                # Flatten any tensor lists
                # TODO: Simplify this
                for x in tmp:
                    if isinstance(x, list) and isinstance(x[0], torch.Tensor):
                        outputs.extend(x)
                    elif isinstance(x, torch.Tensor):
                        outputs.append(x)
        except Exception as e:
            print(f"Run op exception Error: {e}, node id: {node.id}, func: {func}, inputs: {inputs}")
            exit(1)

        for (_, t_id, _), output in zip(get_output_tensors(node), outputs):
            if t_id in self.dependency_permanent.keys() and self.tensors_mapping[(node.id, t_id)] not in self.unchangeable_intermediate_tensors:
                if self.tensors_mapping[(node.id, t_id)] not in self.instantiate:
                    self.tensor_registry[self.tensors_mapping[(node.id, t_id)]] = output

        if self.profile_memory:
            self.op_allocated_mem[node] = torch.cuda.memory_allocated(self.cuda) - self.current_allocated_mem
            self.current_allocated_mem = torch.cuda.memory_allocated(self.cuda)
            self.op_reserved_mem[node] = torch.cuda.memory_reserved(self.cuda) - self.current_reserved_mem
            self.current_reserved_mem = torch.cuda.memory_reserved(self.cuda)


    def benchTime(self):
        self.preprocess_graph()
        print("Start to execution: ")
        time.sleep(10)
        total_time = 0.0
        event_1 = torch.cuda.Event(enable_timing=True)
        event_2 = torch.cuda.Event(enable_timing=True)

        eg_file = "/tmp/replay_eg.json"
        eg = ExecutionGraphObserver()
        eg.register_callback(eg_file)

        if self.profile_replay:
            with torch.profiler.profile(
                activities=[
                torch.profiler.ProfilerActivity.CPU,
                torch.profiler.ProfilerActivity.CUDA,
                ],
                record_shapes=True,
                # schedule=torch.profiler.schedule(
                #     skip_first=10,
                #     wait=10,
                #     warmup=10,
                #     active=10,
                # ),
                on_trace_ready=trace_handler,
                # profile_memory=True,
            ) as prof:
                for iter in range(self.numWarmupIters + self.numIters):
                    if iter == self.numWarmupIters:
                        eg.start()
                    if iter == self.numWarmupIters + 1:
                        eg.stop()
                        eg.unregister_callback()
                    event_1.record()
                    for node in self.sorted_nodes:
                        self.run_op(node)
                    event_2.record()
                    torch.cuda.synchronize()
                    if iter >= self.numWarmupIters:
                        total_time += event_1.elapsed_time(event_2)
                    # Comment out this for now since it will introduce additional cudaMalloc
                    # self.reset_registry()
                    prof.step()
                    # print(iter, torch.cuda.memory_allocated(self.cuda))
            # print(prof.key_averages().table(sort_by="self_cpu_memory_usage", row_limit=20))
        else:
            for iter in range(self.numWarmupIters + self.numIters):
                event_1.record()
                for node in self.sorted_nodes:
                    self.run_op(node)
                event_2.record()
                torch.cuda.synchronize()
                if iter >= self.numWarmupIters:
                    total_time += event_1.elapsed_time(event_2)
                # Comment out this for now since it will introduce additional cudaMalloc
                # self.reset_registry()

        if self.profile_memory:
            print("Allocated GPU memory(B):")
            for node in dict(sorted(self.op_allocated_mem.items(), key=lambda item: item[1], reverse=True)[:100]):
                print(node.id, self.op_allocated_mem[node])
            print("Reserved GPU memory(B):")
            for node in dict(sorted(self.op_reserved_mem.items(), key=lambda item: item[1], reverse=True)[:100]):
                print(node.id, self.op_reserved_mem[node])

        # print("Replay time{}: {:.2f} ms".format(
        #     " (profiled)" if self.profile_replay else "",
        #     total_time / self.numIters
        # ))


def main():
    parser = argparse.ArgumentParser(description="Execution Graph Replay")
    parser.add_argument(
        "-w", "--warmup", type=int, default=5, help="Number of warm up iterations."
    )
    parser.add_argument(
        "--iter", type=int, default=30, help="Number of replay iterations."
    )
    parser.add_argument(
        "--input", type=str, required=True, help="Input execution graph json file."
    )
    parser.add_argument(
        "-p", "--profile-replay", action="store_true", help="Profile replay and get trace."
    )
    parser.add_argument(
        "-m", "--profile-memory", action="store_true", help="Profile memory usage in replay."
    )

    args = parser.parse_args()

    # Load PyTorch implementations for data generator and operators.
    load_modules(lib_pytorch)

    # Load PyTorch operator workloads.
    load_modules(workloads_pytorch)

    exgr = args.input
    replay_manager = ExgrReplayManager(exgr, args)
    replay_manager.benchTime()

if __name__ == "__main__":
    main()
