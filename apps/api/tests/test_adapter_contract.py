"""Pure unit tests for the Phase 12.1A model-free adapter contract."""

from __future__ import annotations

import io
import json

import pytest

from app import adapter_contract as contract

pytestmark = pytest.mark.unit


def _external_config() -> dict[str, object]:
    return {
        "base_model_name_or_path": contract.BASE_MODEL_ID,
        "revision": None,
        "peft_type": "LORA",
        "task_type": "CAUSAL_LM",
        "inference_mode": True,
        "auto_mapping": None,
        "peft_version": "0.18.1",
        "r": 16,
        "target_modules": list(contract.TARGET_MODULES),
        "exclude_modules": None,
        "lora_alpha": 32,
        "lora_dropout": 0.05,
        "fan_in_fan_out": False,
        "bias": "none",
        "use_rslora": False,
        "modules_to_save": None,
        "init_lora_weights": True,
        "layers_to_transform": None,
        "layers_pattern": None,
        "rank_pattern": {},
        "alpha_pattern": {},
        "megatron_config": None,
        "megatron_core": "megatron.core",
        "trainable_token_indices": None,
        "loftq_config": {},
        "eva_config": None,
        "corda_config": None,
        "use_dora": False,
        "alora_invocation_tokens": None,
        "use_qalora": False,
        "qalora_group_size": 16,
        "layer_replication": None,
        "lora_bias": False,
        "target_parameters": None,
        "arrow_config": None,
        "ensure_weight_tying": False,
    }


class _RecordingReader:
    def __init__(self, data: bytes, *, chunk_size: int | None = None) -> None:
        self._data = data
        self._offset = 0
        self.chunk_size = chunk_size
        self.reads: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        if size < 0:
            size = len(self._data) - self._offset
        if self.chunk_size is not None:
            size = min(size, self.chunk_size)
        result = self._data[self._offset : self._offset + size]
        self._offset += len(result)
        return result


def _header_entries(dtype: str = "F16") -> dict[str, object]:
    entries: dict[str, object] = {}
    offset = 0
    for name in contract.EXPECTED_TENSOR_NAMES:
        shape = list(contract.EXPECTED_TENSOR_SHAPES[name])
        size = shape[0] * shape[1] * contract.TENSOR_DTYPE_BYTES[dtype]
        entries[name] = {"dtype": dtype, "shape": shape, "data_offsets": [offset, offset + size]}
        offset += size
    entries["__metadata__"] = {"format": "pt"}
    return entries


def _header_bytes(entries: dict[str, object], *, padding: bool = True) -> bytes:
    raw = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if padding:
        raw += b" " * ((8 - (len(raw) % 8)) % 8)
    return raw


def _reader_for(
    entries: dict[str, object], *, dtype: str = "F16", file_size: int | None = None
) -> tuple[_RecordingReader, int, bytes]:
    header = _header_bytes(entries)
    payload_size = contract.EXPECTED_TENSOR_BYTES[dtype]
    size = file_size if file_size is not None else 8 + len(header) + payload_size
    reader = _RecordingReader(len(header).to_bytes(8, "little") + header)
    return reader, size, header


def _assert_code(fn, code: str) -> None:
    with pytest.raises(contract.AdapterContractError) as exc_info:
        fn()
    assert str(exc_info.value) == code
    assert set(str(exc_info.value)) <= set("abcdefghijklmnopqrstuvwxyz_")


def test_contract_versions_and_phase11_values_are_fixed() -> None:
    assert contract.ADAPTER_ARTIFACT_CONTRACT_VERSION == "phase12-adapter-artifact-v1"
    assert contract.ADAPTER_CONFIG_CONTRACT_VERSION == "phase12-adapter-config-v1"
    assert contract.ADAPTER_TENSOR_CONTRACT_VERSION == "phase12-adapter-tensors-v1"
    assert contract.LLAMAFACTORY_VERSION == "0.9.5"
    assert contract.BASE_MODEL_ID == "Qwen/Qwen3-0.6B"
    assert contract.BASE_MODEL_REVISION == "c1899de289a04d12100db370d81485cdf75e47ca"
    assert contract.TARGET_MODULES == (
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "gate_proj",
        "up_proj",
        "down_proj",
    )


