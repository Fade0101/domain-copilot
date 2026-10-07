import { expect, test } from "@playwright/test";

const password = "synthetic-browser-password-25";
const flaggedRun = "00000000-0000-4000-8000-000000000025";

async function login(page, role = "analyst") {
  await page.goto("/");
  await page.getByLabel("Email", { exact: true }).fill(`${role}@example.com`);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("#account")).toContainText(`${role}@example.com`);
}

async function startWorkflow(page, summary = "A synthetic assessment case. No real patient data.") {
  await page.getByRole("link", { name: "Jobs & workflows" }).click();
  await page.locator("#workflow-form-panel > summary").click();
  await page.getByLabel("Clinical question", { exact: true }).fill("Review the synthetic evidence.");
  await page.getByLabel("Synthetic case summary").fill(summary);
  const accepted = page.waitForResponse((r) => r.url().endsWith("/api/v1/runs") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Start workflow", exact: true }).click();
  const response = await accepted;
  expect(response.status()).toBe(202);
  return response.json();
}

async function loadReview(page, id) {
  await page.getByRole("link", { name: "Review", exact: true }).click();
  await page.getByLabel("Workflow ID", { exact: true }).fill(id);
  await page.getByRole("button", { name: "Load review" }).click();
  await expect(page.getByRole("heading", { name: "Draft · human review required" })).toBeVisible();
}

test("streamed Q&A persists, then an analyst workflow is reviewed and finalized", async ({ page, browser }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await login(page);
  await expect(page.getByRole("link", { name: "Review", exact: true })).toBeHidden();
  await page.getByLabel("New conversation title").fill("Browser journey conversation");
  await page.getByRole("button", { name: "New conversation", exact: true }).click();
  await page.getByLabel("Ask the evidence corpus").fill("When does the synthetic clinic open?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.locator("#chat-status")).toHaveText("Receiving checked answer…");
  await expect(page.locator("#messages .assistant .prose")).not.toBeEmpty();
  await expect(page.locator("#chat-status")).toHaveText("Answer and citations saved.");
  await expect(page.locator("#messages")).toContainText("The synthetic clinic opens on Monday.");
  await page.locator(".citation summary").click();
  await expect(page.locator(".citation blockquote")).toContainText("fictional");
  await page.getByRole("button", { name: "View answer trace" }).click();
  await expect(page.locator("#trace-detail")).toContainText("llm.complete");

  // Fresh document / JWT login: conversations are loaded from PostgreSQL.
  await login(page);
  await page.getByRole("button", { name: "Browser journey conversation", exact: true }).click();
  await expect(page.locator("#messages .message")).toHaveCount(2);
  await expect(page.locator("#messages .citation")).toHaveCount(1);
  const run = await startWorkflow(page);
  await expect(page.locator("#run-detail")).toContainText("AWAITING_APPROVAL");
  await expect(page.getByRole("button", { name: "Approve draft", exact: true })).toBeHidden();

  const reviewer = await browser.newPage({ baseURL: new URL(page.url()).origin });
  try {
    await login(reviewer, "reviewer");
    await loadReview(reviewer, run.workflow_id);
    await reviewer.getByRole("button", { name: "Approve draft", exact: true }).click();
    await expect(reviewer.locator("#review-status")).toContainText("APPROVED");
    await expect(reviewer.getByRole("heading", { name: "Finalized clinical note" })).toHaveCount(0);
    await reviewer.getByRole("button", { name: "Finalize approved note" }).click();
    await expect(reviewer.getByRole("heading", { name: "Finalized clinical note" })).toBeVisible({ timeout: 30_000 });
    await expect(reviewer.locator(".success")).toContainText("Follow the documented evidence.");
    await expect(page.locator("#job-title")).toContainText("COMPLETED");
    await expect(page.locator("#run-detail .success")).toContainText("Synthetic reviewed finding.");
    await page.getByRole("button", { name: "View workflow traces" }).click();
    await expect(page.locator("#trace-list li")).not.toHaveCount(0);
    await expect(page.locator("#usage")).toContainText(/Estimated cost/);
  } finally { await reviewer.close(); }
  expect(errors).toEqual([]);
});

test("edit-and-approve shows a diff and finalizes the persisted edited text", async ({ page }) => {
  await login(page, "admin");
  const run = await startWorkflow(page);
  await expect(page.locator("#run-detail")).toContainText("AWAITING_APPROVAL");
  await page.getByRole("button", { name: "Open clinical review" }).click();
  await page.locator("#edit-panel > summary").click();
  const edited = "Synthetic reviewed finding.\nDocument the clinician's clarification.\n";
  await page.getByLabel("Reviewed note", { exact: true }).fill(edited);
  await expect(page.locator("#edit-diff .diff-remove")).toContainText("Follow the documented evidence.");
  await expect(page.locator("#edit-diff .diff-add")).toContainText("clinician's clarification");
  await page.getByRole("button", { name: "Approve edited note" }).click();
  await expect(page.getByRole("heading", { name: "Recorded edit diff" })).toBeVisible();
  // Hold the real resume response until the real worker has finalized. This
  // covers a fast worker/slow network without substituting backend responses.
  await page.route(`**/api/v1/runs/${run.workflow_id}/resume`, async (route) => {
    const response = await route.fetch();
    expect(response.status()).toBe(202);
    await expect.poll(async () => {
      const status = await page.request.get(`/api/v1/runs/${run.workflow_id}/status`, {
        headers: { Authorization: route.request().headers().authorization },
      });
      return (await status.json()).state;
    }, { timeout: 30_000 }).toBe("COMPLETED");
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "Finalize approved note" }).click();
  await expect(page.locator("#review-content .success .prose")).toHaveText(edited, { timeout: 30_000 });
  expect(run.workflow_id).toBeTruthy();
});

test("flagged claims remain visible and reviewers can reject with a reason", async ({ page }) => {
  await login(page, "reviewer");
  await loadReview(page, flaggedRun);
  await expect(page.locator("#review-content .flag").first()).toContainText("Synthetic DrugA");
  await expect(page.getByRole("button", { name: "Approve draft", exact: true })).toBeHidden();
  await expect(page.locator("#edit-panel")).toBeHidden();
  await page.locator("#reject-panel > summary").click();
  await page.getByLabel("Reason for rejection").fill("Synthetic case requires more supporting evidence.");
  await page.getByRole("button", { name: "Reject draft", exact: true }).click();
  await expect(page.locator("#review-status")).toContainText("REJECTED");
  await expect(page.locator("#review-content")).toContainText("Synthetic case requires more supporting evidence.");
  await expect(page.getByRole("heading", { name: "Finalized clinical note" })).toHaveCount(0);
});

test("a disconnected answer recovers from history without repeating POST /ask; markup stays inert", async ({ page }) => {
  let asks = 0;
  page.on("request", (request) => { if (request.url().endsWith("/api/v1/ask")) asks++; });
  await login(page);
  await page.getByLabel("Ask the evidence corpus").fill('When does the clinic open? <img src=x onerror="window.injected=1">');
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.locator("#chat-status")).toHaveText("Receiving checked answer…");
  await page.getByRole("button", { name: "Stop receiving" }).click();
  await expect(page.locator("#chat-status")).toContainText("Delivery stopped");
  await page.getByRole("button", { name: "Reload history" }).click();
  await expect(page.locator("#messages .message")).toHaveCount(2);
  await expect(page.locator("#messages .message.user")).toContainText("<img");
  expect(await page.locator("#messages img").count()).toBe(0);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  expect(asks).toBe(1);
  await page.getByLabel("Ask the evidence corpus").fill("unknown information");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.locator("#messages .refusal")).toContainText("Not enough information in the corpus");
  await expect(page.locator("#messages .refusal .citation")).toHaveCount(0);
});

