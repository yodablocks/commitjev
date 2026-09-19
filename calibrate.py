#!/usr/bin/env python3
"""Measure what commitjev catches, using commits whose defect is known.

    python3 calibrate.py              # build the cases, judge them, print the table
    python3 calibrate.py --keep       # leave the throwaway repo behind to look at

It builds a temporary git repository, branches each case in cases.py off the
same base commit, and runs the real pipeline over every case tip. Then, per
rule, it reports the probability on the commits that carry that rule's defect
against the highest probability on every other commit. The gap between the two
is what the thresholds in rules.py have to sit inside.

A rule that fires on its own defect and stays quiet elsewhere separates. A rule
with no separation is not carrying its weight, whatever it scores in isolation.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

import cases as case_data
import client
import gitio
import rules


def build_repo(root: Path) -> list[tuple[case_data.Case, str]]:
    """A base commit, then one branch per case. Returns (case, sha) pairs."""
    gitio.git("init", "-q", "-b", "base", str(root))
    gitio.git("config", "user.email", "calibrate@example.com", cwd=root)
    gitio.git("config", "user.name", "calibrate", cwd=root)

    for path, text in case_data.BASE_FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    gitio.git("add", "-A", cwd=root)
    gitio.git("commit", "-q", "-m", "Add the ledger service", cwd=root)

    built = []
    for case in case_data.CASES:
        gitio.git("checkout", "-q", "-B", case.name, "base", cwd=root)
        for path, text in case.writes.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        for path in case.deletes:
            gitio.git("rm", "-q", str(root / path), cwd=root)
        gitio.git("add", "-A", cwd=root)
        gitio.git("commit", "-q", "-m", case.message, cwd=root)
        built.append((case, gitio.git("rev-parse", "HEAD", cwd=root).strip()))
    gitio.git("checkout", "-q", "base", cwd=root)
    return built


def judge_all(root: Path, built: list, jev: client.Jev) -> dict[str, rules.CommitReport]:
    questions = rules.questions()
    commits = [gitio.read_commit(sha, root) for _, sha in built]

    def one(commit):
        return rules.judge(commit, jev.ask(commit.state(), questions))

    outcomes = client.in_parallel(commits, one, workers=8)
    reports = {}
    for (case, _), outcome in zip(built, outcomes):
        if isinstance(outcome, Exception):
            print(f"{case.name}: {type(outcome).__name__}: {outcome}", file=sys.stderr)
            continue
        reports[case.name] = outcome
    return reports


def separation_table(reports: dict[str, rules.CommitReport]) -> tuple[int, int]:
    """Per rule: its score on its own defect, on other defects, and on clean commits.

    Only the clean column measures false alarms. A rule firing on another
    defective commit is often right, because a commit with one planted defect
    frequently has a second real problem: the message that hides a credential
    also fails to describe the diff.
    """
    by_case_defect = {c.name: c.defect for c in case_data.CASES}
    misses, alarms = 0, 0

    header = (f"{'rule':<22}{'own defect':>14}{'other defects':>15}"
              f"{'clean':>8}{'margin':>8}  fires")
    print(header)
    print("-" * len(header))
    for rule in rules.JEV_RULES:
        own, other, clean = [], [], []
        for name, report in reports.items():
            hit = next((r for r in report.results if r.rule.id == rule.id), None)
            if hit is None:
                continue
            defect = by_case_defect[name]
            bucket = own if defect == rule.id else (clean if defect is None else other)
            bucket.append(hit.noul)

        # "Loudest" is whichever end of the scale means the rule is complaining.
        loudest = min if rule.good_when_yes else max
        quietest = max if rule.good_when_yes else min

        if not own:
            print(f"{rule.title:<22}{'no case':>14}{loudest(other, default=0):>15.2f}"
                  f"{loudest(clean, default=0):>8.2f}{'':>8}  untested")
            continue

        worst_own = loudest(own)
        loudest_clean = loudest(clean, default=(1.0 if rule.good_when_yes else 0.0))
        # Margin between the defect and the quietest thing it must beat.
        margin = (loudest_clean - worst_own) if rule.good_when_yes else (worst_own - loudest_clean)

        fired_clean = sum(1 for v in clean if rule.verdict(v) == rules.WARN)
        alarms += fired_clean
        caught = all(rule.verdict(v) == rules.WARN for v in own)
        partly = any(rule.verdict(v) == rules.WARN for v in own)
        if not caught:
            misses += 1

        label = "yes" if caught else ("some" if partly else "NO")
        if fired_clean:
            label += f", {fired_clean} false"
        shown = " ".join(f"{v:.2f}" for v in sorted(own))
        print(f"{rule.title:<22}{shown:>14}{loudest(other, default=0):>15.2f}"
              f"{loudest_clean:>8.2f}{margin:>8.2f}  {label}")
    return misses, alarms


def per_case_table(reports: dict[str, rules.CommitReport]) -> int:
    """Did each case get the verdict its label says it should?"""
    by_case = {c.name: c for c in case_data.CASES}
    wrong = 0
    print(f"\n{'case':<20}{'defect':<24}{'verdict':<10}fired")
    print("-" * 92)
    for name, report in reports.items():
        case = by_case[name]
        fired = [r.rule.id for r in report.results if r.verdict == rules.WARN]
        fired += [f.check for f in report.findings if f.verdict == rules.WARN]
        ok = (case.defect in fired) if case.defect else not fired
        if not ok:
            wrong += 1
        print(f"{name:<20}{case.defect or 'none':<24}{report.verdict:<10}"
              f"{', '.join(fired) or '-'}{'' if ok else '   <- not as labelled'}")
    return wrong


def headline_table(reports: dict[str, rules.CommitReport]) -> None:
    """Does the Choice name the right defect when several rules fire at once?

    This is the only thing the Choice is for. The Nouls each answer in
    isolation, so a commit with one real problem often trips four of them, and
    the Choice is the question that asks which one a reviewer would actually
    raise. If it cannot name the planted defect, it is decoration.
    """
    by_case = {c.name: c for c in case_data.CASES}
    right = considered = 0
    print(f"\n{'case':<20}{'defect':<24}{'headline':<24}{'conf':>6}  spoke")
    print("-" * 92)
    for name, report in reports.items():
        case = by_case[name]
        spoke = report.headline_text is not None
        agrees = report.headline == case.defect
        if case.defect:
            considered += 1
            right += agrees and spoke
        flag = "yes" if spoke else f"no, under {rules.HEADLINE_CONFIDENCE:.2f}"
        print(f"{name:<20}{case.defect or 'none':<24}{report.headline or '-':<24}"
              f"{report.headline_confidence:>6.2f}  {flag}")
    print(f"\nthe Choice named the planted defect on {right}/{considered} "
          f"defective commits")


def stability_table(root: Path, built: list, repeat: int, model: str) -> None:
    """Ask the same questions about the same commits several times over.

    Jev is not deterministic. Most rules barely move, but a rule that wanders
    across a threshold makes the same commit pass on one run and warn on the
    next, which matters for something that gates a commit. The cache hides
    this: whichever answer arrives first is the one that sticks.
    """
    # Inside the throwaway repo, so the writes go away with it. Never read from.
    jev = client.Jev(
        cache_path=root / ".stability-cache.sqlite3", model=model, use_cache=False
    )
    questions = rules.questions()
    commits = [gitio.read_commit(sha, root) for _, sha in built]
    samples: dict[tuple[str, str], list[float]] = {}

    try:
        for _ in range(repeat):
            outcomes = client.in_parallel(
                commits, lambda c: jev.ask(c.state(), questions), workers=8
            )
            for (case, _), answers in zip(built, outcomes):
                if isinstance(answers, Exception):
                    continue
                for rule in rules.JEV_RULES:
                    if rule.id in answers:
                        samples.setdefault((case.name, rule.id), []).append(
                            float(answers[rule.id]["noul"])
                        )
    finally:
        jev.close()

    header = f"\n{'rule':<22}{'widest spread':>15}{'where':>18}  verdict flips"
    print(header)
    print("-" * (len(header) - 1))
    total_flips = 0
    for rule in rules.JEV_RULES:
        mine = {case: v for (case, rid), v in samples.items() if rid == rule.id}
        if not mine:
            continue
        widest_case = max(mine, key=lambda c: max(mine[c]) - min(mine[c]))
        widest = max(mine[widest_case]) - min(mine[widest_case])
        flips = sum(
            1 for v in mine.values() if len({rule.verdict(x) for x in v}) > 1
        )
        total_flips += flips
        print(f"{rule.title:<22}{widest:>15.2f}{widest_case:>18}  "
              f"{flips or '-'}")
    print(f"\n{repeat} runs over {len(built)} commits, "
          f"{total_flips} case and rule pairs changed verdict between runs, "
          f"${jev.usage.cost_usd:.4f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true",
                        help="leave the throwaway repository in place")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--model", default=client.DEFAULT_MODEL)
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="ask N times with the cache off and report how much "
                             "the answers move between runs")
    args = parser.parse_args(argv)

    root = Path(tempfile.mkdtemp(prefix="commitjev-calibrate-"))
    started = time.time()
    try:
        built = build_repo(root)
        jev = client.Jev(
            cache_path=Path(__file__).resolve().parent / ".calibrate-cache.sqlite3",
            model=args.model, use_cache=not args.no_cache,
        )
        try:
            reports = judge_all(root, built, jev)
        finally:
            jev.close()

        if args.repeat > 1:
            stability_table(root, built, args.repeat, args.model)

        misses, alarms = separation_table(reports)
        wrong = per_case_table(reports)
        headline_table(reports)
        elapsed = time.time() - started
        print(
            f"\n{len(reports)} cases, {len(rules.JEV_RULES) - misses}"
            f"/{len(rules.JEV_RULES)} rules caught their defect, "
            f"{alarms} false alarm{'s' if alarms != 1 else ''} on clean commits, "
            f"{elapsed:.1f} s, ${jev.usage.cost_usd:.4f}"
        )
        if args.keep:
            print(f"\nrepository left at {root}")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