def test_phase11_shared_contract_values_remain_identical() -> None:
    from app.training_job_domain import (
        BASE_MODEL_ID,
        BASE_MODEL_LICENSE,
        BASE_MODEL_REVISION,
        LLAMAFACTORY_VERSION,
    )

    assert (contract.BASE_MODEL_ID, contract.BASE_MODEL_REVISION, contract.BASE_MODEL_LICENSE) == (
        BASE_MODEL_ID,
        BASE_MODEL_REVISION,
        BASE_MODEL_LICENSE,
    )
    assert contract.LLAMAFACTORY_VERSION == LLAMAFACTORY_VERSION


def test_architecture_and_exact_shapes_are_content_free() -> None:
    assert contract.QWEN3_ARCHITECTURE == "Qwen3ForCausalLM"
    assert contract.QWEN3_MODEL_TYPE == "qwen3"
    assert contract.QWEN3_HIDDEN_SIZE == 1024
    assert contract.QWEN3_INTERMEDIATE_SIZE == 3072
    assert contract.QWEN3_NUM_HIDDEN_LAYERS == 28
    assert contract.QWEN3_NUM_ATTENTION_HEADS == 16
    assert contract.QWEN3_NUM_KEY_VALUE_HEADS == 8
    assert contract.QWEN3_HEAD_DIM == 128
    assert contract.QWEN3_ATTENTION_BIAS is False
    assert contract.QWEN3_TIE_WORD_EMBEDDINGS is True
    assert len(contract.EXPECTED_TENSOR_NAMES) == 392
    assert len(contract.EXPECTED_TENSOR_SHAPES) == 392
    assert sum(a * b for a, b in contract.EXPECTED_TENSOR_SHAPES.values()) == 10_092_544
    assert contract.EXPECTED_TENSOR_BYTES == {
        "F16": 20_185_088,
        "BF16": 20_185_088,
        "F32": 40_370_176,
    }


def test_external_config_accepts_reviewed_lora_profile() -> None:
    summary = contract.parse_external_adapter_config(
        json.dumps(_external_config(), sort_keys=True).encode("utf-8")
    )
    assert summary.base_model_name_or_path == contract.BASE_MODEL_ID
    assert summary.target_modules == tuple(sorted(contract.TARGET_MODULES))
    assert summary.r == 16
    assert summary.lora_alpha == 32
    assert summary.lora_dropout == 0.05
    assert summary.inference_mode is True
    assert summary.auto_mapping is None
    assert summary.peft_version == "0.18.1"


def test_peft_0181_saved_artifact_contract() -> None:
    config_raw = json.dumps(_external_config(), sort_keys=True).encode("utf-8")
    summary = contract.parse_external_adapter_config(config_raw)
    canonical = contract.canonicalize_external_adapter_config(config_raw)
    reader, file_size, header = _reader_for(_header_entries())
    tensor_summary = contract.validate_safetensors_metadata(reader, file_size)
    canonical_object = json.loads(canonical)

    assert summary.inference_mode is True
    assert summary.auto_mapping is None
    assert summary.peft_version == "0.18.1"
    assert canonical_object["inference_mode"] is True
    assert canonical_object["auto_mapping"] is None
    assert canonical_object["peft_version"] == "0.18.1"
    assert contract.parse_canonical_adapter_config(canonical) == summary
    assert contract.canonicalize_external_adapter_config(canonical) == canonical
    assert tensor_summary.tensor_count == 392
    assert reader._offset == 8 + len(header)


def test_external_config_accepts_reviewed_qlora_profile_same_artifact_contract() -> None:
    raw = json.dumps(_external_config(), sort_keys=True).encode("utf-8")
    assert contract.parse_external_adapter_config(raw).r == contract.LORA_RANK
    assert contract.canonicalize_external_adapter_config(
        raw
    ) == contract.canonicalize_external_adapter_config(raw)


