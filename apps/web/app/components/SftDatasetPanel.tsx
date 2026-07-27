"use client";

import { useCallback, useEffect, useState } from "react";

type Source = {
  id: string;
  status: string;
  artifact_contract_version: string;
  normalization_version: string;
  example_contract_version: string;
  example_count: number;
  group_count: number;
  source_reference_count: number;
  created_at: string;
};

type Build = {
  id: string;
  source_bundle_id: string;
  status: string;
  review_status: string;
  artifact_contract_version: string;
  example_contract_version: string;
  normalization_version: string;
  split_version: string;
  validation_ratio: string;
  source_example_count: number;
  source_group_count: number;
  source_reference_count: number;
  train_example_count: number | null;
  validation_example_count: number | null;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  version: number;
};

type List<T> = { items: T[] };

export function SftDatasetPanel({ departmentId }: { departmentId: string }) {
  const [sources, setSources] = useState<Source[]>([]);
  const [builds, setBuilds] = useState<Build[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "forbidden" | "error">("loading");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    setState("loading");
    try {
      const root = `/api/departments/${encodeURIComponent(departmentId)}/sft`;
      const [sourceResponse, buildResponse] = await Promise.all([
        fetch(`${root}/sources?limit=25&offset=0`, { credentials: "same-origin" }),
        fetch(`${root}/builds?limit=25&offset=0`, { credentials: "same-origin" }),
      ]);
      if (sourceResponse.status === 403 || buildResponse.status === 403) {
        setState("forbidden");
        return;
      }
      if (!sourceResponse.ok || !buildResponse.ok) throw new Error("metadata request failed");
      setSources((await sourceResponse.json() as List<Source>).items);
      setBuilds((await buildResponse.json() as List<Build>).items);
      setState("ready");
    } catch {
      setState("error");
    }
  }, [departmentId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function enqueue(source: Source) {
    setMessage("");
    const response = await fetch(
      `/api/departments/${encodeURIComponent(departmentId)}/sft/sources/${encodeURIComponent(source.id)}/builds`,
      { method: "POST", credentials: "same-origin" },
    );
    setMessage(response.ok ? "Dataset build queued." : "The build could not be queued.");
    if (response.ok) void load();
  }

  async function mutate(build: Build, action: "cancel" | "approve" | "reject" | "archive") {
    setMessage("");
    const root = `/api/departments/${encodeURIComponent(departmentId)}/sft/builds/${encodeURIComponent(build.id)}`;
    const response = await fetch(action === "cancel" ? `${root}/cancel` : `${root}/review`, {
      method: action === "cancel" ? "POST" : "PATCH",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(action === "cancel" ? { expected_version: build.version } : { action, expected_version: build.version }),
    });
    setMessage(response.ok ? "Dataset metadata updated." : "The update could not be applied. Refresh metadata.");
    if (response.ok) void load();
  }

  return (
    <section className="reviewQueue" aria-labelledby="sft-dataset-title">
      <p className="kicker">Phase 10 metadata only</p>
      <h1 id="sft-dataset-title">SFT dataset builds</h1>
      <p className="lede">
        This view shows department-scoped source and dataset metadata only. It never displays or stores instructions, responses, source references, hashes, paths, or artifact content.
      </p>
      {state === "loading" && <p role="status">Loading dataset metadata…</p>}
      {state === "forbidden" && <p className="ragError" role="alert">This dataset view is not available.</p>}
      {state === "error" && <p className="ragError" role="alert">Dataset metadata could not be loaded.</p>}
      {message && <p className="queueMessage" role="status">{message}</p>}
      {state === "ready" && (
        <>
          <h2>Source bundles</h2>
          <ol className="feedbackQueueList">
            {sources.map((source) => (
              <li key={source.id}>
                <div className="feedbackMeta">
                  <strong>{source.id}</strong>
                  <span>Status: {source.status}</span>
                  <span>Examples: {source.example_count}; groups: {source.group_count}</span>
                  <span>Contracts: {source.artifact_contract_version}, {source.example_contract_version}</span>
                </div>
                {source.status === "active" && <button type="button" onClick={() => void enqueue(source)}>Queue dataset build</button>}
              </li>
            ))}
          </ol>
          <h2>Dataset builds</h2>
          <ol className="feedbackQueueList">
            {builds.map((build) => (
              <li key={build.id}>
                <div className="feedbackMeta">
                  <strong>{build.id}</strong>
                  <span>Build: {build.status}; review: {build.review_status}</span>
                  <span>Output: {build.train_example_count ?? "—"} train, {build.validation_example_count ?? "—"} validation</span>
                  <span>Split: {build.split_version} at {build.validation_ratio}</span>
                </div>
                {(build.status === "queued" || build.status === "running") && <button type="button" onClick={() => void mutate(build, "cancel")}>Cancel</button>}
                {build.status === "succeeded" && build.review_status === "pending" && <><button type="button" onClick={() => void mutate(build, "approve")}>Approve</button><button type="button" onClick={() => void mutate(build, "reject")}>Reject</button></>}
                {build.status === "succeeded" && (build.review_status === "approved" || build.review_status === "rejected") && <button type="button" onClick={() => void mutate(build, "archive")}>Archive</button>}
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}
