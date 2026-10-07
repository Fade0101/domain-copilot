import { API, identifier, queryString } from "./api.js";
import { estimatedCost, lineDiff, reviewPermissions } from "./model.js";
import { button, citations, fields, message, node, renderDiff } from "./render.js";
import { readSSE, sleep, terminal, watchJob } from "./stream.js";

const $ = (id) => document.getElementById(id);
let prefix, api, principal, lifetime, activeView = "chat";
let sessions = [], session = null, messages = [], messagesOffset = 0;
let jobs = [], selectedJob = null, review = null, traceOffset = 0, spanOffset = 0, selectedTrace = null;
let askController = null, reviewBusy = false, reviewRevision = 0;
const tasks = new Map(), snapshots = new Map(), runIds = new Map();

function task(name) {
  tasks.get(name)?.abort();
  const controller = new AbortController();
  tasks.set(name, controller);
  return controller;
}

function clearError() { $("error").hidden = true; }
function report(error) {
  if (error?.name === "AbortError") return;
  if (error?.status === 401 && principal) signOut();
  $("error").textContent = error?.status === 401 ? "Your session ended. Please sign in again." : error.message || "The request failed. Please try again.";
  $("error").hidden = false;
}

function bind(id, event, action) {
  $(id).addEventListener(event, async (event) => {
    event.preventDefault();
    clearError();
    const control = event.submitter;
    if (control) control.disabled = true;
    try { await action(event); } catch (error) { report(error); }
    finally { if (control) control.disabled = false; }
  });
}

function invoke(action) { return () => { clearError(); Promise.resolve().then(action).catch(report); }; }
function may(permission) { return principal?.permissions.includes(permission) === true; }

function signOut() {
  lifetime?.abort();
  for (const controller of tasks.values()) controller.abort();
  tasks.clear();
  askController?.abort();
  askController = null;
  api = null;
  principal = null;
  sessions = []; session = null; messages = []; jobs = []; selectedJob = null; review = null;
  messagesOffset = 0; traceOffset = 0; spanOffset = 0; selectedTrace = null;
  reviewRevision++;
  snapshots.clear(); runIds.clear();
  $("workspace").hidden = true;
  $("identity").hidden = true;
  $("login-panel").hidden = false;
  $("password").value = "";
  $("question").value = "";
  $("edited-note").value = "";
  $("reject-reason").value = "";
  $("workflow-form").reset();
  for (const id of ["messages", "session-list", "job-list", "job-detail", "job-events", "job-tokens", "run-detail", "review-content", "trace-list", "trace-detail", "usage"]) $(id).replaceChildren();
  $("ask").disabled = false;
  $("question").disabled = false;
  $("stop-answer").hidden = true;
  $("review-actions").hidden = true;
  $("login-status").textContent = "Conversations are saved to your account. Sign in to reopen them.";
}

bind("login-form", "submit", async () => {
  $("login-status").textContent = "Signing in…";
  try {
    const auth = new API(prefix);
    const issued = await auth.json("/auth/token", { method: "POST", body: { email: $("email").value.trim(), password: $("password").value } });
    lifetime = new AbortController();
    api = new API(prefix, issued.access_token, lifetime.signal);
    principal = await api.json("/auth/me");
    $("password").value = "";
    $("account").textContent = `${principal.email} · ${principal.role}`;
    $("identity").hidden = false;
    $("workspace").hidden = false;
    $("login-panel").hidden = true;
    $("review-nav").hidden = !may("view_pending_approvals");
    $("workflow-form-panel").hidden = !may("run_workflow");
    $("login-status").textContent = "";
    await route();
  } catch (error) { $("login-status").textContent = "Sign-in failed."; throw error; }
});
bind("logout", "click", signOut);

function navigate(view, id) {
  const hash = `#${view}${id ? `/${identifier(id)}` : ""}`;
  if (location.hash === hash) return route();
  location.hash = hash;
}