def test_external_path_is_accepted_but_not_preserved() -> None:
    config = _external_config()
    config["base_model_name_or_path"] = contract.MODEL_CACHE_PATH
    raw = json.dumps(config).encode("utf-8")
    summary = contract.parse_external_adapter_config(raw)
    assert summary.base_model_name_or_path == contract.MODEL_CACHE_PATH
    canonical = contract.canonicalize_external_adapter_config(raw)
    assert contract.BASE_MODEL_ID.encode() in canonical
    assert contract.MODEL_CACHE_PATH.encode() not in canonical


def test_canonical_config_is_sorted_compact_and_round_trips_byte_identically() -> None:
    raw = json.dumps(_external_config(), indent=2).encode("utf-8")
    canonical = contract.canonicalize_external_adapter_config(raw)
    assert canonical.endswith(b"\n")
    assert b"\n" not in canonical[:-1]
    assert (
        contract.parse_canonical_adapter_config(canonical).base_model_id == contract.BASE_MODEL_ID
    )
    assert contract.canonicalize_external_adapter_config(canonical) == canonical


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_model_name_or_path", "other/model"),
        ("revision", "wrong-revision"),
        ("peft_type", "ADALORA"),
        ("task_type", "SEQ_CLS"),
        ("inference_mode", False),
        ("auto_mapping", {"base_model": "not-accepted"}),
        ("peft_version", "0.18.0"),
        ("peft_version", "0.18.1.dev0"),
        ("r", 8),
        ("lora_alpha", 8),
        ("lora_dropout", 0.1),
        ("target_modules", "all-linear"),
        ("target_modules", [*contract.TARGET_MODULES, "lm_head"]),
        ("target_modules", ["q_proj", *contract.TARGET_MODULES]),
        ("target_modules", ["q_proj"]),
        ("modules_to_save", ["lm_head"]),
        ("rank_pattern", {"layers": 8}),
        ("alpha_pattern", {"layers": 8}),
        ("target_parameters", ["foo"]),
        ("use_dora", True),
        ("use_rslora", True),
        ("loftq_config", {"bits": 4}),
        ("eva_config", {}),
        ("corda_config", {}),
        ("alora_invocation_tokens", [1]),
        ("use_qalora", True),
        ("layer_replication", [[0, 2]]),
        ("trainable_token_indices", [1]),
        ("megatron_config", {}),
        ("ensure_weight_tying", True),
    ],
)
def test_external_config_rejects_unsupported_value(field: str, value: object) -> None:
    config = _external_config()
    config[field] = value
    _assert_code(
        lambda: contract.parse_external_adapter_config(json.dumps(config).encode()),
        "adapter_config_unsupported",
    )


def test_external_config_rejects_missing_and_unknown_keys() -> None:
    missing = _external_config()
    missing.pop("arrow_config")
    _assert_code(
        lambda: contract.parse_external_adapter_config(json.dumps(missing).encode()),
        "adapter_config_invalid",
    )
    for field in ("operator_note", "runtime_config"):
        unknown = _external_config()
        unknown[field] = "not accepted"
        _assert_code(
            lambda unknown=unknown: contract.parse_external_adapter_config(
                json.dumps(unknown).encode()
            ),
            "adapter_config_invalid",
        )
    for field in ("auto_mapping", "peft_version"):
        missing_field = _external_config()
        missing_field.pop(field)
        _assert_code(
            lambda missing_field=missing_field: contract.parse_external_adapter_config(
                json.dumps(missing_field).encode()
            ),
            "adapter_config_invalid",
        )
    null_version = _external_config()
    null_version["peft_version"] = None
    _assert_code(
        lambda: contract.parse_external_adapter_config(json.dumps(null_version).encode()),
        "adapter_config_unsupported",
    )


