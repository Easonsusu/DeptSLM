"""Fail-closed Linux/NVIDIA/BF16 and QLoRA NF4 hardware preflight."""

from __future__ import annotations

import importlib
import platform
from dataclasses import dataclass

from .contract import BITSANDBYTES_VERSION, canonical_hardware_fingerprint


class HardwarePreflightError(RuntimeError):
    def __init__(self, code: str = "runtime_hardware_unsupported") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HardwareEvidence:
    hardware_profile_id: str
    hardware_fingerprint: str
    fields: dict[str, object]


def preflight_hardware(*, qlora: bool = False) -> HardwareEvidence:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise HardwarePreflightError()
    try:
        torch = importlib.import_module("torch")
        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise HardwarePreflightError()
        if not bool(torch.cuda.is_bf16_supported()):
            raise HardwarePreflightError()
        properties = torch.cuda.get_device_properties(0)
        capability = (
            getattr(properties, "major", None),
            getattr(properties, "minor", None),
        )
        if not all(type(value) is int for value in capability):
            raise HardwarePreflightError()
        cuda_version = getattr(torch.version, "cuda", None)
        name = getattr(properties, "name", None)
        if not isinstance(cuda_version, str) or not isinstance(name, str):
            raise HardwarePreflightError()
        fields: dict[str, object] = {
            "hardware_profile_id": "linux-x86_64-nvidia-one-gpu-bf16",
            "platform": "Linux",
            "architecture": "x86_64",
            "torch_cuda_version": cuda_version,
            "gpu_model": name,
            "compute_capability": f"{capability[0]}.{capability[1]}",
            "bf16_supported": True,
        }
        if qlora:
            bitsandbytes = importlib.import_module("bitsandbytes")
            version = getattr(bitsandbytes, "__version__", None)
            if version != BITSANDBYTES_VERSION:
                raise HardwarePreflightError()
            _nf4_kernel_preflight(torch, bitsandbytes)
            fields["bitsandbytes_version"] = version
            fields["qlora_nf4_kernel_preflight"] = True
            profile = "linux-x86_64-nvidia-one-gpu-bf16-nf4"
        else:
            profile = "linux-x86_64-nvidia-one-gpu-bf16"
        fields["hardware_profile_id"] = profile
        return HardwareEvidence(profile, canonical_hardware_fingerprint(fields), fields)
    except HardwarePreflightError:
        raise
    except Exception as error:
        raise HardwarePreflightError() from error


def _nf4_kernel_preflight(torch: object, bitsandbytes: object) -> None:
    del bitsandbytes
    try:
        layer = torch.nn.Linear(2, 2, bias=False).cuda()
        quantized = torch.nn.Parameter(layer.weight.detach().clone())
        # Importing the module is not enough: execute a real CUDA-side 4-bit
        # conversion and a forward pass before accepting QLoRA.
        from bitsandbytes.functional import quantize_4bit

        packed, state = quantize_4bit(quantized, quant_type="nf4", compress_statistics=True)
        if packed.device.type != "cuda" or state is None:
            raise HardwarePreflightError()
        del packed, state, layer
        torch.cuda.synchronize()
    except HardwarePreflightError:
        raise
    except Exception as error:
        raise HardwarePreflightError() from error
