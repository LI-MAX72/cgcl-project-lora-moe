from __future__ import annotations

import math
import warnings
from typing import Any, Optional, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import svd_lowrank
from transformers.pytorch_utils import Conv1D

from peft.tuners.tuners_utils import BaseTunerLayer, check_adapters_to_merge
from peft.utils.integrations import dequantize_module_weight, gather_params_ctx
from peft.utils.other import transpose

from peft.tuners.lora.config import LoraConfig


class LoraLayer(BaseTunerLayer):
    # All names of layers that may contain (trainable) adapter weights
    adapter_layer_names = ("lora_A", "lora_B", "lora_embedding_A", "lora_embedding_B")
    # All names of other parameters that may contain adapter-related parameters
    other_param_names = ("r", "lora_alpha", "scaling", "lora_dropout")

    def __init__(self, base_layer: nn.Module, **kwargs) -> None:
        self.base_layer = base_layer
        self.r = {}
        self.lora_alpha = {}
        self.scaling = {}
        self.lora_dropout = nn.ModuleDict({})
        self.lora_A = nn.ModuleDict({})
        self.lora_B = nn.ModuleDict({})
        # For Embedding layer
        self.lora_embedding_A = nn.ParameterDict({})
        self.lora_embedding_B = nn.ParameterDict({})
        # Mark the weight as unmerged
        self._disable_adapters = False
        self.merged_adapters = []
        self.use_dora: dict[str, bool] = {}
        self.lora_magnitude_vector: Optional[torch.nn.ParameterDict] = None  # for DoRA
        self._caches: dict[str, Any] = {}
        self.kwargs = kwargs

        base_layer = self.get_base_layer()
        if isinstance(base_layer, nn.Linear):
            in_features, out_features = base_layer.in_features, base_layer.out_features
        elif isinstance(base_layer, nn.Conv2d):
            in_features, out_features = base_layer.in_channels, base_layer.out_channels
        elif isinstance(base_layer, nn.Embedding):
            in_features, out_features = base_layer.num_embeddings, base_layer.embedding_dim
        elif isinstance(base_layer, Conv1D):
            in_features, out_features = (
                base_layer.weight.ds_shape if hasattr(base_layer.weight, "ds_shape") else base_layer.weight.shape
            )
        elif hasattr(base_layer, "infeatures") and hasattr(base_layer, "outfeatures"):
            # QuantLinear
            in_features, out_features = base_layer.infeatures, base_layer.outfeatures
        elif hasattr(base_layer, "input_size") and hasattr(base_layer, "output_size"):
            # Megatron ColumnParallelLinear,RowParallelLinear
            in_features, out_features = base_layer.input_size, base_layer.output_size
        elif hasattr(base_layer, "codebooks") and base_layer.__class__.__name__ == "QuantizedLinear":
            # AQLM QuantLinear
            in_features, out_features = base_layer.in_features, base_layer.out_features
        elif hasattr(base_layer, "w_bit") and base_layer.__class__.__name__ == "WQLinear_GEMM":
            # Awq layers
            in_features, out_features = base_layer.in_features, base_layer.out_features
        elif base_layer.__class__.__name__ == "EetqLinear":
            # Eetq layers
            in_features, out_features = base_layer.in_features, base_layer.out_features
        elif hasattr(base_layer, "W_q") and base_layer.__class__.__name__ == "HQQLinear":
            # HQQ layers
            in_features, out_features = base_layer.in_features, base_layer.out_features
        else:
            raise ValueError(f"Unsupported layer type {type(base_layer)}")

        self.in_features = in_features
        self.out_features = out_features

    def update_layer(
        self, adapter_name, r, lora_alpha, lora_dropout, init_lora_weights, use_rslora, use_dora: bool = False
    ):
        # This code works for linear layers, override for other layer types
        if r <= 0:
            raise ValueError(f"`r` should be a positive integer value but the value passed is {r}")

        self.r[adapter_name] = r
        self.lora_alpha[adapter_name] = lora_alpha
        if lora_dropout > 0.0:
            lora_dropout_layer = nn.Dropout(p=lora_dropout)
        else:
            lora_dropout_layer = nn.Identity()

        self.lora_dropout.update(nn.ModuleDict({adapter_name: lora_dropout_layer}))
        # Actual trainable parameters
        self.lora_A[adapter_name] = nn.Linear(self.in_features, r, bias=False)
        self.lora_B[adapter_name] = nn.Linear(r, self.out_features, bias=False)
        if use_rslora:
            self.scaling[adapter_name] = lora_alpha / math.sqrt(r)
        else:
            self.scaling[adapter_name] = lora_alpha / r

        if isinstance(init_lora_weights, str) and init_lora_weights.startswith("pissa"):
            self.pissa_init(adapter_name, init_lora_weights)
        elif init_lora_weights == "loftq":
            self.loftq_init(adapter_name)
        elif init_lora_weights:
            self.reset_lora_parameters(adapter_name, init_lora_weights)

        # check weight and qweight (for GPTQ)
        for weight_name in ("weight", "qweight"):
            weight = getattr(self.get_base_layer(), weight_name, None)
            if weight is not None:
                # the layer is already completely initialized, this is an update
                if weight.dtype.is_floating_point or weight.dtype.is_complex:
                    self.to(weight.device, dtype=weight.dtype)
                else:
                    self.to(weight.device)
                break

        if use_dora:
            self.dora_init(adapter_name)
            self.use_dora[adapter_name] = True
        else:
            self.use_dora[adapter_name] = False

        self.set_adapter(self.active_adapters)

    def reset_lora_parameters(self, adapter_name, init_lora_weights):
        if init_lora_weights is False:
            return

        if adapter_name in self.lora_A.keys():
            if init_lora_weights is True:
                # initialize A the same way as the default for nn.Linear and B to zero
                # https://github.com/microsoft/LoRA/blob/a0a92e0f26c067cf94747bdbf1ce73793fa44d19/loralib/layers.py#L124
                nn.init.kaiming_uniform_(self.lora_A[adapter_name].weight, a=math.sqrt(5))
            elif init_lora_weights.lower() == "gaussian":
                nn.init.normal_(self.lora_A[adapter_name].weight, std=1 / self.r[adapter_name])
            else:
                raise ValueError(f"Unknown initialization {init_lora_weights=}")
            nn.init.zeros_(self.lora_B[adapter_name].weight)
        if adapter_name in self.lora_embedding_A.keys():
            # initialize a the same way as the default for nn.linear and b to zero
            nn.init.zeros_(self.lora_embedding_A[adapter_name])
            nn.init.normal_(self.lora_embedding_B[adapter_name])

    def pissa_init(self, adapter_name, init_lora_weights):
        weight = self.get_base_layer().weight
        dtype = weight.dtype
        if dtype not in [torch.float32, torch.float16, torch.bfloat16]:
            raise TypeError(
                "Please initialize PiSSA under float32, float16, or bfloat16. "
                "Subsequently, re-quantize the residual model to help minimize quantization errors."
            )
        weight = weight.to(torch.float32)

        if init_lora_weights == "pissa":
            # USV^T = W <-> VSU^T = W^T, where W^T = weight.data in R^{out_channel, in_channel},
            V, S, Uh = torch.linalg.svd(weight.data, full_matrices=False)
            Vr = V[:, : self.r[adapter_name]]
            Sr = S[: self.r[adapter_name]]
            Sr /= self.scaling[adapter_name]
            Uhr = Uh[: self.r[adapter_name]]
        elif len(init_lora_weights.split("_niter_")) == 2:
            Vr, Sr, Ur = svd_lowrank(
                weight.data, self.r[adapter_name], niter=int(init_lora_weights.split("_niter_")[-1])
            )
            Sr /= self.scaling[adapter_name]
            Uhr = Ur.t()
        else:
            raise ValueError(
                f"init_lora_weights should be 'pissa' or 'pissa_niter_[number of iters]', got {init_lora_weights} instead."
            )

        lora_A = torch.diag(torch.sqrt(Sr)) @ Uhr
        lora_B = Vr @ torch.diag(torch.sqrt(Sr))
        self.lora_A[adapter_name].weight.data = lora_A
        self.lora_B[adapter_name].weight.data = lora_B
        weight = weight.data - self.scaling[adapter_name] * lora_B @ lora_A
        weight = weight.to(dtype)
        self.get_base_layer().weight.data = weight

    def loftq_init(self, adapter_name):
        from peft.utils.loftq_utils import loftq_init

        weight = self.get_base_layer().weight
        kwargs = {
            "num_bits": self.kwargs.get("loftq_bits", 4),
            "reduced_rank": self.r[adapter_name],
            "num_iter": self.kwargs.get("loftq_iter", 1),
        }

        qweight, lora_A, lora_B = loftq_init(weight, **kwargs)
        if adapter_name in self.lora_A.keys():
            # initialize A the same way as the default for nn.Linear and B to zero
            self.lora_A[adapter_name].weight.data = lora_A
            self.lora_B[adapter_name].weight.data = lora_B
        if adapter_name in self.lora_embedding_A.keys():
            # initialize a the same way as the default for nn.linear and b to zero
            self.lora_embedding_A[adapter_name].weight.data = lora_A
            self.lora_embedding_B[adapter_name].weight.data = lora_B
        self.get_base_layer().weight.data = qweight

    def _get_weight_norm(self, weight, lora_weight, scaling) -> torch.Tensor:
        # calculate L2 norm of weight matrix, column-wise
        weight = transpose(weight, self.fan_in_fan_out)
        weight = weight + scaling * lora_weight
        weight_norm = torch.linalg.norm(weight, dim=1).to(weight.dtype)
        return weight_norm

    def dora_init(self, adapter_name: str) -> None:
        lora_A = self.lora_A[adapter_name].weight
        lora_B = self.lora_B[adapter_name].weight
        # temporarily convert fp16 to fp32, as fp16 can cause trouble on CPU with PyTorch < 2.2
        dtype_is_fp16 = lora_A.dtype == torch.float16
        if dtype_is_fp16:
            lora_A = lora_A.float()
            lora_B = lora_B.float()

        scaling = self.scaling[adapter_name]
        with gather_params_ctx(self.get_base_layer().parameters()):
            base_layer = self.get_base_layer()
            weight = dequantize_module_weight(base_layer)
            if weight.data.ndim == 4:  # For handling LoRAs applied to Conv2Ds.
                lora_weight = torch.mm(lora_B.flatten(start_dim=1), lora_A.flatten(start_dim=1))
                lora_weight = lora_weight.reshape(weight.shape)
            else:
                lora_weight = lora_B @ lora_A

            if dtype_is_fp16:
                lora_weight = lora_weight.half()
            weight_norm = self._get_weight_norm(weight, lora_weight, scaling)

        self.lora_magnitude_vector = nn.ParameterDict()
        self.lora_magnitude_vector[adapter_name] = nn.Parameter(weight_norm, requires_grad=True)
        # add lora_magnitude_vector to the list of learnable parameters
        self.adapter_layer_names = self.adapter_layer_names[:] + ("lora_magnitude_vector",)

    def _cache_store(self, key: str, value: Any) -> None:
        self._caches[key] = value

    def _cache_pop(self, key: str) -> Any:
        value = self._caches.pop(key)
        return value

    def _apply_dora(self, x, lora_A, lora_B, scaling, active_adapter):
        """
        For DoRA, calculate the extra output from LoRA with DoRA applied. This should be added on top of the base layer
        output.
        """
        lora_weight = lora_B.weight @ lora_A.weight
        magnitude = self.lora_magnitude_vector[active_adapter]
        base_layer = self.get_base_layer()
        weight = dequantize_module_weight(base_layer)
        weight = weight.to(x.dtype)
        weight_norm = self._get_weight_norm(weight, lora_weight, scaling)
        # see section 4.3 of DoRA (https://arxiv.org/abs/2402.09353)
        # "[...] we suggest treating ||V +∆V ||_c in
        # Eq. (5) as a constant, thereby detaching it from the gradient
        # graph. This means that while ||V + ∆V ||_c dynamically
        # reflects the updates of ∆V , it won’t receive any gradient
        # during backpropagation"
        weight_norm = weight_norm.detach()
        mag_norm_scale = (magnitude / weight_norm).view(1, -1)
        result_dora = (mag_norm_scale - 1) * (
            F.linear(x, transpose(weight, self.fan_in_fan_out))
        ) + mag_norm_scale * lora_B(lora_A(x)) * scaling

        # Note: Computation could potentially be accelerated by using the code below instead of calculating X@W again.
        # This is only correct if dropout=0, otherwise results will differ:
        # https://github.com/huggingface/peft/pull/1474#issuecomment-1964682771
        # bias = self.get_base_layer().bias
        # if bias is not None:
        #     result = result - bias
        # result = mag_norm_scale * result + mag_norm_scale * lora_B(lora_A(x)) * scaling
        # if bias is not None:
        #     result = result + bias

        return result_dora

    def set_scale(self, adapter, scale):
        if adapter not in self.scaling:
            # Ignore the case where the adapter is not in the layer
            return
        self.scaling[adapter] = scale * self.lora_alpha[adapter] / self.r[adapter]

    def scale_layer(self, scale: float) -> None:
        if scale == 1:
            return

        for active_adapter in self.active_adapters:
            if active_adapter not in self.lora_A.keys():
                continue

            self.scaling[active_adapter] *= scale

    def unscale_layer(self, scale=None) -> None:
        for active_adapter in self.active_adapters:
            if active_adapter not in self.lora_A.keys():
                continue

            if scale is None:
                self.scaling[active_adapter] = self.lora_alpha[active_adapter] / self.r[active_adapter]
            else:
                self.scaling[active_adapter] /= scale

    def _check_forward_args(self, x, *args, **kwargs):
        """Check if the arguments are compatible with the configs and state of the model"""
        adapter_names = kwargs.get("adapter_names", None)
        if adapter_names is None:
            return

        if len(x) != len(adapter_names):
            msg = (
                "Length of `adapter_names` should be the same as the number of inputs, but got "
                f"{len(adapter_names)} and {len(x)} respectively."
            )
            raise ValueError(msg)

        if self.merged:
            # It is unclear what would be the right thing to do if users pass adapter_names and there are merged
            # adapters. Therefore, it is better to raise an error in this case.
            msg = "Cannot pass `adapter_names` when there are merged adapters, please call `unmerge_adapter` first."
            raise ValueError(msg)

        unique_adapters = set(self.active_adapters)
        for adapter_name in unique_adapters:
            if self.use_dora.get(adapter_name, False):
                msg = "Cannot pass `adapter_names` when DoRA is enabled."
                raise ValueError(msg)

    def _mixed_batch_forward(
        self, x: torch.Tensor, *args: Any, adapter_names: list[str], **kwargs: Any
    ) -> torch.Tensor:
        # This is a special method that handles the case when users pass the argument `adapter_names`. This is an
        # extra argument that allows mixing different adapters in the same batch at inference time.
        result = self.base_layer(x, *args, **kwargs)
        torch_result_dtype = result.dtype

        unique_adapters = set(adapter_names)
        sub_batch_indices_list = []
        for adapter in unique_adapters:
            sub_batch_indices_list.append([index for index, item in enumerate(adapter_names) if item == adapter])

        for i, active_adapter in enumerate(unique_adapters):
            if active_adapter == "__base__":
                continue
            if active_adapter not in self.lora_A.keys():
                continue

            lora_A = self.lora_A[active_adapter]
            lora_B = self.lora_B[active_adapter]
            dropout = self.lora_dropout[active_adapter]
            scaling = self.scaling[active_adapter]

            # getting the sub-batch, passing it to LoRA layers and updating the corresponding indices of the linear
            # layer output
            sub_batch = x[sub_batch_indices_list[i]].to(lora_A.weight.dtype)
            lora_output = lora_B(lora_A(dropout(sub_batch))) * scaling
            result[sub_batch_indices_list[i]] += lora_output.to(torch_result_dtype)

        return result