def test_external_config_rejects_duplicate_keys_nan_inf_bom_invalid_utf8_and_oversize() -> None:
    duplicate = b'{"r":16,"r":16}'
    _assert_code(
        lambda: contract.parse_external_adapter_config(duplicate), "adapter_config_invalid"
    )
    for raw in (b"NaN", b"Infinity", b"-Infinity"):
        _assert_code(
            lambda raw=raw: contract.parse_external_adapter_config(raw), "adapter_config_invalid"
        )
    _assert_code(
        lambda: contract.parse_external_adapter_config(b"\xef\xbb\xbf{}"), "adapter_config_invalid"
    )
    _assert_code(lambda: contract.parse_external_adapter_config(b"{\xff"), "adapter_config_invalid")
    _assert_code(
        lambda: contract.parse_external_adapter_config(b" " * (contract.MAX_CONFIG_BYTES + 1)),
        "adapter_config_invalid",
    )


def test_boolean_is_not_accepted_as_integer() -> None:
    config = _external_config()
    config["r"] = True
    _assert_code(
        lambda: contract.parse_external_adapter_config(json.dumps(config).encode()),
        "adapter_config_unsupported",
    )


@pytest.mark.parametrize("dtype", ["F16", "BF16", "F32"])
def test_valid_safetensors_metadata_for_all_reviewed_dtypes_and_no_payload_read(dtype: str) -> None:
    reader, file_size, header = _reader_for(_header_entries(dtype), dtype=dtype)
    summary = contract.validate_safetensors_metadata(reader, file_size)
    assert summary.dtype == dtype
    assert summary.tensor_count == 392
    assert summary.total_tensor_elements == 10_092_544
    assert summary.total_tensor_bytes == contract.EXPECTED_TENSOR_BYTES[dtype]
    assert sum(reader.reads) <= 8 + len(header)
    assert reader.reads == [8, len(header)]


def test_reader_with_short_reads_still_never_reads_payload() -> None:
    entries = _header_entries()
    _, file_size, header = _reader_for(entries)
    data = len(header).to_bytes(8, "little") + header
    reader = _RecordingReader(data, chunk_size=3)
    contract.validate_safetensors_metadata(reader, file_size)
    assert reader._offset <= 8 + len(header)


def test_header_reader_never_reads_declared_payload_even_when_simulated_file_is_sparse() -> None:
    entries = _header_entries("F32")
    reader, file_size, header = _reader_for(entries, dtype="F32")
    contract.validate_safetensors_metadata(reader, file_size)
    assert reader._offset == 8 + len(header)


_MISSING = object()


@pytest.mark.parametrize(
    "metadata_value",
    [
        _MISSING,
        {},
        {"format": "torch"},
        {"format": "PT"},
        {"format": 1},
        {"format": None},
        {"format": "pt", "operator_note": "not accepted"},
        [],
        "pt",
    ],
)
def test_header_requires_exact_peft_safetensors_metadata(metadata_value: object) -> None:
    entries = _header_entries()
    if metadata_value is _MISSING:
        entries.pop("__metadata__")
    else:
        entries["__metadata__"] = metadata_value
    reader, file_size, _ = _reader_for(entries)
    _assert_code(
        lambda: contract.validate_safetensors_metadata(reader, file_size),
        "adapter_tensor_set_invalid",
    )


