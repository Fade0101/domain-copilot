"""Human-readable rendering of the same measured JSON values, including failures."""

from __future__ import annotations

from typing import Any


def markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    pins = (report.get("run") or {}).get("pins", {})
    lines = [
        "# Retrieval evaluation report",
        "",
        f"Job: `{report['job_id']}` — state: **{report['state']}**",
        "",
        f"Corpus: `{pins.get('corpus_version', 'unavailable')}`  ",
        f"Golden set: `{pins.get('dataset_version', 'unavailable')}`  ",
        f"Executed: {summary['executed_cases']}/{summary['total_cases']}; "
        f"case expectations passed: {summary['passed_cases']}; "
        f"execution errors: {summary['execution_errors']}.",
        "",
        f"BRD metric targets met: **{'YES' if summary['targets_met'] else 'NO'}**",
        "",
        "| Metric | Measured | Numerator / denominator | Target | Met |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for name, metric in summary["metrics"].items():
        if "percent" in metric:
            value = "N/A" if metric["percent"] is None else f"{metric['percent']:.2f}%"
            counts = f"{metric['numerator']} / {metric['denominator']}"
            target = "—" if metric["target_percent"] is None else f"≥{metric['target_percent']}%"
        else:
            value, counts, target = str(metric["count"]), "—", "0"
        met = {True: "yes", False: "no", None: "N/A"}[metric["target_met"]]
        lines.append(f"| {name} | {value} | {counts} | {target} | {met} |")
    lines.extend(["", "## Failed expectations", ""])
    for failure in summary["failures"]:
        lines.append(f"- `{failure['case_id']}`: {', '.join(failure['reasons'])}")
    if not summary["failures"]:
        lines.append("No failed expectations among executed cases.")
    if report.get("setup_error"):
        lines.extend(["", f"Setup/resume error: `{report['setup_error']}`."])
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Retrieval hit-rate uses the selected evidence passed to the app's answer boundary. "
            "Candidate hit-rate diagnoses whether a miss occurred before selection. "
            "Groundedness counts verbatim sentence/newline units supported by resolved "
            "clean-corpus "
            "citations; it does not assert clinical truth or answer relevance. "
            "Answer coverage, exact refusal decisions and clinical safety are checked separately. "
            "Attack-fixture instructions never count as clinical support. Refusals contribute no "
            "factual claims. Empty denominators are N/A. Incomplete runs and execution errors "
            "cannot "
            "meet the overall targets.",
            "",
            "See the accompanying JSON for every answer, claim label, actual chunk UUID, "
            "retrieval/ask trace, score, latency, runtime version and configuration pin.",
            "",
        ]
    )
    return "\n".join(lines)