# Below code is based on https://github.com/microsoft/LoRA/blob/main/loralib/layers.py
# and modified to work with PyTorch FSDP


#  ------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  ------------------------------------------------------------------------------------------


class Linear(nn.Module, LoraLayer):
    # Lora implemented in a dense layer
    def __init__(
        self,
        base_layer,
        adapter_name: str,
        r: int = 0,
        lora_alpha: int = 1,
        lora_dropout: float = 0.0,
        fan_in_fan_out: bool = False,  # Set this to True if the layer to replace stores weight like (fan_in, fan_out)
        is_target_conv_1d_layer: bool = False,
        init_lora_weights: Union[bool, str] = True,
        use_rslora: bool = False,
        use_dora: bool = False,
        **kwargs,
    ) -> None:
        super().__init__()
        LoraLayer.__init__(self, base_layer, **kwargs)
        self.fan_in_fan_out = fan_in_fan_out

        self._active_adapter = adapter_name
        self.update_layer(
            adapter_name,
            r,
            lora_alpha=lora_alpha,
            lora_dropout=lora_dropout,
            init_lora_weights=init_lora_weights,
            use_rslora=use_rslora,
            use_dora=use_dora,
        )
        self.is_target_conv_1d_layer = is_target_conv_1d_layer

    def merge(self, safe_merge: bool = False, adapter_names: Optional[list[str]] = None) -> None:
        """
        Merge the active adapter weights into the base weights

        Args:
            safe_merge (`bool`, *optional*):
                If True, the merge operation will be performed in a copy of the original weights and check for NaNs
                before merging the weights. This is useful if you want to check if the merge operation will produce
                NaNs. Defaults to `False`.
            adapter_names (`list[str]`, *optional*):
                The list of adapter names that should be merged. If None, all active adapters will be merged. Defaults
                to `None`.
        """
        adapter_names = check_adapters_to_merge(self, adapter_names)
        if not adapter_names:
            # no adapter to merge
            return

        for active_adapter in adapter_names:
            if active_adapter in self.lora_A.keys():
                base_layer = self.get_base_layer()
                if safe_merge:
                    # Note that safe_merge will be slower than the normal merge
                    # because of the copy operation.
                    orig_weights = base_layer.weight.data.clone()
                    delta_weight = self.get_delta_weight(active_adapter)
                    if not self.use_dora[active_adapter]:
                        orig_weights = orig_weights + delta_weight
                    else:
                        # handle dora
                        # since delta_weight already includes scaling, set it to 1 here
                        weight_norm = self._get_weight_norm(
                            orig_weights, transpose(delta_weight, self.fan_in_fan_out), scaling=1
                        ).detach()
                        # We need to cache weight_norm because it has to be based on the original weights. We
                        # cannot calculate it on the fly based on the merged weights when unmerging because its a
                        # different value
                        self._cache_store(f"{active_adapter}-weight_norm", weight_norm)
                        dora_factor = self.lora_magnitude_vector[active_adapter] / weight_norm
                        dora_factor = transpose(dora_factor.view(-1, 1), self.fan_in_fan_out)
                        orig_weights = dora_factor * (orig_weights + delta_weight)

                    if not torch.isfinite(orig_weights).all():
                        raise ValueError(
                            f"NaNs detected in the merged weights. The adapter {active_adapter} seems to be broken"
                        )

                    base_layer.weight.data = orig_weights
                else:
                    delta_weight = self.get_delta_weight(active_adapter)
                    if not self.use_dora[active_adapter]:
                        base_layer.weight.data = base_layer.weight.data + delta_weight
                    else:
                        # handle dora
                        # since delta_weight already includes scaling, set it to 1 here
                        weight_norm = self._get_weight_norm(
                            base_layer.weight, transpose(delta_weight, self.fan_in_fan_out), scaling=1
                        ).detach()
                        # We need to cache weight_norm because it has to be based on the original weights. We
                        # cannot calculate it on the fly based on the merged weights when unmerging because its a
                        # different value
                        self._cache_store(f"{active_adapter}-weight_norm", weight_norm)
                        dora_factor = self.lora_magnitude_vector[active_adapter] / weight_norm
                        dora_factor = transpose(dora_factor.view(-1, 1), self.fan_in_fan_out)
                        new_weight = dora_factor * (base_layer.weight.data + delta_weight)
                        base_layer.weight.data = new_weight

                self.merged_adapters.append(active_adapter)

    def unmerge(self) -> None:
        """
        This method unmerges all merged adapter layers from the base weights.
        """
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return
        while len(self.merged_adapters) > 0:
            active_adapter = self.merged_adapters.pop()
            if active_adapter in self.lora_A.keys():
                weight = self.get_base_layer().weight
                delta_weight = self.get_delta_weight(active_adapter)
                if not self.use_dora[active_adapter]:
                    weight.data -= delta_weight
                else:
                    weight_norm = self._cache_pop(f"{active_adapter}-weight_norm")
                    dora_factor = self.lora_magnitude_vector[active_adapter] / weight_norm
                    weight_orig = weight.data / dora_factor.view(-1, 1) - delta_weight
                    weight.data = weight_orig

    def get_delta_weight(self, adapter) -> torch.Tensor:
        """
        Compute the delta weight for the given adapter.

        Args:
            adapter (str):
                The name of the adapter for which the delta weight should be computed.
        """
        device = self.lora_B[adapter].weight.device
        dtype = self.lora_B[adapter].weight.dtype

        # In case users wants to merge the adapter weights that are in
        # float16 while being on CPU, we need to cast the weights to float32, perform the merge and then cast back to
        # float16 because the `@` and matmul operation in general is not supported in torch + cpu + fp16.
        cast_to_fp32 = device.type == "cpu" and dtype == torch.float16

        weight_A = self.lora_A[adapter].weight
        weight_B = self.lora_B[adapter].weight

        if cast_to_fp32:
            weight_A = weight_A.float()
            weight_B = weight_B.float()

        output_tensor = transpose(weight_B @ weight_A, self.fan_in_fan_out) * self.scaling[adapter]

        if cast_to_fp32:
            output_tensor = output_tensor.to(dtype=dtype)

            # cast back the weights
            self.lora_A[adapter].weight.data = weight_A.to(dtype)
            self.lora_B[adapter].weight.data = weight_B.to(dtype)

        return output_tensor

    def forward(self, x: torch.Tensor, *args: Any, **kwargs: Any) -> torch.Tensor:
        self._check_forward_args(x, *args, **kwargs)
        adapter_names = kwargs.pop("adapter_names", None)

        if self.disable_adapters:
            if self.merged:
                self.unmerge()
            result = self.base_layer(x, *args, **kwargs)
        elif adapter_names is not None:
            result = self._mixed_batch_forward(x, *args, adapter_names=adapter_names, **kwargs)
        elif self.merged:
            result = self.base_layer(x, *args, **kwargs)
        else:
            result = self.base_layer(x, *args, **kwargs)
            torch_result_dtype = result.dtype
            for active_adapter in self.active_adapters:
                if active_adapter not in self.lora_A.keys():
                    continue
                lora_A = self.lora_A[active_adapter]
                lora_B = self.lora_B[active_adapter]
                dropout = self.lora_dropout[active_adapter]
                scaling = self.scaling[active_adapter]
                x = x.to(lora_A.weight.dtype)

                if not self.use_dora[active_adapter]:
                    result = result + lora_B(lora_A(dropout(x))) * scaling
                else:
                    x = dropout(x)
                    result = result + self._apply_dora(x, lora_A, lora_B, scaling, active_adapter)

            result = result.to(torch_result_dtype)

        return result

    def __repr__(self) -> str:
        rep = super().__repr__()
        return "lora." + rep


