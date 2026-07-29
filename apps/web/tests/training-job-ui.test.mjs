import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../app/components/TrainingJobPanel.tsx", import.meta.url), "utf8");

test("Phase 11 UI uses only department-scoped metadata endpoints", () => {
  assert.match(source, /encodeURIComponent\(departmentId\)/);
  assert.match(source, /\/training\/jobs\?limit=25&offset=0/);
  assert.match(source, /\/training\/jobs`/);
  assert.match(source, /credentials: "same-origin"/);
});

test("Phase 11 UI keeps bundles and dataset content out of browser state", () => {
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB|serviceWorker/);
  assert.doesNotMatch(source, /training_yaml:|dataset_info:|artifact_manifest|sha256:|source_chunk_ids/);
  assert.match(source, /dataset_rights_confirmed: true/);
  assert.match(source, /evaluation_contamination_reviewed: true/);
});
