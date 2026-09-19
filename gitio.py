"""Reading commits out of git, in a shape small enough to send to Jev.

Jev's documented failure mode 5 is a large state full of irrelevant detail, so
the diff that goes into the state is truncated twice: once per file, so one
generated file cannot crowd out the rest of the commit, and once overall. When
either cap bites, the state says so, because a model that cannot see the rest
of a change should not be answering as though it did.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Caps on what reaches the model, in diff lines.
MAX_LINES_PER_FILE = 120
MAX_LINES_TOTAL = 400

# A literal separator cannot go in argv, so git is asked to emit 0x1f itself.
SEP_FORMAT = "%x1f"
SEP = "\x1f"


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ("git", *args), cwd=cwd, capture_output=True, text=True, errors="replace"
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or 'failed'}")
    return proc.stdout


def repo_root(start: Path) -> Path:
    return Path(git("rev-parse", "--show-toplevel", cwd=start).strip())


@dataclass
class FileChange:
    status: str          # A, M, D, R, C, T
    path: str
    old_path: str | None = None
    added: int | None = None    # None for binary files
    removed: int | None = None

    @property
    def binary(self) -> bool:
        return self.added is None


@dataclass
class Commit:
    sha: str
    subject: str
    body: str
    author: str
    date: str
    parents: list[str] = field(default_factory=list)
    files: list[FileChange] = field(default_factory=list)
    diff: str = ""
    diff_truncated: bool = False
    added_lines: list[str] = field(default_factory=list)   # every "+" line, untruncated

    @property
    def short(self) -> str:
        return self.sha[:7] if self.sha != "STAGED" else "staged"

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def message(self) -> str:
        return f"{self.subject}\n\n{self.body}".strip()

    def state(self) -> dict:
        """The Jev state for this commit. Only what the questions need."""
        files = []
        for f in self.files:
            entry = {"path": f.path, "change": _STATUS_WORDS.get(f.status[0], f.status)}
            if f.old_path:
                entry["renamed_from"] = f.old_path
            if f.binary:
                entry["note"] = "binary file, contents not shown"
            else:
                entry["lines_added"] = f.added
                entry["lines_removed"] = f.removed
            files.append(entry)
        state = {
            "commit_message": {"subject": self.subject, "body": self.body or ""},
            "files_changed": files,
            "diff": self.diff,
        }
        if self.diff_truncated:
            state["diff_note"] = (
                "This diff is truncated. Some changed lines are not shown, and the "
                "places where lines were cut are marked with a truncation marker."
            )
        return state


_STATUS_WORDS = {
    "A": "added", "M": "modified", "D": "deleted",
    "R": "renamed", "C": "copied", "T": "type changed",
}


def resolve_range(rev_range: str, cwd: Path) -> list[str]:
    """Commit SHAs oldest first. Accepts any revision range git understands."""
    out = git("rev-list", "--reverse", rev_range, cwd=cwd)
    return [line.strip() for line in out.splitlines() if line.strip()]


def read_commit(sha: str, cwd: Path) -> Commit:
    fmt = SEP_FORMAT.join(["%H", "%an", "%aI", "%P", "%s", "%b"])
    meta = git("show", "-s", f"--format={fmt}", sha, cwd=cwd).split(SEP)
    full_sha, author, date, parents, subject, body = (meta + [""] * 6)[:6]
    commit = Commit(
        sha=full_sha.strip(),
        subject=subject.strip(),
        body=body.strip(),
        author=author.strip(),
        date=date.strip(),
        parents=parents.split(),
    )
    if commit.is_merge:
        return commit
    commit.files = _file_changes(
        git("show", "--format=", "--name-status", "-M", sha, cwd=cwd),
        git("show", "--format=", "--numstat", "-M", sha, cwd=cwd),
    )
    raw = git("show", "--format=", "--unified=3", "--no-color", "-M", sha, cwd=cwd)
    commit.diff, commit.diff_truncated = truncate_diff(raw)
    commit.added_lines = added_lines(raw)
    return commit


def read_staged(cwd: Path, message: str) -> Commit:
    subject, _, body = message.strip().partition("\n")
    commit = Commit(
        sha="STAGED", subject=subject.strip(), body=body.strip(),
        author=git("config", "user.name", cwd=cwd).strip() or "you", date="",
    )
    commit.files = _file_changes(
        git("diff", "--cached", "--name-status", "-M", cwd=cwd),
        git("diff", "--cached", "--numstat", "-M", cwd=cwd),
    )
    raw = git("diff", "--cached", "--unified=3", "--no-color", "-M", cwd=cwd)
    commit.diff, commit.diff_truncated = truncate_diff(raw)
    commit.added_lines = added_lines(raw)
    return commit


def _file_changes(name_status: str, numstat: str) -> list[FileChange]:
    counts: dict[str, tuple[int | None, int | None]] = {}
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add, rem, path = parts[0], parts[1], parts[-1]
        counts[path] = (
            (None, None) if add == "-" else (int(add), int(rem))
        )

    changes = []
    for line in name_status.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        old = parts[1] if status[0] in ("R", "C") and len(parts) >= 3 else None
        added, removed = counts.get(path, (None, None))
        changes.append(FileChange(status, path, old, added, removed))
    return changes


def added_lines(raw_diff: str) -> list[str]:
    """Every added line, before truncation. Code-side checks scan these."""
    return [
        line[1:]
        for line in raw_diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def truncate_diff(
    raw: str,
    per_file: int = MAX_LINES_PER_FILE,
    total: int = MAX_LINES_TOTAL,
) -> tuple[str, bool]:
    """Cap the diff per file and overall. Returns the text and whether it was cut."""
    if not raw.strip():
        return "", False

    sections: list[list[str]] = []
    for line in raw.splitlines():
        if line.startswith("diff --git ") or not sections:
            sections.append([])
        sections[-1].append(line)

    kept: list[str] = []
    truncated = False
    budget = total
    for section in sections:
        allowance = min(per_file, budget)
        if allowance <= 0:
            truncated = True
            break
        if len(section) > allowance:
            cut = len(section) - allowance
            kept.extend(section[:allowance])
            kept.append(f"... {cut} more lines of this file not shown ...")
            truncated = True
            budget -= allowance
        else:
            kept.extend(section)
            budget -= len(section)
    return "\n".join(kept), truncated
