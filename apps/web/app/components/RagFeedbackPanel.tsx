"use client";

import { FormEvent, useMemo, useState } from "react";

type Sentiment = "helpful" | "unhelpful" | "report";
type AnswerStatus = "answered" | "insufficient_information";

type Feedback = {
  id: string;
  run_id: string;
  answer_status: AnswerStatus;
  sentiment: Sentiment;
  reason_codes: string[];
  source_ids: string[];
  status: "open" | "triaged" | "resolved" | "dismissed";
  resolution_code: string | null;
  created_at: string;
  reviewed_at: string | null;
  expires_at: string;
  version: number;
};

const HELPFUL_REASONS = [
  ["clear", "Clear"],
  ["complete", "Complete"],
  ["well_supported", "Well supported"],
  ["useful_citations", "Useful citations"],
] as const;

const NEGATIVE_REASONS = [
  ["incorrect", "Incorrect"],
  ["unsupported_claim", "Unsupported claim"],
  ["missing_information", "Missing information"],
  ["wrong_citation", "Wrong citation"],
  ["irrelevant_source", "Irrelevant source"],
  ["unsafe_content", "Unsafe content"],
  ["formatting_problem", "Formatting problem"],
  ["insufficient_when_expected", "Expected an answer"],
  ["other_unspecified", "Other structured issue"],
] as const;

const TARGETING_REASONS = new Set(["wrong_citation", "irrelevant_source"]);

export function RagFeedbackPanel({
  departmentId,
  runId,
  answerStatus,
  sourceIds,
}: {
  departmentId: string;
  runId: string;
  answerStatus: AnswerStatus;
  sourceIds: string[];
}) {
  const [sentiment, setSentiment] = useState<Sentiment | null>(null);
  const [reasons, setReasons] = useState<string[]>([]);
  const [targets, setTargets] = useState<string[]>([]);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [state, setState] = useState<"idle" | "submitting" | "conflict" | "error">("idle");

  const availableReasons = useMemo(() => {
    if (sentiment === "helpful") return HELPFUL_REASONS;
    if (sentiment === null) return [];
    return NEGATIVE_REASONS.filter(([code]) =>
      answerStatus === "answered"
        ? code !== "insufficient_when_expected"
        : !TARGETING_REASONS.has(code),
    );
  }, [answerStatus, sentiment]);
  const needsSources = reasons.some((reason) => TARGETING_REASONS.has(reason));

  function chooseSentiment(value: Sentiment) {
    if (state === "submitting" || feedback) return;
    setSentiment(value);
    setReasons([]);
    setTargets([]);
    setState("idle");
  }

  function toggleReason(code: string) {
    setReasons((current) => {
      const next = current.includes(code)
        ? current.filter((item) => item !== code)
        : [...current, code];
      if (!next.some((item) => TARGETING_REASONS.has(item))) setTargets([]);
      return next;
    });
  }

  function toggleTarget(sourceId: string) {
    setTargets((current) =>
      current.includes(sourceId)
        ? current.filter((item) => item !== sourceId)
        : [...current, sourceId],
    );
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!sentiment || !canSubmit(sentiment, reasons, targets, needsSources) || feedback) return;
    setState("submitting");
    const reasonOrder = [...HELPFUL_REASONS, ...NEGATIVE_REASONS].map(([code]) => code);
    const canonicalReasons = reasonOrder.filter((code) => reasons.includes(code));
    const canonicalTargets = sourceIds.filter((sourceId) => targets.includes(sourceId));
    try {
      const response = await fetch(
        `/api/departments/${encodeURIComponent(departmentId)}/rag/answers/${encodeURIComponent(runId)}/feedback`,
        {
          method: "PUT",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            sentiment,
            reason_codes: canonicalReasons,
            source_ids: canonicalTargets,
          }),
        },
      );
      if (response.status === 409) {
        setState("conflict");
        return;
      }
      if (!response.ok) throw new Error("feedback request failed");
      setFeedback((await response.json()) as Feedback);
      setState("idle");
    } catch {
      setState("error");
    }
  }

  return (
    <section className="feedbackPanel" aria-labelledby={`feedback-title-${runId}`}>
      <h2 id={`feedback-title-${runId}`}>Was this result useful?</h2>
      <p>Choose structured feedback only. Nothing is sent until you submit.</p>
      <div className="choiceRow" role="group" aria-label="Feedback sentiment">
        {([
          ["helpful", "Helpful"],
          ["unhelpful", "Not helpful"],
          ["report", "Report"],
        ] as const).map(([value, label]) => (
          <button
            aria-pressed={sentiment === value}
            disabled={Boolean(feedback)}
            key={value}
            onClick={() => chooseSentiment(value)}
            type="button"
          >
            {label}
          </button>
        ))}
      </div>

      {sentiment && !feedback && (
        <form onSubmit={submit}>
          <fieldset>
            <legend>{sentiment === "helpful" ? "What worked?" : "What needs review?"}</legend>
            <div className="checkboxGrid">
              {availableReasons.map(([code, label]) => (
                <label key={code}>
                  <input
                    checked={reasons.includes(code)}
                    onChange={() => toggleReason(code)}
                    type="checkbox"
                  />
                  {label}
                </label>
              ))}
            </div>
          </fieldset>

          {answerStatus === "answered" && needsSources && (
            <fieldset>
              <legend>Which cited sources should be reviewed?</legend>
              <div className="choiceRow">
                {sourceIds.map((sourceId) => (
                  <label className="sourceChoice" key={sourceId}>
                    <input
                      checked={targets.includes(sourceId)}
                      onChange={() => toggleTarget(sourceId)}
                      type="checkbox"
                    />
                    {sourceId}
                  </label>
                ))}
              </div>
            </fieldset>
          )}

          <button
            className="feedbackSubmit"
            disabled={!canSubmit(sentiment, reasons, targets, needsSources) || state === "submitting"}
            type="submit"
          >
            {state === "submitting" ? "Submitting…" : "Submit feedback"}
          </button>
        </form>
      )}

      {state === "conflict" && (
        <p className="ragError" role="alert">
          Feedback already exists for this result and cannot be changed.
        </p>
      )}
      {state === "error" && (
        <p className="ragError" role="alert">
          Feedback could not be submitted. Nothing was retried automatically.
        </p>
      )}
      {feedback && (
        <p className="feedbackSuccess" role="status">
          Feedback received. Review status: {feedback.status}. Retained until{" "}
          {formatDate(feedback.expires_at)}.
        </p>
      )}
    </section>
  );
}

function canSubmit(
  sentiment: Sentiment,
  reasons: string[],
  targets: string[],
  needsSources: boolean,
): boolean {
  if (sentiment !== "helpful" && reasons.length === 0) return false;
  if (needsSources && targets.length === 0) return false;
  return true;
}

function formatDate(raw: string): string {
  const value = new Date(raw);
  return Number.isNaN(value.valueOf()) ? "the configured expiry date" : value.toLocaleDateString();
}
