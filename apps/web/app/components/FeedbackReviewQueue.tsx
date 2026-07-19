"use client";

import { useCallback, useEffect, useState } from "react";

type FeedbackStatus = "open" | "triaged" | "resolved" | "dismissed";
type ReviewStatus = Exclude<FeedbackStatus, "open">;

type Feedback = {
  id: string;
  run_id: string;
  answer_status: "answered" | "insufficient_information";
  sentiment: "helpful" | "unhelpful" | "report";
  reason_codes: string[];
  source_ids: string[];
  status: FeedbackStatus;
  resolution_code: string | null;
  created_at: string;
  reviewed_at: string | null;
  expires_at: string;
  version: number;
};

type QueueResponse = { items: Feedback[]; next_cursor: string | null };
type QueueState = "loading" | "ready" | "forbidden" | "error";

const RESOLUTIONS = {
  resolved: [
    ["confirmed_quality_issue", "Confirmed quality issue"],
    ["confirmed_safety_issue", "Confirmed safety issue"],
    ["addressed_externally", "Addressed externally"],
    ["no_action_required", "No action required"],
  ],
  dismissed: [
    ["duplicate", "Duplicate"],
    ["not_reproducible", "Not reproducible"],
    ["out_of_scope", "Out of scope"],
    ["no_issue_found", "No issue found"],
  ],
  triaged: [],
} as const;

export function FeedbackReviewQueue({ departmentId }: { departmentId: string }) {
  const [statusFilter, setStatusFilter] = useState("");
  const [sentimentFilter, setSentimentFilter] = useState("");
  const [items, setItems] = useState<Feedback[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [queueState, setQueueState] = useState<QueueState>("loading");
  const [actionMessage, setActionMessage] = useState("");

  const load = useCallback(
    async (cursor: string | null) => {
      setQueueState("loading");
      setActionMessage("");
      const query = new URLSearchParams({ limit: "25" });
      if (statusFilter) query.set("status", statusFilter);
      if (sentimentFilter) query.set("sentiment", sentimentFilter);
      if (cursor) query.set("cursor", cursor);
      try {
        const response = await fetch(
          `/api/departments/${encodeURIComponent(departmentId)}/rag/feedback?${query.toString()}`,
          { credentials: "same-origin" },
        );
        if (response.status === 403) {
          setQueueState("forbidden");
          setItems([]);
          return;
        }
        if (!response.ok) throw new Error("queue request failed");
        const value = (await response.json()) as QueueResponse;
        setItems((current) => (cursor ? [...current, ...value.items] : value.items));
        setNextCursor(value.next_cursor);
        setQueueState("ready");
      } catch {
        setQueueState("error");
      }
    },
    [departmentId, sentimentFilter, statusFilter],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  async function review(feedback: Feedback, status: ReviewStatus, resolutionCode: string | null) {
    setActionMessage("");
    try {
      const response = await fetch(
        `/api/departments/${encodeURIComponent(departmentId)}/rag/feedback/${encodeURIComponent(feedback.id)}`,
        {
          method: "PATCH",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            status,
            resolution_code: resolutionCode,
            expected_version: feedback.version,
          }),
        },
      );
      if (response.status === 409) {
        setActionMessage("This item changed before your review was applied. Refresh the queue.");
        return;
      }
      if (response.status === 404) {
        setActionMessage("This feedback expired or is no longer available.");
        return;
      }
      if (response.status === 403) {
        setActionMessage("This review action is not available.");
        return;
      }
      if (!response.ok) throw new Error("review request failed");
      const updated = (await response.json()) as Feedback;
      setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setActionMessage("Review transition applied.");
    } catch {
      setActionMessage("The review action could not be applied. Nothing was retried automatically.");
    }
  }

  return (
    <section className="reviewQueue" aria-labelledby="feedback-review-title">
      <p className="kicker">Phase 8 structured review</p>
      <h1 id="feedback-review-title">Feedback review queue</h1>
      <p className="lede">
        This department-only queue contains structured metadata, not questions, answers, source
        text, filenames, or user identities.
      </p>

      <div className="reviewFilters" aria-label="Queue filters">
        <label>
          Status
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="">All active statuses</option>
            <option value="open">Open</option>
            <option value="triaged">Triaged</option>
            <option value="resolved">Resolved</option>
            <option value="dismissed">Dismissed</option>
          </select>
        </label>
        <label>
          Sentiment
          <select
            value={sentimentFilter}
            onChange={(event) => setSentimentFilter(event.target.value)}
          >
            <option value="">All sentiments</option>
            <option value="helpful">Helpful</option>
            <option value="unhelpful">Not helpful</option>
            <option value="report">Report</option>
          </select>
        </label>
      </div>

      {queueState === "loading" && <p role="status">Loading feedback…</p>}
      {queueState === "forbidden" && (
        <p className="ragError" role="alert">This review queue is not available.</p>
      )}
      {queueState === "error" && (
        <p className="ragError" role="alert">The review queue could not be loaded.</p>
      )}
      {actionMessage && <p className="queueMessage" role="status">{actionMessage}</p>}

      {queueState === "ready" && items.length === 0 && <p>No active feedback matches these filters.</p>}
      <ol className="feedbackQueueList">
        {items.map((feedback) => (
          <li key={feedback.id}>
            <div className="feedbackMeta">
              <strong>{label(feedback.sentiment)}</strong>
              <span>Status: {label(feedback.status)}</span>
              <span>Answer result: {label(feedback.answer_status)}</span>
              <span>Submitted: {formatDate(feedback.created_at)}</span>
              <span>Expires: {formatDate(feedback.expires_at)}</span>
            </div>
            <p>Reasons: {feedback.reason_codes.map(label).join(", ") || "None selected"}</p>
            {feedback.source_ids.length > 0 && (
              <p>Source labels: {feedback.source_ids.join(", ")}</p>
            )}
            {feedback.status === "open" && (
              <button type="button" onClick={() => void review(feedback, "triaged", null)}>
                Mark triaged
              </button>
            )}
            {(feedback.status === "open" || feedback.status === "triaged") && (
              <ResolutionActions feedback={feedback} onReview={review} />
            )}
          </li>
        ))}
      </ol>

      {queueState === "ready" && nextCursor && (
        <button className="loadMore" type="button" onClick={() => void load(nextCursor)}>
          Load older feedback
        </button>
      )}
    </section>
  );
}

