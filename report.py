"""Rendering a run: for a terminal, for a PR comment, for a machine.

Every renderer shows the probability next to the verdict. A rule that lands at
0.52 and one that lands at 0.98 are not the same finding, and hiding the number
behind a tick would make the tool look more certain than it is.
"""

from __future__ import annotations

import json
import sys

from rules import OK, REVIEW, WARN

MARK = {OK: "ok  ", REVIEW: "?   ", WARN: "WARN"}
COLOR = {OK: "\033[32m", REVIEW: "\033[33m", WARN: "\033[31m"}
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def _use_color(stream) -> bool:
    return stream.isatty() and "NO_COLOR" not in _env()


def _env() -> dict:
    import os
    return os.environ


def terminal(reports: list, usage, elapsed: float, show_all: bool,
             stream=sys.stdout) -> None:
    color = _use_color(stream)

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if color else text

    for report in reports:
        commit = report.commit
        head = paint(commit.short, BOLD) + "  " + commit.subject
        print(head, file=stream)

        if report.skipped:
            print("  " + paint(f"skipped, {report.skipped}", DIM), file=stream)
            print(file=stream)
            continue

        headline = report.headline_text
        if headline:
            print("  " + paint(headline, DIM), file=stream)

        shown = report.results if show_all else report.flagged
        for result in sorted(shown, key=lambda r: r.rule.title):
            line = (
                f"  {paint(MARK[result.verdict], COLOR[result.verdict])}  "
                f"{result.rule.title:<22} {result.noul:.2f}"
            )
            if result.verdict != OK:
                line += "  " + paint(result.rule.on_fail, DIM)
            print(line, file=stream)

        for finding in report.findings:
            if finding.verdict == OK and not show_all:
                continue
            print(
                f"  {paint(MARK[finding.verdict], COLOR[finding.verdict])}  "
                f"{finding.check:<22} {'':4}  {paint(finding.detail, DIM)}",
                file=stream,
            )

        if not shown and not report.findings:
            print("  " + paint("clean", COLOR[OK]), file=stream)
        print(file=stream)

    print(summary_line(reports, usage, elapsed), file=stream)


def summary_line(reports: list, usage, elapsed: float) -> str:
    judged = [r for r in reports if not r.skipped]
    warns = sum(1 for r in judged if r.verdict == WARN)
    reviews = sum(1 for r in judged if r.verdict == REVIEW)
    skipped = len(reports) - len(judged)

    parts = [f"{len(judged)} commit{'s' if len(judged) != 1 else ''}"]
    parts.append(f"{warns} warning{'s' if warns != 1 else ''}")
    parts.append(f"{reviews} to review")
    if skipped:
        parts.append(f"{skipped} skipped")
    parts.append(f"{elapsed:.1f} s")
    if usage.cache_hits:
        parts.append(f"{usage.cache_hits} cached")
    parts.append(f"${usage.cost_usd:.4f}")
    return ", ".join(parts)


def markdown(reports: list, usage, elapsed: float) -> str:
    lines = ["## commitjev", ""]
    for report in reports:
        commit = report.commit
        lines.append(f"**`{commit.short}`** {commit.subject}")
        if report.skipped:
            lines.append(f"- skipped, {report.skipped}")
            lines.append("")
            continue
        rows = [
            f"- **{r.rule.title}** `{r.noul:.2f}` {r.rule.on_fail}"
            for r in report.flagged
        ]
        rows += [
            f"- **{f.check}** {f.detail}. {f.fix}"
            for f in report.findings if f.verdict != OK
        ]
        lines.extend(rows or ["- clean"])
        lines.append("")
    lines.append(f"_{summary_line(reports, usage, elapsed)}_")
    return "\n".join(lines)


def as_json(reports: list, usage, elapsed: float, model: str) -> str:
    payload = {
        "model": model,
        "elapsed_s": round(elapsed, 3),
        "usage": {
            "requests": usage.requests,
            "cache_hits": usage.cache_hits,
            "input_tokens": usage.input_tokens,
            "cost_usd": round(usage.cost_usd, 6),
        },
        "commits": [
            {
                "sha": r.commit.sha,
                "subject": r.commit.subject,
                "verdict": r.verdict,
                "skipped": r.skipped,
                "headline": r.headline,
                "headline_confidence": r.headline_confidence,
                "rules": [
                    {"id": x.rule.id, "title": x.rule.title,
                     "noul": x.noul, "verdict": x.verdict}
                    for x in r.results
                ],
                "code_checks": [
                    {"check": f.check, "verdict": f.verdict,
                     "detail": f.detail, "fix": f.fix}
                    for f in r.findings
                ],
            }
            for r in reports
        ],
    }
    return json.dumps(payload, indent=2)
