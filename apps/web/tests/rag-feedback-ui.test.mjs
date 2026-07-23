import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const submitter = await readFile(
  new URL("../app/components/RagFeedbackPanel.tsx", import.meta.url),
  "utf8",
);
const reviewer = await readFile(
  new URL("../app/components/FeedbackReviewQueue.tsx", import.meta.url),
  "utf8",
);

test("offers the three sentiments and reviewed compatible reason codes", () => {
  for (const marker of [
    "Helpful",
    "Not helpful",
    "Report",
    "well_supported",
    "wrong_citation",
    "insufficient_when_expected",
  ]) {
    assert.match(submitter, new RegExp(marker));
  }
  assert.match(submitter, /answerStatus === "answered" && needsSources/);
  assert.match(submitter, /!TARGETING_REASONS\.has\(code\)/);
});

test("submits one canonical immutable PUT and handles replay conflict safely", () => {
  assert.match(submitter, /method: "PUT"/);
  assert.match(submitter, /reason_codes: canonicalReasons/);
  assert.match(submitter, /source_ids: canonicalTargets/);
  assert.match(submitter, /response\.status === 409/);
  assert.match(submitter, /Review status:/);
  assert.match(submitter, /expires_at/);
  assert.doesNotMatch(submitter, /setInterval|retry\(|automaticRetry/);
});

test("contains no free-text feedback or browser persistence boundary", () => {
  const combined = `${submitter}\n${reviewer}`;
  assert.doesNotMatch(combined, /<textarea|type="text"/);
  assert.doesNotMatch(combined, /localStorage|sessionStorage|indexedDB|document\.cookie/);
  assert.doesNotMatch(combined, /dangerouslySetInnerHTML|innerHTML|analytics|trackEvent/);
});

test("review queue supports filters, opaque cursor pagination, and safe states", () => {
  for (const marker of [
    "statusFilter",
    "sentimentFilter",
    "next_cursor",
    "Load older feedback",
    "This review queue is not available",
    "changed before your review was applied",
    "expired or is no longer available",
  ]) {
    assert.match(reviewer, new RegExp(marker));
  }
  assert.doesNotMatch(reviewer, /offset|user_id|question:|answer:|filename:|document_id/);
});

test("review transitions carry expected_version and only reviewed resolution codes", () => {
  assert.match(reviewer, /expected_version: feedback\.version/);
  assert.match(reviewer, /confirmed_quality_issue/);
  assert.match(reviewer, /no_issue_found/);
  assert.match(reviewer, /"triaged", null/);
  assert.match(reviewer, /method: "PATCH"/);
});