class Embedding(nn.Module, LoraLayer):
    # LoRA implemented in a Embedding layer
    def __init__(
        self,
        base_layer: nn.Module,
        adapter_name: str,
        r: int = 0,
        lora_alpha: int = 1,
        lora_dropout: float = 0.0,
        init_lora_weights: Union[bool, str] = True,
        use_rslora: bool = False,
        use_dora: bool = False,
        **kwargs,
    ) -> None:
        super().__init__()
        LoraLayer.__init__(self, base_layer)

        if use_dora:
            raise ValueError(f"{self.__class__.__name__} does not support DoRA yet, please set it to False")

        self._active_adapter = adapter_name
        self.update_layer(
            adapter_name,
            r,
            lora_alpha=lora_alpha,
            lora_dropout=lora_dropout,
            init_lora_weights=init_lora_weights,
            use_rslora=use_rslora,
            use_dora=use_dora,
        )

    def update_layer(self, adapter_name, r, lora_alpha, lora_dropout, init_lora_weights, use_rslora, use_dora):
        if r <= 0:
            raise ValueError(f"`r` should be a positive integer value but the value passed is {r}")

        self.r[adapter_name] = r
        self.lora_alpha[adapter_name] = lora_alpha
        if lora_dropout > 0.0:
            lora_dropout_layer = nn.Dropout(p=lora_dropout)
        else:
            lora_dropout_layer = nn.Identity()

        self.lora_dropout[adapter_name] = lora_dropout_layer
        # Actual trainable parameters
        weight_A = torch.randn((r, self.in_features))
        weight_B = torch.randn((self.out_features, r))
        self.lora_embedding_A[adapter_name] = nn.Parameter(weight_A)
        self.lora_embedding_B[adapter_name] = nn.Parameter(weight_B)
        if use_rslora:
            self.scaling[adapter_name] = lora_alpha / math.sqrt(r)
        else:
            self.scaling[adapter_name] = lora_alpha / r

        if init_lora_weights == "loftq":
            self.loftq_init(adapter_name)
        elif init_lora_weights:
            self.reset_lora_parameters(adapter_name, init_lora_weights)

        base_layer = self.get_base_layer()
        weight = getattr(base_layer, "weight", None)
        if weight is not None:
            # the layer is already completely initialized, this is an update
            self.to(base_layer.weight.device, dtype=weight.dtype)

        self.set_adapter(self.active_adapters)

    def merge(self, safe_merge: bool = False, adapter_names: Optional[list[str]] = None) -> None:
        """
        Merge the active adapter weights into the base weights

        Args:
            safe_merge (`bool`, *optional*):
                If True, the merge operation will be performed in a copy of the original weights and check for NaNs
                before merging the weights. This is useful if you want to check if the merge operation will produce
                NaNs. Defaults to `False`.
            adapter_names (`list[str]`, *optional*):
                The list of adapter names that should be merged. If None, all active adapters will be merged. Defaults
                to `None`.
        """
        adapter_names = check_adapters_to_merge(self, adapter_names)
        if not adapter_names:
            # no adapter to merge
            return

        for active_adapter in adapter_names:
            if active_adapter in self.lora_embedding_A.keys():
                base_layer = self.get_base_layer()
                if safe_merge:
                    # Note that safe_merge will be slower than the normal merge
                    # because of the copy operation.
                    orig_weights = base_layer.weight.data.clone()
                    orig_weights = orig_weights + self.get_delta_weight(active_adapter)

                    if not torch.isfinite(orig_weights).all():
                        raise ValueError(
                            f"NaNs detected in the merged weights. The adapter {active_adapter} seems to be broken"
                        )

                    base_layer.weight.data = orig_weights
                else:
                    base_layer.weight.data = base_layer.weight.data + self.get_delta_weight(active_adapter)
                self.merged_adapters.append(active_adapter)

    def unmerge(self) -> None:
        """
        This method unmerges all merged adapter layers from the base weights.
        """
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return
        while len(self.merged_adapters) > 0:
            active_adapter = self.merged_adapters.pop()
            if active_adapter in self.lora_embedding_A.keys():
                self.get_base_layer().weight.data -= self.get_delta_weight(active_adapter)

    def get_delta_weight(self, adapter) -> torch.Tensor:
        """
        Compute the delta weight for the given adapter.

        Args:
            adapter (str):
                The name of the adapter for which the delta weight should be computed.
        """
        device = self.lora_embedding_B[adapter].device
        dtype = self.lora_embedding_A[adapter].dtype

        # In case users wants to merge the adapter weights that are in
        # float16 while being on CPU, we need to cast the weights to float32, perform the merge and then cast back to
        # float16 because the `@` and matmul operation in general is not supported in torch + cpu + fp16.
        cast_to_fp32 = device.type == "cpu" and dtype == torch.float16

        weight_A = self.lora_embedding_A[adapter]
        weight_B = self.lora_embedding_B[adapter]

        if cast_to_fp32:
            weight_A = weight_A.float()
            weight_B = weight_B.float()

        output_tensor = transpose(weight_B @ weight_A, True) * self.scaling[adapter]

        if cast_to_fp32:
            output_tensor = output_tensor.to(dtype=dtype)

            # cast back the weights
            self.lora_embedding_A[adapter] = weight_A.to(dtype)
            self.lora_embedding_B[adapter] = weight_B.to(dtype)

        return output_tensor

    def _mixed_batch_forward(
        self, x: torch.Tensor, *args: Any, adapter_names: list[str], **kwargs: Any
    ) -> torch.Tensor:
        # This is a special method that handles the case when users pass the argument `adapter_names`. This is an
        # extra argument that allows mixing different adapters in the same batch at inference time.
        result = self.base_layer(x, *args, **kwargs)

        unique_adapters = set(adapter_names)
        sub_batch_indices_list = []
        for adapter in unique_adapters:
            sub_batch_indices_list.append([index for index, item in enumerate(adapter_names) if item == adapter])

        for i, active_adapter in enumerate(unique_adapters):
            if active_adapter == "__base__":
                continue
            if active_adapter not in self.lora_embedding_A.keys():
                continue

            embedding_A = self.lora_embedding_A[active_adapter].T
            embedding_B = self.lora_embedding_B[active_adapter].T
            scaling = self.scaling[active_adapter]

            # getting the sub-batch, passing it to LoRA layers and updating the corresponding indices of the linear
            # layer output
            sub_batch = x[sub_batch_indices_list[i]]
            after_A = self._embed(sub_batch, embedding_A)
            result[sub_batch_indices_list[i]] += (after_A @ embedding_B) * scaling

        return result

    def _embed(self, input: torch.Tensor, weight: torch.Tensor) -> torch.Tensor:
        base_layer = self.get_base_layer()
        return F.embedding(
            input,
            weight,
            padding_idx=base_layer.padding_idx,
            max_norm=base_layer.max_norm,
            norm_type=base_layer.norm_type,
            scale_grad_by_freq=base_layer.scale_grad_by_freq,
            sparse=base_layer.sparse,
        )

    def forward(self, x: torch.Tensor, *args: Any, **kwargs: Any) -> torch.Tensor:
        # TODO: no dtype conversion here, unlike in Linear, is that correct?
        self._check_forward_args(x, *args, **kwargs)
        adapter_names = kwargs.pop("adapter_names", None)

        if self.disable_adapters:
            if self.merged:
                self.unmerge()
            result = self.base_layer(x, *args, **kwargs)
        elif adapter_names is not None:
            result = self._mixed_batch_forward(x, *args, adapter_names=adapter_names, **kwargs)
        elif self.merged:
            result = self.base_layer(x, *args, **kwargs)
        else:
            result = self.base_layer(x, *args, **kwargs)
            torch_result_dtype = result.dtype
            for active_adapter in self.active_adapters:
                if active_adapter not in self.lora_embedding_A:
                    continue
                embedding_A = self.lora_embedding_A[active_adapter].T
                embedding_B = self.lora_embedding_B[active_adapter].T
                scaling = self.scaling[active_adapter]
                after_A = self._embed(x, embedding_A)
                result = result + (after_A @ embedding_B) * scaling
            result = result.to(torch_result_dtype)

        return result

    def __repr__(self) -> str:
        rep = super().__repr__()
        return "lora." + rep


