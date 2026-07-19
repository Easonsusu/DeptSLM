import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../app/components/RagAnswerPanel.tsx", import.meta.url), "utf8");
const nextConfig = await readFile(new URL("../next.config.mjs", import.meta.url), "utf8");

test("renders answered, insufficient, citation, loading, and failure states as React text", () => {
  for (const marker of [
    "Grounded answer",
    "Insufficient information",
    "Citations",
    "Checking sources",
    "temporarily unavailable",
    "original_filename",
  ]) {
    assert.match(source, new RegExp(marker));
  }
  assert.doesNotMatch(source, /dangerouslySetInnerHTML|marked\(|innerHTML/);
});

test("enforces the input bound without persistence, streaming, retry, or document filters", () => {
  assert.match(source, /MAX_QUESTION_CHARS = 2000/);
  assert.match(source, /maxLength=\{MAX_QUESTION_CHARS\}/);
  assert.doesNotMatch(source, /localStorage|sessionStorage|EventSource|WebSocket|setInterval/);
  assert.doesNotMatch(source, /document_id.*fetch|top_k|temperature|query_vector/);
});

test("posts only the normalized question to the selected department route", () => {
  assert.match(source, /encodeURIComponent\(departmentId\)/);
  assert.match(source, /JSON\.stringify\(\{ question: normalized \}\)/);
});

test("keeps the browser request same-origin and proxies only through the configured API", () => {
  assert.match(source, /fetch\(`\/api\/departments\//);
  assert.match(nextConfig, /source: "\/api\/:path\*"/);
  assert.match(nextConfig, /process\.env\.API_URL/);
  assert.doesNotMatch(source, /API_URL|http:\/\/api|localhost:8000/);
});
