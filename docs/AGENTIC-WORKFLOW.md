# Agentic Development Workflow

> How AI is governed as a development participant in Domain Copilot.

This document describes the agentic coding workflow used to develop the Domain
Copilot project. AI is not "used to write code" — it is a **governed
participant** in an engineering system where a human is responsible for
architecture decisions, acceptance, and merges.

---

## 1. Overview

The development workflow is built around six categories of agentic
configuration, each described in detail below:

| # | Category | Location | Purpose |
|---|----------|----------|---------|
| 1 | Project instruction files | `.agents/rules/` | Architecture and security rules the AI must follow |
| 2 | Versioned prompt assets / skills | `.agents/skills/`, `prompts/` | Reusable, scoped task definitions and product prompt artifacts |
| 3 | Scoped sub-agents | `.agents/skills/{security-reviewer,test-writer,doc-writer}/` | Role-specific agents with explicit permissions and forbidden actions |
| 4 | Hooks enforcing quality gates | `scripts/pre-commit-check.py`, `scripts/install-hooks.py` | Automated checks before every commit |
| 5 | Custom commands | `scripts/validate-prompts.py`, `scripts/check-boundaries.py` | Repeatable operations for common tasks |
| 6 | Versioned prompt library | `prompts/*.yaml` | Product prompts as versioned, schema-validated artifacts |
| 7 | Spec-Driven Development (SpecPilot) | `.tasks/`, `.context/` | SpecPilot orchestrates planning, implementation, and MCP usage |

---

## 2. Project Instruction Files (Category 1)

**Location:** [`.agents/rules/architecture.md`](../.agents/rules/architecture.md),
[`.agents/rules/security.md`](../.agents/rules/security.md)

These files encode the project's non-negotiable rules so that any AI agent
working in the repository respects the architectural boundaries automatically.

### Architecture rules (`.agents/rules/architecture.md`)

Encodes the Clean Hexagonal Architecture from ADR-005 and ADR-006:

- **Layer dependency direction:** domain ← application ← core ← infrastructure/presentation.
- **Forbidden patterns:** no direct infrastructure imports from presentation, no
  `HTTPException` in use cases, no inline prompt strings, no secrets in code, no
  pydantic in domain/application.
- **Commit conventions:** Conventional Commits, atomic changes, messages explain
  *why*.
- **Testing conventions:** fakes over mocks, integration tests through
  `TestClient`, architecture boundary tests.
- **File placement table:** tells the AI exactly where each kind of code goes.

### Security rules (`.agents/rules/security.md`)

Encodes OWASP Web Top 10, OWASP LLM Top 10, and the project's secrets hygiene
policy:

- Never hardcode secrets; use `SecretStr` via `pydantic_settings`.
- Server faults never return internal details.
- Tool arguments are schema-validated before execution.
- Strict privilege separation between instructions and retrieved content.
- Each agent has an explicit tool allow-list.

### Why this matters

Without these instruction files, an AI agent might:
- Add a `pydantic` import inside `app/domain/`, breaking the architecture
  contract.
- Hardcode an API key in a test fixture.
- Raise `HTTPException` directly from a use case.

With them, the AI is constrained to follow the same rules a human developer
would, and boundary violations are caught immediately by the CI and pre-commit
hook.

---

## 3. Scoped Sub-Agents (Category 2 & 3)

**Location:** `.agents/skills/`

Each sub-agent has a `SKILL.md` file defining its **role**, **responsibilities**,
**forbidden actions**, and **required context files**. This implements the
principle of least privilege for AI development participants.

### Security Reviewer ([`.agents/skills/security-reviewer/SKILL.md`](../.agents/skills/security-reviewer/SKILL.md))

| Attribute | Value |
|-----------|-------|
| **Role** | Review code for OWASP vulnerabilities and secrets |
| **Scope** | Read-only — may analyze any file |
| **Forbidden** | Modify source, commit, push, run destructive commands, access external URLs |
| **Output** | Structured security report (Critical / Warning / Info / Summary) |

**Example use:** Before opening a PR that adds a new API endpoint, the security
reviewer sub-agent is invoked to check for missing auth, input validation gaps,
or error responses that leak internals.