class Conv2d(nn.Module, LoraLayer):
    # Lora implemented in a conv2d layer
    def __init__(
        self,
        base_layer: nn.Module,
        adapter_name: str,
        r: int = 0,
        lora_alpha: int = 1,
        lora_dropout: float = 0.0,
        init_lora_weights: Union[bool, str] = True,
        use_rslora: bool = False,
        use_dora: bool = False,
        **kwargs,
    ) -> None:
        super().__init__()
        LoraLayer.__init__(self, base_layer)

        self._active_adapter = adapter_name
        self.update_layer(
            adapter_name,
            r,
            lora_alpha=lora_alpha,
            lora_dropout=lora_dropout,
            init_lora_weights=init_lora_weights,
            use_rslora=use_rslora,
            use_dora=use_dora,
        )

    def update_layer(self, adapter_name, r, lora_alpha, lora_dropout, init_lora_weights, use_rslora, use_dora):
        if r <= 0:
            raise ValueError(f"`r` should be a positive integer value but the value passed is {r}")

        self.r[adapter_name] = r
        self.lora_alpha[adapter_name] = lora_alpha
        if lora_dropout > 0.0:
            lora_dropout_layer = nn.Dropout(p=lora_dropout)
        else:
            lora_dropout_layer = nn.Identity()

        self.lora_dropout[adapter_name] = lora_dropout_layer
        # Actual trainable parameters
        base_layer = self.get_base_layer()
        kernel_size = base_layer.kernel_size
        stride = base_layer.stride
        padding = base_layer.padding
        self.lora_A[adapter_name] = nn.Conv2d(self.in_features, r, kernel_size, stride, padding, bias=False)
        self.lora_B[adapter_name] = nn.Conv2d(r, self.out_features, (1, 1), (1, 1), bias=False)
        if use_rslora:
            self.scaling[adapter_name] = lora_alpha / math.sqrt(r)
        else:
            self.scaling[adapter_name] = lora_alpha / r

        if init_lora_weights == "loftq":
            self.loftq_init(adapter_name)
        elif init_lora_weights:
            self.reset_lora_parameters(adapter_name, init_lora_weights)

        weight = getattr(base_layer, "weight", None)
        if weight is not None:
            # the layer is already completely initialized, this is an update
            self.to(base_layer.weight.device, dtype=weight.dtype)

        if use_dora:
            self.dora_init(adapter_name)
            self.use_dora[adapter_name] = True
        else:
            self.use_dora[adapter_name] = False

        self.set_adapter(self.active_adapters)

    def merge(self, safe_merge: bool = False, adapter_names: Optional[list[str]] = None) -> None:
        """
        Merge the active adapter weights inside the base weights

        Args:
            safe_merge (`bool`, *optional*):
                If True, the merge operation will be performed in a copy of the original weights and check for NaNs
                before merging the weights. This is useful if you want to check if the merge operation will produce
                NaNs. Defaults to `False`.
            adapter_names (`list[str]`, *optional*):
                The list of adapter names that should be merged. If None, all active adapters will be merged. Defaults
                to `None`.
        """
        adapter_names = check_adapters_to_merge(self, adapter_names)
        if not adapter_names:
            # no adapter to merge
            return

        for active_adapter in adapter_names:
            if active_adapter in self.lora_A.keys():
                base_layer = self.get_base_layer()
                if safe_merge:
                    # Note that safe_merge will be slower than the normal merge
                    # because of the copy operation.
                    orig_weights = base_layer.weight.data.clone()
                    delta_weight = self.get_delta_weight(active_adapter)

                    if not self.use_dora[active_adapter]:
                        orig_weights = orig_weights + delta_weight
                    else:
                        # handle dora
                        # since delta_weight already includes scaling, set it to 1 here
                        weight_norm = self._get_weight_norm(orig_weights, delta_weight, scaling=1).detach()
                        # We need to cache weight_norm because it has to be based on the original weights. We
                        # cannot calculate it on the fly based on the merged weights when unmerging because its a
                        # different value
                        self._cache_store(f"{active_adapter}-weight_norm", weight_norm)
                        dora_factor = self.lora_magnitude_vector[active_adapter] / weight_norm
                        orig_weights = dora_factor.view(-1, 1, 1, 1) * (orig_weights + delta_weight)

                    if not torch.isfinite(orig_weights).all():
                        raise ValueError(
                            f"NaNs detected in the merged weights. The adapter {active_adapter} seems to be broken"
                        )
                    base_layer.weight.data = orig_weights
                else:
                    delta_weight = self.get_delta_weight(active_adapter)
                    if not self.use_dora[active_adapter]:
                        base_layer.weight.data = base_layer.weight.data + delta_weight
                    else:
                        # handle dora
                        # since delta_weight already includes scaling, set it to 1 here
                        weight_norm = self._get_weight_norm(base_layer.weight, delta_weight, scaling=1).detach()
                        # We need to cache weight_norm because it has to be based on the original weights. We
                        # cannot calculate it on the fly based on the merged weights when unmerging because its a
                        # different value
                        self._cache_store(f"{active_adapter}-weight_norm", weight_norm)
                        dora_factor = self.lora_magnitude_vector[active_adapter] / weight_norm
                        new_weight = dora_factor.view(-1, 1, 1, 1) * (base_layer.weight.data + delta_weight)
                        base_layer.weight.data = new_weight

                self.merged_adapters.append(active_adapter)

    def unmerge(self) -> None:
        """
        This method unmerges all merged adapter layers from the base weights.
        """
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return
        while len(self.merged_adapters) > 0:
            active_adapter = self.merged_adapters.pop()
            if active_adapter in self.lora_A.keys():
                weight = self.get_base_layer().weight
                delta_weight = self.get_delta_weight(active_adapter)
                if not self.use_dora[active_adapter]:
                    weight.data -= delta_weight
                else:
                    weight_norm = self._cache_pop(f"{active_adapter}-weight_norm")
                    dora_factor = self.lora_magnitude_vector[active_adapter] / weight_norm
                    weight_orig = weight.data / dora_factor.view(-1, 1, 1, 1) - delta_weight
                    weight.data = weight_orig

    def get_delta_weight(self, adapter) -> torch.Tensor:
        """
        Compute the delta weight for the given adapter.

        Args:
            adapter (str):
                The name of the adapter for which the delta weight should be computed.
        """
        device = self.lora_B[adapter].weight.device
        dtype = self.lora_A[adapter].weight.dtype

        # In case users wants to merge the adapter weights that are in
        # float16 while being on CPU, we need to cast the weights to float32, perform the merge and then cast back to
        # float16 because the `@` and matmul operation in general is not supported in torch + cpu + fp16.
        cast_to_fp32 = device.type == "cpu" and dtype == torch.float16

        weight_A = self.lora_A[adapter].weight
        weight_B = self.lora_B[adapter].weight

        if cast_to_fp32:
            weight_A = weight_A.float()
            weight_B = weight_B.float()

        # https://github.com/bmaltais/kohya_ss/blob/feb6728762a8f463d15ba936d189d4c3abfaa1ab/networks/lora.py#L117
        if self.get_base_layer().weight.size()[2:4] == (1, 1):
            # conv2d 1x1
            output_tensor = (weight_B.squeeze(3).squeeze(2) @ weight_A.squeeze(3).squeeze(2)).unsqueeze(2).unsqueeze(
                3
            ) * self.scaling[adapter]
        else:
            # conv2d 3x3
            output_tensor = (
                F.conv2d(
                    weight_A.permute(1, 0, 2, 3),
                    weight_B,
                ).permute(1, 0, 2, 3)
                * self.scaling[adapter]
            )

        if cast_to_fp32:
            output_tensor = output_tensor.to(dtype=dtype)

            # cast back the weights
            self.lora_A[adapter].weight.data = weight_A.to(dtype)
            self.lora_B[adapter].weight.data = weight_B.to(dtype)

        return output_tensor

    def _get_weight_norm(self, weight, lora_weight, scaling) -> torch.Tensor:
        # calculate L2 norm of weight matrix, channel-wise
        weight = weight + scaling * lora_weight
        # the following is needed to have compatibility with the 4D weight tensors of Conv2D
        weight_norm = weight.norm(p=2, dim=(1, 2, 3), keepdim=True).transpose(1, 0)
        return weight_norm

    def _apply_dora(self, x, lora_A, lora_B, scaling, active_adapter):
        """
        For DoRA, calculate the extra output from LoRA with DoRA applied. This should be added on top of the base layer
        output.
        """
        base_layer = self.get_base_layer()
        weight = base_layer.weight
        lora_weight = torch.mm(lora_B.weight.flatten(start_dim=1), lora_A.weight.flatten(start_dim=1))
        lora_weight = lora_weight.reshape(weight.shape)
        magnitude = self.lora_magnitude_vector[active_adapter]
        weight_norm = self._get_weight_norm(weight, lora_weight, scaling)
        # see section 4.3 of DoRA (https://arxiv.org/abs/2402.09353)
        # "[...] we suggest treating ||V +∆V ||_c in
        # Eq. (5) as a constant, thereby detaching it from the gradient
        # graph. This means that while ||V + ∆V ||_c dynamically
        # reflects the updates of ∆V , it won’t receive any gradient
        # during backpropagation"
        weight_norm = weight_norm.detach()
        mag_norm_scale = magnitude / weight_norm
        result_dora = (mag_norm_scale - 1) * (
            F.conv2d(
                x,
                weight,
                bias=None,
                stride=base_layer.stride,
                padding=base_layer.padding,
                dilation=base_layer.dilation,
                groups=base_layer.groups,
            )
        ) + mag_norm_scale * lora_B(lora_A(x)) * scaling

        return result_dora

    def forward(self, x: torch.Tensor, *args, **kwargs) -> torch.Tensor:
        self._check_forward_args(x, *args, **kwargs)
        adapter_names = kwargs.pop("adapter_names", None)

        if self.disable_adapters:
            if self.merged:
                self.unmerge()
            result = self.base_layer(x, *args, **kwargs)
        elif adapter_names is not None:
            result = self._mixed_batch_forward(x, *args, adapter_names=adapter_names, **kwargs)
        elif self.merged:
            result = self.base_layer(x, *args, **kwargs)
        else:
            result = self.base_layer(x, *args, **kwargs)
            torch_result_dtype = result.dtype

            for active_adapter in self.active_adapters:
                if active_adapter not in self.lora_A.keys():
                    continue
                lora_A = self.lora_A[active_adapter]
                lora_B = self.lora_B[active_adapter]
                dropout = self.lora_dropout[active_adapter]
                scaling = self.scaling[active_adapter]
                x = x.to(lora_A.weight.dtype)

                if not self.use_dora[active_adapter]:
                    result = result + lora_B(lora_A(dropout(x))) * scaling
                else:
                    x = dropout(x)
                    result = result + self._apply_dora(x, lora_A, lora_B, scaling, active_adapter)

            result = result.to(torch_result_dtype)
        return result

    def __repr__(self) -> str:
        rep = super().__repr__()
        return "lora." + rep


