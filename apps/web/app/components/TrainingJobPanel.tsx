"use client";

import { useCallback, useEffect, useState } from "react";

type DatasetBuild = {
  id: string;
  status: string;
  review_status: string;
  version: number;
  train_example_count: number | null;
  validation_example_count: number | null;
};

type TrainingJob = {
  id: string;
  dataset_build_id: string;
  status: string;
  review_status: string;
  profile_id: string;
  base_model_id: string;
  base_model_revision: string;
  llamafactory_version: string;
  train_example_count: number | null;
  validation_example_count: number | null;
  requested_at: string;
  finished_at: string | null;
  version: number;
};

type List<T> = { items: T[] };

const profiles = ["phase11-qwen3-0.6b-lora-v1", "phase11-qwen3-0.6b-qlora-nf4-v1"];

export function TrainingJobPanel({ departmentId }: { departmentId: string }) {
  const [builds, setBuilds] = useState<DatasetBuild[]>([]);
  const [jobs, setJobs] = useState<TrainingJob[]>([]);
  const [profile, setProfile] = useState(profiles[0]);
  const [state, setState] = useState<"loading" | "ready" | "forbidden" | "error">("loading");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    setState("loading");
    try {
      const department = encodeURIComponent(departmentId);
      const [buildResponse, jobResponse] = await Promise.all([
        fetch(`/api/departments/${department}/sft/builds?limit=25&offset=0`, { credentials: "same-origin" }),
        fetch(`/api/departments/${department}/training/jobs?limit=25&offset=0`, { credentials: "same-origin" }),
      ]);
      if (buildResponse.status === 403 || jobResponse.status === 403) {
        setState("forbidden");
        return;
      }
      if (!buildResponse.ok || !jobResponse.ok) throw new Error("metadata request failed");
      setBuilds((await buildResponse.json() as List<DatasetBuild>).items);
      setJobs((await jobResponse.json() as List<TrainingJob>).items);
      setState("ready");
    } catch {
      setState("error");
    }
  }, [departmentId]);

  useEffect(() => { void load(); }, [load]);

  async function enqueue(build: DatasetBuild) {
    setMessage("");
    const response = await fetch(`/api/departments/${encodeURIComponent(departmentId)}/training/jobs`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dataset_build_id: build.id,
        profile_id: profile,
        expected_dataset_version: build.version,
        dataset_rights_confirmed: true,
        evaluation_contamination_reviewed: true,
      }),
    });
    setMessage(response.ok ? "Training-job bundle queued." : "The training job could not be queued.");
    if (response.ok) void load();
  }

  async function mutate(job: TrainingJob, action: "cancel" | "approve" | "reject" | "archive") {
    const root = `/api/departments/${encodeURIComponent(departmentId)}/training/jobs/${encodeURIComponent(job.id)}`;
    const response = await fetch(action === "cancel" ? `${root}/cancel` : `${root}/review`, {
      method: action === "cancel" ? "POST" : "PATCH",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(action === "cancel" ? { expected_version: job.version } : { action, expected_version: job.version }),
    });
    setMessage(response.ok ? "Training-job metadata updated." : "The update could not be applied. Refresh metadata.");
    if (response.ok) void load();
  }

  return (
    <section className="reviewQueue" aria-labelledby="training-job-title">
      <p className="kicker">Phase 11 metadata only</p>
      <h1 id="training-job-title">LlamaFactory training-job bundles</h1>
      <p className="lede">This view publishes reviewed configuration metadata only. It never exposes datasets, prompts, outputs, model files, adapters, paths, hashes, or training execution.</p>
      {state === "loading" && <p role="status">Loading training-job metadata…</p>}
      {state === "forbidden" && <p className="ragError" role="alert">This training-job view is not available.</p>}
      {state === "error" && <p className="ragError" role="alert">Training-job metadata could not be loaded.</p>}
      {message && <p className="queueMessage" role="status">{message}</p>}
      {state === "ready" && <>
        <label>Profile <select value={profile} onChange={(event) => setProfile(event.target.value)}>{profiles.map((item) => <option key={item}>{item}</option>)}</select></label>
        <h2>Approved datasets</h2>
        <ol className="feedbackQueueList">{builds.filter((build) => build.status === "succeeded" && build.review_status === "approved").map((build) => <li key={build.id}><div className="feedbackMeta"><strong>{build.id}</strong><span>Examples: {build.train_example_count ?? "—"} train, {build.validation_example_count ?? "—"} validation</span></div><button type="button" onClick={() => void enqueue(build)}>Queue job bundle</button></li>)}</ol>
        <h2>Training jobs</h2>
        <ol className="feedbackQueueList">{jobs.map((job) => <li key={job.id}><div className="feedbackMeta"><strong>{job.id}</strong><span>Job: {job.status}; review: {job.review_status}</span><span>{job.profile_id}; LlamaFactory {job.llamafactory_version}</span><span>Output: {job.train_example_count ?? "—"} train, {job.validation_example_count ?? "—"} validation</span></div>{(job.status === "queued" || job.status === "running") && <button type="button" onClick={() => void mutate(job, "cancel")}>Cancel</button>}{job.status === "succeeded" && job.review_status === "pending" && <><button type="button" onClick={() => void mutate(job, "approve")}>Approve</button><button type="button" onClick={() => void mutate(job, "reject")}>Reject</button></>}{job.status === "succeeded" && (job.review_status === "approved" || job.review_status === "rejected") && <button type="button" onClick={() => void mutate(job, "archive")}>Archive</button>}</li>)}</ol>
      </>}
    </section>
  );
}
