"use client";

import { FormEvent, useState } from "react";

const MAX_QUESTION_CHARS = 2000;

type Citation = {
  source_id: string;
  document_id: string;
  original_filename: string;
  chunk_id: string;
  ordinal: number;
  provenance_kind: "page" | "line";
  page_start: number | null;
  page_end: number | null;
  line_start: number | null;
  line_end: number | null;
};

type Answer = {
  id: string;
  status: "answered" | "insufficient_information";
  answer: string;
  citations: Citation[];
  generation_model: string;
  created_at: string;
};

export function RagAnswerPanel({ departmentId }: { departmentId: string }) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = question.trim();
    if (!normalized || normalized.length > MAX_QUESTION_CHARS || state === "loading") return;
    setState("loading");
    setAnswer(null);
    try {
      const response = await fetch(`/api/departments/${encodeURIComponent(departmentId)}/rag/answers`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: normalized }),
      });
      if (!response.ok) throw new Error("request failed");
      const value = (await response.json()) as Answer;
      if (
        !value ||
        !["answered", "insufficient_information"].includes(value.status) ||
        typeof value.answer !== "string" ||
        !Array.isArray(value.citations)
      ) {
        throw new Error("invalid response");
      }
      setAnswer(value);
      setState("idle");
    } catch {
      setState("error");
    }
  }

  return (
    <section className="ragPanel" aria-labelledby="rag-title">
      <p className="kicker">Phase 7 grounded answers</p>
      <h1 id="rag-title">Ask this department&apos;s approved sources.</h1>
      <p className="lede">
        Answers use current department-authorized evidence. Retrieved text remains untrusted, and
        unsupported questions return a clear insufficient-information response.
      </p>
      <form onSubmit={submit}>
        <label htmlFor="rag-question">Question</label>
        <textarea
          id="rag-question"
          maxLength={MAX_QUESTION_CHARS}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Ask one question about approved department documents."
          rows={5}
          value={question}
        />
        <div className="questionFooter">
          <span>{question.length} / {MAX_QUESTION_CHARS}</span>
          <button disabled={!question.trim() || state === "loading"} type="submit">
            {state === "loading" ? "Checking sources…" : "Ask with evidence"}
          </button>
        </div>
      </form>

      {state === "error" && (
        <p className="ragError" role="alert">
          The grounded-answer service is temporarily unavailable. Review the question and try
          again when ready.
        </p>
      )}

      {answer && (
        <article className="answerCard" aria-live="polite">
          <p className="answerStatus">
            {answer.status === "answered" ? "Grounded answer" : "Insufficient information"}
          </p>
          <p className="answerText">{answer.answer}</p>
          {answer.citations.length > 0 && (
            <ol className="citationList" aria-label="Citations">
              {answer.citations.map((citation) => (
                <li key={citation.source_id}>
                  <strong>{citation.source_id}</strong>
                  <span>{citation.original_filename}</span>
                  <span>{citationRange(citation)}</span>
                </li>
              ))}
            </ol>
          )}
        </article>
      )}
    </section>
  );
}

function citationRange(citation: Citation): string {
  if (citation.provenance_kind === "page") {
    return `Pages ${citation.page_start}–${citation.page_end}`;
  }
  return `Lines ${citation.line_start}–${citation.line_end}`;
}
