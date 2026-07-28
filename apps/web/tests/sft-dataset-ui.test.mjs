import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../app/components/SftDatasetPanel.tsx", import.meta.url), "utf8");

test("SFT metadata UI uses only department-scoped metadata endpoints", () => {
  assert.match(source, /encodeURIComponent\(departmentId\)/);
  assert.match(source, /\$\{root\}\/sources\?limit=25&offset=0/);
  assert.match(source, /\$\{root\}\/builds\?limit=25&offset=0/);
  assert.match(source, /credentials: "same-origin"/);
});

test("SFT metadata UI has no browser persistence or content fields", () => {
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB|serviceWorker/);
  assert.doesNotMatch(source, /instruction:|target_response|source_chunk_ids|artifact_manifest|sha256/);
  assert.match(source, /source_reference_count/);
  assert.match(source, /review_status/);
});
