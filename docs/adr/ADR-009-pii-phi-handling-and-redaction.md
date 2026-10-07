# ADR-009: Healthcare PII/PHI Handling, Evaluation, and Redaction Decision

- **Status:** Accepted
- **Date:** 2026-10-07
- **Ticket:** #26
- **Requirements:** SEC-2c; BR-06; OWASP LLM07; OWASP API3:2023

## Context

Healthcare agentic workflows involve patient notes, guideline research, and LLM completions. Ensuring patient privacy requires evaluating how Personally Identifiable Information (PII) and Protected Health Information (PHI) are detected, handled, and redacted across system boundaries (SEC-2c).

We evaluated two architectural approaches for PII/PHI redaction:

1. **Option A: Microsoft Presidio (`presidio-analyzer` + `presidio-anonymizer`)**:
   - Industry-standard framework developed by Microsoft combining regex with NLP (spaCy / transformers) for Named Entity Recognition (NER).
   - *Drawbacks*:
     - Requires installing `spaCy` and executing unverified external downloads of language models (`python -m spacy download en_core_web_sm`, ~15MB–500MB) during build or runtime, violating reproducible, offline air-gapped deployment requirements and hash-locked pip installations.
     - Adds over 30 transitive dependencies, expanding the attack surface and complicating supply-chain vulnerability audits (`pip-audit`).
     - NER models in healthcare exhibit non-trivial false-positive and false-negative rates on clinical jargon and medical abbreviations (e.g. confusing medication names with surnames).

2. **Option B: In-Tree Deterministic Sanitizer (`IPiiRedactor` + `DeterministicPiiRedactor`)**:
   - Lightweight, dependency-free in-tree implementation using verified regular expressions and formal algorithmic checksum validation.
   - Implements NHS Number Modulus 11 check-digit verification (weights 10 down to 2) to eliminate false positives on random 10-digit integers.
   - Deterministic pattern detection for Social Security Numbers (SSN), Medical Record Numbers (MRN), dates of birth, emails, and telephone numbers.
   - Zero network dependencies; full compatibility with offline CI and SHA-256 hash-locked wheels.

Additionally, Business Rule **BR-06** strictly mandates that Domain Copilot operates solely on synthetic, non-PII research data, with zero real patient PHI permitted in the repository or test suites.

## Decision

1. **Adopt the In-Tree Deterministic Sanitizer** as the primary detection and redaction engine for Domain Copilot:
   - Defined behind the clean architectural port `IPiiRedactor` in `app/application/ports/security.py`.
   - Implemented by `DeterministicPiiRedactor` in `app/infrastructure/security/pii.py`.
   - Registered in the composition root (`app/core/container.py`) as `container.pii_redactor`.
2. **Standardized Algorithmic Checksum Validation**:
   - The redactor executes the official NHS England Modulus 11 check algorithm on 10-digit candidates, ensuring that legitimate 10-digit NHS numbers are neutralized while other numeric codes are not corrupted.
3. **Typed Redaction Tokens**:
   - Redactions replace sensitive spans with deterministic tokens: `[REDACTED_NHS_NUMBER]`, `[REDACTED_SSN]`, `[REDACTED_MRN]`, `[REDACTED_EMAIL]`, `[REDACTED_PHONE]`, `[REDACTED_DOB]`.
4. **Pluggable Port Preservation**:
   - Because `IPiiRedactor` is a decoupled protocol, deployments requiring heavy statistical NER (such as enterprise Presidio sidecars) can plug in an external adapter without altering domain or application layer contracts.
5. **Enforcement of BR-06**:
   - Verification suites assert that all evaluation datasets, documents, and test fixtures contain zero real patient records.

## Consequences & Honest Residual Risk

- **Precision vs. Recall**:
  - The deterministic engine provides exact, reproducible detection for structured clinical and personal identifiers.
  - *Residual Risk*: Unstructured freeform patient names (e.g. "John visited the clinic") lack rigid syntax and cannot be identified by deterministic regex without NLP NER. Statistical NER was rejected due to air-gap incompatibilities and false negatives.
  - *Mitigation*: The primary boundary is structural: Domain Copilot processes synthetic and public guideline literature only (BR-06), and real-world deployment requires air-gapped clinical network isolation and human approval before note finalization.