def dispatch_default_mola(
    target: torch.nn.Module,
    adapter_name: str,
    lora_config: LoraConfig,
    **kwargs,
) -> Optional[torch.nn.Module]:
    new_module = None

    if isinstance(target, BaseTunerLayer):
        target_base_layer = target.get_base_layer()
    else:
        target_base_layer = target

    if isinstance(target_base_layer, torch.nn.Embedding):
        embedding_kwargs = kwargs.copy()
        embedding_kwargs.pop("fan_in_fan_out", None)
        embedding_kwargs.update(lora_config.loftq_config)
        new_module = Embedding(target, adapter_name, **embedding_kwargs)
    elif isinstance(target_base_layer, torch.nn.Conv2d):
        kwargs.update(lora_config.loftq_config)
        new_module = Conv2d(target, adapter_name, **kwargs)
    ##newmodified
    ## target modules of MoLA are all the instances of torch.nn.Linear
    elif isinstance(target_base_layer, torch.nn.Linear):
        in_features, out_features = target.in_features, target.out_features
        if kwargs["fan_in_fan_out"]:
            warnings.warn(
                "fan_in_fan_out is set to True but the target module is `torch.nn.Linear`. "
                "Setting fan_in_fan_out to False."
            )
            kwargs["fan_in_fan_out"] = lora_config.fan_in_fan_out = False
        kwargs.update(lora_config.loftq_config)
        # new_module = Linear(target, adapter_name, **kwargs)
        ##newmodified
        bias = hasattr(target, "bias") and target.bias is not None
        layer_index = kwargs.pop('layer_index', None)
        number_experts = kwargs.pop('number_experts', None)
        top_k = kwargs.pop('top_k', None)
        new_module = Linear_MoE(adapter_name, in_features, out_features, bias=bias, layer_index=layer_index,number_experts=number_experts, top_k=top_k, **kwargs) ##newmodified
    elif isinstance(target_base_layer, Conv1D):
        if not kwargs["fan_in_fan_out"]:
            warnings.warn(
                "fan_in_fan_out is set to False but the target module is `Conv1D`. " "Setting fan_in_fan_out to True."
            )
            kwargs["fan_in_fan_out"] = lora_config.fan_in_fan_out = True
        kwargs.update(lora_config.loftq_config)
        new_module = Linear(target, adapter_name, is_target_conv_1d_layer=True, **kwargs)

    return new_module
