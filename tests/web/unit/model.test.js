import assert from "node:assert/strict";
import { test } from "node:test";
import { API, identifier, queryString } from "../../../app/presentation/web/assets/api.js";
import { estimatedCost, lineDiff, reviewPermissions } from "../../../app/presentation/web/assets/model.js";

const ready = { workflow_state: "AWAITING_APPROVAL", job_state: "STARTED", approval_status: "PENDING", approval_allowed: true };
const reviewer = { permissions: ["view_pending_approvals", "approve_clinical_note", "edit_clinical_note", "reject_clinical_note"] };

test("analysts have no approval controls even when a draft is approvable", () => {
  assert.deepEqual(reviewPermissions({ role: "admin", permissions: ["run_workflow"] }, ready), { view: false, approve: false, edit: false, reject: false });
});

test("reviewer permissions and the server's safety decision are both required", () => {
  assert.equal(reviewPermissions(reviewer, ready).approve, true);
  for (const changed of [{ approval_allowed: false }, { cancellation_requested: true }, { decision: {} }, { job_state: "FAILED" }, { workflow_state: "FINALIZE" }]) {
    assert.equal(reviewPermissions(reviewer, { ...ready, ...changed }).approve, false);
    assert.equal(reviewPermissions(reviewer, { ...ready, ...changed }).edit, false);
  }
  assert.equal(reviewPermissions(reviewer, { ...ready, approval_allowed: false }).reject, true);
});

test("line diff preserves additions, removals, CRLF and final-newline changes", () => {
  const before = "a\r\nold\nend", after = "a\r\nnew\nend\n";
  const diff = lineDiff(before, after);
  assert.equal(diff.filter((line) => line.kind !== "add").map((line) => line.text).join(""), before);
  assert.equal(diff.filter((line) => line.kind !== "remove").map((line) => line.text).join(""), after);
  assert.deepEqual(lineDiff("same", "same"), []);
  assert.equal(lineDiff("", "new")[0].kind, "add");
});

test("costs retain estimated/unavailable distinctions, including zero estimates", () => {
  assert.equal(estimatedCost({ estimated_cost: null }), "Estimated cost unavailable");
  assert.match(estimatedCost({ estimated_cost: 0, currency: "USD" }), /Estimated cost: 0.000000 USD/);
});

test("IDs are validated and query values are encoded", () => {
  assert.throws(() => identifier("../approval"), /UUID/);
  assert.equal(queryString({ correlation_id: "a&user_id=other", run_id: "" }), "correlation_id=a%26user_id%3Dother");
});

test("fetch carries bearer headers, never URL credentials, and mutation is sent once", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => { calls.push({ url, options }); return new Response('{"detail":"conflict","code":"INVALID_STATE_TRANSITION"}', { status: 409 }); });
  const api = new API("/api/v1", "synthetic-token");
  await assert.rejects(() => api.json("/runs/run/resume", { method: "POST" }), (error) => error.status === 409);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.headers.get("Authorization"), "Bearer synthetic-token");
  assert.equal(calls[0].options.credentials, "omit");
  assert.ok(!calls[0].url.includes("synthetic-token"));
});
