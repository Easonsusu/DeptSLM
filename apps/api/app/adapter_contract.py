"""Model-free Phase 12.1A adapter compatibility contracts.

This module is deliberately limited to immutable metadata validation.  It does
not import a model, tokenizer, PEFT, Transformers, PyTorch, safetensors, or
LlamaFactory, and it never reads tensor payload bytes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import BinaryIO

ADAPTER_ARTIFACT_CONTRACT_VERSION = "phase12-adapter-artifact-v1"
ADAPTER_MANIFEST_CONTRACT_VERSION = "phase12-adapter-manifest-v1"
ADAPTER_CONFIG_CONTRACT_VERSION = "phase12-adapter-config-v1"
ADAPTER_TENSOR_CONTRACT_VERSION = "phase12-adapter-tensors-v1"
ADAPTER_SOURCE_CONTRACT_VERSION = "phase12-adapter-source-v1"
ADAPTER_INTAKE_CONTRACT_VERSION = "phase12-adapter-intake-v1"
QWEN3_ARCHITECTURE_CONTRACT_VERSION = "phase12-qwen3-0.6b-architecture-v1"

LLAMAFACTORY_VERSION = "0.9.5"
LLAMAFACTORY_PEFT_MIN_VERSION = "0.18.0"
LLAMAFACTORY_PEFT_MAX_VERSION = "0.18.1"
LLAMAFACTORY_TRANSFORMERS_MIN_VERSION = "4.55.0"
LLAMAFACTORY_TRANSFORMERS_MAX_VERSION = "5.6.0"
LLAMAFACTORY_TRANSFORMERS_EXCLUDED_VERSION = "4.57.0"
PEFT_FORMAT_REFERENCE_VERSION = "0.18.1"
TRANSFORMERS_ARCHITECTURE_REFERENCE_VERSION = "4.55.0"
SAFETENSORS_FORMAT_REFERENCE_VERSION = "0.7.0"

BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
BASE_MODEL_LICENSE = "Apache-2.0"
MODEL_CACHE_PATH = f"/runtime/deptslm/model_cache/qwen3-0.6b-{BASE_MODEL_REVISION}"

QWEN3_ARCHITECTURE = "Qwen3ForCausalLM"
QWEN3_MODEL_TYPE = "qwen3"
QWEN3_HIDDEN_SIZE = 1024
QWEN3_INTERMEDIATE_SIZE = 3072
QWEN3_NUM_HIDDEN_LAYERS = 28
QWEN3_NUM_ATTENTION_HEADS = 16
QWEN3_NUM_KEY_VALUE_HEADS = 8
QWEN3_HEAD_DIM = 128
QWEN3_ATTENTION_BIAS = False
QWEN3_TIE_WORD_EMBEDDINGS = True
QWEN3_LAYER_INDICES = tuple(range(QWEN3_NUM_HIDDEN_LAYERS))

LORA_RANK = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05
PEFT_TYPE = "LORA"
TASK_TYPE = "CAUSAL_LM"
LORA_BIAS = "none"
LORA_FAN_IN_FAN_OUT = False
LORA_USE_RSLORA = False
LORA_USE_DORA = False
LORA_MODULES_TO_SAVE = None
LORA_TARGET_PARAMETERS = None

ATTENTION_TARGET_MODULES = ("q_proj", "k_proj", "v_proj", "o_proj")
MLP_TARGET_MODULES = ("gate_proj", "up_proj", "down_proj")
TARGET_MODULES = ATTENTION_TARGET_MODULES + MLP_TARGET_MODULES

BASE_LINEAR_SHAPES: dict[str, tuple[int, int]] = {
    "q_proj": (2048, 1024),
    "k_proj": (1024, 1024),
    "v_proj": (1024, 1024),
    "o_proj": (1024, 2048),
    "gate_proj": (3072, 1024),
    "up_proj": (3072, 1024),
    "down_proj": (1024, 3072),
}
ADAPTER_A_SHAPES: dict[str, tuple[int, int]] = {
    module: (LORA_RANK, base_shape[1]) for module, base_shape in BASE_LINEAR_SHAPES.items()
}
ADAPTER_B_SHAPES: dict[str, tuple[int, int]] = {
    module: (base_shape[0], LORA_RANK) for module, base_shape in BASE_LINEAR_SHAPES.items()
}

EXPECTED_TENSOR_COUNT = QWEN3_NUM_HIDDEN_LAYERS * len(TARGET_MODULES) * 2
EXPECTED_TENSOR_ELEMENTS = 10_092_544
TENSOR_DTYPE_BYTES = {"F16": 2, "BF16": 2, "F32": 4}
EXPECTED_TENSOR_BYTES = {
    dtype: EXPECTED_TENSOR_ELEMENTS * size for dtype, size in TENSOR_DTYPE_BYTES.items()
}
MAX_CONFIG_BYTES = 65_536
MAX_SAFETENSORS_HEADER_BYTES = 1_048_576
MAX_ADAPTER_FILE_BYTES = 44_040_192


SAFE_ERROR_CODES = frozenset(
    {
        "adapter_config_invalid",
        "adapter_config_unsupported",
        "adapter_header_invalid",
        "adapter_header_too_large",
        "adapter_file_too_large",
        "adapter_tensor_set_invalid",
        "adapter_tensor_shape_invalid",
        "adapter_tensor_dtype_invalid",
        "adapter_tensor_offsets_invalid",
        "adapter_tensor_size_invalid",
    }
)


class AdapterContractError(RuntimeError):
    """A fixed, content-free adapter contract failure."""

    def __init__(self, code: str = "adapter_config_invalid") -> None:
        self.code = code if code in SAFE_ERROR_CODES else "adapter_config_invalid"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class AdapterConfigSummary:
    """Content-free values from one accepted PEFT adapter configuration."""

    base_model_name_or_path: str
    revision: None
    peft_type: str
    task_type: str
    inference_mode: bool
    r: int
    target_modules: tuple[str, ...]
    lora_alpha: int
    lora_dropout: float

    @property
    def base_model_id(self) -> str:
        return BASE_MODEL_ID


@dataclass(frozen=True, slots=True)
class SafetensorsSummary:
    """Content-free safetensors metadata; no tensor values are retained."""

    contract_version: str
    layer_count: int
    target_module_count: int
    tensor_count: int
    dtype: str
    total_tensor_elements: int
    total_tensor_bytes: int

    @property
    def total_elements(self) -> int:
        return self.total_tensor_elements

    @property
    def total_bytes(self) -> int:
        return self.total_tensor_bytes


# This is the exact dataclass serialization field set of PEFT 0.18.1's
# LoraConfig.to_dict() after its runtime_config field is intentionally removed.
EXTERNAL_CONFIG_KEYS = frozenset(
    {
        "base_model_name_or_path",
        "revision",
        "peft_type",
        "task_type",
        "inference_mode",
        "r",
        "target_modules",
        "exclude_modules",
        "lora_alpha",
        "lora_dropout",
        "fan_in_fan_out",
        "bias",
        "use_rslora",
        "modules_to_save",
        "init_lora_weights",
        "layers_to_transform",
        "layers_pattern",
        "rank_pattern",
        "alpha_pattern",
        "megatron_config",
        "megatron_core",
        "trainable_token_indices",
        "loftq_config",
        "eva_config",
        "corda_config",
        "use_dora",
        "alora_invocation_tokens",
        "use_qalora",
        "qalora_group_size",
        "layer_replication",
        "lora_bias",
        "target_parameters",
        "arrow_config",
        "ensure_weight_tying",
    }
)

_CANONICAL_TARGET_MODULES = tuple(TARGET_MODULES)
_TENSOR_KEY = re.compile(
    r"\Abase_model\.model\.model\.layers\.([0-9]+)\."
    r"(self_attn|mlp)\.([A-Za-z0-9_]+)\.lora_([AB])\.weight\Z",
    re.ASCII,
)


def expected_tensor_names() -> tuple[str, ...]:
    """Return the deterministic, complete adapter tensor key set."""

    names: list[str] = []
    for layer in QWEN3_LAYER_INDICES:
        for module in TARGET_MODULES:
            family = "self_attn" if module in ATTENTION_TARGET_MODULES else "mlp"
            for component in ("A", "B"):
                names.append(
                    f"base_model.model.model.layers.{layer}.{family}.{module}."
                    f"lora_{component}.weight"
                )
    return tuple(names)


EXPECTED_TENSOR_NAMES = expected_tensor_names()


def expected_tensor_shapes() -> dict[str, tuple[int, int]]:
    """Return a fresh mapping of every exact expected tensor shape."""

    result: dict[str, tuple[int, int]] = {}
    for layer in QWEN3_LAYER_INDICES:
        for module in TARGET_MODULES:
            family = "self_attn" if module in ATTENTION_TARGET_MODULES else "mlp"
            result[f"base_model.model.model.layers.{layer}.{family}.{module}.lora_A.weight"] = (
                ADAPTER_A_SHAPES[module]
            )
            result[f"base_model.model.model.layers.{layer}.{family}.{module}.lora_B.weight"] = (
                ADAPTER_B_SHAPES[module]
            )
    return result


EXPECTED_TENSOR_SHAPES = expected_tensor_shapes()


def _fail(code: str) -> None:
    raise AdapterContractError(code)


def _as_config_bytes(raw: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(raw, str):
        try:
            raw = raw.encode("utf-8")
        except UnicodeEncodeError:
            _fail("adapter_config_invalid")
    elif isinstance(raw, (bytearray, memoryview)):
        raw = bytes(raw)
    if not isinstance(raw, bytes) or not raw:
        _fail("adapter_config_invalid")
    if len(raw) > MAX_CONFIG_BYTES:
        _fail("adapter_config_invalid")
    if raw.startswith(b"\xef\xbb\xbf"):
        _fail("adapter_config_invalid")
    return raw


class _DuplicateKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey()
        result[key] = value
    return result


def _reject_json_constant(_: str) -> object:
    raise ValueError()


def _parse_config_object(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError, json.JSONDecodeError):
        _fail("adapter_config_invalid")
    if not isinstance(value, dict):
        _fail("adapter_config_invalid")
    return value


def _is_exact_int(value: object) -> bool:
    return type(value) is int


def _is_exact_bool(value: object) -> bool:
    return type(value) is bool


def _validate_config_values(value: dict[str, object]) -> AdapterConfigSummary:
    if set(value) != EXTERNAL_CONFIG_KEYS:
        _fail("adapter_config_invalid")

    if value["base_model_name_or_path"] not in (BASE_MODEL_ID, MODEL_CACHE_PATH):
        _fail("adapter_config_unsupported")
    if value["revision"] is not None:
        _fail("adapter_config_unsupported")
    if value["peft_type"] != PEFT_TYPE or value["task_type"] != TASK_TYPE:
        _fail("adapter_config_unsupported")
    if not _is_exact_bool(value["inference_mode"]) or value["inference_mode"]:
        _fail("adapter_config_unsupported")
    if not _is_exact_int(value["r"]) or value["r"] != LORA_RANK:
        _fail("adapter_config_unsupported")
    if not _is_exact_int(value["lora_alpha"]) or value["lora_alpha"] != LORA_ALPHA:
        _fail("adapter_config_unsupported")
    if type(value["lora_dropout"]) is not float or value["lora_dropout"] != LORA_DROPOUT:
        _fail("adapter_config_unsupported")
    if value["bias"] != LORA_BIAS or value["fan_in_fan_out"] is not False:
        _fail("adapter_config_unsupported")
    if value["use_rslora"] is not False or value["use_dora"] is not False:
        _fail("adapter_config_unsupported")

    target_modules = value["target_modules"]
    if not isinstance(target_modules, list) or any(
        not isinstance(item, str) for item in target_modules
    ):
        _fail("adapter_config_unsupported")
    if len(target_modules) != len(TARGET_MODULES) or len(set(target_modules)) != len(
        target_modules
    ):
        _fail("adapter_config_unsupported")
    if set(target_modules) != set(TARGET_MODULES):
        _fail("adapter_config_unsupported")

    if value["exclude_modules"] is not None:
        _fail("adapter_config_unsupported")
    if value["modules_to_save"] is not None:
        _fail("adapter_config_unsupported")
    if value["init_lora_weights"] is not True:
        _fail("adapter_config_unsupported")
    if value["layers_to_transform"] is not None or value["layers_pattern"] is not None:
        _fail("adapter_config_unsupported")
    if value["rank_pattern"] != {} or value["alpha_pattern"] != {}:
        _fail("adapter_config_unsupported")
    if value["megatron_config"] is not None or value["megatron_core"] != "megatron.core":
        _fail("adapter_config_unsupported")
    if value["trainable_token_indices"] is not None:
        _fail("adapter_config_unsupported")
    if (
        value["loftq_config"] != {}
        or value["eva_config"] is not None
        or value["corda_config"] is not None
    ):
        _fail("adapter_config_unsupported")
    if value["alora_invocation_tokens"] is not None or value["use_qalora"] is not False:
        _fail("adapter_config_unsupported")
    if not _is_exact_int(value["qalora_group_size"]) or value["qalora_group_size"] != 16:
        _fail("adapter_config_unsupported")
    if value["layer_replication"] is not None or value["lora_bias"] is not False:
        _fail("adapter_config_unsupported")
    if value["target_parameters"] is not None or value["arrow_config"] is not None:
        _fail("adapter_config_unsupported")
    if value["ensure_weight_tying"] is not False:
        _fail("adapter_config_unsupported")

    return AdapterConfigSummary(
        base_model_name_or_path=value["base_model_name_or_path"],  # type: ignore[arg-type]
        revision=None,
        peft_type=PEFT_TYPE,
        task_type=TASK_TYPE,
        inference_mode=False,
        r=LORA_RANK,
        target_modules=tuple(sorted(target_modules)),  # type: ignore[arg-type]
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
    )


def parse_external_adapter_config(
    raw: bytes | bytearray | memoryview | str,
) -> AdapterConfigSummary:
    """Parse one complete external PEFT 0.18.1 config without dependencies."""

    return _validate_config_values(_parse_config_object(_as_config_bytes(raw)))


def validate_adapter_config(raw: bytes | bytearray | memoryview | str) -> AdapterConfigSummary:
    """Alias for the model-free external configuration parser."""

    return parse_external_adapter_config(raw)


def _canonical_config_object() -> dict[str, object]:
    # Keep this object byte-compatible with PEFT 0.18.1's closed LoraConfig
    # serialization while removing the operator-controlled model-cache path.
    return {
        "alpha_pattern": {},
        "alora_invocation_tokens": None,
        "arrow_config": None,
        "base_model_name_or_path": BASE_MODEL_ID,
        "bias": LORA_BIAS,
        "corda_config": None,
        "ensure_weight_tying": False,
        "exclude_modules": None,
        "eva_config": None,
        "fan_in_fan_out": False,
        "inference_mode": False,
        "init_lora_weights": True,
        "layer_replication": None,
        "layers_pattern": None,
        "layers_to_transform": None,
        "lora_alpha": LORA_ALPHA,
        "lora_bias": False,
        "lora_dropout": LORA_DROPOUT,
        "megatron_config": None,
        "megatron_core": "megatron.core",
        "modules_to_save": None,
        "peft_type": PEFT_TYPE,
        "qalora_group_size": 16,
        "rank_pattern": {},
        "r": LORA_RANK,
        "revision": None,
        "target_modules": list(_CANONICAL_TARGET_MODULES),
        "target_parameters": None,
        "task_type": TASK_TYPE,
        "trainable_token_indices": None,
        "use_dora": False,
        "use_qalora": False,
        "use_rslora": False,
        "loftq_config": {},
    }


def canonical_adapter_config_bytes() -> bytes:
    """Return the deterministic DeptSLM registry PEFT config bytes."""

    return (
        json.dumps(
            _canonical_config_object(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )


def canonicalize_external_adapter_config(
    raw: bytes | bytearray | memoryview | str,
) -> bytes:
    """Validate an external config and return the path-free canonical bytes."""

    parse_external_adapter_config(raw)
    return canonical_adapter_config_bytes()


def canonicalize_adapter_config(raw: bytes | bytearray | memoryview | str) -> bytes:
    """Compatibility alias for canonical external configuration conversion."""

    return canonicalize_external_adapter_config(raw)


def parse_canonical_adapter_config(
    raw: bytes | bytearray | memoryview | str,
) -> AdapterConfigSummary:
    """Require exact canonical bytes, then return the content-free summary."""

    raw_bytes = _as_config_bytes(raw)
    summary = parse_external_adapter_config(raw_bytes)
    if raw_bytes != canonical_adapter_config_bytes():
        _fail("adapter_config_invalid")
    return summary


def _read_exact(reader: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        try:
            chunk = reader.read(remaining)
        except Exception:
            _fail("adapter_header_invalid")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            _fail("adapter_header_invalid")
        chunk_bytes = bytes(chunk)
        if not chunk_bytes:
            _fail("adapter_header_invalid")
        if len(chunk_bytes) > remaining:
            _fail("adapter_header_invalid")
        chunks.append(chunk_bytes)
        remaining -= len(chunk_bytes)
    return b"".join(chunks)


class _HeaderDuplicateKey(ValueError):
    pass


def _header_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _HeaderDuplicateKey()
        result[key] = value
    return result


def _header_constant(_: str) -> object:
    raise ValueError()


def _parse_header(header: bytes) -> dict[str, object]:
    if not header or header[:1] != b"{":
        _fail("adapter_header_invalid")
    # safetensors permits ASCII space padding after the JSON object, but no
    # other trailing byte is accepted by this closed contract.
    trimmed = header.rstrip(b" ")
    if not trimmed or trimmed[:1] != b"{" or trimmed[-1:] != b"}":
        _fail("adapter_header_invalid")
    try:
        value = json.loads(
            trimmed.decode("utf-8"),
            object_pairs_hook=_header_pairs,
            parse_constant=_header_constant,
        )
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError, json.JSONDecodeError):
        _fail("adapter_header_invalid")
    if not isinstance(value, dict):
        _fail("adapter_header_invalid")
    return value


def _shape_elements(shape: object) -> int:
    if not isinstance(shape, list) or not shape:
        _fail("adapter_tensor_shape_invalid")
    result = 1
    for dimension in shape:
        if not _is_exact_int(dimension) or dimension <= 0:
            _fail("adapter_tensor_shape_invalid")
        result *= dimension
    return result


def _tensor_name_shape(name: object) -> tuple[int, int] | None:
    if not isinstance(name, str):
        return None
    match = _TENSOR_KEY.fullmatch(name)
    if match is None:
        return None
    layer_text, family, module, component = match.groups()
    if layer_text != str(int(layer_text)) or int(layer_text) not in QWEN3_LAYER_INDICES:
        return None
    if family == "self_attn" and module not in ATTENTION_TARGET_MODULES:
        return None
    if family == "mlp" and module not in MLP_TARGET_MODULES:
        return None
    return EXPECTED_TENSOR_SHAPES.get(name)


def validate_safetensors_metadata(reader: BinaryIO, file_size: int) -> SafetensorsSummary:
    """Validate only the bounded safetensors header and never tensor payload."""

    if not _is_exact_int(file_size) or file_size <= 0:
        _fail("adapter_header_invalid")
    if file_size > MAX_ADAPTER_FILE_BYTES:
        _fail("adapter_file_too_large")
    if file_size < 8:
        _fail("adapter_header_invalid")
    prefix = _read_exact(reader, 8)
    header_length = int.from_bytes(prefix, "little", signed=False)
    if header_length == 0:
        _fail("adapter_header_invalid")
    if header_length > MAX_SAFETENSORS_HEADER_BYTES:
        _fail("adapter_header_too_large")
    if 8 + header_length > file_size:
        _fail("adapter_header_invalid")
    header = _read_exact(reader, header_length)
    metadata = _parse_header(header)
    if "__metadata__" in metadata:
        _fail("adapter_tensor_set_invalid")
    if set(metadata) != set(EXPECTED_TENSOR_NAMES):
        _fail("adapter_tensor_set_invalid")

    ranges: list[tuple[int, int]] = []
    dtype: str | None = None
    total_elements = 0
    for name in EXPECTED_TENSOR_NAMES:
        descriptor = metadata.get(name)
        if not isinstance(descriptor, dict) or set(descriptor) != {
            "dtype",
            "shape",
            "data_offsets",
        }:
            _fail("adapter_tensor_set_invalid")
        descriptor_dtype = descriptor["dtype"]
        if not isinstance(descriptor_dtype, str) or descriptor_dtype not in TENSOR_DTYPE_BYTES:
            _fail("adapter_tensor_dtype_invalid")
        if dtype is None:
            dtype = descriptor_dtype  # type: ignore[assignment]
        elif dtype != descriptor_dtype:
            _fail("adapter_tensor_dtype_invalid")
        expected_shape = EXPECTED_TENSOR_SHAPES[name]
        shape = descriptor["shape"]
        elements = _shape_elements(shape)
        if tuple(shape) != expected_shape:  # type: ignore[arg-type]
            _fail("adapter_tensor_shape_invalid")
        total_elements += elements
        offsets = descriptor["data_offsets"]
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or not _is_exact_int(offsets[0])
            or not _is_exact_int(offsets[1])
            or offsets[0] < 0
            or offsets[1] < 0
            or offsets[0] >= offsets[1]
        ):
            _fail("adapter_tensor_offsets_invalid")
        begin, end = offsets
        expected_bytes = elements * TENSOR_DTYPE_BYTES[descriptor_dtype]
        if end - begin != expected_bytes:
            _fail("adapter_tensor_size_invalid")
        ranges.append((begin, end))

    if dtype is None or total_elements != EXPECTED_TENSOR_ELEMENTS:
        _fail("adapter_tensor_size_invalid")
    expected_payload_bytes = EXPECTED_TENSOR_BYTES[dtype]
    ordered = sorted(ranges)
    if not ordered or ordered[0][0] != 0:
        _fail("adapter_tensor_offsets_invalid")
    previous_end = 0
    for begin, end in ordered:
        if begin != previous_end:
            _fail("adapter_tensor_offsets_invalid")
        previous_end = end
    if previous_end != expected_payload_bytes:
        _fail("adapter_tensor_offsets_invalid")
    if file_size != 8 + header_length + previous_end:
        _fail("adapter_tensor_offsets_invalid")
    return SafetensorsSummary(
        contract_version=ADAPTER_TENSOR_CONTRACT_VERSION,
        layer_count=QWEN3_NUM_HIDDEN_LAYERS,
        target_module_count=len(TARGET_MODULES),
        tensor_count=EXPECTED_TENSOR_COUNT,
        dtype=dtype,
        total_tensor_elements=total_elements,
        total_tensor_bytes=previous_end,
    )


def validate_safetensors_header(reader: BinaryIO, file_size: int) -> SafetensorsSummary:
    """Alias for the bounded metadata validator."""

    return validate_safetensors_metadata(reader, file_size)


def validate_safetensors(reader: BinaryIO, file_size: int) -> SafetensorsSummary:
    """Alias for callers that use the artifact format name directly."""

    return validate_safetensors_metadata(reader, file_size)


__all__ = [
    "ADAPTER_ARTIFACT_CONTRACT_VERSION",
    "ADAPTER_INTAKE_CONTRACT_VERSION",
    "ADAPTER_MANIFEST_CONTRACT_VERSION",
    "ADAPTER_SOURCE_CONTRACT_VERSION",
    "ADAPTER_TENSOR_CONTRACT_VERSION",
    "ADAPTER_CONFIG_CONTRACT_VERSION",
    "QWEN3_ARCHITECTURE_CONTRACT_VERSION",
    "LLAMAFACTORY_VERSION",
    "LLAMAFACTORY_PEFT_MIN_VERSION",
    "LLAMAFACTORY_PEFT_MAX_VERSION",
    "LLAMAFACTORY_TRANSFORMERS_MIN_VERSION",
    "LLAMAFACTORY_TRANSFORMERS_MAX_VERSION",
    "LLAMAFACTORY_TRANSFORMERS_EXCLUDED_VERSION",
    "PEFT_FORMAT_REFERENCE_VERSION",
    "TRANSFORMERS_ARCHITECTURE_REFERENCE_VERSION",
    "SAFETENSORS_FORMAT_REFERENCE_VERSION",
    "BASE_MODEL_ID",
    "BASE_MODEL_REVISION",
    "BASE_MODEL_LICENSE",
    "MODEL_CACHE_PATH",
    "QWEN3_ARCHITECTURE",
    "QWEN3_MODEL_TYPE",
    "QWEN3_HIDDEN_SIZE",
    "QWEN3_INTERMEDIATE_SIZE",
    "QWEN3_NUM_HIDDEN_LAYERS",
    "QWEN3_NUM_ATTENTION_HEADS",
    "QWEN3_NUM_KEY_VALUE_HEADS",
    "QWEN3_HEAD_DIM",
    "QWEN3_ATTENTION_BIAS",
    "QWEN3_TIE_WORD_EMBEDDINGS",
    "QWEN3_LAYER_INDICES",
    "LORA_RANK",
    "LORA_ALPHA",
    "LORA_DROPOUT",
    "PEFT_TYPE",
    "TASK_TYPE",
    "LORA_BIAS",
    "TARGET_MODULES",
    "BASE_LINEAR_SHAPES",
    "ADAPTER_A_SHAPES",
    "ADAPTER_B_SHAPES",
    "EXPECTED_TENSOR_COUNT",
    "EXPECTED_TENSOR_ELEMENTS",
    "EXPECTED_TENSOR_BYTES",
    "MAX_CONFIG_BYTES",
    "MAX_SAFETENSORS_HEADER_BYTES",
    "MAX_ADAPTER_FILE_BYTES",
    "EXTERNAL_CONFIG_KEYS",
    "EXPECTED_TENSOR_NAMES",
    "EXPECTED_TENSOR_SHAPES",
    "AdapterContractError",
    "AdapterConfigSummary",
    "SafetensorsSummary",
    "expected_tensor_names",
    "expected_tensor_shapes",
    "parse_external_adapter_config",
    "validate_adapter_config",
    "canonical_adapter_config_bytes",
    "canonicalize_external_adapter_config",
    "canonicalize_adapter_config",
    "parse_canonical_adapter_config",
    "validate_safetensors_metadata",
    "validate_safetensors_header",
    "validate_safetensors",
]