def test_header_rejects_duplicate_metadata_key() -> None:
    entries = _header_entries()
    entries.pop("__metadata__")
    raw = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    duplicate = raw[:-1] + b',"__metadata__":{"format":"pt"},"__metadata__":{"format":"pt"}}'
    duplicate += b" " * ((8 - (len(duplicate) % 8)) % 8)
    reader = _RecordingReader(len(duplicate).to_bytes(8, "little") + duplicate)
    _assert_code(
        lambda: contract.validate_safetensors_metadata(
            reader, 8 + len(duplicate) + contract.EXPECTED_TENSOR_BYTES["F16"]
        ),
        "adapter_header_invalid",
    )


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda e: e.pop(next(iter(e))), "adapter_tensor_set_invalid"),
        (lambda e: e.update({"unexpected": next(iter(e.values()))}), "adapter_tensor_set_invalid"),
        (lambda e: e.__setitem__(next(iter(e)), {"dtype": "F16"}), "adapter_tensor_set_invalid"),
        (
            lambda e: e.__setitem__(
                next(iter(e)), {"dtype": "F64", "shape": [1, 1], "data_offsets": [0, 2]}
            ),
            "adapter_tensor_dtype_invalid",
        ),
        (
            lambda e: e.__setitem__(
                next(iter(e)), {"dtype": "F16", "shape": [1, 1], "data_offsets": [0, 2]}
            ),
            "adapter_tensor_shape_invalid",
        ),
        (
            lambda e: e.__setitem__(
                next(iter(e)), {"dtype": "F16", "shape": [16, 1024], "data_offsets": [-1, 32767]}
            ),
            "adapter_tensor_offsets_invalid",
        ),
    ],
)
def test_header_rejects_tensor_set_shape_dtype_and_offsets(mutator, code: str) -> None:
    entries = _header_entries()
    mutator(entries)
    reader, file_size, _ = _reader_for(entries)
    _assert_code(lambda: contract.validate_safetensors_metadata(reader, file_size), code)


def test_header_rejects_duplicate_key_metadata_and_non_object() -> None:
    entries = _header_entries()
    first = next(iter(entries))
    header = (
        "{"
        + json.dumps(first)
        + ":{"
        + '"dtype":"F16","shape":[16,1024],"data_offsets":[0,32768]},'
        + json.dumps(first)
        + ":{"
        + '"dtype":"F16","shape":[16,1024],"data_offsets":[0,32768]}}'
    ).encode()
    header += b" " * ((8 - (len(header) % 8)) % 8)
    reader = _RecordingReader(len(header).to_bytes(8, "little") + header)
    _assert_code(
        lambda: contract.validate_safetensors_metadata(
            reader, 8 + len(header) + contract.EXPECTED_TENSOR_BYTES["F16"]
        ),
        "adapter_header_invalid",
    )
    for value in (b"[]", b'"text"'):
        padded = value + b" " * ((8 - (len(value) % 8)) % 8)
        reader = _RecordingReader(len(padded).to_bytes(8, "little") + padded)
        _assert_code(
            lambda reader=reader, padded=padded: contract.validate_safetensors_metadata(
                reader, 8 + len(padded) + contract.EXPECTED_TENSOR_BYTES["F16"]
            ),
            "adapter_header_invalid",
        )


def test_header_rejects_prefix_length_and_file_bounds() -> None:
    reader = _RecordingReader(b"\x00" * 8)
    _assert_code(
        lambda: contract.validate_safetensors_metadata(reader, 8), "adapter_header_invalid"
    )
    reader = _RecordingReader((contract.MAX_SAFETENSORS_HEADER_BYTES + 1).to_bytes(8, "little"))
    _assert_code(
        lambda: contract.validate_safetensors_metadata(reader, 8), "adapter_header_too_large"
    )
    reader = _RecordingReader((100).to_bytes(8, "little"))
    _assert_code(
        lambda: contract.validate_safetensors_metadata(reader, 8 + 10), "adapter_header_invalid"
    )
    reader = _RecordingReader(b"\x00" * 8)
    _assert_code(
        lambda: contract.validate_safetensors_metadata(reader, contract.MAX_ADAPTER_FILE_BYTES + 1),
        "adapter_file_too_large",
    )


@pytest.mark.parametrize("mutation", ["overlap", "gap", "reversed", "wrong_size", "trailing"])
def test_header_rejects_offset_integrity_failures(mutation: str) -> None:
    entries = _header_entries()
    names = list(contract.EXPECTED_TENSOR_NAMES)
    if mutation == "overlap":
        entries[names[1]]["data_offsets"] = [1, 65_537]  # type: ignore[index]
    elif mutation == "gap":
        entries[names[1]]["data_offsets"][0] += 1  # type: ignore[index]
        entries[names[1]]["data_offsets"][1] += 1  # type: ignore[index]
    elif mutation == "reversed":
        entries[names[0]]["data_offsets"] = [100, 0]
    elif mutation == "wrong_size":
        entries[names[0]]["data_offsets"][1] -= 1  # type: ignore[index]
    else:
        entries[names[-1]]["data_offsets"][1] += 1  # type: ignore[index]
        entries[names[-1]]["data_offsets"][0] += 1  # type: ignore[index]
    reader, file_size, _ = _reader_for(entries)
    _assert_code(
        lambda: contract.validate_safetensors_metadata(reader, file_size),
        "adapter_tensor_offsets_invalid"
        if mutation in {"overlap", "gap", "reversed", "trailing"}
        else "adapter_tensor_size_invalid",
    )


