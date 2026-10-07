import assert from "node:assert/strict";
import { test } from "node:test";
import { acceptJobEvent, readSSE, SSEParser, watchJob } from "../../../app/presentation/web/assets/stream.js";

function stream(text, chunkSize = 1) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({ start(controller) {
    for (let i = 0; i < bytes.length; i += chunkSize) controller.enqueue(bytes.slice(i, i + chunkSize));
    controller.close();
  } }), { headers: { "Content-Type": "text/event-stream" } });
}

test("SSE handles split UTF-8, CRLF, comments and multiline data", async () => {
  const events = [];
  await readSSE(stream('\ufeff: keep-alive\r\nid: 7\r\nevent: token\r\ndata: café\r\ndata: جرعة\r\n\r\n'), (event) => events.push(event));
  assert.deepEqual(events, [{ event: "token", id: "7", data: "café\nجرعة" }]);
});

test("unfinished SSE event never becomes a completion", async () => {
  const events = [];
  await readSSE(stream('event: stream_completed\ndata: {"state":"COMPLETED"}\n'), (event) => events.push(event));
  assert.deepEqual(events, []);
});

test("bare CR delimiters and unknown fields follow SSE framing", () => {
  const events = [], parser = new SSEParser((event) => events.push(event));
  for (const char of "retry: 1\rdata: first\r\rdata: second\r\r") parser.feed(char);
  parser.finish();
  assert.deepEqual(events.map((event) => event.data), ["first", "second"]);
});

test("a huge or non-SSE response is rejected", async () => {
  const parser = new SSEParser(() => {});
  assert.throws(() => parser.feed("a".repeat(2_097_153)), /too large/);
  await assert.rejects(() => readSSE(new Response("<html>"), () => {}), /event stream/);
});

const snapshot = () => ({ job: { job_id: "job-1", state: "STARTED" }, cursor: 0, events: [], tokens: "" });
const token = (id, delta = "one") => ({ id: String(id), event: "token", data: JSON.stringify({ job_id: "job-1", delta }) });

test("job replay deduplicates tokens and rejects another job", () => {
  const state = snapshot();
  assert.equal(acceptJobEvent(state, token(1)), true);
  assert.equal(acceptJobEvent(state, token(1)), false);
  assert.equal(state.tokens, "one");
  assert.equal(state.cursor, 1);
  assert.throws(() => acceptJobEvent(state, { ...token(2), data: '{"job_id":"other"}' }), /another job/);
  assert.equal(state.cursor, 1);
});

test("bad replay IDs and malformed JSON cannot advance the cursor", () => {
  const state = snapshot();
  for (const id of [null, "-1", "1.5", "2147483648", "abc"]) assert.throws(() => acceptJobEvent(state, { ...token(1), id }));
  assert.throws(() => acceptJobEvent(state, { ...token(1), data: "{" }));
  assert.equal(state.cursor, 0);
});

test("job reconnect sends Last-Event-ID and does not duplicate replayed tokens", async () => {
  const state = snapshot(), cursors = [], paths = [];
  let attempts = 0;
  const api = {
    async request(path, options) {
      paths.push(path); cursors.push(options.headers["Last-Event-ID"]); attempts++;
      return stream(attempts === 1
        ? 'id: 1\nevent: token\ndata: {"job_id":"job-1","delta":"one"}\n\n'
        : 'id: 1\nevent: token\ndata: {"job_id":"job-1","delta":"one"}\n\nid: 2\nevent: token\ndata: {"job_id":"job-1","delta":" two"}\n\n');
    },
    async json(path) { paths.push(path); return { job_id: "job-1", state: attempts > 1 ? "COMPLETED" : "STARTED", result: attempts > 1 ? { ok: true } : null }; },
  };
  await watchJob({ api, snapshot: state, signal: new AbortController().signal, onUpdate() {}, onConnection() {}, wait: async () => {} });
  assert.deepEqual(cursors, ["0", "1"]);
  assert.equal(state.tokens, "one two");
  assert.deepEqual(state.job.result, { ok: true });
  assert.ok(paths.every((path) => !path.includes("/cancel")));
});

test("disconnecting a progress view never sends cancellation", async () => {
  const controller = new AbortController(), paths = [];
  const api = { async request(path) { paths.push(path); controller.abort(); throw controller.signal.reason; } };
  await watchJob({ api, snapshot: snapshot(), signal: controller.signal, onUpdate() {}, onConnection() {} });
  assert.deepEqual(paths, ["/jobs/job-1/events"]);
});

test("revoked access stops reconnecting instead of looping", async () => {
  let count = 0;
  const api = { async request() { count++; throw Object.assign(new Error("Forbidden"), { status: 403 }); } };
  await assert.rejects(watchJob({ api, snapshot: snapshot(), signal: new AbortController().signal, onUpdate() {}, onConnection() {} }), /Forbidden/);
  assert.equal(count, 1);
});
