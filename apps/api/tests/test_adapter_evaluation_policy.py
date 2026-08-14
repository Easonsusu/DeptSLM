from uuid import uuid4

from deptslm_worker.adapter_evaluation_pipeline import _case_contract

from app.adapter_evaluation_policy import execute_paired_rag_case
from app.rag_answer_services import (
    EphemeralRagOutcome,
    PreparedRagPolicyContext,
    SafeRetrievalTrace,
)


def test_paired_case_prepares_retrieval_once_and_reuses_seed(monkeypatch):
    calls = {"prepare": 0, "lane": []}
    context = PreparedRagPolicyContext(
        question="q",
        selected=(),
        loaded=(),
        trace=SafeRetrievalTrace(0, 0, (), ()),
        prompt_identity="prompt-hash",
    )
    outcome = EphemeralRagOutcome("insufficient_information", "not enough", (), 0, 0, (), ())

    def prepare(*args, **kwargs):
        calls["prepare"] += 1
        return context

    def lane(ctx, data_dir, scope, runtime, settings, *, seed, **kwargs):
        assert ctx is context
        calls["lane"].append((runtime, seed))
        return outcome

    monkeypatch.setattr("app.adapter_evaluation_policy.prepare_rag_policy_context", prepare)
    monkeypatch.setattr("app.adapter_evaluation_policy.execute_rag_policy_lane", lane)
    result = execute_paired_rag_case(
        None,
        None,
        None,
        None,
        "q",
        department_id=uuid4(),
        adapter_id=uuid4(),
        adapter_version=1,
        suite_id=uuid4(),
        case_id=uuid4(),
        baseline_runtime="baseline",
        candidate_runtime="candidate",
        qdrant=None,
    )

    assert calls["prepare"] == 1
    assert len(calls["lane"]) == 2
    assert calls["lane"][0][1] == calls["lane"][1][1] == result.case_seed
    assert result.context.prompt_identity == "prompt-hash"


def test_adapter_evaluation_reads_canonical_phase9_suite_case_shape():
    case_id = uuid4()
    chunk_id = uuid4()
    expected, relevant, accepted = _case_contract(
        {
            "case_id": str(case_id),
            "expected_status": "answered",
            "question": "What is approved?",
            "relevant_sources": [{"chunk_id": str(chunk_id)}],
            "accepted_answers": ["Approved."],
        }
    )

    assert expected == "answered"
    assert relevant == (chunk_id,)
    assert accepted == ("Approved.",)
