import math
import torch
import torch.nn as nn
from torch.nn import functional as F
import torch.distributed as ddp

from sparsebit.quantization.quantizers import Quantizer as BaseQuantizer
from sparsebit.quantization.quantizers import register_quantizer
from .lsq import gs_scaling, STE


@register_quantizer
class Quantizer(BaseQuantizer):
    TYPE = "LSQ+"

    def __init__(self, config):
        super(Quantizer, self).__init__(config)
        self.eps = 1e-6
        self.init_params = False

    def calc_qparams(self):
        if self.fake_fused:
            return self.scale, self.zero_point
        if not self.init_params:
            if self.is_perchannel:
                x_oc = torch.cat(self.observer.data_cache, axis=1)
                self.observer.reset_data_cache()
                assert (
                    self.is_symmetric
                ), "LSQ+ only support per-channel-sysmetric quant for weight"
                mean, std = x_oc.mean(axis=1), x_oc.std(axis=1)
                scale = (
                    2
                    * torch.maximum((mean - 3 * std).abs(), (mean + 3 * std).abs())
                    / (self.qdesc.qmax - self.qdesc.qmin)
                )
                self.scale = nn.Parameter(self._broadcast_qparams(scale)).to(
                    self.device
                )
                self.zero_point = self._broadcast_qparams(torch.zeros_like(self.scale))
            else:
                assert (
                    not self.is_symmetric
                ), "LSQ+ only support per-tensor-affine quant for activation"
                scale, zero_point = self.observer.calc_qparams()
                self.scale = nn.Parameter(self._broadcast_qparams(scale)).to(
                    self.device
                )
                self.zero_point = nn.Parameter(self._broadcast_qparams(zero_point)).to(
                    self.device
                )
            self.init_params = True
        return self.scale, self.zero_point

    def _forward(self, x):
        if self.is_perchannel:
            num_perchannel = x.numel() / x.shape[self.qdesc.ch_axis]
            gs_ratio = 1.0 / math.sqrt(num_perchannel * self.qdesc.qmax)
        else:
            gs_ratio = 1.0 / math.sqrt(x.numel() * self.qdesc.qmax)
        scale = gs_scaling.apply(self.scale.clamp(self.eps), gs_ratio)
        zero_point = self.zero_point
        if self.zero_point.requires_grad:
            zero_point = gs_scaling.apply(self.zero_point, gs_ratio)
        x_dq = STE.apply(x, scale, zero_point, self.qdesc, self.backend)
        return x_dq