@pytest.mark.parametrize(
    "bad_key",
    [
        "base_model.model.layers.0.self_attn.q_proj.lora_A.weight",
        "base_model.model.model.layers.00.self_attn.q_proj.lora_A.weight",
        "base_model.model.model.layers.0.mlp.q_proj.lora_A.weight",
        "base_model.model.model.layers.0.self_attn.q_proj.lora_A.default.weight",
        "base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight/extra",
        "base_model.model.model.layers.0.self_attn.q_proj.lora_A.weіght",
        "base_model.model.model.layers.0.self_attn.embed_tokens.lora_A.weight",
        "base_model.model.model.layers.0.self_attn.q_proj.weight",
        "model.embed_tokens.weight",
        "lm_head.weight",
    ],
)
def test_header_rejects_alternate_full_model_or_unsafe_key_grammar(bad_key: str) -> None:
    entries = _header_entries()
    original = next(iter(entries))
    entries[bad_key] = entries.pop(original)
    reader, file_size, _ = _reader_for(entries)
    _assert_code(
        lambda: contract.validate_safetensors_metadata(reader, file_size),
        "adapter_tensor_set_invalid",
    )


def test_header_rejects_dimensions_that_are_zero_negative_or_boolean() -> None:
    for shape in ([0, 1024], [-1, 1024], [True, 1024]):
        entries = _header_entries()
        name = next(iter(entries))
        entries[name]["shape"] = shape  # type: ignore[index]
        reader, file_size, _ = _reader_for(entries)
        _assert_code(
            lambda reader=reader, file_size=file_size: contract.validate_safetensors_metadata(
                reader, file_size
            ),
            "adapter_tensor_shape_invalid",
        )


def test_header_rejects_mixed_dtype() -> None:
    entries = _header_entries()
    first = next(iter(entries))
    entries[first]["dtype"] = "F32"  # type: ignore[index]
    entries[first]["data_offsets"][1] += 32_768  # type: ignore[index]
    reader, file_size, _ = _reader_for(entries)
    _assert_code(
        lambda: contract.validate_safetensors_metadata(reader, file_size),
        "adapter_tensor_dtype_invalid",
    )


def test_header_rejects_wrong_prefix_bom_and_non_space_padding() -> None:
    entries = _header_entries()
    header = _header_bytes(entries)
    bad_header = b"\xef\xbb\xbf" + header[3:]
    reader = _RecordingReader(len(bad_header).to_bytes(8, "little") + bad_header)
    _assert_code(
        lambda: contract.validate_safetensors_metadata(
            reader, 8 + len(bad_header) + contract.EXPECTED_TENSOR_BYTES["F16"]
        ),
        "adapter_header_invalid",
    )
    bad_padding = header.rstrip(b" ") + b"\t"
    reader = _RecordingReader(len(bad_padding).to_bytes(8, "little") + bad_padding)
    _assert_code(
        lambda: contract.validate_safetensors_metadata(
            reader, 8 + len(bad_padding) + contract.EXPECTED_TENSOR_BYTES["F16"]
        ),
        "adapter_header_invalid",
    )


def test_valid_header_with_untrusted_reader_failure_is_fixed_code() -> None:
    reader = io.BytesIO(b"\x08\x00\x00\x00\x00\x00\x00\x00")
    _assert_code(
        lambda: contract.validate_safetensors_metadata(reader, 8), "adapter_header_invalid"
    )