async function route() {
  if (!principal) return;
  const [requested, id] = location.hash.slice(1).split("/");
  activeView = ["chat", "jobs", "review", "traces"].includes(requested) ? requested : "chat";
  if (activeView === "review" && !may("view_pending_approvals")) {
    activeView = "chat";
    report(new Error("Clinical review requires a reviewer or admin account."));
  }
  for (const view of ["chat", "jobs", "review", "traces"]) $(`${view}-view`).hidden = view !== activeView;
  for (const link of document.querySelectorAll("nav [data-view]")) {
    if (link.dataset.view === activeView) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (activeView !== "jobs") tasks.get("job-stream")?.abort();
  if (activeView !== "review") { tasks.get("review")?.abort(); tasks.get("review-poll")?.abort(); }
  if (activeView === "chat") await loadSessions();
  if (activeView === "jobs") {
    await loadJobs();
    if (id || selectedJob) await openJob(id || selectedJob);
  }
  if (activeView === "review" && id) await loadReview(identifier(id));
  if (activeView === "traces") {
    if (id) await openTrace(identifier(id));
    else await loadTraces();
  }
}
window.addEventListener("hashchange", invoke(route));

async function loadSessions(more = false) {
  const controller = task("sessions");
  const page = await api.json(`/sessions?limit=50&offset=${more ? sessions.length : 0}`, { signal: controller.signal });
  sessions = more ? [...sessions, ...page.items] : page.items;
  $("more-sessions").hidden = page.items.length < 50;
  $("session-list").replaceChildren();
  for (const item of sessions) {
    const entry = button(item.title || "Untitled conversation", invoke(() => openSession(item)));
    entry.dataset.sessionId = item.id;
    entry.setAttribute("aria-current", String(item.id === session?.id));
    const li = node("li"); li.append(entry); $("session-list").append(li);
  }
  if (!sessions.length) $("session-list").append(node("li", "No saved conversations yet.", "subtle"));
}

async function createSession(title) {
  $("ask").disabled = true;
  try {
    const created = await api.json("/sessions", { method: "POST", body: { title: title || "Untitled conversation" } });
    await openSession(created);
    await loadSessions();
  } finally { $("ask").disabled = Boolean(askController); }
}

async function openSession(item) {
  $("ask").disabled = true;
  askController?.abort();
  session = item;
  messages = [];
  $("conversation-title").textContent = item.title || "Untitled conversation";
  $("messages").replaceChildren();
  $("chat-status").textContent = "Loading saved conversation…";
  try { await loadMessages(); }
  finally { $("ask").disabled = Boolean(askController); }
  for (const entry of $("session-list").querySelectorAll("button")) entry.setAttribute("aria-current", String(entry.dataset.sessionId === item.id));
}

async function loadMessages(more = false) {
  if (!session) return;
  const controller = task("messages");
  const page = await api.json(`/sessions/${session.id}/messages?limit=100&offset=${more ? messagesOffset : 0}`, { signal: controller.signal });
  messages = more ? [...messages, ...page.items] : page.items;
  messagesOffset = (more ? messagesOffset : 0) + page.items.length;
  $("more-messages").hidden = page.items.length < 100;
  $("messages").replaceChildren(...messages.map((item) => message(item.role, item.content, item.answer, (id) => navigate("traces", id))));
  $("chat-status").textContent = messages.length ? "Saved conversation loaded." : "Ask the first question in this conversation.";
}

bind("session-form", "submit", async () => { await createSession($("session-title").value.trim()); $("session-title").value = ""; });
bind("refresh-sessions", "click", () => loadSessions());
bind("more-sessions", "click", () => loadSessions(true));
bind("more-messages", "click", () => loadMessages(true));
bind("reload-history", "click", async () => { askController?.abort(); await loadMessages(); });
bind("stop-answer", "click", () => askController?.abort());

bind("ask-form", "submit", async () => {
  if (askController) return;
  const question = $("question").value.trim();
  if (!question) return;
  if (!session) await createSession(question.slice(0, 80));
  // Finish paginated history before appending a new exchange at its true end.
  while (!$("more-messages").hidden) await loadMessages(true);
  const currentSession = session.id;
  const controller = new AbortController();
  askController = controller;
  $("ask").disabled = true;
  $("question").disabled = true;
  $("stop-answer").hidden = false;
  $("chat-status").textContent = "Checking available evidence…";
  const userMessage = message("user", question);
  let answerMessage = message("assistant", "");
  const content = answerMessage.querySelector(".prose");
  $("messages").append(userMessage, answerMessage);
  let completed = false;
  try {
    const response = await api.request("/ask", { method: "POST", signal: controller.signal,
      headers: { Accept: "text/event-stream" }, body: { question, stream: true, session_id: currentSession } });
    await readSSE(response, (event) => {
      controller.signal.throwIfAborted();
      const data = JSON.parse(event.data);
      if (event.event === "token" && typeof data.delta === "string") {
        content.textContent += data.delta;
        $("chat-status").textContent = "Receiving checked answer…";
      }
      if (event.event === "stream_completed" || event.event === "refusal") {
        const finalMessage = message("assistant", data.answer, data, (id) => navigate("traces", id));
        answerMessage.replaceWith(finalMessage);
        answerMessage = finalMessage;
        completed = true;
        $("chat-status").textContent = "Answer and citations saved.";
        $("question").value = "";
        $("clinical-question").value = question;
      }
    });
    if (!completed) throw new Error("Answer delivery ended before completion. Reload history before asking again.");
  } catch (error) {
    if (principal && session?.id === currentSession) {
      answerMessage.querySelector("h3").textContent = "Incomplete delivery";
      $("chat-status").textContent = "Delivery stopped. Reload history to check for the saved answer. The question was not resubmitted.";
    }
    if (error.name !== "AbortError") throw error;
  } finally {
    if (askController === controller) {
      askController = null;
      $("ask").disabled = false;
      $("question").disabled = false;
      $("stop-answer").hidden = true;
    }
  }
});

async function loadJobs(more = false) {
  const controller = task("jobs");
  const page = await api.json(`/jobs?limit=50&offset=${more ? jobs.length : 0}`, { signal: controller.signal });
  jobs = more ? [...jobs, ...page.items] : page.items;
  $("more-jobs").hidden = page.items.length < 50;
  $("job-list").replaceChildren();
  for (const job of jobs) {
    const entry = button(`${job.operation_type} · ${job.state}\n${job.job_id}`, invoke(() => navigate("jobs", job.job_id)));
    entry.setAttribute("aria-current", String(job.job_id === selectedJob));
    const li = node("li"); li.append(entry); $("job-list").append(li);
  }
  if (!jobs.length) $("job-list").append(node("li", "No jobs available.", "subtle"));
}

async function openJob(value) {
  const id = identifier(value);
  const controller = task("job-stream");
  selectedJob = id;
  $("job-id").value = id;
  $("run-detail").replaceChildren();
  const job = await api.json(`/jobs/${id}`, { signal: controller.signal });
  const snapshot = snapshots.get(id) || { job, cursor: 0, events: [], tokens: "" };
  snapshot.job = { ...snapshot.job, ...job };
  snapshots.set(id, snapshot);
  renderJob(snapshot);
  const signal = AbortSignal.any([controller.signal, lifetime.signal]);
  void watchJob({ api, snapshot, signal,
    onUpdate: (value) => { if (!signal.aborted) renderJob(value); },
    onConnection: (text) => { if (!signal.aborted) $("job-connection").textContent = text; },
  }).catch(report);
  void (async () => {
    while (!signal.aborted) {
      await refreshRun(snapshot, signal);
      if (terminal(snapshot.job.state)) return;
      await sleep(2500, signal);
    }
  })().catch(report);
}

function renderJob(snapshot) {
  const job = snapshot.job;
  if (job.result?.workflow_id || job.workflow_id) runIds.set(job.job_id, job.result?.workflow_id || job.workflow_id);
  $("job-title").textContent = `${job.operation_type} · ${job.state}`;
  const detail = $("job-detail");
  detail.replaceChildren(fields({ "Job ID": job.job_id, "Correlation ID": job.correlation_id, "State": job.state, "Workflow phase": job.workflow_state, "Attempt": job.attempt_number }));
  if (job.cancellation_requested) detail.append(node("p", "Cancellation requested. Waiting for the worker to stop.", "notice"));
  if (job.error) detail.append(node("p", job.error, "flag"));
  if (job.result) {
    const result = node("details"); result.append(node("summary", "Job result"), node("pre", JSON.stringify(job.result, null, 2))); detail.append(result);
    if (job.result.refused) detail.append(node("p", job.result.refusal_reason || job.result.rejection_reason || "The workflow was refused.", "flag"));
  }
  detail.append(button("View job traces", invoke(() => filterTraces({ job_id: job.job_id }))));
  $("cancel-job").hidden = terminal(job.state);
  $("cancel-job").disabled = Boolean(job.cancellation_requested);
  $("reconnect-job").hidden = false;
  $("job-events").replaceChildren(...snapshot.events.map((event) => {
    const phase = event.workflow_state || event.step_name || event.step || "";
    const flags = [event.paused && "Waiting for approval", event.resumed && "Resumed", event.cancellation_requested && "Cancellation requested", event.retry_scheduled && "Retry scheduled"].filter(Boolean);
    return node("li", `#${event.id} ${event.state || "Progress"}${phase ? ` · ${phase}` : ""}${flags.length ? ` · ${flags.join(" · ")}` : ""}`);
  }));
  $("job-tokens").hidden = !snapshot.tokens;
  $("job-tokens").textContent = snapshot.tokens;
}

async function refreshRun(snapshot, signal) {
  const id = runIds.get(snapshot.job.job_id);
  if (!id) return;
  const root = $("run-detail");
  let status;
  try { status = await api.json(`/runs/${id}/status`, { signal }); }
  catch (error) { if (error.status !== 404) throw error; }
  signal.throwIfAborted();
  root.replaceChildren(node("h3", "Clinical workflow"), fields({ "Workflow ID": id, "Phase": status?.state || "Queued" }));
  if (may("view_pending_approvals")) root.append(button("Open clinical review", invoke(() => navigate("review", id))));
  else root.append(node("p", "Share this workflow ID with a reviewer for the approval step.", "subtle"));
  root.append(button("View workflow traces", invoke(() => filterTraces({ run_id: id }))));
  if (status?.state === "COMPLETED" || snapshot.job.result?.note_id) await appendFinalNote(root, id, signal);
}

async function appendFinalNote(root, id, signal) {
  const note = await api.json(`/runs/${id}/note`, { signal });
  signal?.throwIfAborted();
  const section = node("section", undefined, "success");
  section.append(node("h2", "Finalized clinical note"), node("div", note.note, "prose"), fields({ "Note ID": note.note_id, "Approval ID": note.approval_id, "Finalized at": note.finalized_at }));
  root.append(section);
}

bind("workflow-form", "submit", async () => {
  const accepted = await api.json("/runs", { method: "POST", body: { clinical_question: $("clinical-question").value, case_summary: $("case-summary").value, patient_context: $("patient-context").value } });
  runIds.set(accepted.job_id, accepted.workflow_id);
  $("workflow-form-panel").open = false;
  await navigate("jobs", accepted.job_id);
});
bind("job-open-form", "submit", () => navigate("jobs", $("job-id").value));
bind("refresh-jobs", "click", () => loadJobs());
bind("more-jobs", "click", () => loadJobs(true));
bind("reconnect-job", "click", () => openJob(selectedJob));
window.addEventListener("offline", () => {
  if (principal && activeView === "jobs" && selectedJob) {
    tasks.get("job-stream")?.abort();
    $("job-connection").textContent = "Connection interrupted while offline. Progress will reconnect when you are online.";
  }
});
window.addEventListener("online", invoke(async () => {
  if (principal && activeView === "jobs" && selectedJob) await openJob(selectedJob);
}));
bind("cancel-job", "click", async () => {
  if (!selectedJob) return;
  const job = await api.json(`/jobs/${selectedJob}/cancel`, { method: "POST" });
  const snapshot = snapshots.get(job.job_id);
  if (snapshot) {
    snapshot.job = { ...snapshot.job, ...job };
    if (selectedJob === job.job_id) renderJob(snapshot);
  }
});

async function loadReview(value, polling = false) {
  const id = identifier(value);
  const controller = task("review");
  const revision = ++reviewRevision;
  $("review-id").value = id;
  if (!polling) {
    tasks.get("review-poll")?.abort();
    $("review-status").textContent = "Loading draft and safety findings…";
    $("review-content").replaceChildren();
    $("review-actions").hidden = true;
  }
  let record;
  for (let attempt = 0; ; attempt++) {
    try {
      record = await api.json(`/runs/${id}/approval`, { signal: controller.signal });
      break;
    } catch (error) {
      if (![404, 409].includes(error.status) || attempt >= 20) throw error;
      // A workflow reaches AWAITING_APPROVAL just before its review transaction
      // commits. Do not mistake that small window for a missing or unsafe draft.
      const status = await api.json(`/runs/${id}/status`, { signal: controller.signal });
      if (!["RESEARCH", "SAFETY_CHECK", "DRAFT", "AWAITING_APPROVAL"].includes(status.state)) throw error;
      $("review-status").textContent = "The workflow is preparing its review. Waiting for the saved draft…";
      await sleep(500, controller.signal);
    }
  }
  if (revision !== reviewRevision) return;
  const changed = review?.draft.draft_id !== record.draft.draft_id || review?.workflow_id !== record.workflow_id;
  review = record;
  $("review-id").value = id;
  if (changed) { $("edited-note").value = record.draft.note; $("reject-reason").value = ""; }
  renderReview();
  if (record.workflow_state === "COMPLETED") await appendFinalNote($("review-content"), id, controller.signal);
  if (!polling && !["COMPLETED", "REJECTED", "FAILED", "CANCELLED"].includes(record.workflow_state)) {
    const poll = task("review-poll");
    const signal = AbortSignal.any([poll.signal, lifetime.signal]);
    void (async () => {
      while (!signal.aborted && activeView === "review") {
        await sleep(3000, signal);
        if (!reviewBusy) await loadReview(id, true);
        if (["COMPLETED", "REJECTED", "FAILED", "CANCELLED"].includes(review?.workflow_state)) return;
      }
    })().catch(report);
  }
}

function renderReview() {
  if (!review) return;
  const permissions = reviewPermissions(principal, review);
  $("review-status").textContent = `${review.workflow_state} · ${review.approval_status}${review.cancellation_requested ? " · Cancellation requested" : ""}`;
  const root = $("review-content");
  const draft = node("section", undefined, "card");
  draft.append(node("h2", "Draft · human review required"), node("div", review.draft.note, "prose"), citations(review.draft.citations));
  draft.append(fields({ "Draft ID": review.draft.draft_id, "Workflow ID": review.workflow_id, "Job ID": review.job_id }));
  const safety = node("section", undefined, "card");
  safety.append(node("h2", `Safety check · ${review.safety_verdict.status}`));
  for (const reason of review.safety_verdict.reasons || []) safety.append(node("p", reason));
  const flags = [...(review.safety_verdict.flags || []), ...(review.draft.excluded_claims || []), ...(review.draft.deferred_claims || [])];
  safety.append(node("h3", "Flagged, excluded & deferred claims"));
  if (!flags.length) safety.append(node("p", "No flagged, excluded or deferred claims recorded."));
  for (const flag of flags) {
    const item = node("article", undefined, "flag");
    item.append(node("strong", `${flag.severity || flag.status || "Flag"} · ${flag.claim}`), node("p", flag.reason), citations(flag.citations));
    safety.append(item);
  }
  const checks = node("details"); checks.append(node("summary", "Checked claims"));
  for (const claim of review.safety_verdict.checked_claims || []) checks.append(node("p", `${claim.target}: ${claim.status} — ${claim.detail}`), citations(claim.citations));
  safety.append(checks);
  root.replaceChildren(draft, safety);
  if (review.decision) {
    const decision = node("section", undefined, "card");
    decision.append(node("h2", `Decision · ${review.decision.action}`), fields({ "Reviewer": review.decision.actor_id, "Recorded at": review.decision.created_at }));
    if (review.decision.reason) decision.append(node("p", review.decision.reason));
    if (review.decision.approved_note) decision.append(node("h3", "Approved text · awaiting finalization unless confirmed below"), node("div", review.decision.approved_note, "prose"));
    if (review.decision.diff) decision.append(node("h3", "Recorded edit diff"), node("pre", review.decision.diff));
    if (review.decision.status === "APPROVED" && ["AWAITING_APPROVAL", "APPROVED"].includes(review.workflow_state) && !review.cancellation_requested) {
      const resume = button("Finalize approved note", invoke(resumeReview), "primary"); resume.disabled = reviewBusy; decision.append(resume);
    }
    root.append(decision);
  } else if (!review.approval_allowed) root.append(node("p", "This draft cannot be approved. Review the safety findings; rejection remains available while the review is pending.", "flag"));
  $("review-actions").hidden = !(permissions.approve || permissions.reject || permissions.edit);
  $("approve").hidden = !permissions.approve;
  $("approve").disabled = reviewBusy;
  $("edit-panel").hidden = !permissions.edit;
  $("reject-panel").hidden = !permissions.reject;
  $("reject").disabled = reviewBusy;
  updateDiff();
}

function updateDiff() {
  if (!review) return;
  renderDiff($("edit-diff"), lineDiff(review.draft.note, $("edited-note").value));
  $("edit-approve").disabled = reviewBusy || !$("edited-note").value.trim() || $("edited-note").value === review.draft.note;
}

async function decide(action, extra = {}) {
  if (reviewBusy || !review) return;
  const record = review;
  reviewRevision++;
  reviewBusy = true; renderReview();
  try {
    const result = await api.json(`/runs/${record.workflow_id}/approval/${action}`, { method: "POST", body: { draft_id: record.draft.draft_id, ...extra } });
    review = result.review;
  } catch (error) {
    if (error.status === 409) await loadReview(record.workflow_id);
    throw error;
  } finally { reviewBusy = false; renderReview(); }
}

async function resumeReview() {
  if (reviewBusy || !review) return;
  reviewRevision++;
  reviewBusy = true; renderReview();
  try {
    await api.json(`/runs/${review.workflow_id}/resume`, { method: "POST" });
    $("review-status").textContent = "Finalization requested. Waiting for the guarded workflow to complete.";
    await loadReview(review.workflow_id);
  } finally {
    reviewBusy = false;
    // loadReview may already have rendered the persisted final note. Keep it
    // when the worker finishes before the resume response reaches the browser.
    if (review?.workflow_state !== "COMPLETED") renderReview();
  }
}

bind("review-form", "submit", () => navigate("review", $("review-id").value));
bind("refresh-review", "click", () => loadReview($("review-id").value));
bind("approve", "click", () => decide("approve"));
bind("reject-form", "submit", () => decide("reject", { reason: $("reject-reason").value.trim() }));
bind("edit-approve", "click", () => decide("edit-and-approve", { edited_note: $("edited-note").value }));
$("edited-note").addEventListener("input", updateDiff);

function traceFilters() {
  const values = Object.fromEntries(new FormData($("trace-filter")));
  for (const field of ["run_id", "job_id"]) if (values[field]) values[field] = identifier(values[field]);
  for (const field of ["start_time", "end_time"]) if (values[field]) values[field] = new Date(values[field]).toISOString();
  return values;
}

async function filterTraces(values) {
  $("trace-filter").reset();
  for (const [name, value] of Object.entries(values)) $("trace-filter").elements.namedItem(name).value = value;
  await navigate("traces");
}

async function loadTraces(more = false) {
  const controller = task("traces");
  const filters = traceFilters();
  if (!more) { tasks.get("spans")?.abort(); traceOffset = 0; $("trace-list").replaceChildren(); $("trace-detail").hidden = true; $("more-spans").hidden = true; }
  $("trace-status").textContent = "Loading traces…";
  const page = await api.json(`/traces?${queryString({ ...filters, limit: 50, offset: traceOffset })}`, { signal: controller.signal });
  for (const trace of page.items) {
    const li = node("li");
    li.append(button(`${trace.start_time} · ${trace.id}\nRun: ${trace.run_id || "—"} · Correlation: ${trace.correlation_id || "—"}`, invoke(() => navigate("traces", trace.id))));
    $("trace-list").append(li);
  }
  traceOffset += page.items.length;
  $("more-traces").hidden = page.items.length < 50;
  $("trace-status").textContent = traceOffset ? `${traceOffset} traces loaded.` : "No traces match these filters.";
  if (!more) {
    $("usage").hidden = true;
    const usage = await api.json(`/usage?${queryString(filters)}`, { signal: controller.signal });
    $("usage").replaceChildren(node("h2", estimatedCost(usage)), node("p", usage.notice), fields({ "Provider calls": usage.calls, "Known tokens": usage.total_tokens, "Token usage complete": usage.usage_complete ? "Yes" : "No — some provider usage is unavailable", "Cost coverage": usage.cost_status }));
    for (const item of usage.breakdown) $("usage").append(node("p", `${item.provider} / ${item.model}: ${item.calls} calls · ${item.total_tokens} known tokens · ${item.estimated_cost == null ? "estimated cost unavailable" : `${item.estimated_cost.toFixed(6)} ${usage.currency} estimated`}`));
    $("usage").hidden = false;
  }
}

async function openTrace(id, more = false) {
  const controller = task("spans");
  selectedTrace = identifier(id);
  if (!more) spanOffset = 0;
  const page = await api.json(`/traces/${selectedTrace}/spans?limit=100&offset=${spanOffset}`, { signal: controller.signal });
  const root = $("trace-detail");
  if (!more) root.replaceChildren(node("h2", "Trace spans"), fields({ "Trace ID": page.trace.id, "Run ID": page.trace.run_id, "Job ID": page.trace.job_id, "Correlation ID": page.trace.correlation_id, "Started at": page.trace.start_time }));
  for (const span of page.spans) {
    const entry = node("details");
    entry.append(node("summary", `${span.name} · ${span.step_type} · ${span.status}`), fields({ "Started at": span.start_time, "Duration (seconds)": span.duration ?? "Unavailable", "Tokens": span.tokens ?? "Unavailable" }), node("pre", JSON.stringify({ inputs: span.inputs, outputs: span.outputs }, null, 2)));
    root.append(entry);
  }
  spanOffset += page.spans.length;
  if (!spanOffset) root.append(node("p", "No spans have been recorded yet."));
  $("more-spans").hidden = page.spans.length < 100;
  root.hidden = false;
}
bind("trace-filter", "submit", () => loadTraces());
bind("more-traces", "click", () => loadTraces(true));
bind("more-spans", "click", () => openTrace(selectedTrace, true));

try {
  const response = await fetch("/ui/config", { cache: "no-store" });
  if (!response.ok) throw new Error("Could not load the application configuration.");
  const config = await response.json();
  if (!/^\/(?!\/)[a-zA-Z0-9/_-]+$/.test(config.api_prefix)) throw new Error("Invalid API configuration.");
  prefix = config.api_prefix.replace(/\/$/, "");
} catch (error) { report(error); $("login-form").querySelector("button").disabled = true; }
