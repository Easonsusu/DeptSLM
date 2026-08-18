# Phase 12.1A static adapter contract

## Status and scope

Phase 12.0 (adapter-registry contract and threat model) is complete. Phase 12.1
is complete; this document and `apps/api/app/adapter_contract.py` implement
Phase 12.1A, the model-free static compatibility contract. Phase 12.1B now
uses this contract from its isolated source-validation child, and Phase 12.1C
reuses the same model-free checks in its registry publication child; this
document does not describe storage or import authority. Phase 12.1D adds only a
separate PostgreSQL metadata-read boundary; Phase 12.1E-A separately adds the
administrator-only artifact reconciliation foundation. Phase 12.1E-B is the
completed separate purge authority and does not change this static contract;
Phase 12.1E-C is the completed reviewed metadata-only lifecycle-release boundary.
Phase 12.2, Phase 12.3, and Phase 12.4 are completed. Phase 13 is the final
security, Docker-demo, and documentation hardening scope.

The static-contract implementation itself adds no intake command, database model,
migration, API route, queue, worker, registry, storage directory,
reconciliation, purge, Docker change, dependency, model/tokenizer loading,
adapter loading, or training execution. Phase 12.1B adds a separate
administrator-only source-intake boundary and a separate registry worker that
call this validator without changing its semantics. Static acceptance and
registry publication do not prove that an external environment used the
declared dataset, job bundle, or training configuration.

## Immutable compatibility evidence

The contract was reviewed against these primary, immutable sources:

- [LlamaFactory v0.9.5 tree](https://github.com/hiyouga/LlamaFactory/tree/v0.9.5)
  and its [pinned `pyproject.toml`](https://raw.githubusercontent.com/hiyouga/LlamaFactory/v0.9.5/pyproject.toml).
- [PEFT v0.18.1 tree](https://github.com/huggingface/peft/tree/v0.18.1),
  including the [LoraConfig source](https://raw.githubusercontent.com/huggingface/peft/v0.18.1/src/peft/tuners/lora/config.py),
  [PeftConfig source](https://raw.githubusercontent.com/huggingface/peft/v0.18.1/src/peft/config.py),
  and [saved-state naming source](https://raw.githubusercontent.com/huggingface/peft/v0.18.1/src/peft/utils/save_and_load.py).
- [Transformers v4.55.0 Qwen3 configuration](https://raw.githubusercontent.com/huggingface/transformers/v4.55.0/src/transformers/models/qwen3/configuration_qwen3.py).
- [Pinned Qwen3-0.6B configuration](https://huggingface.co/Qwen/Qwen3-0.6B/blob/c1899de289a04d12100db370d81485cdf75e47ca/config.json).
- [Safetensors repository and format](https://github.com/huggingface/safetensors),
  referenced at format version `0.7.0`.

The reference stack is metadata only. The API does not install or import these
packages, and normal CI downloads no model, tokenizer, adapter, or tensor
payload. The selected compatibility ranges are LlamaFactory `0.9.5` with PEFT
`>=0.18.0,<=0.18.1` and Transformers `>=4.55.0,<=5.6.0` excluding `4.57.0`;
the reviewed references are PEFT `0.18.1`, Transformers `4.55.0`, and
safetensors `0.7.0`.

## Fixed model and adapter architecture

The base contract is `Qwen/Qwen3-0.6B` at revision
`c1899de289a04d12100db370d81485cdf75e47ca`, with `Apache-2.0` license
metadata. Its reviewed architecture is `Qwen3ForCausalLM` / `qwen3`: hidden
size `1024`, intermediate size `3072`, `28` layers, `16` attention heads,
`8` key/value heads, head dimension `128`, `attention_bias=false`, and tied
word embeddings.

The accepted adapter is PEFT `LORA`, task `CAUSAL_LM`, rank `16`, alpha `32`,
dropout `0.05`, bias `none`, `fan_in_fan_out=false`, with RS-LoRA and DoRA
disabled. It has no modules-to-save, target parameters, rank/alpha patterns,
layer replication, trainable tokens, or other advanced initialization. The
exact target modules are:

`q_proj`, `k_proj`, `v_proj`, `o_proj`, `gate_proj`, `up_proj`, `down_proj`.

| Module | Base weight | LoRA A | LoRA B |
| --- | --- | --- | --- |
| `q_proj` | `[2048,1024]` | `[16,1024]` | `[2048,16]` |
| `k_proj` | `[1024,1024]` | `[16,1024]` | `[1024,16]` |
| `v_proj` | `[1024,1024]` | `[16,1024]` | `[1024,16]` |
| `o_proj` | `[1024,2048]` | `[16,2048]` | `[1024,16]` |
| `gate_proj` | `[3072,1024]` | `[16,1024]` | `[3072,16]` |
| `up_proj` | `[3072,1024]` | `[16,1024]` | `[3072,16]` |
| `down_proj` | `[1024,3072]` | `[16,3072]` | `[1024,16]` |

There are exactly `28 × 7 × 2 = 392` tensors and `10,092,544` elements.
The permitted complete tensor payload is `20,185,088` bytes for F16 or BF16,
and `40,370,176` bytes for F32. LoRA and QLoRA Phase 11 profiles use this same
adapter file contract; base-model quantization does not add adapter tensors.

## Tensor-key grammar

PEFT 0.18.1 removes the arbitrary adapter name when it returns the saved state
dict. The closed key grammar therefore accepts only:

```text
base_model.model.model.layers.<0..27>.self_attn.<q_proj|k_proj|v_proj|o_proj>.lora_A.weight
base_model.model.model.layers.<0..27>.self_attn.<q_proj|k_proj|v_proj|o_proj>.lora_B.weight
base_model.model.model.layers.<0..27>.mlp.<gate_proj|up_proj|down_proj>.lora_A.weight
base_model.model.model.layers.<0..27>.mlp.<gate_proj|up_proj|down_proj>.lora_B.weight
```

Layer numbers are canonical ASCII decimal with no leading zero. There is no
adapter-name segment, alternate prefix, suffix, empty segment, Unicode
look-alike, duplicate, unknown module, embedding, `lm_head`, normalization,
bias, full-model, or quantized-base tensor.

## External and canonical configuration

The parser accepts only the complete PEFT 0.18.1 saved configuration field set
after PEFT removes its runtime-only `runtime_config`. This is the completed
artifact written by `PeftModel.save_pretrained`, not the in-memory training
configuration: PEFT temporarily writes `inference_mode=true`,
`auto_mapping=null` for this non-null `CAUSAL_LM` task type, and
`peft_version="0.18.1"`. Every field must have the reviewed value:
`peft_type=LORA`, `task_type=CAUSAL_LM`, `r=16`, `lora_alpha=32`,
`lora_dropout=0.05`, `bias=none`, the exact seven target modules,
`modules_to_save=null`, `use_rslora=false`, `use_dora=false`, empty rank/alpha
patterns, `target_parameters=null`, `revision=null`,
`megatron_core=megatron.core`, empty `loftq_config`, and all other advanced
fields at their reviewed disabled/default values. Unknown keys, including
`runtime_config`, remain rejected. The external base value is either
`Qwen/Qwen3-0.6B` or the exact Phase 11 runtime cache path; no other identifier
or path is accepted.

`canonicalize_external_adapter_config` emits a generated UTF-8 JSON object with
sorted keys, compact separators, one trailing LF, the same closed PEFT field
set, the exact model ID (never the external path), and the deterministic target
module order above. `parse_canonical_adapter_config` requires byte-identical
canonical output. This is verified compatibility metadata, not training
provenance.

The parser enforces a `65,536` byte input limit and rejects empty, oversized,
BOM, invalid UTF-8, duplicate-key, non-object, non-finite, unknown, missing,
wrong-type, arbitrary-path, regex, `all-linear`, and unsupported advanced
configuration values. Failures expose only fixed `AdapterContractError` codes.

## Bounded safetensors metadata validation

The validator receives a binary reader and a separately established positive
file size. It reads exactly the first 8-byte unsigned little-endian header
length and at most the declared UTF-8 JSON header, bounded to `1,048,576`
bytes. It never reads tensor payload bytes and never allocates a complete
adapter. The maximum accepted file size is `44,040,192` bytes.

The header must be an object containing exactly the 392 expected tensor names
plus `__metadata__`. PEFT 0.18.1 writes the exact metadata object
`{"format":"pt"}`; no other metadata, operator text, or arbitrary extension is
accepted. `__metadata__` is not a tensor and does not count toward the 392
tensor total. Missing, duplicate, empty, non-object, or differently shaped
metadata, unknown entries, duplicate keys, malformed descriptors, mixed or
unsupported dtypes, zero/negative/boolean dimensions, wrong shapes, invalid
offsets, overlaps, reversals, duplicate ranges, gaps, overruns, underruns,
trailing bytes, and full-model groups fail closed. Each tensor descriptor
contains only `dtype`, `shape`, and `data_offsets`; all ranges are contiguous
from zero, end at the exact dtype-specific payload total, and make the complete
file size exactly `8 + header_length + payload_bytes`. Only F16, BF16, and F32
are accepted, one uniform dtype per file.

The returned `SafetensorsSummary` contains only contract version, layer/module
counts, tensor count, dtype, element count, and byte count. Tests use an
instrumented reader to prove no read crosses the header boundary.

## Governance and future phases

Phase 12.1C binds an adapter to one same-department Phase 10 dataset and Phase
11 job as **verified governance lineage** after this static validation. That
association cannot prove external training origin, exact dataset use, declared
execution, or unmodified weight production. Evaluation, approval, promotion,
runtime loading, rollback, purge, and later adapter lifecycle remain future
reviewed phases; Phase 12.1E-A only reconciles non-authoritative artifacts. No adapter is trusted or usable because this static contract or the
metadata-only registry accepts it.