# had to adapt it for `lora_only` to work
##newmodified
def mark_only_lora_as_trainable(model: nn.Module, bias: str = "none") -> None:
    for n, p in model.named_parameters():
        if "lora_" not in n and 'router' not in n and 'resnet' not in n:
            p.requires_grad = False
    if bias == "none":
        return
    elif bias == "all":
        for n, p in model.named_parameters():
            if "bias" in n:
                p.requires_grad = True
    elif bias == "lora_only":
        for m in model.modules():
            if isinstance(m, LoraLayer) and hasattr(m, "bias") and m.bias is not None:
                m.bias.requires_grad = True
    else:
        raise NotImplementedError

##newmodified
def mark_only_lora_B_as_trainable(model: nn.Module, bias: str = "none") -> None:
    print('*******************', 'ONLY TRAIN EXPERTS WITHOUT ROUTER', '*******************')
    for n, p in model.named_parameters():
        if "lora_" not in n:
            p.requires_grad = False
    if bias == "none":
        return
    elif bias == "all":
        for n, p in model.named_parameters():
            if "bias" in n:
                p.requires_grad = True
    elif bias == "lora_only":
        for m in model.modules():
            if isinstance(m, LoraLayer) and hasattr(m, "bias") and m.bias is not None:
                m.bias.requires_grad = True
    else:
        raise NotImplementedError