**Real outcome (Ticket #3):** The security reviewer flagged that the original
error handler in `app.py` returned `str(exc)` for all exceptions, including
server faults. This violated SDD §A.5.1 (never expose internal details). The
fix was to split error handlers by fault type in `app/presentation/api/errors.py`
— client faults carry contextual messages, server faults carry static safe
messages.

### Test Writer ([`.agents/skills/test-writer/SKILL.md`](../.agents/skills/test-writer/SKILL.md))

| Attribute | Value |
|-----------|-------|
| **Role** | Generate unit, integration, and architecture tests |
| **Scope** | `tests/` only — may not modify `app/` |
| **Forbidden** | Modify application source, import infrastructure in unit tests, add secrets to fixtures |
| **Output** | Complete test files with imports, fixtures, and test functions |

**Example use:** After implementing a new use case, the test writer generates
domain unit tests using fakes from `tests/support/fakes.py` and integration
tests using `TestClient`.

**Real outcome (Ticket #3):** The test writer generated 25 tests for the new
error model and configuration surface (`test_errors.py`, `test_settings.py`,
`test_yaml_prompt_provider.py`, `test_error_handling.py`). All tests follow the
project conventions (fakes over mocks, one behavior per test). The human
reviewed and adjusted edge case expectations before committing.

### Documentation Writer ([`.agents/skills/doc-writer/SKILL.md`](../.agents/skills/doc-writer/SKILL.md))

| Attribute | Value |
|-----------|-------|
| **Role** | Maintain ADRs, ARCHITECTURE.md, BRD traceability |
| **Scope** | `docs/` and `README.md` only — may not modify `app/` or `tests/` |
| **Forbidden** | Modify SYSTEM-DESIGN.md (frozen), modify source/tests, claim unimplemented features exist |
| **Output** | Complete markdown documents or targeted diffs |

**Example use:** After completing a ticket, the doc writer updates
`ARCHITECTURE.md` with the new components and updates the BRD traceability
matrix to reflect changed requirement statuses.

---

## 4. Hooks Enforcing Quality Gates (Category 4)

**Location:** [`scripts/pre-commit-check.py`](../scripts/pre-commit-check.py),
[`scripts/install-hooks.py`](../scripts/install-hooks.py)

### Pre-commit quality gate

The `pre-commit-check.py` script runs **five checks** in sequence:

1. **Ruff lint** — catches style and import errors
2. **Ruff format** — enforces consistent formatting
3. **Mypy type-check** — catches type errors
4. **Import-linter contracts** — enforces architecture layer boundaries
5. **Pytest** — runs the full test suite

If any check fails, the commit is blocked.

### Installation

```bash
python scripts/install-hooks.py
```

This copies the pre-commit hook into `.git/hooks/pre-commit`. Every subsequent
`git commit` runs the quality gate automatically.

### Why a custom hook instead of pre-commit framework?

The `pre-commit` framework is powerful but adds another dependency. Since our
checks are already defined in CI (`.github/workflows/ci.yml`), the hook script
simply mirrors those same steps locally using tools already in the virtualenv.
This keeps the developer experience consistent: what passes locally passes in CI.

### Verification

```bash
# Run manually (without installing the hook)
python scripts/pre-commit-check.py
```

Expected output when all checks pass:

```
============================================================
  Ruff lint
============================================================
  ✅ PASSED: Ruff lint
  ...
  🎉 All pre-commit checks passed.
```

---

## 5. Custom Commands (Category 5)

**Location:** `scripts/`

These scripts automate repeated operations that would otherwise require
remembering multiple commands or flags.

### `validate-prompts.py` — Validate prompt artifacts

```bash
python scripts/validate-prompts.py
```

Loads every `*.yaml` file in `prompts/` through the `YamlPromptProvider` in
strict mode. This is the same validation that runs at application startup —
running it manually catches prompt schema issues before committing.

**Example output:**

```
Validating prompt artifacts in prompts/

  ✅ grounded_answer v1 — System prompt for grounded clinical question answeri...
  ✅ safety_check v1 — System prompt for the mandatory clinical safety check. ...

🎉 All 2 prompt artifact(s) validated successfully.
```

### `check-boundaries.py` — Architecture boundary check

```bash
python scripts/check-boundaries.py
```

Runs both the declarative import-linter contracts and the AST boundary tests in
a single command. Use after any refactoring to verify layer rules are intact.

### When to use these commands

| Scenario | Command |
|----------|---------|
| Added or modified a prompt YAML file | `python scripts/validate-prompts.py` |
| Moved code between layers or added imports | `python scripts/check-boundaries.py` |
| About to commit any change | `python scripts/pre-commit-check.py` |
| Setting up a fresh clone | `python scripts/install-hooks.py` |

---

## 6. Versioned Prompt Library (Category 6)

**Location:** [`prompts/`](../prompts/)

Product prompts are **versioned artifacts, never inline string literals**. This
is a core architectural decision (BRD AR-4, ADR-006).

### How it works

Each prompt is a YAML file with a strict schema:

```yaml
id: grounded_answer        # Unique identifier
version: 1                 # Integer version (authoritative, not the filename)
description: >-            # Human-readable purpose
  System prompt for grounded clinical question answering.
input_variables:           # Declared placeholders (validated at render time)
  - question
  - evidence
template: |                # The actual prompt text with {variable} placeholders
  You are a clinical documentation assistant...
```

### Current prompts

| Prompt ID | Version | Purpose |
|-----------|---------|---------|
| `grounded_answer` | v1 | Grounded clinical Q&A with evidence-only answers and refusal |
| `safety_check` | v1 | Mandatory clinical safety gate screening drafted answers |

### Design decisions

1. **Versioning is explicit and deterministic.** The `version` field inside the
   YAML is authoritative; the filename convention (`<id>.v<n>.yaml`) is for human
   readability only.
2. **Schema validation is eager.** When `strict=True` (the default in
   production), all prompts are loaded and validated at application startup. A
   malformed prompt crashes the boot, not a user request.
3. **Variable mismatch is an error.** `Prompt.render()` validates that exactly
   the declared `input_variables` are provided. Missing or unexpected variables
   raise `PromptValidationError` (a server fault → HTTP 500 with a safe message).
4. **Infrastructure isolation.** The YAML parser lives in
   `app/infrastructure/prompts/yaml_prompt_provider.py` — the only module that
   imports `yaml`. The application depends on `IPromptProvider` (a `Protocol`),
   so the prompt backend could be swapped to a database or remote store without
   changing any use case.

### Adding a new prompt

1. Create `prompts/<id>.v<version>.yaml` with the required schema fields.
2. Run `python scripts/validate-prompts.py` to verify it loads correctly.
3. Commit the file — the pre-commit hook runs the full test suite.
4. The `YamlPromptProvider` picks it up automatically at next startup.

---

## 7. Spec-Driven Development & MCP (SpecPilot)

**Location:** `.tasks/`, `.context/`, `.mcp.json`

To supplement the native sub-agents and ensure a rigorous planning phase before
any code is written, this repository also utilizes **SpecPilot**, a spec-driven
development plugin for Claude Code / GitHub Copilot CLI.

### Workflow Integration

Instead of prompting the AI to "just build it," we use SpecPilot's four-command
loop to ensure work is specified, planned, and reviewed:

1. `/specify` — Defines requirements and acceptance criteria for a ticket. If
   the brief is vague, the agent surfaces hidden assumptions before writing the
   spec into `.tasks/<feature>/specifications.md`.
2. `/plan` — Turns the spec into a concrete, step-by-step implementation roadmap
   (`plan.md`). No code is written at this stage.
3. `/implement` — The agent executes the plan file by file, respecting the
   project's architecture rules.
4. `/code-review` — The Auditor sub-agent audits the diff for correctness, edge
   cases, and security.

### Artifacts and Audit Trail

Everything SpecPilot writes lands in two version-controlled folders:
- `.tasks/<feature>/` — Holds the specifications, plans, and implementation notes
  for each ticket.
- `.context/<topic>/` — Holds analysis reports (from `/analyse`) and saved
  conversation summaries (from `/save`).

These artifacts provide a paper trail that can be audited by the reviewer (or
the instructor/grader), proving that the AI operated from a shared intent rather
than guesswork.

### MCP Servers

SpecPilot also brings pre-configured **Model Context Protocol (MCP)** servers
via `.mcp.json`, fulfilling the assessment's MCP requirement:
- `sequential-thinking` for iterative reasoning on complex architectural decisions.
- `context-mode` for sandboxed analysis of large data sets.
- `context7` for up-to-date library documentation.

---

## 8. Development Flow — Putting It All Together

A typical ticket implementation follows this flow:

```
1. Human creates a branch from dev
         │
2. Human defines scope and acceptance criteria (from GitHub issue)
         │
3. AI agent reads .agents/rules/ to understand constraints
         │
4. AI implements code (respecting architecture rules)
         │
5. Test-writer sub-agent generates tests (scoped to tests/)
         │
6. Security-reviewer sub-agent reviews for vulnerabilities (read-only)
         │
7. Human reviews AI output, adjusts, and makes architecture decisions
         │
8. Pre-commit hook runs quality gates automatically
         │
9. Human opens PR with description, self-reviews with inline comments
         │
10. CI runs the same checks (lint, type-check, boundaries, tests, audit)
         │
11. Human merges via PR (never direct push to main)
```

### Human review points

The human is responsible for:

- **Architecture decisions** — which layer code goes in, which patterns to use.
- **Acceptance** — verifying that AI output meets the ticket's acceptance criteria.
- **Merges** — all work merges via PR with real descriptions and self-review.
- **Correcting AI mistakes** — documented in `docs/AI-USAGE-LOG.md`.

### What the AI decides vs. what the human decides

| Decision | Owner |
|----------|-------|
| Variable names, formatting, boilerplate | AI |
| Which layer a class belongs to | Human (guided by rules) |
| Test structure and assertions | AI (human reviews) |
| Error taxonomy design | Human |
| Whether a requirement is met | Human |
| Commit message wording | AI draft → Human review |
| PR description | AI draft → Human review |
| Architecture patterns (ports, adapters, DI) | Human |
| Documentation accuracy | Human verifies |

---

## 8. Where the Agentic Approach Failed

Honest accounting of where AI assistance was misleading or counterproductive:

1. **Circular dependency suggestion.** Early in development, the AI suggested
   importing an infrastructure adapter directly from a use case "for convenience."
   This would have violated the core architecture contract. The instruction file
   in `.agents/rules/architecture.md` was created partly in response to this
   incident — to prevent it from recurring.

2. **Over-eager feature claims.** The documentation writer sub-agent initially
   marked AR-4 (Configuration & Prompts) as "Implemented" when only the
   configuration surface existed and no prompt infrastructure had been built.
   The human caught this during PR review and corrected the BRD traceability
   matrix. The doc-writer's `SKILL.md` now explicitly forbids claiming
   unimplemented features.

3. **Test coverage gaps.** The test writer generated tests for happy paths but
   initially missed edge cases for the prompt validation (e.g., `version` being
   a boolean `True` which is technically an `int` in Python). The human added
   the `isinstance(version, bool)` guard and the corresponding test.

4. **Generic error messages.** The AI's first implementation of error handlers
   returned `str(exc)` for all errors, including server faults. This would have
   leaked internal configuration details (file paths, prompt names) to API
   callers. The security reviewer sub-agent was instrumental in catching this.

These failures are documented in detail in [`AI-USAGE-LOG.md`](./AI-USAGE-LOG.md).

---

## 9. Reproducing This Workflow

A contributor can reproduce the documented workflow:

```bash
# 1. Clone and set up
git clone https://github.com/Fade0101/domain-copilot.git
cd domain-copilot
python -m venv .venv
.venv/Scripts/activate  # or source .venv/bin/activate on Unix
pip install -r requirements.txt

# 2. Install the pre-commit hook
python scripts/install-hooks.py

# 3. Verify everything passes
python scripts/pre-commit-check.py
python scripts/validate-prompts.py
python scripts/check-boundaries.py

# 4. Start working on a ticket
git checkout -b feat/my-feature dev
# ... implement using the sub-agent skills and rules ...
# ... the pre-commit hook catches violations automatically ...

# 5. Open a PR
git push origin feat/my-feature
# CI runs the same checks as the pre-commit hook
```

---

## 10. File Inventory

```
.agents/
  rules/
    architecture.md        # Architecture rules (Category 1)
    security.md            # Security rules (Category 1)
  skills/
    security-reviewer/
      SKILL.md             # Security reviewer sub-agent (Category 3)
    test-writer/
      SKILL.md             # Test writer sub-agent (Category 3)
    doc-writer/
      SKILL.md             # Documentation writer sub-agent (Category 3)

scripts/
  pre-commit-check.py      # Pre-commit quality gate hook (Category 4)
  install-hooks.py         # Hook installer command (Category 4/5)
  validate-prompts.py      # Prompt validation command (Category 5)
  check-boundaries.py      # Boundary check command (Category 5)

prompts/
  grounded_answer.v1.yaml  # Product prompt artifact (Category 6)
  safety_check.v1.yaml     # Product prompt artifact (Category 6)
```
