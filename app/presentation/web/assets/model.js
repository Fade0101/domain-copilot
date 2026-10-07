export function reviewPermissions(principal, review) {
  const may = (permission) => principal?.permissions?.includes(permission) === true;
  const pending = Boolean(review && review.workflow_state === "AWAITING_APPROVAL"
    && review.job_state === "STARTED" && !review.cancellation_requested
    && !review.decision && review.approval_status === "PENDING");
  return {
    view: may("view_pending_approvals"),
    approve: pending && review.approval_allowed === true && may("approve_clinical_note"),
    edit: pending && review.approval_allowed === true && may("approve_clinical_note") && may("edit_clinical_note"),
    reject: pending && may("reject_clinical_note"),
  };
}

// A bounded line diff: shared prefix/suffix stay unchanged; the differing
// region is shown in full. This is intentionally not a quadratic LCS algorithm
// on a potentially 64,000-character note. Final-newline changes stay visible.
export function lineDiff(original, edited) {
  if (original === edited) return [];
  const lines = (text) => text.match(/[^\n]*\n|[^\n]+$/g) || [];
  const a = lines(original), b = lines(edited);
  let first = 0, suffix = 0;
  while (first < a.length && first < b.length && a[first] === b[first]) first++;
  while (suffix < a.length - first && suffix < b.length - first
      && a[a.length - 1 - suffix] === b[b.length - 1 - suffix]) suffix++;
  return [
    ...a.slice(0, first).map((text) => ({ kind: "same", text })),
    ...a.slice(first, a.length - suffix).map((text) => ({ kind: "remove", text })),
    ...b.slice(first, b.length - suffix).map((text) => ({ kind: "add", text })),
    ...a.slice(a.length - suffix).map((text) => ({ kind: "same", text })),
  ];
}

export function estimatedCost(usage) {
  if (usage.estimated_cost == null) return "Estimated cost unavailable";
  return `Estimated cost: ${Number(usage.estimated_cost).toFixed(6)} ${usage.currency}`;
}
