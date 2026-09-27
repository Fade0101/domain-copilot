# Security Controls

This document defines the security controls for Domain Copilot and serves as an implementation and verification checklist.

Controls are mapped to the application's web/API security, data protection, authentication and authorization, LLM/agent security, and development supply-chain requirements.

Evidence, tests, and implementation references will be added as each control is completed.

---

## 1. Identity & Access Management

* [ ] **Authentication** — Secure login using a strong password hashing algorithm such as bcrypt or Argon2.
* [ ] **Password Security** — Passwords are never stored in plaintext, logged, or returned through API responses.
* [ ] **JWT Security** — Tokens use a strong configured signing secret, appropriate expiration, and server-side validation of signature, expiry, and claims.
* [ ] **Role-Based Access Control** — Server-side authorization enforces the required roles:
  * `analyst`
  * `reviewer`
  * `admin`
* [ ] **Resource Ownership** — Analysts can access only resources they are authorized to access. Reviewer/admin access follows the role permissions defined by the BRD.
* [ ] **Approval Authorization** — Only authorized reviewers/admins can approve, reject, or edit-and-approve clinical notes.
* [ ] **Server-Side Enforcement** — Authorization decisions are never based solely on UI visibility or client-provided role information.

---

## 2. API & Input Security

* [ ] **Input Validation** — All API inputs are validated using typed schemas.
* [ ] **Payload Limits** — Request bodies, query parameters, and uploaded documents have explicit size and complexity limits.
* [ ] **File Validation** — Uploaded documents are restricted to supported formats and validated before ingestion.
* [ ] **Path Safety** — Uploaded filenames and paths cannot cause path traversal or arbitrary filesystem access.
* [ ] **SQL Injection Protection** — Database access uses parameterized queries/ORM/database abstractions. User-controlled values must never be interpolated directly into SQL.
* [ ] **Rate Limiting** — Appropriate endpoints have configurable rate limits to reduce abuse and resource exhaustion.
* [ ] **CORS** — CORS allows only explicitly configured origins.
* [ ] **Security Headers** — Production-facing HTTP responses use appropriate security headers such as HSTS, Content-Security-Policy, X-Content-Type-Options, and frame protection where applicable.
* [ ] **Error Handling** — API errors do not expose stack traces, secrets, database credentials, internal paths, or other sensitive implementation details.

---

## 3. Data & Privacy Protection

* [ ] **Synthetic/Public Data Only** — The assessment implementation must not use real patient/PHI data.
* [ ] **PII Minimization** — Personally identifiable information is minimized and is not unnecessarily included in prompts, traces, logs, or error messages.
* [ ] **PII Detection** — Presidio or an equivalent mechanism is used where required to detect/redact sensitive information before data is sent outside the intended infrastructure boundary.
* [ ] **LLM Data Boundary** — The application documents what data may be sent to external LLM providers and what data remains local.
* [ ] **Secrets Management** — API keys, JWT secrets, database credentials, and other secrets are supplied through environment/configuration mechanisms and never hard-coded.
* [ ] **No Secrets in Logs** — Tokens, passwords, API keys, authorization headers, and other credentials are excluded from logs and traces.
* [ ] **Secret Scanning** — Gitleaks or equivalent secret scanning runs in CI and is performed before submission, including repository history where applicable.
* [ ] **Dependency Scanning** — Third-party dependencies are scanned for known vulnerabilities using the project's configured security tooling.

---

## 4. LLM & Prompt Injection Security

Retrieved and ingested content is treated as **untrusted data**.

Content retrieved from the corpus must never be treated as system/developer instructions.

* [ ] **Instruction Hierarchy** — System/developer policies remain authoritative over user input and retrieved documents.
* [ ] **Direct Prompt Injection Defense** — User input cannot override safety rules, tool permissions, workflow state, or approval requirements.
* [ ] **Indirect Prompt Injection Defense** — Instructions embedded inside retrieved or ingested documents cannot modify agent behavior.
* [ ] **Evidence Boundary** — Retrieved content is evidence, not executable instructions.
* [ ] **No Policy Mutation** — Documents and user input cannot change system policies, prompts, tool allowlists, or security configuration.
* [ ] **Prompt Isolation** — Prompts clearly separate trusted instructions from untrusted user/retrieved content.
* [ ] **Injection Evaluation** — At least three prompt-injection cases are included in the evaluation set, including direct and indirect injection scenarios.

