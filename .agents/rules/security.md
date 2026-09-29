# Security Rules — AI Development Guardrails

These rules enforce the security posture documented in `docs/SECURITY.md`,
BRD SEC-1/SEC-2/SEC-3, and SDD §A.5.1.

## Secrets Hygiene (C6, SEC-3)

- **Never hardcode** API keys, passwords, database URLs with credentials, or
  tokens anywhere in source code, test fixtures, or documentation.
- All secrets are read from environment variables via `pydantic_settings` with
  `SecretStr` typing, which masks values in logs and `repr`.
- `.env` is gitignored. `.env.example` documents every variable with blank or
  placeholder values only.
- Before any commit, mentally verify no secret is present. When in doubt, leave
  the value blank and note it requires environment configuration.

## Error Handling (SDD §A.5.1)

- **Server faults (500)** must NEVER return internal error details to the caller.
  Use the static messages defined in `app/presentation/api/errors.py`.
- **Client faults (4xx)** may return the error's own message — it is safe and
  useful to the caller.
- All unhandled exceptions hit the catch-all handler which logs the real cause
  and returns a generic message.

## Input Validation

- All API inputs are validated through pydantic schemas before reaching use cases.
- File uploads must be validated for type, size, and content.
- Tool arguments in agent workflows are schema-validated before execution.
- Never render model output as raw HTML or pass it to shell/SQL/file path
  unvalidated (OWASP LLM Top 10 — Insecure Output Handling).

## Prompt Injection Defence

- Strict privilege separation between system instructions and retrieved content.
- Retrieved chunks are treated as untrusted user content, never as instructions.
- The safety checker agent screens all drafted outputs before they reach the user.

## Agent Permissions (Least Privilege)

- Each agent has an explicit tool allow-list. No agent may call tools outside
  its defined set.
- Write/side-effecting tools (e.g., finalising a clinical note) must pass the
  human approval gate before execution.
- The orchestrator enforces max-iteration and per-step timeout limits from
  `settings.limits`.
