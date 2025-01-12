import logging
import itertools
from typing import Optional

import torch
from torch.testing._internal import common_utils

logging.getLogger("torch").setLevel(logging.WARNING)

from apex._autocast_utils import _get_autocast_dtypes
from apex.transformer import parallel_state
from apex.transformer.pipeline_parallel import utils as pp_utils
from apex.transformer.pipeline_parallel.schedules.common import (
    FwdStepFunc,
    build_model,
    _get_params_for_weight_decay_optimization,
)
from apex.transformer.pipeline_parallel.schedules.fwd_bwd_no_pipelining import (
    forward_backward_no_pipelining,
)
from apex.transformer.pipeline_parallel.schedules.fwd_bwd_pipelining_with_interleaving import (
    _forward_backward_pipelining_with_interleaving,
)
from apex.transformer.pipeline_parallel.schedules.fwd_bwd_pipelining_without_interleaving import (
    forward_backward_pipelining_without_interleaving,
)
from apex.transformer.testing.distributed_test_base import DistributedTestBase
from apex.transformer.testing import commons as testing_utils

logging.getLogger("apex").setLevel(logging.WARNING)

weight_coeff = 1024

@torch.no_grad()
def init_weights(m):
    rank = torch.distributed.get_rank()
    if isinstance(m, torch.nn.Linear):
        m.weight.fill_((rank + 1.0) / weight_coeff)
        m.bias.fill_(1.0)


def get_target_loss(hidden_size: int, microbatch_size: int, parallel_model_world_size: int, world_size: int) -> float:  
    layers_per_rank = world_size // parallel_model_world_size
    data = torch.arange(start = 0, end = layers_per_rank, dtype = torch.int) + 1

    w = (torch.arange(world_size, dtype = torch.float) + 1.0) / weight_coeff
    b = torch.ones(world_size, dtype = torch.int)
    w = hidden_size * w

    for s_id in range(0, world_size, layers_per_rank):
        e_id = s_id+layers_per_rank
        data = w[s_id:e_id] * data + b[s_id:e_id]

    return hidden_size * hidden_size * torch.sum(data).item() * microbatch_size / layers_per_rank


class PipelineParallelForwardBackwardTest(DistributedTestBase):

    GLOBAL_BATCH_SIZE = 16
    MICRO_BATCH_SIZE = 2
    HIDDEN_SIZE = 32

    @property
    def world_size(self) -> int:
        return min(torch.cuda.device_count(), 8)

    def _forward_backward_test_impl(
        self,
        forward_only: bool,
        fwd_bwd_func: FwdStepFunc,
        pipeline_model_parallel_world_size: Optional[int],
        virtual_pipeline_model_parallel_size: Optional[int],
    ) -> None:
        for dtype, deallocate_pipeline_outputs in itertools.product(
            [torch.float32] + _get_autocast_dtypes(), (True, False),
        ):
            grad_scaler = (
                torch.cuda.amp.GradScaler(init_scale=4.0)
                if dtype == torch.half
                else None
            )
            tensor_model_parallel_world_size = 1
            data_parallel_size = 1 + (self.world_size >= 8 and self.world_size % 2 == 0)

            if pipeline_model_parallel_world_size is None:
                pipeline_model_parallel_world_size =  self.world_size // (tensor_model_parallel_world_size * data_parallel_size)
            else:
                data_parallel_size = self.world_size // (tensor_model_parallel_world_size * pipeline_model_parallel_world_size)

            parallel_state.initialize_model_parallel(
                tensor_model_parallel_size_=tensor_model_parallel_world_size,
                pipeline_model_parallel_size_=pipeline_model_parallel_world_size,
                virtual_pipeline_model_parallel_size_=virtual_pipeline_model_parallel_size,
            )
            pp_utils._reconfigure_microbatch_calculator(
                rank=parallel_state.get_tensor_model_parallel_rank(),
                rampup_batch_size=None,
                global_batch_size=PipelineParallelForwardBackwardTest.GLOBAL_BATCH_SIZE,
                micro_batch_size=PipelineParallelForwardBackwardTest.MICRO_BATCH_SIZE,
                data_parallel_size=parallel_state.get_data_parallel_world_size(),
            )

            global_batch_shape = (
                PipelineParallelForwardBackwardTest.GLOBAL_BATCH_SIZE
                // parallel_state.get_data_parallel_world_size(),
                PipelineParallelForwardBackwardTest.HIDDEN_SIZE,
                PipelineParallelForwardBackwardTest.HIDDEN_SIZE,
            )

            batch =(((self.rank + 1) * torch.ones(global_batch_shape)).cuda(), )

            model = build_model(
                testing_utils.model_provider_func,
                wrap_with_ddp=True,
                virtual_pipeline_model_parallel_size=virtual_pipeline_model_parallel_size,
                hidden_size=PipelineParallelForwardBackwardTest.HIDDEN_SIZE,
            )

            for model_module in model:
                model_module.apply(init_weights)

            _param_groups = _get_params_for_weight_decay_optimization(model)
            optimizer = torch.optim.Adam(_param_groups, lr=1e-3)

            pp_utils.update_num_microbatches(0)

            loss = fwd_bwd_func(
                testing_utils.fwd_step_func,
                batch,
                model,
                forward_only=forward_only,
                # `tensor_shape` is the shape of micro batch.
                tensor_shape=(
                    PipelineParallelForwardBackwardTest.MICRO_BATCH_SIZE,
                    PipelineParallelForwardBackwardTest.HIDDEN_SIZE,
                    PipelineParallelForwardBackwardTest.HIDDEN_SIZE,
                ),
                dtype=dtype,
                grad_scaler=grad_scaler,
                deallocate_pipeline_output=deallocate_pipeline_outputs,
            )

            if dtype == torch.float32:
                hidden_size = PipelineParallelForwardBackwardTest.HIDDEN_SIZE
                microbatch_size = PipelineParallelForwardBackwardTest.MICRO_BATCH_SIZE
                target_loss = get_target_loss(hidden_size, microbatch_size, pipeline_model_parallel_world_size, self.world_size)

                for loss_item in loss:
                    x = loss_item['avg']
                    torch.testing.assert_close(x, target_loss*torch.ones_like(x))

            if not forward_only:
                for m in model:
                    for p in m.parameters():
                        self.assertIsNotNone(p.grad)
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)

            parallel_state.destroy_model_parallel()

    def test_no_pipelining(self):
        self._forward_backward_test_impl(False, forward_backward_no_pipelining, 1, None)

    def test_no_pipelining_inference(self):
        self._forward_backward_test_impl(True, forward_backward_no_pipelining, 1, None)

    def test_pipelining(self):
        self._forward_backward_test_impl(
            False, forward_backward_pipelining_without_interleaving, None, None
        )

    def test_pipelining_inference(self):
        self._forward_backward_test_impl(
            True, forward_backward_pipelining_without_interleaving, None, None
        )

    def test_pipelining_with_interleaving(self):
        self._forward_backward_test_impl(
            False, _forward_backward_pipelining_with_interleaving, None, None
        )

    def test_pipelining_with_interleaving_inference(self):
        self._forward_backward_test_impl(
            True, _forward_backward_pipelining_with_interleaving, None, None
        )


if __name__ == "__main__":
    common_utils.run_tests()
