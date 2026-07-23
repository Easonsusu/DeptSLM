"""Unit coverage for Phase 9 contracts, metrics, artifacts, and isolation."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from deptslm_worker import evaluation_pipeline
from deptslm_worker.evaluation_queue import ClaimedEvaluationRun
from deptslm_worker.evaluation_settings import (
    EvaluationConfigurationError,
    EvaluationSettings,
)

from app.authorization import DepartmentScope
from app.evaluation_artifacts import (
    RUN_FILES,
    SUITE_FILES,
    ArtifactDigest,
    EvaluationArtifactStore,
    canonical_json_bytes,
    validate_suite_source_directory,
)
from app.evaluation_domain import (
    ANSWER_NORMALIZATION_VERSION,
    ARTIFACT_CONTRACT_VERSION,
    GATE_POLICY_VERSION,
    METRIC_CONTRACT_VERSION,
    RUNNER_CONTRACT_VERSION,
    SUITE_CONTRACT_VERSION,
    AggregateMetrics,
    EvaluationCaseScore,
    EvaluationContractError,
    aggregate_metrics,
    character_f1,
    decode_evaluation_cursor,
    derive_base_seed,
    derive_case_seed,
    encode_evaluation_cursor,
    evaluate_gates,
    normalize_answer,
    parse_quality_gates,
    score_case,
)
from app.evaluation_suites import _compare_canonical_snapshots, _parse_cases
from app.models import EvaluationCaseResult, EvaluationRun, EvaluationSuite
from app.rag_answer_services import EphemeralRagOutcome
from app.rag_domain import EvidenceSource, RagContractError
from app.rag_runtime_client import RagRuntimeClient

pytestmark = pytest.mark.unit


def _gates(value: str = "0.50") -> dict[str, str]:
    return {
        "retrieval_recall_at_5_min": value,
        "retrieval_mrr_at_20_min": value,
        "answer_status_accuracy_min": value,
        "citation_precision_min": value,
        "citation_recall_min": value,
        "normalized_exact_match_min": value,
        "character_f1_min": value,
        "invalid_contract_rate_max": "0.00",
    }


def _score(**changes) -> EvaluationCaseScore:
    value = {
        "case_id": uuid4(),
        "expected_status": "answered",
        "actual_status": "answered",
        "relevant_chunk_count": 1,
        "retrieved_relevant_at_5": 1,
        "retrieved_relevant_at_10": 1,
        "retrieved_relevant_at_20": 1,
        "reciprocal_rank_at_20": Decimal(1),
        "status_correct": True,
        "cited_count": 1,
        "cited_relevant_count": 1,
        "citation_precision": Decimal(1),
        "citation_recall": Decimal(1),
        "normalized_exact_match": Decimal(1),
        "character_f1": Decimal(1),
        "answer_contract_valid": True,
        "case_gate_passed": True,
        "error_code": None,
    }
    value.update(changes)
    return EvaluationCaseScore(**value)


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "eval_results").mkdir(parents=True)
    (root / "extracted_text").mkdir()
    return root


def test_fixed_evaluation_contract_identifiers() -> None:
    assert SUITE_CONTRACT_VERSION == "phase9-evaluation-suite-v1"
    assert ARTIFACT_CONTRACT_VERSION == "phase9-evaluation-artifact-v1"
    assert METRIC_CONTRACT_VERSION == "phase9-deterministic-metrics-v1"
    assert ANSWER_NORMALIZATION_VERSION == "phase9-answer-normalization-v1"
    assert GATE_POLICY_VERSION == "phase9-quality-gates-v1"
    assert RUNNER_CONTRACT_VERSION == "phase9-evaluation-runner-v1"


def test_quality_gates_are_exact_decimals() -> None:
    gates = parse_quality_gates(_gates("0.7500"))
    assert gates.character_f1_min == Decimal("0.7500")
    assert isinstance(gates.character_f1_min, Decimal)


@pytest.mark.parametrize(
    "value",
    ["", "1.00000", ".5", "01", "-0.1", "1e-1", "NaN", " 0.5", "０.５"],
)
def test_quality_gates_reject_noncanonical_decimal_strings(value: str) -> None:
    raw = _gates()
    raw["character_f1_min"] = value
    with pytest.raises(EvaluationContractError):
        parse_quality_gates(raw)


def test_quality_gates_reject_unknown_missing_and_float_values() -> None:
    unknown = _gates()
    unknown["other"] = "0.1"
    missing = _gates()
    missing.pop("citation_recall_min")
    floating = _gates()
    floating["citation_recall_min"] = 0.5  # type: ignore[assignment]
    for value in (unknown, missing, floating):
        with pytest.raises(EvaluationContractError):
            parse_quality_gates(value)


def test_answer_normalization_retains_punctuation_and_collapses_unicode_space() -> None:
    assert normalize_answer("  Café,\u2003WORLD!  ") == "café, world!"
    assert normalize_answer("answer.") != normalize_answer("answer")


def test_character_f1_uses_non_whitespace_codepoint_multisets() -> None:
    assert character_f1("A A", "aa") == Decimal(1)
    assert character_f1("ab", "ac") == Decimal("0.5")
    assert character_f1("", "") == Decimal(1)


def test_retrieval_answer_and_citation_metrics_use_exact_chunk_ids() -> None:
    relevant = (uuid4(), uuid4())
    foreign = uuid4()
    score = score_case(
        case_id=uuid4(),
        expected_status="answered",
        relevant_chunk_ids=relevant,
        accepted_answers=("The policy applies.",),
        actual_status="answered",
        generated_answer="the  POLICY\u2003applies.",
        authorized_candidate_ids=(foreign, relevant[1], relevant[0]),
        cited_chunk_ids=(relevant[1],),
        answer_contract_valid=True,
    )
    assert score.retrieved_relevant_at_5 == 2
    assert score.reciprocal_rank_at_20 == Decimal("0.5")
    assert score.citation_precision == Decimal(1)
    assert score.citation_recall == Decimal("0.5")
    assert score.normalized_exact_match == Decimal(1)


def test_duplicate_candidates_fail_the_case_contract() -> None:
    relevant = uuid4()
    score = score_case(
        case_id=uuid4(),
        expected_status="answered",
        relevant_chunk_ids=(relevant,),
        accepted_answers=("answer",),
        actual_status="answered",
        generated_answer="answer",
        authorized_candidate_ids=(relevant, relevant),
        cited_chunk_ids=(relevant,),
        answer_contract_valid=True,
    )
    assert not score.answer_contract_valid
    assert score.error_code == "retrieval_authority_failed"


def test_insufficient_case_requires_zero_citations_for_a_valid_contract() -> None:
    score = score_case(
        case_id=uuid4(),
        expected_status="insufficient_information",
        relevant_chunk_ids=(),
        accepted_answers=(),
        actual_status="insufficient_information",
        generated_answer="I do not have enough information.",
        authorized_candidate_ids=(),
        cited_chunk_ids=(),
        answer_contract_valid=True,
    )
    assert score.status_correct
    assert score.citation_precision == Decimal(1)
    assert score.citation_recall == Decimal(1)


def test_macro_aggregation_excludes_insufficient_from_answer_and_retrieval_metrics() -> None:
    metrics = aggregate_metrics(
        (
            _score(),
            _score(
                expected_status="insufficient_information",
                actual_status="insufficient_information",
                relevant_chunk_count=0,
                retrieved_relevant_at_5=0,
                retrieved_relevant_at_10=0,
                retrieved_relevant_at_20=0,
                reciprocal_rank_at_20=Decimal(0),
                cited_count=0,
                cited_relevant_count=0,
                normalized_exact_match=Decimal(0),
                character_f1=Decimal(0),
            ),
        )
    )
    assert metrics.retrieval_recall_at_5 == Decimal(1)
    assert metrics.normalized_exact_match == Decimal(1)
    assert metrics.answer_status_accuracy == Decimal(1)


def test_quality_gate_failure_is_not_metric_computation_failure() -> None:
    metrics = AggregateMetrics(
        retrieval_recall_at_5=Decimal("0.4"),
        retrieval_recall_at_10=Decimal("0.5"),
        retrieval_recall_at_20=Decimal("0.6"),
        retrieval_mrr_at_20=Decimal("0.4"),
        answer_status_accuracy=Decimal("0.8"),
        citation_precision=Decimal("0.8"),
        citation_recall=Decimal("0.8"),
        normalized_exact_match=Decimal("0.8"),
        character_f1=Decimal("0.8"),
        invalid_contract_rate=Decimal(0),
    )
    gate = evaluate_gates(metrics, parse_quality_gates(_gates("0.5")))
    assert not gate.passed
    assert gate.failed_count == 2


def test_seed_derivation_is_deterministic_bounded_and_case_specific() -> None:
    run_id = uuid4()
    case_a = uuid4()
    case_b = uuid4()
    base = derive_base_seed(run_id)
    assert base == derive_base_seed(run_id)
    assert 0 <= base <= (1 << 63) - 1
    assert derive_case_seed(base, case_a) == derive_case_seed(base, case_a)
    assert derive_case_seed(base, case_a) != derive_case_seed(base, case_b)


def test_evaluation_cursor_is_opaque_and_bound_to_department_and_resource() -> None:
    department_id = uuid4()
    resource_id = uuid4()
    created_at = datetime.now(UTC)
    cursor = encode_evaluation_cursor(
        department_id=department_id,
        resource="suite",
        created_at=created_at,
        resource_id=resource_id,
    )
    assert str(department_id) not in cursor
    decoded = decode_evaluation_cursor(
        cursor,
        department_id=department_id,
        resource="suite",
    )
    assert decoded.created_at == created_at
    assert decoded.resource_id == resource_id
    for wrong_department, wrong_resource in (
        (uuid4(), "suite"),
        (department_id, "run"),
    ):
        with pytest.raises(EvaluationContractError):
            decode_evaluation_cursor(
                cursor,
                department_id=wrong_department,
                resource=wrong_resource,
            )


def test_evaluation_models_have_no_content_or_source_identifier_columns() -> None:
    columns = {
        column.name
        for table in (
            EvaluationSuite.__table__,
            EvaluationRun.__table__,
            EvaluationCaseResult.__table__,
        )
        for column in table.columns
    }
    prohibited = {
        "question",
        "accepted_answer",
        "generated_answer",
        "answer",
        "prompt",
        "evidence",
        "text",
        "vector",
        "model_output",
        "filename",
        "path",
        "chunk_id",
        "document_id",
        "extraction_id",
        "indexing_id",
    }
    assert columns.isdisjoint(prohibited)


def test_external_suite_publication_is_exact_private_and_immutable(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    store = EvaluationArtifactStore(root)
    scope = DepartmentScope(uuid4())
    suite_id = uuid4()
    staged = store.stage_suite(
        scope,
        suite_id,
        uuid4(),
        {"suite_id": suite_id, "department_id": scope.value},
        (canonical_json_bytes({"case_id": str(uuid4())}) + b"\n",),
    )
    store.publish(staged, SUITE_FILES)
    assert {item.name for item in staged.final_path.iterdir()} == SUITE_FILES
    assert stat_mode(staged.final_path) == 0o700
    assert all(stat_mode(item) == 0o600 for item in staged.final_path.iterdir())
    duplicate = store.stage_suite(
        scope,
        suite_id,
        uuid4(),
        {"suite_id": suite_id},
        (b"{}\n",),
    )
    with pytest.raises(EvaluationContractError):
        store.publish(duplicate, SUITE_FILES)
    store.cleanup_stage(scope, suite_id, UUID(duplicate.path.name), suite=True)


def test_result_artifact_contains_only_numeric_content_free_case_fields(
    tmp_path: Path,
) -> None:
    root = _data_root(tmp_path)
    store = EvaluationArtifactStore(root)
    scope = DepartmentScope(uuid4())
    score = _score()
    staged, _summary = store.stage_run(
        scope,
        uuid4(),
        uuid4(),
        uuid4(),
        manifest_value={"case_count": 1},
        summary_value={"metrics": {"character_f1": Decimal(1)}},
        scores=(score,),
    )
    store.publish(staged, RUN_FILES)
    line = json.loads((staged.final_path / "case_results.jsonl").read_text())
    assert line["case_id"] == str(score.case_id)
    assert not {
        "question",
        "answer",
        "prompt",
        "evidence",
        "chunk_id",
        "document_id",
        "path",
    } & set(line)
    combined = "\n".join(path.read_text() for path in staged.final_path.iterdir() if path.is_file())
    for prohibited in (
        "generated_answer",
        "accepted_answer",
        "question",
        "prompt",
        "evidence",
        "runtime_response",
        "document_id",
        "chunk_id",
    ):
        assert prohibited not in combined


def test_suite_source_rejects_symlink_unknown_entry_and_repository_path(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    source = tmp_path / "source"
    repository.mkdir()
    source.mkdir()
    (source / "suite.json").write_text("{}")
    (source / "cases.jsonl").write_text("{}\n")
    assert validate_suite_source_directory(source, repository) == source.resolve()
    (source / "unknown").write_text("x")
    with pytest.raises(EvaluationContractError):
        validate_suite_source_directory(source, repository)
    (source / "unknown").unlink()
    (source / "suite.json").unlink()
    os.symlink(source / "cases.jsonl", source / "suite.json")
    with pytest.raises(EvaluationContractError):
        validate_suite_source_directory(source, repository)


@pytest.mark.parametrize(
    "case_value",
    [
        {
            "case_id": str(uuid4()),
            "expected_status": "answered",
            "question": "Synthetic question?",
            "relevant_chunk_ids": [],
            "accepted_answers": ["Synthetic answer."],
        },
        {
            "case_id": str(uuid4()),
            "expected_status": "insufficient_information",
            "question": "Synthetic question?",
            "relevant_chunk_ids": [str(uuid4())],
            "accepted_answers": [],
        },
        {
            "case_id": str(uuid4()),
            "expected_status": "answered",
            "question": "Unsafe\u202equestion",
            "relevant_chunk_ids": [str(uuid4())],
            "accepted_answers": ["Synthetic answer."],
        },
    ],
)
def test_suite_case_contract_fails_closed(tmp_path: Path, case_value: dict[str, object]) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "cases.jsonl").write_text(json.dumps(case_value) + "\n")
    with pytest.raises(EvaluationContractError):
        tuple(_parse_cases(source))


def test_suite_case_parser_rejects_duplicate_ids_chunks_and_normalized_answers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    case_id = str(uuid4())
    chunk_id = str(uuid4())
    duplicate_chunks = {
        "case_id": case_id,
        "expected_status": "answered",
        "question": "Synthetic question?",
        "relevant_chunk_ids": [chunk_id, chunk_id],
        "accepted_answers": ["Synthetic answer."],
    }
    (source / "cases.jsonl").write_text(json.dumps(duplicate_chunks) + "\n")
    with pytest.raises(EvaluationContractError):
        tuple(_parse_cases(source))
    duplicate_answers = {
        **duplicate_chunks,
        "relevant_chunk_ids": [chunk_id],
        "accepted_answers": ["Answer", " answer "],
    }
    (source / "cases.jsonl").write_text(json.dumps(duplicate_answers) + "\n")
    with pytest.raises(EvaluationContractError):
        tuple(_parse_cases(source))
    second = {**duplicate_answers, "accepted_answers": ["Different"]}
    (source / "cases.jsonl").write_text(json.dumps(second) + "\n" + json.dumps(second) + "\n")
    with pytest.raises(EvaluationContractError):
        tuple(_parse_cases(source))


def test_ground_truth_snapshot_mismatch_fails_as_suite_source_stale() -> None:
    chunk_id = uuid4()
    cases = (
        {
            "case_id": str(uuid4()),
            "expected_status": "answered",
            "question": "Synthetic question?",
            "relevant_sources": [
                {
                    "chunk_id": str(chunk_id),
                    "content_sha256": "a" * 64,
                }
            ],
            "accepted_answers": ["Synthetic answer."],
        },
    )
    current = {
        chunk_id: {
            "chunk_id": str(chunk_id),
            "content_sha256": "b" * 64,
        }
    }
    with pytest.raises(EvaluationContractError) as caught:
        _compare_canonical_snapshots(cases, current)
    assert caught.value.code == "suite_source_stale"


def test_phase9_uses_production_policy_and_never_imports_feedback_or_qdrant_client() -> None:
    root = Path(__file__).resolve().parents[1]
    public_service = (root / "app" / "rag_answer_services.py").read_text()
    evaluator = (
        root.parent.parent / "services" / "rag-worker" / "deptslm_worker" / "evaluation_pipeline.py"
    ).read_text()
    evaluation_sources = (
        "\n".join(path.read_text() for path in sorted((root / "app").glob("evaluation_*.py")))
        + evaluator
    )
    assert "execute_rag_policy(" in public_service
    assert "execute_rag_policy(" in evaluator
    assert "verify_canonical_suite_authority_without_open_transaction" in evaluator
    for prohibited in (
        "rag_answer_feedback",
        "rag_feedback_services",
        "QdrantClient",
        "qdrant_client",
        "RagAnswerRun(",
    ):
        assert prohibited not in evaluation_sources


def test_runtime_client_seed_is_private_bounded_and_optional() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "answered",
                "answer": "Synthetic [S1].",
                "citations": ["S1"],
            },
        )

    client = RagRuntimeClient(
        "https://runtime.invalid",
        "phase9-runtime-token-0123456789-abcdef",
        5,
        transport=httpx.MockTransport(handler),
    )
    evidence = (EvidenceSource("S1", "Synthetic evidence."),)
    client.generate("Synthetic question?", evidence)
    client.generate("Synthetic question?", evidence, seed=9)
    assert "seed" not in seen[0]
    assert seen[1]["seed"] == 9
    for invalid in (-1, 1 << 63, True):
        with pytest.raises(RagContractError):
            client.generate("Synthetic question?", evidence, seed=invalid)  # type: ignore[arg-type]


def test_evaluator_one_case_fake_runtime_smoke_is_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    department_id = uuid4()
    suite_id = uuid4()
    run_id = uuid4()
    case_id = uuid4()
    chunk_id = uuid4()
    job = ClaimedEvaluationRun(
        id=run_id,
        department_id=department_id,
        suite_id=suite_id,
        requested_by_user_id=uuid4(),
        worker_id=uuid4(),
        claim_token=uuid4(),
        stale_claim_token=None,
        base_seed=9,
        case_count=1,
        code_revision="9" * 40,
    )
    suite = SimpleNamespace(
        id=suite_id,
        suite_contract_version=SUITE_CONTRACT_VERSION,
        artifact_contract_version=ARTIFACT_CONTRACT_VERSION,
        metric_contract_version=METRIC_CONTRACT_VERSION,
        answer_normalization_version=ANSWER_NORMALIZATION_VERSION,
        gate_policy_version=GATE_POLICY_VERSION,
        artifact_manifest_sha256="a" * 64,
        canonical_cases_sha256="b" * 64,
        canonical_cases_byte_size=1,
        **{name: Decimal(0) for name in _gates()},
    )
    cases = (
        {
            "case_id": str(case_id),
            "expected_status": "answered",
            "question": "Synthetic question?",
            "relevant_sources": [{"chunk_id": str(chunk_id)}],
            "accepted_answers": ["Synthetic answer."],
        },
    )
    captured: dict[str, object] = {}

    class Store:
        def iter_suite_cases(self, *_args, **_kwargs):
            return iter(cases)

        def stage_run(self, *_args, manifest_value, summary_value, scores, **_kwargs):
            rendered = json.dumps(
                {"manifest": manifest_value, "summary": summary_value},
                default=str,
            )
            assert "Synthetic question?" not in rendered
            assert "Synthetic answer." not in rendered
            captured["scores"] = tuple(scores)
            return SimpleNamespace(), ArtifactDigest("c" * 64, 1)

    for name in (
        "require_live_claim",
        "renew_lease",
        "record_progress",
        "revalidate_ephemeral_sources",
    ):
        monkeypatch.setattr(evaluation_pipeline, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evaluation_pipeline, "validate_claim_authority", lambda *_args: suite)
    monkeypatch.setattr(
        evaluation_pipeline, "_verify_suite_authority", lambda *_args, **_kwargs: None
    )

    def execute(*_args, seed, **_kwargs):
        captured["seed"] = seed
        return EphemeralRagOutcome(
            "answered",
            "Synthetic answer.",
            (),
            1,
            1,
            (chunk_id,),
            (),
        )

    monkeypatch.setattr(evaluation_pipeline, "execute_rag_policy", execute)
    monkeypatch.setattr(
        evaluation_pipeline,
        "finalize_success",
        lambda *_args, **_kwargs: captured.update(finalized=True),
    )
    settings = SimpleNamespace(
        lease_seconds=300,
        operation_timeout_seconds=30,
        data_dir=Path("/unused"),
        rag=object(),
    )
    assert evaluation_pipeline.process_evaluation_run(
        object(), settings, Store(), object(), object(), job, lambda: False
    )
    assert captured["finalized"] is True
    assert captured["seed"] == derive_case_seed(9, case_id)
    assert len(captured["scores"]) == 1


def test_evaluator_settings_require_exact_worker_and_code_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _data_root(tmp_path)
    values = {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost/test",
        "DEPTSLM_DATA_DIR": str(root),
        "ENVIRONMENT": "test",
        "DEPTSLM_QDRANT_URL": "http://localhost:6333",
        "DEPTSLM_QDRANT_API_KEY": "phase9-qdrant-key",
        "DEPTSLM_RAG_RUNTIME_URL": "http://localhost:8010",
        "DEPTSLM_RAG_RUNTIME_TOKEN": "phase9-runtime-token-0123456789-abcdef",
        "DEPTSLM_EVALUATION_WORKER_ID": str(uuid4()),
        "DEPTSLM_EVALUATION_CODE_REVISION": "a" * 40,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = EvaluationSettings.from_environment()
    assert settings.code_revision == "a" * 40
    monkeypatch.setenv("DEPTSLM_EVALUATION_CODE_REVISION", "A" * 40)
    with pytest.raises(EvaluationConfigurationError):
        EvaluationSettings.from_environment()


def test_evaluator_settings_reject_heartbeat_or_timeout_at_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _data_root(tmp_path)
    values = {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost/test",
        "DEPTSLM_DATA_DIR": str(root),
        "ENVIRONMENT": "test",
        "DEPTSLM_QDRANT_URL": "http://localhost:6333",
        "DEPTSLM_QDRANT_API_KEY": "phase9-qdrant-key",
        "DEPTSLM_RAG_RUNTIME_URL": "http://localhost:8010",
        "DEPTSLM_RAG_RUNTIME_TOKEN": "phase9-runtime-token-0123456789-abcdef",
        "DEPTSLM_EVALUATION_WORKER_ID": str(uuid4()),
        "DEPTSLM_EVALUATION_CODE_REVISION": "b" * 40,
        "DEPTSLM_EVALUATION_LEASE_SECONDS": "30",
        "DEPTSLM_EVALUATION_HEARTBEAT_SECONDS": "30",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(EvaluationConfigurationError):
        EvaluationSettings.from_environment()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