##newmodified
class LoraMoE_Layer:
    def __init__(self, in_features: int, out_features: int,number_experts: int, **kwargs):
        self.r = {}
        self.lora_alpha = {}
        self.scaling = {}
        self.lora_dropout = nn.ModuleDict({})
        self.lora_A = nn.ModuleDict({})
        self.lora_B = nn.ModuleDict({})
        self.lora_C = nn.ModuleDict({})
        self.router = nn.ModuleDict({})
        self.resnet = nn.ModuleDict({})
        # For Embedding layer
        self.lora_embedding_A = nn.ParameterDict({})
        self.lora_embedding_B = nn.ParameterDict({})
        self.lora_embedding_C = nn.ParameterDict({})
        # Mark the weight as unmerged
        self.lora_X = nn.Parameter(torch.zeros((number_experts, in_features),device='cuda'),requires_grad=False)
        self.merged = False
        self.disable_adapters = False
        self.in_features = in_features
        self.out_features = out_features
        self.kwargs = kwargs

    def update_layer(self, adapter_name, r, lora_alpha, lora_dropout, init_lora_weights, layer_index): ### newmodified
        self.r[adapter_name] = r
        self.lora_alpha[adapter_name] = lora_alpha
        if lora_dropout > 0.0:
            lora_dropout_layer = nn.Dropout(p=lora_dropout)
        else:
            lora_dropout_layer = nn.Identity()

        self.lora_dropout.update(nn.ModuleDict({adapter_name: lora_dropout_layer}))
        # Actual trainable parameters
        if isinstance(r, list): 
            ##newmodified
            # print("LINEAR", self.number_experts)
            self.router.update(nn.ModuleDict({'router': nn.Linear(self.in_features, self.number_experts, bias=False)}))
            self.resnet.update(nn.ModuleDict({'resnet': nn.Linear(self.in_features,r[layer_index], bias=False)}))#add
            for i in range(self.number_experts):
                adapter_name_moe = adapter_name + '_' + str(i)
                r_tmp = int(r[layer_index]-self.number_experts/2+i+1)
                r_tmp = max(1, r_tmp)
                # self.lora_A.update(nn.ModuleDict({adapter_name_moe: nn.Linear(self.in_features, r[layer_index], bias=False)}))
                # self.lora_B.update(nn.ModuleDict({adapter_name_moe: nn.Linear(r[layer_index], self.out_features, bias=False)}))
                self.lora_A.update(nn.ModuleDict({adapter_name_moe: nn.Linear(self.in_features+r[layer_index], r_tmp, bias=False)}))#add
                self.lora_B.update(nn.ModuleDict({adapter_name_moe: nn.Linear(r_tmp, self.out_features, bias=False)}))
                self.lora_C.update(nn.ModuleDict({adapter_name_moe: nn.Linear(r_tmp,r_tmp, bias=False)}))
            self.scaling[adapter_name] = lora_alpha / r[layer_index]
        else:
            if r > 0:
                self.lora_A.update(nn.ModuleDict({adapter_name: nn.Linear(self.in_features+r, r, bias=False)}))#add
                self.lora_B.update(nn.ModuleDict({adapter_name: nn.Linear(r, self.out_features, bias=False)}))
                self.lora_C.update(nn.ModuleDict({adapter_name: nn.Linear(r, r, bias=False)}))
                self.scaling[adapter_name] = lora_alpha / r
        ##newmodified
        if init_lora_weights:
            if isinstance(r, list):
                self.reset_lora_parameters_MoE(adapter_name)
            else:
                self.reset_lora_parameters(adapter_name)
        self.to(self.weight.device)

    def update_layer_conv2d(self, adapter_name, r, lora_alpha, lora_dropout, init_lora_weights):
        self.r[adapter_name] = r
        self.lora_alpha[adapter_name] = lora_alpha
        if lora_dropout > 0.0:
            lora_dropout_layer = nn.Dropout(p=lora_dropout)
        else:
            lora_dropout_layer = nn.Identity()

        self.lora_dropout.update(nn.ModuleDict({adapter_name: lora_dropout_layer}))
        # Actual trainable parameters
        if r > 0:
            kernel_size = self.kwargs["kernel_size"]
            stride = self.kwargs["stride"]
            padding = self.kwargs["padding"]
            self.lora_A.update(
                nn.ModuleDict({adapter_name: nn.Conv2d(self.in_features+r, r, kernel_size, stride, padding, bias=False)})#add
            )
            self.lora_B.update(
                nn.ModuleDict({adapter_name: nn.Conv2d(r, self.out_features, (1, 1), (1, 1), bias=False)})
            )
            self.lora_C.update(
                nn.ModuleDict({adapter_name: nn.Conv2d(r, r, (1, 1), (1, 1), bias=False)})
            )
            self.scaling[adapter_name] = lora_alpha / r
        if init_lora_weights:
            self.reset_lora_parameters(adapter_name)
        self.to(self.weight.device)

    def update_layer_embedding(self, adapter_name, r, lora_alpha, lora_dropout, init_lora_weights):
        self.r[adapter_name] = r
        self.lora_alpha[adapter_name] = lora_alpha
        if lora_dropout > 0.0:
            lora_dropout_layer = nn.Dropout(p=lora_dropout)
        else:
            lora_dropout_layer = nn.Identity()

        self.lora_dropout.update(nn.ModuleDict({adapter_name: lora_dropout_layer}))
        # Actual trainable parameters
        if r > 0:
            self.lora_embedding_A.update(
                nn.ParameterDict({adapter_name: nn.Parameter(self.weight.new_zeros((r, self.in_features)))})
            )
            self.lora_embedding_B.update(
                nn.ParameterDict({adapter_name: nn.Parameter(self.weight.new_zeros((self.out_features, r)))})
            )
            self.lora_embedding_C.update(
                nn.ParameterDict({adapter_name: nn.Parameter(self.weight.new_zeros((r, r)))})
            )
            self.scaling[adapter_name] = lora_alpha / r
        if init_lora_weights:
            self.reset_lora_parameters(adapter_name)
        self.to(self.weight.device)
    ##newmodified
    def reset_lora_parameters_MoE(self, adapter_name):
        nn.init.kaiming_uniform_(self.router['router'].weight, a=math.sqrt(5))
        nn.init.kaiming_uniform_(self.resnet['resnet'].weight, a=math.sqrt(5))
        for i in range(self.number_experts):
            adapter_name_moe = adapter_name + '_' + str(i)
            if adapter_name_moe in self.lora_A.keys():
                # initialize A the same way as the default for nn.Linear and B to zero
                nn.init.kaiming_uniform_(self.lora_A[adapter_name_moe].weight, a=math.sqrt(5))
                nn.init.zeros_(self.lora_B[adapter_name_moe].weight)
                nn.init.zeros_(self.lora_C[adapter_name_moe].weight)
            if adapter_name_moe in self.lora_embedding_A.keys():
                # initialize a the same way as the default for nn.linear and b to zero
                nn.init.zeros_(self.lora_embedding_A[adapter_name_moe])
                nn.init.normal_(self.lora_embedding_B[adapter_name_moe])
                nn.init.zeros_(self.lora_embedding_C[adapter_name_moe])
    def reset_lora_parameters(self, adapter_name):
        if adapter_name in self.lora_A.keys():
            # initialize A the same way as the default for nn.Linear and B to zero
            nn.init.kaiming_uniform_(self.lora_A[adapter_name].weight, a=math.sqrt(5))
            nn.init.zeros_(self.lora_B[adapter_name].weight)
            nn.init.zeros_(self.lora_C[adapter_name].weight)
        if adapter_name in self.lora_embedding_A.keys():
            # initialize a the same way as the default for nn.linear and b to zero
            nn.init.zeros_(self.lora_embedding_A[adapter_name])
            nn.init.normal_(self.lora_embedding_B[adapter_name])
            nn.init.zeros_(self.lora_embedding_C[adapter_name])

