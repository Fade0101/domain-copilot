// Fetch-based SSE keeps bearer credentials out of URLs. No EventSource cookies.
export class SSEParser {
  constructor(onEvent) {
    this.onEvent = onEvent;
    this.buffer = "";
    this.data = [];
    this.event = "message";
    this.id = null;
    this.size = 0;
  }

  feed(chunk) {
    this.buffer += chunk;
    while (true) {
      const end = this.buffer.search(/[\r\n]/);
      if (end < 0 || (this.buffer[end] === "\r" && end === this.buffer.length - 1)) break;
      const width = this.buffer.slice(end, end + 2) === "\r\n" ? 2 : 1;
      const line = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + width);
      this.line(line);
    }
    if (this.size + this.buffer.length > 2_097_152) throw new Error("Stream event is too large.");
  }

  line(line) {
    if (!line) {
      if (this.data.length) this.onEvent({ event: this.event, data: this.data.join("\n"), id: this.id });
      this.data = [];
      this.event = "message";
      this.id = null;
      this.size = 0;
      return;
    }
    if (line.startsWith(":")) return;
    this.size += line.length;
    if (this.size > 2_097_152) throw new Error("Stream event is too large.");
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") this.data.push(value);
    if (field === "event") this.event = value || "message";
    if (field === "id" && !value.includes("\0")) this.id = value;
  }

  finish() {
    // A trailing CR is a delimiter. An unfinished event without a blank line
    // is discarded: it must never become a fabricated completion.
    if (this.buffer.endsWith("\r")) this.feed("\n");
  }
}

export async function readSSE(response, onEvent) {
  if (!response.headers.get("content-type")?.includes("text/event-stream") || !response.body) {
    throw new Error("The server did not return an event stream.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser(onEvent);
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode());
    parser.finish();
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    signal?.throwIfAborted();
    const abort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, ms);
    signal?.addEventListener("abort", abort, { once: true });
  });
}

export const terminal = (state) => ["COMPLETED", "FAILED", "CANCELLED"].includes(state);

export function acceptJobEvent(snapshot, event) {
  if (!event.id || !/^\d+$/.test(event.id)) throw new Error("Job event has no valid replay ID.");
  const id = Number(event.id);
  if (!Number.isSafeInteger(id) || id > 2_147_483_647) throw new Error("Invalid job replay ID.");
  if (id <= snapshot.cursor) return false;
  const data = JSON.parse(event.data);
  if (data.job_id !== snapshot.job.job_id) throw new Error("Received an event for another job.");
  if (event.event === "job_progress" || event.event === "stream_completed") {
    snapshot.job = { ...snapshot.job, ...data };
    snapshot.events.push({ id, ...data });
    if (snapshot.events.length > 500) snapshot.events.shift();
  } else if (event.event === "token") {
    if (typeof data.delta !== "string") throw new Error("Invalid job token event.");
    snapshot.tokens = (snapshot.tokens + data.delta).slice(-64_000);
  }
  snapshot.cursor = id;
  return true;
}

export async function watchJob({ api, snapshot, signal, onUpdate, onConnection, wait = sleep }) {
  let retries = 0;
  while (!signal.aborted) {
    try {
      onConnection(retries ? "Reconnecting and replaying progress…" : "Live progress");
      const response = await api.request(`/jobs/${snapshot.job.job_id}/events`, {
        signal, headers: { Accept: "text/event-stream", "Last-Event-ID": String(snapshot.cursor) },
      });
      await readSSE(response, (event) => {
        if (acceptJobEvent(snapshot, event)) { retries = 0; onUpdate(snapshot); }
      });
      signal.throwIfAborted();
      // Terminal jobs need not have stream_completed (e.g. cancellation, or a
      // non-token job). Durable GET is authoritative, including its result.
      snapshot.job = { ...snapshot.job, ...await api.json(`/jobs/${snapshot.job.job_id}`, { signal }) };
      onUpdate(snapshot);
      if (terminal(snapshot.job.state)) { onConnection("Progress complete"); return; }
    } catch (error) {
      if (signal.aborted) return;
      if ([400, 401, 403, 404, 422].includes(error.status)) throw error;
      onConnection("Connection interrupted. Reconnecting; the job is still running.");
    }
    await wait(Math.min(500 * 2 ** Math.min(retries++, 5), 10_000), signal);
  }
}
