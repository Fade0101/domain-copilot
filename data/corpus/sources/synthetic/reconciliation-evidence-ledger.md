# SYNTHETIC: Medication reconciliation evidence ledger

SYNTHETIC TRAINING DOCUMENT. Project-authored on 2026-10-03, version 1.
The Kestrel training service is fictional. No medicine list for a real or
simulated patient is supplied; the document defines empty field meanings only.

## Separate the source from the conclusion

The fictional ledger records what an information source reports. It does not
decide which medicine a person should take. A reported list, a dispensing record
and a clinician-confirmed list are different kinds of evidence and must remain
labelled as such.

## Field meanings

The medicine-name field preserves the supplied name. The source field identifies
where that name came from. Strength, route, frequency and last-confirmed status
are recorded only when explicitly available. Blank fields remain unspecified;
the author must not calculate a regimen or infer current use from a historical
entry.

The discrepancy field records differences between supplied sources without
silently combining them. The resolution field contains the responsible
reviewer's documented resolution, if one is supplied. A discrepancy is not
resolved merely because a newer file was uploaded.

## Interaction and contraindication information

The ledger may link to an attributed interaction or contraindication statement
provided with an exercise. Record the source title, version and applicable
conditions. Do not infer compatibility when a reference is silent. Do not move
a statement concerning one medicine, route or population into another entry.
This policy supplies no actual drug interaction or contraindication conclusion.

## Review and handoff

The reviewer checks source attribution, unresolved discrepancies and statements
that have changed certainty during drafting. A next action identifies the role
responsible for clarifying missing information. It does not imply that the
clarification, prescription or clinical review has already occurred.

## Intended use

This synthetic process reference supports documentation of uncertainty. It is
not an interaction database, medication order, prescribing guideline or patient
record. All examples are unfilled field descriptions.
