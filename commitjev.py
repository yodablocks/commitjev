#!/usr/bin/env python3
"""commitjev: read a commit the way a reviewer would, before anyone else does.

    commitjev.py HEAD~12..HEAD        # judge a range
    commitjev.py --staged             # judge what is about to be committed
    commitjev.py --staged --message-file .git/COMMIT_EDITMSG   # from a git hook

Exit status is 0 when nothing warns, 1 when something does, 2 when the run
itself failed. With --strict, anything Jev was unsure about also fails.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import client
import gitio
import report as render
import rules

DEFAULT_RANGE = "HEAD~10..HEAD"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="commitjev",
        description="Check commits for a message that does not match the diff, "
                    "bundled changes, and what the message leaves out.",
    )
    parser.add_argument(
        "rev_range", nargs="?", default=None,
        help=f"a git revision range, for example HEAD~5..HEAD (default {DEFAULT_RANGE})",
    )
    parser.add_argument("--staged", action="store_true",
                        help="judge the staged changes instead of a range")
    parser.add_argument("--message-file", type=Path, default=None,
                        help="read the commit message from this file, for git hooks")
    parser.add_argument("-m", "--message", default=None,
                        help="the commit message to judge with --staged")
    parser.add_argument("-C", "--repo", type=Path, default=Path.cwd(),
                        help="run against this repository")
    parser.add_argument("--all", action="store_true",
                        help="show every rule, not only the ones that did not pass")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero on anything Jev was unsure about")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--markdown", action="store_true",
                        help="print markdown, for pasting into a pull request")
    parser.add_argument("--model", default=client.DEFAULT_MODEL)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--no-cache", action="store_true",
                        help="ask again even when the answer is cached")
    parser.add_argument("--quiet", action="store_true", help="no progress output")
    return parser.parse_args(argv)


def collect(args: argparse.Namespace, root: Path) -> list[gitio.Commit]:
    if args.staged:
        message = args.message
        if message is None and args.message_file:
            message = args.message_file.read_text()
        if message is None:
            message = gitio.git("log", "-1", "--format=%B", cwd=root)
        commit = gitio.read_staged(root, _strip_comments(message))
        if not commit.files:
            return []
        return [commit]

    rev_range = args.rev_range or DEFAULT_RANGE
    shas = gitio.resolve_range(rev_range, root)
    return [gitio.read_commit(sha, root) for sha in shas]


def _strip_comments(message: str) -> str:
    """git hands hooks the editor buffer, comments and all."""
    return "\n".join(
        line for line in message.splitlines() if not line.startswith("#")
    ).strip()


def run(args: argparse.Namespace) -> int:
    root = gitio.repo_root(args.repo)
    commits = collect(args, root)
    if not commits:
        print("Nothing to judge.", file=sys.stderr)
        return 0

    jev = client.Jev(
        cache_path=root / ".commitjev" / "cache.sqlite3",
        model=args.model,
        use_cache=not args.no_cache,
    )
    questions = rules.questions()
    progress = _Progress(len(commits), enabled=not args.quiet and sys.stderr.isatty())
    started = time.time()

    def judge(commit: gitio.Commit) -> rules.CommitReport:
        if commit.is_merge:
            return _skip(commit, "merge commit")
        if not commit.files:
            return _skip(commit, "no file changes")
        if not commit.diff.strip():
            return _skip(commit, "no textual diff")
        answers = jev.ask(commit.state(), questions)
        return rules.judge(commit, answers)

    try:
        outcomes = client.in_parallel(
            commits, judge, workers=args.workers, on_done=progress.tick
        )
    finally:
        progress.done()
        jev.close()

    reports, failures = [], []
    for commit, outcome in zip(commits, outcomes):
        if isinstance(outcome, Exception):
            failures.append((commit, outcome))
        else:
            reports.append(outcome)

    elapsed = time.time() - started
    if args.json:
        print(render.as_json(reports, jev.usage, elapsed, args.model))
    elif args.markdown:
        print(render.markdown(reports, jev.usage, elapsed))
    else:
        render.terminal(reports, jev.usage, elapsed, show_all=args.all)

    for commit, exc in failures:
        print(f"{commit.short}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if failures:
        return 2

    worst = {r.verdict for r in reports}
    if rules.WARN in worst:
        return 1
    if args.strict and rules.REVIEW in worst:
        return 1
    return 0


def _skip(commit: gitio.Commit, why: str) -> rules.CommitReport:
    return rules.CommitReport(
        commit=commit, results=[], findings=[], headline=None,
        headline_confidence=0.0, skipped=why,
    )


class _Progress:
    def __init__(self, total: int, enabled: bool):
        self.total, self.enabled, self.count = total, enabled, 0
        self._lock = threading.Lock()

    def tick(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.count += 1
            print(f"\r  {self.count}/{self.total} commits", end="", file=sys.stderr)

    def done(self) -> None:
        if self.enabled:
            print("\r" + " " * 32 + "\r", end="", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except client.NoAPIKey as exc:
        print(exc, file=sys.stderr)
        return 2
    except gitio.GitError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
