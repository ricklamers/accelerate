import math
from types import MethodType
from typing import Any, Dict, Optional

from .state import PartialState
from .utils import (
    calculate_maximum_sizes,
    convert_bytes,
    ignorant_find_batch_size,
    infer_auto_device_map,
    is_pippy_available,
    pad_input_tensors,
    send_to_device,
)


if is_pippy_available():
    from pippy.IR import Pipe, PipeSplitWrapper, annotate_split_points
    from pippy.PipelineStage import PipelineStage


def generate_device_map(model, num_processes: int = 1, no_split_module_classes=None, max_memory: dict = None):
    """
    Calculates the device map for `model` with an offset for PiPPy
    """
    if num_processes == 1:
        return infer_auto_device_map(model, no_split_module_classes=no_split_module_classes, clean_result=False)
    if max_memory is None:
        model_size, shared = calculate_maximum_sizes(model)

        # Split into `n` chunks for each GPU
        memory = (model_size + shared[0]) / num_processes
        memory = convert_bytes(memory)
        value, ending = memory.split(" ")

        # Add a chunk to deal with potential extra shared memory instances
        memory = math.ceil(float(value)) * 1.1
        memory = f"{memory} {ending}"
        max_memory = {i: memory for i in range(num_processes)}
    device_map = infer_auto_device_map(
        model,
        max_memory=max_memory,
        no_split_module_classes=no_split_module_classes,
        clean_result=False,
    )
    return device_map


def find_pippy_batch_size(args, kwargs):
    found_batch_size = None
    if args is not None:
        for arg in args:
            found_batch_size = ignorant_find_batch_size(arg)
            if found_batch_size is not None:
                break
    if kwargs is not None and found_batch_size is None:
        for kwarg in kwargs.values():
            found_batch_size = ignorant_find_batch_size(kwarg)
            if found_batch_size is not None:
                break
    return found_batch_size


def build_pipeline(model, split_points, args, kwargs, num_chunks):
    """
    Attaches the split points to the model based on `self.device_map` and generates a `PipelineStage`. Requires passing
    in needed `args` and `kwargs` as the model needs on the CPU.

    Users can pass in custom `num_chunks` as an optional hyper-parameter. By default will use
    `AcceleratorState.num_processes`
    """
    # We need to annotate the split points in the model for PiPPy
    state = PartialState()
    annotate_split_points(model, {split_point: PipeSplitWrapper.SplitPoint.BEGINNING for split_point in split_points})
    found_batch_size = find_pippy_batch_size(args, kwargs)
    if found_batch_size != num_chunks:
        args = pad_input_tensors(args, found_batch_size, num_chunks)
        kwargs = pad_input_tensors(kwargs, found_batch_size, num_chunks)
    pipe = Pipe.from_tracing(model, num_chunks=num_chunks, example_args=args, example_kwargs=kwargs)
    stage = PipelineStage(pipe, state.local_process_index, device=state.device)

    return stage


def pippy_forward(forward, num_chunks, *args, **kwargs):
    state = PartialState()
    output = None

    if state.num_processes == 1:
        output = forward(*args, **kwargs)
    elif state.is_local_main_process:
        found_batch_size = find_pippy_batch_size(args, kwargs)
        if found_batch_size is None:
            raise ValueError("Could not find batch size from args or kwargs")
        else:
            if found_batch_size != num_chunks:
                args = pad_input_tensors(args, found_batch_size, num_chunks)
                kwargs = pad_input_tensors(kwargs, found_batch_size, num_chunks)
        forward(*args, **kwargs)
    elif state.is_last_process:
        output = forward()
    else:
        forward()
    return output


def prepare_pippy(
    model,
    split_points="auto",
    no_split_module_classes=None,
    example_args=(),
    example_kwargs: Optional[Dict[str, Any]] = None,
    num_chunks=None,
):
    """
    Wraps `model` for PipelineParallelism

    Args:
        model (`torch.nn.Module`):
            A model we want to split for pipeline-parallel inference
        split_points (`str`, defaults to 'auto'):
            How to generate the split points and chunk the model across each GPU. 'auto' will find the best balanced
            split given any model.
        no_split_module_classes (`List[str]`):
            A list of class names for layers we don't want to be split.
        example_args (tuple of `torch.Tensor`):
            The expected inputs for the model that uses order-based inputs. Recommended to use this method if possible.
        example_kwargs (dict of `torch.Tensor`)
            The expected inputs for the model that uses dictionary-based inputs. This is a *highly* limiting structure
            that requires the same keys be present at *all* inference calls. Not recommended unless the prior condition
            is true for all cases.
        num_chunks (`int`):
            The number of different stages the Pipeline will have. By default it will assign one chunk per GPU, but
            this can be tuned and played with. In general one should have num_chunks >= num_gpus.
    """
    if not is_pippy_available():
        raise ImportError(
            "`pippy` was not found to be installed on your system. Please "
            "install using `pip install torchpippy` or ensure you have at least version 0.2.0"
        )
    state = PartialState()
    example_args = send_to_device(example_args, "cpu")
    example_kwargs = send_to_device(example_kwargs, "cpu")
    if num_chunks is None:
        num_chunks = state.num_processes
    if split_points == "auto":
        device_map = generate_device_map(model, num_chunks, no_split_module_classes=no_split_module_classes)
        split_points = []
        for i in range(1, num_chunks):
            split_points.append(next(k for k, v in device_map.items() if v == i))
    model.hf_split_points = split_points
    stage = build_pipeline(model, split_points, example_args, example_kwargs, num_chunks)
    model._original_forward = model.forward
    model._original_call = model.__call__
    model.pippy_stage = stage
    model.hf_split_points = split_points

    def forward(*args, **kwargs):
        return pippy_forward(stage.forward, num_chunks, *args, **kwargs)

    # To act like a decorator so that it can be popped when doing `extract_model_from_parallel`
    # Note: creates an infinite recursion loop with `generate`
    model_forward = MethodType(forward, model)
    forward.__wrapped__ = model_forward
    model.forward = forward
    return model