##newmodified
class Linear_MoE(nn.Linear, LoraMoE_Layer):
    # Lora implemented in a dense layer
    def __init__(
        self,
        adapter_name: str,
        in_features: int,
        out_features: int,
        r: int = 0,
        lora_alpha: int = 1,
        lora_dropout: float = 0.0,
        fan_in_fan_out: bool = False,  # Set this to True if the layer to replace stores weight like (fan_in, fan_out)
        layer_index: int = 0, ## newmodified
        number_experts: list = [8] * 32,
        top_k: list = [2] * 32,
        **kwargs,
    ):
        self.layer_index = layer_index
        init_lora_weights = kwargs.pop("init_lora_weights", True)
        use_rslora = kwargs.pop("use_rslora", None)
        use_dora = kwargs.pop("use_dora", None)
        loaded_in_8bit = kwargs.pop("loaded_in_8bit", None)
        loaded_in_4bit = kwargs.pop("loaded_in_4bit", None)

        nn.Linear.__init__(self, in_features, out_features, **kwargs)
        ##newmodified
        LoraMoE_Layer.__init__(self, in_features=in_features, out_features=out_features,number_experts = number_experts[layer_index])
        
        # Freezing the pre-trained weight matrix
        self.weight.requires_grad = False
        self.number_experts = number_experts[layer_index]
        self.top_k = top_k[layer_index]
        self.fan_in_fan_out = fan_in_fan_out
        if fan_in_fan_out:
            self.weight.data = self.weight.data.T

        nn.Linear.reset_parameters(self)
        self.update_layer(adapter_name, r, lora_alpha, lora_dropout, init_lora_weights, layer_index) ##newmodified
        self.active_adapter = adapter_name
        self.softmax = nn.Softmax(dim=-1) ##newmodified
        self.sigmoid = nn.Sigmoid()

    def merge(self):
        if self.active_adapter not in self.lora_A.keys():
            return
        if self.merged:
            warnings.warn("Already merged. Nothing to do.")
            return
        if self.r[self.active_adapter] > 0:
            self.weight.data += (
                transpose(
                    self.lora_B[self.active_adapter].weight@ self.lora_C[self.active_adapter].weight @ self.lora_A[self.active_adapter].weight,
                    self.fan_in_fan_out,
                )
                * self.scaling[self.active_adapter]
            )
            self.merged = True

    def unmerge(self):
        if self.active_adapter not in self.lora_A.keys():
            return
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return
        if self.r[self.active_adapter] > 0:
            self.weight.data -= (
                transpose(
                    self.lora_B[self.active_adapter].weight@ self.lora_C[self.active_adapter].weight @ self.lora_A[self.active_adapter].weight,
                    self.fan_in_fan_out,
                )
                * self.scaling[self.active_adapter]
            )
            self.merged = False

    def forward(self, x: torch.Tensor):
        previous_dtype = x.dtype
        top_k_tmp = self.top_k
        ##newmodified
        ### modified2 elif self.r[self.active_adapter] > 0 and not self.merged:
        # if x.dtype != self.weight.dtype:
        #     x = x.to(self.weight.dtype)
        result = F.linear(x, transpose(self.weight, self.fan_in_fan_out), bias=self.bias)
        batch_size, sequence_length, hidden_dim = x.shape
        number_experts = self.lora_X.shape[0]
        self.lora_X = nn.Parameter(self.lora_X.to(x.device), requires_grad=False)
        with torch.no_grad():
           # 展平 x 以匹配 lora_X 的形状
            x_flat = x.view(-1, hidden_dim)  # [batch_size * sequence_length, hidden_dim]

            # 计算曼哈顿距离并直接进行归一化和平均值计算
            aggregated_distances = torch.sum(
                torch.abs(self.lora_X.unsqueeze(1) - x_flat.unsqueeze(0)), dim=-1
            ).mean(dim=-1)  # [number_experts]
            aggregated_distances /= aggregated_distances.sum()  # 归一化

            # 计算熵
            def entropy(p):
                p = torch.clamp(p, min=1e-10)  # 避免 log(0)
                return -torch.sum(p * torch.log(p))

            # 计算归一化熵值
            log2_number_experts = torch.log2(torch.tensor(number_experts, dtype=torch.float32))  # 预先计算常量
            entropy_value = entropy(aggregated_distances) / log2_number_experts  # 计算熵值

            #print("aggregated_distances", aggregated_distances)
            # print("entropy_value", entropy_value.item())  # 输出熵值
            # print("number_experts", number_experts)  # 输出熵值
            if entropy_value.item() > 0.693:
                #print("entropy_value", entropy_value.item())  # 输出熵值
                top_k_tmp = min(self.number_experts,top_k_tmp+1)
        
        
        x = x.view(-1, hidden_dim)
        router_logits = self.router['router'](x)
        # router_probabilities = nn.functional.softmax(router_logits, dim=-1, dtype=self.dtype).to(self.input_dtype)
        routing_weights_before = F.softmax(router_logits, dim=1, dtype=torch.float)
        routing_weights, selected_experts = torch.topk(routing_weights_before, top_k_tmp, dim=-1)
        routing_weights /= routing_weights.sum(dim=-1, keepdim=True)
        routing_weights = routing_weights.to(x.dtype)

        expert_mask = torch.nn.functional.one_hot(selected_experts, num_classes=self.number_experts).permute(2, 1, 0)

        final_hidden_states = torch.zeros(
            (batch_size * sequence_length, self.out_features), dtype=x.dtype, device=x.device
        )

        for expert_idx in range(self.number_experts):
            adapter_name_moe = self.active_adapter + '_' + str(expert_idx)
            idx, top_x = torch.where(expert_mask[expert_idx])

            if top_x.shape[0] == 0:
                continue

            # in torch it is faster to index using lists than torch tensors
            top_x_list = top_x.tolist()
            idx_list = idx.tolist()

            # Index the correct hidden states and compute the expert hidden state for
            # the current expert. We need to make sure to multiply the output hidden
            # states by `routing_weights` on the corresponding tokens (top-1 and top-2)
            current_state = x[None, top_x_list].reshape(-1, hidden_dim)
            with torch.no_grad():
                self.lora_X[expert_idx] = (current_state.mean(dim=0)*0.7+self.lora_X[expert_idx]*0.3)
            res_tmp = self.resnet['resnet'](current_state)
            res_tmp = res_tmp+torch.tanh(res_tmp)
            res_tmp = F.layer_norm(res_tmp, res_tmp.size()[1:])#ranjinle
            current_state = torch.cat([current_state, res_tmp], dim=1)
            tmp_a  = self.lora_A[adapter_name_moe](self.lora_dropout[self.active_adapter](current_state))
            tmp_a = torch.relu(tmp_a)+tmp_a
            tmp_a = F.layer_norm(tmp_a, tmp_a.size()[1:])
            tmp_c = self.lora_C[adapter_name_moe](tmp_a)
            all_c = torch.relu(tmp_c)+tmp_c
            all_c = F.layer_norm(all_c, all_c.size()[1:])
            # all_c = tmp_c
            # for name, param in self.lora_A[adapter_name_moe].named_parameters():
            #     if param.grad is not None:
            #         print(f"Gradient of {name} in lora_A[{adapter_name_moe}]: {param.grad}")
            # for param in self.lora_C[adapter_name_moe].parameters():
            #     print(param.requires_grad)
            #print("grad",self.lora_C[adapter_name_moe].named_parameters().parm.requires_grad)
            if self.layer_index > 0:#changed
                all_c = tmp_a    
            expert_output = (
                    self.lora_B[adapter_name_moe](
                        all_c
                    )
                    * self.scaling[self.active_adapter]
            )
            current_hidden_states = expert_output * routing_weights[top_x_list, idx_list, None]

            # However `index_add_` only support torch tensors for indexing so we'll use
            # the `top_x` tensor here.
            final_hidden_states.index_add_(0, top_x, current_hidden_states.to(x.dtype))

            # x = x.to(self.lora_A[adapter_name_moe].weight.dtype)

            # expert_Gating = router_probabilities[:,:,i].unsqueeze(-1)
            # expert_o = expert_Gating * expert_output
            # result += router_probabilities[:,:,i].unsqueeze(-1) * expert_output

        final_hidden_states = final_hidden_states.reshape(batch_size, sequence_length, self.out_features)
        result += final_hidden_states


        result = result.to(previous_dtype)

        return result, router_logits