test("job navigation/reconnect replays once; only the explicit cancel button cancels", async ({ page, context }) => {
  const replayIds = [];
  let cancellations = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/events")) replayIds.push(request.headers()["last-event-id"]);
    if (request.url().endsWith("/cancel")) cancellations++;
  });
  await login(page);
  const run = await startWorkflow(page, "Wait for a synthetic cancellation test.");
  await expect(page.locator("#job-title")).toContainText("STARTED");
  // The job GET can report STARTED before the SSE response arrives. Receive an
  // event before disconnecting so there is a nonzero replay cursor to preserve.
  await expect(page.locator("#job-events li")).not.toHaveCount(0);
  await page.getByRole("link", { name: "Conversations", exact: true }).click();
  expect(cancellations).toBe(0);
  await page.getByRole("link", { name: "Jobs & workflows" }).click();
  await expect(page.locator("#job-title")).toContainText("STARTED");
  await context.setOffline(true);
  await expect(page.locator("#job-connection")).toContainText(/Reconnect|interrupted/);
  await context.setOffline(false);
  await page.getByRole("button", { name: "Reconnect progress" }).click();
  await expect.poll(() => replayIds.some((id) => Number(id) > 0)).toBe(true);
  const events = await page.locator("#job-events li").allTextContents();
  expect(new Set(events).size).toBe(events.length);
  expect(cancellations).toBe(0);
  await page.getByRole("button", { name: "Cancel job", exact: true }).click();
  await expect(page.locator("#job-title")).toContainText("CANCELLED");
  expect(cancellations).toBe(1);
  expect(run.job_id).toBeTruthy();
});

test("minimal layout remains usable on a narrow screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await expect(page.getByLabel("Ask the evidence corpus")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
