"""Closed, contentful-on-disk but metadata-only-in-PostgreSQL Phase 10 contracts."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

SOURCE_ARTIFACT_CONTRACT_VERSION = "phase10-sft-source-v1"
EXAMPLE_CONTRACT_VERSION = "phase10-sft-example-v1"
NORMALIZATION_VERSION = "phase10-sft-normalization-v1"
SPLIT_VERSION = "phase10-sft-group-split-v1"
DATASET_ARTIFACT_CONTRACT_VERSION = "phase10-sft-dataset-v1"
MAX_SOURCE_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_EXAMPLE_BYTES = 40_000
MAX_INSTRUCTION_BYTES = 12_000
MAX_RESPONSE_BYTES = 32_000
MAX_SOURCE_IDS = 8
VALIDATION_RATIO = Decimal("0.10")


class SftContractError(RuntimeError):
    """Fixed, content-free validation failure for SFT source or dataset contracts."""

    def __init__(self, code: str = "source_contract_invalid") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SftExample:
    example_id: UUID
    group_id: UUID
    instruction: str
    response: str
    source_chunk_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ParsedSftSource:
    department_id: UUID
    source_bundle_id: UUID
    import_attempt_id: UUID
    stage_id: UUID
    examples: tuple[SftExample, ...]
    manifest: dict[str, object]
    examples_sha256: str
    examples_byte_size: int

    @property
    def source_reference_count(self) -> int:
        return sum(len(example.source_chunk_ids) for example in self.examples)

    @property
    def group_count(self) -> int:
        return len({example.group_id for example in self.examples})


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def parse_source_bundle(manifest_raw: bytes, examples_raw: bytes) -> ParsedSftSource:
    """Validate a complete source bundle without exposing its instructions or responses."""

    if not 1 <= len(examples_raw) <= MAX_SOURCE_BUNDLE_BYTES or not examples_raw.endswith(b"\n"):
        raise SftContractError()
    manifest = _json_object(manifest_raw)
    expected = {
        "artifact_contract_version",
        "department_id",
        "source_bundle_id",
        "import_attempt_id",
        "stage_id",
        "normalization_version",
        "example_contract_version",
        "example_count",
        "group_count",
        "source_reference_count",
        "files",
    }
    if set(manifest) != expected:
        raise SftContractError()
    if (
        manifest.get("artifact_contract_version") != SOURCE_ARTIFACT_CONTRACT_VERSION
        or manifest.get("normalization_version") != NORMALIZATION_VERSION
        or manifest.get("example_contract_version") != EXAMPLE_CONTRACT_VERSION
    ):
        raise SftContractError()
    department_id = _uuid(manifest.get("department_id"))
    source_bundle_id = _uuid(manifest.get("source_bundle_id"))
    import_attempt_id = _uuid(manifest.get("import_attempt_id"))
    stage_id = _uuid(manifest.get("stage_id"))
    declared_examples = _strict_int(manifest.get("example_count"), minimum=2, maximum=100_000)
    declared_groups = _strict_int(manifest.get("group_count"), minimum=2, maximum=100_000)
    declared_references = _strict_int(
        manifest.get("source_reference_count"), minimum=declared_examples, maximum=800_000
    )
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"examples.jsonl"}:
        raise SftContractError()
    descriptor = files["examples.jsonl"]
    if not isinstance(descriptor, dict) or set(descriptor) != {"sha256", "byte_size"}:
        raise SftContractError()
    digest = hashlib.sha256(examples_raw).hexdigest()
    if descriptor.get("sha256") != digest or _strict_int(
        descriptor.get("byte_size"), minimum=1, maximum=MAX_SOURCE_BUNDLE_BYTES
    ) != len(examples_raw):
        raise SftContractError()
    examples = tuple(_parse_lines(examples_raw))
    if (
        len(examples) != declared_examples
        or len({item.group_id for item in examples}) != declared_groups
    ):
        raise SftContractError()
    if sum(len(item.source_chunk_ids) for item in examples) != declared_references:
        raise SftContractError()
    return ParsedSftSource(
        department_id,
        source_bundle_id,
        import_attempt_id,
        stage_id,
        examples,
        manifest,
        digest,
        len(examples_raw),
    )


def _parse_lines(raw: bytes):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SftContractError() from error
    example_ids: set[UUID] = set()
    canonical_pairs: set[tuple[str, str]] = set()
    responses_by_instruction: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            raise SftContractError()
        value = _json_object(line.encode("utf-8"))
        if set(value) != {"example_id", "group_id", "instruction", "response", "source_chunk_ids"}:
            raise SftContractError()
        example_id = _uuid(value.get("example_id"))
        group_id = _uuid(value.get("group_id"))
        if example_id in example_ids:
            raise SftContractError()
        example_ids.add(example_id)
        instruction = _canonical_text(value.get("instruction"), maximum=MAX_INSTRUCTION_BYTES)
        response = _canonical_text(value.get("response"), maximum=MAX_RESPONSE_BYTES)
        if len((instruction + response).encode("utf-8")) > MAX_EXAMPLE_BYTES:
            raise SftContractError()
        raw_source_ids = value.get("source_chunk_ids")
        if not isinstance(raw_source_ids, list) or not 1 <= len(raw_source_ids) <= MAX_SOURCE_IDS:
            raise SftContractError()
        source_ids = tuple(_uuid(item) for item in raw_source_ids)
        if len(set(source_ids)) != len(source_ids):
            raise SftContractError()
        pair = (instruction, response)
        if pair in canonical_pairs or (
            instruction in responses_by_instruction
            and responses_by_instruction[instruction] != response
        ):
            raise SftContractError()
        canonical_pairs.add(pair)
        responses_by_instruction[instruction] = response
        yield SftExample(example_id, group_id, instruction, response, source_ids)


def _json_object(raw: bytes) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SftContractError() from error
    if not isinstance(value, dict):
        raise SftContractError()
    return value


def _uuid(value: object) -> UUID:
    try:
        parsed = UUID(value) if isinstance(value, str) else value
    except ValueError as error:
        raise SftContractError() from error
    if not isinstance(parsed, UUID) or parsed.int == 0:
        raise SftContractError()
    return parsed


def _strict_int(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SftContractError()
    return value


def _canonical_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise SftContractError()
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    ).strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > maximum
        or any(_unsafe(character) for character in normalized)
    ):
        raise SftContractError()
    return normalized


def _unsafe(character: str) -> bool:
    value = ord(character)
    category = unicodedata.category(character)
    return (
        value == 0
        or category in {"Cf", "Cs"}
        or (category == "Cc" and character not in {"\t", "\n"})
        or value == 0x034F
        or 0xFDD0 <= value <= 0xFDEF
        or value & 0xFFFF in {0xFFFE, 0xFFFF}
    )


def split_examples(
    source: ParsedSftSource, *, build_id: UUID
) -> tuple[tuple[SftExample, ...], tuple[SftExample, ...]]:
    """Deterministically keep each group entirely in exactly one output split."""

    groups: dict[UUID, list[SftExample]] = {}
    for example in source.examples:
        groups.setdefault(example.group_id, []).append(example)
    ordered = sorted(
        groups,
        key=lambda group_id: hashlib.sha256(
            canonical_json_bytes(
                {
                    "build_id": str(build_id),
                    "group_id": str(group_id),
                    "split_version": SPLIT_VERSION,
                }
            )
        ).digest(),
    )
    target = max(1, round(len(source.examples) * float(VALIDATION_RATIO)))
    validation_groups: set[UUID] = set()
    validation_count = 0
    for group_id in ordered:
        if len(validation_groups) >= len(ordered) - 1:
            break
        validation_groups.add(group_id)
        validation_count += len(groups[group_id])
        if validation_count >= target:
            break
    if not validation_groups or len(validation_groups) == len(groups):
        raise SftContractError("dataset_publication_failed")
    train = tuple(
        sorted(
            (item for item in source.examples if item.group_id not in validation_groups),
            key=lambda item: item.example_id.bytes,
        )
    )
    validation = tuple(
        sorted(
            (item for item in source.examples if item.group_id in validation_groups),
            key=lambda item: item.example_id.bytes,
        )
    )
    if not train or not validation:
        raise SftContractError("dataset_publication_failed")
    return train, validation


def dataset_record(example: SftExample) -> dict[str, object]:
    return {
        "example_id": str(example.example_id),
        "messages": [
            {"role": "user", "content": example.instruction},
            {"role": "assistant", "content": example.response},
        ],
    }


def provenance_record(example: SftExample, *, split: str) -> dict[str, object]:
    return {
        "example_id": str(example.example_id),
        "group_id": str(example.group_id),
        "split": split,
        "source_chunk_ids": [str(value) for value in example.source_chunk_ids],
    }