---

## 5. Agent & Tool Security

Agents operate under explicit least-privilege boundaries.

* [ ] **Tool Allowlists** — Each agent has an explicit list of permitted tools.
* [ ] **Guideline Researcher Restrictions** — Researcher can use only its defined retrieval/research tools.
* [ ] **Safety Checker Restrictions** — Safety Checker can use only its defined safety/retrieval tools.
* [ ] **Documentation Drafter Restrictions** — Drafter cannot execute finalization or approval actions.
* [ ] **Finalization Gate** — `finalize_clinical_note` cannot execute until explicit human approval has been persisted.
* [ ] **No Hidden Writes** — Agents cannot directly modify protected application state outside their declared tool contracts.
* [ ] **Typed Tool Inputs/Outputs** — Tool boundaries validate structured inputs and outputs.
* [ ] **Iteration Limits** — Agents have bounded iteration/tool-call limits.
* [ ] **Token/Payload Limits** — LLM requests and tool payloads have configurable size limits.
* [ ] **Failure Safety** — Safety Checker failure during a clinical note workflow results in a safe failure/refusal rather than bypassing the safety stage.

---

## 6. Healthcare Safety Controls

Because Domain Copilot operates on healthcare guidance, safety controls apply specifically to clinical claims.

* [ ] **No Dosage Inference** — The system must not invent or infer dosage information that is not explicitly supported by retrieved evidence.
* [ ] **No Contraindication Inference** — The system must not infer contraindications beyond the available evidence.
* [ ] **No Interaction Inference** — The system must not infer drug interactions beyond the retrieved evidence.
* [ ] **Unknown Remains Unknown** — Missing evidence must not be interpreted as evidence of safety.
* [ ] **Evidence Traceability** — Clinical claims in drafted notes must trace to exact retrieved chunks.
* [ ] **Unsupported Claims Excluded** — Unsupported clinical claims cannot silently enter the final approved note.
* [ ] **Mandatory Safety Check** — Every clinical note workflow executes the Safety Checker.
* [ ] **Human Approval** — No clinical note becomes final without explicit reviewer approval.

---

## 7. Job & Async Security

T7 introduces long-running jobs that must remain protected across requests and worker executions.

* [ ] **Job Ownership** — Users cannot access or manipulate jobs belonging to unauthorized users.
* [ ] **Cancel Authorization** — Only authorized users can cancel a job they are permitted to control.
* [ ] **Retry Authorization** — Retry operations require appropriate ownership/role authorization.
* [ ] **Approval Authorization** — Approval commands verify both reviewer permissions and the target workflow/job.
* [ ] **Idempotency** — Repeated requests using the same idempotency key cannot create unintended duplicate operations.
* [ ] **Cancellation Integrity** — Client SSE disconnection does not cancel or alter the underlying job.
* [ ] **Durable State** — Security-relevant job and workflow state is persisted in PostgreSQL rather than relying solely on Redis.

---

## 8. Observability & Audit

* [ ] **Correlation IDs** — Requests, jobs, workflows, agents, tools, and LLM calls can be correlated.
* [ ] **Security Events** — Authentication failures, authorization failures, approval actions, and other security-relevant events are auditable.
* [ ] **Approval Audit Trail** — Approve, reject, and edit-and-approve operations record the actor, target, timestamp, and decision.
* [ ] **Trace Safety** — Agent/tool/LLM traces do not expose credentials or unnecessary sensitive information.
* [ ] **Cost/Token Records** — Token and cost accounting does not contain secret material.

---

## 9. Development & Supply-Chain Security

* [ ] **Pinned/Locked Dependencies** — Dependencies are reproducibly installed using the project's lock/requirements configuration.
* [ ] **Dependency Audit** — Automated dependency vulnerability scanning runs in CI.
* [ ] **Secret Scanning in CI** — Secret scanning runs on pull requests and/or repository history according to the project configuration.
* [ ] **Secure CI Configuration** — CI workflows do not expose secrets to untrusted pull requests.