function ResolutionActions({
  feedback,
  onReview,
}: {
  feedback: Feedback;
  onReview: (feedback: Feedback, status: ReviewStatus, resolutionCode: string | null) => Promise<void>;
}) {
  const [status, setStatus] = useState<"resolved" | "dismissed">("resolved");
  const [resolution, setResolution] = useState<string>(RESOLUTIONS.resolved[0][0]);

  function changeStatus(value: "resolved" | "dismissed") {
    setStatus(value);
    setResolution(RESOLUTIONS[value][0][0]);
  }

  return (
    <div className="resolutionActions">
      <label>
        Final status
        <select value={status} onChange={(event) => changeStatus(event.target.value as typeof status)}>
          <option value="resolved">Resolved</option>
          <option value="dismissed">Dismissed</option>
        </select>
      </label>
      <label>
        Resolution
        <select value={resolution} onChange={(event) => setResolution(event.target.value)}>
          {RESOLUTIONS[status].map(([value, text]) => (
            <option key={value} value={value}>{text}</option>
          ))}
        </select>
      </label>
      <button type="button" onClick={() => void onReview(feedback, status, resolution)}>
        Apply final status
      </button>
    </div>
  );
}

function label(value: string): string {
  return value.replaceAll("_", " ");
}

function formatDate(raw: string): string {
  const value = new Date(raw);
  return Number.isNaN(value.valueOf()) ? "Unavailable" : value.toLocaleString();
}
