"""The judgments commitjev makes about a commit, and the policy over them.

Two kinds of check live here.

Code checks are the ones a regex or git itself answers exactly: an em dash in
the message, a deleted file, a manifest that gained a line, a literal key. Jev
is not asked about any of them, because a model that cannot count reliably has
nothing to add to a question `re.search` already settles.

Jev checks are the ones that need reading: whether the message describes the
change, whether the diff holds together, what the message leaves out. Each is
one Noul carrying its own rule text, because question ids are not sent to the
model. They all ride in one request per commit, evaluated in parallel against
the same state.

A Choice runs alongside them for the headline. Per the jaggedness notes its
probabilities are relative (which problem is worst) while each Noul is absolute
(is this problem present at all), so the two are never mixed: the Nouls decide
every verdict and the Choice only picks the line shown first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A Noul on the good side of PASS passes; on the bad side of FAIL it warns;
# anything between the two is a judgment the model did not actually make, so it
# goes to the human as "review" rather than being rounded to a verdict.
PASS = 0.65
FAIL = 0.35

# The Choice only speaks when its distribution is concentrated.
HEADLINE_CONFIDENCE = 0.50

OK, REVIEW, WARN = "ok", "review", "warn"
RANK = {OK: 0, REVIEW: 1, WARN: 2}


@dataclass(frozen=True)
class JevRule:
    id: str
    title: str
    instructions: str
    true_means: str
    false_means: str
    good_when_yes: bool
    on_fail: str
    # What a confident answer on the bad side means for the run. Most rules
    # describe something that should not be in a commit, so they warn. A rule
    # that only reports a fact worth a glance, however sure it is, is REVIEW:
    # it belongs in the report without failing the run.
    severity: str = "warn"

    def question(self) -> dict:
        return {
            "type": "noul",
            "instructions": self.instructions,
            "criteria": {"true": self.true_means, "false": self.false_means},
        }

    def verdict(self, noul: float) -> str:
        if noul >= PASS:
            return OK if self.good_when_yes else self.severity
        if noul <= FAIL:
            return self.severity if self.good_when_yes else OK
        return REVIEW


JEV_RULES: tuple[JevRule, ...] = (
    JevRule(
        id="message_describes_diff",
        title="message matches diff",
        instructions=(
            "Does the commit message in `commit_message` describe the change that "
            "the code in `diff` actually makes?"
        ),
        true_means=(
            "Someone who read only the message would correctly expect the main "
            "thing this diff does."
        ),
        false_means=(
            "The message names something the diff does not do, or it is so vague "
            "that it gives no idea what changed, or it misses the main change."
        ),
        good_when_yes=True,
        on_fail="Rewrite the message to say what the diff does.",
    ),
    JevRule(
        id="single_purpose",
        title="one logical change",
        instructions=(
            "Do all of the edits in `diff` serve one purpose, so that a reviewer "
            "could describe the whole commit in one sentence without the word "
            "'and' joining two unrelated things?"
        ),
        true_means=(
            "Every edit contributes to the same change. Supporting edits such as a "
            "test, an import, or a doc line for that same change still count as one "
            "purpose."
        ),
        false_means=(
            "The diff mixes work that could have shipped separately, such as a bug "
            "fix together with an unrelated rename, reformat, or new feature."
        ),
        good_when_yes=True,
        on_fail="Split this into one commit per logical change.",
    ),
    JevRule(
        id="undisclosed_change",
        title="hidden change",
        instructions=(
            "Does `diff` change behaviour that `commit_message` does not mention "
            "anywhere?"
        ),
        true_means=(
            "The diff alters what the program does in a way a reader of the message "
            "would not see coming, for example a changed default, a changed "
            "threshold, a removed guard, or a new network call."
        ),
        false_means=(
            "Every behaviour change in the diff is either stated in the message or "
            "is an obvious part of what the message describes."
        ),
        good_when_yes=False,
        on_fail="Say in the message what else this changes.",
    ),
    JevRule(
        id="unexplained_removal",
        title="unexplained removal",
        instructions=(
            "Does `diff` delete existing working code, tests, or documentation "
            "without `commit_message` giving a reason for the deletion?"
        ),
        true_means=(
            "Something that was there and functioning is gone, and the message does "
            "not say why. Code moved to another file in the same diff does not "
            "count as deleted."
        ),
        false_means=(
            "Nothing meaningful was removed, or the message explains the removal, "
            "or the removed lines moved elsewhere in this same diff."
        ),
        good_when_yes=False,
        on_fail="Say why it went, or move it instead of deleting it.",
    ),
    JevRule(
        id="debug_leftovers",
        title="debug leftovers",
        instructions=(
            "Do the added lines in `diff` leave debugging scaffolding behind in the "
            "code?"
        ),
        true_means=(
            "An added line is a stray print or console log used for debugging, a "
            "block commented out rather than removed, a hardcoded test value, or a "
            "temporarily disabled check."
        ),
        false_means=(
            "The added lines are all intended code. Deliberate logging through a "
            "logger, a CLI that prints its own output, and explanatory comments do "
            "not count as scaffolding."
        ),
        good_when_yes=False,
        on_fail="Take the scaffolding out before committing.",
    ),
    JevRule(
        id="new_dependency",
        title="new dependency",
        instructions=(
            "Does `diff` make the project depend on a third-party package it was "
            "not already using?"
        ),
        true_means=(
            "An import, require, or manifest entry names a package outside the "
            "language's standard library that this diff introduces."
        ),
        false_means=(
            "No new third-party package appears, or the only new imports are from "
            "the standard library or from this project's own modules."
        ),
        good_when_yes=False,
        on_fail="Check this is a dependency you meant to add.",
        # Asks whether a dependency arrived, which is a fact, not a fault. A
        # commit that adds one and says so is fine, and the question cannot be
        # reworded to "added without saying so" without the indirection that
        # Jev is documented as answering less reliably. So it reports.
        severity=REVIEW,
    ),
    JevRule(
        id="secret_material",
        title="credential in diff",
        instructions=(
            "Does `diff` add a real credential to a tracked file, such as an API "
            "key, access token, password, or private key?"
        ),
        true_means=(
            "An added line contains the credential value itself."
        ),
        false_means=(
            "No credential value is added. A placeholder such as "
            "'your-key-here', a variable name, a read from the environment, or an "
            "example in documentation does not count."
        ),
        good_when_yes=False,
        on_fail="Rotate the key, then take it out of the history.",
    ),
)

HEADLINE_OPTIONS: dict[str, str] = {
    "none": (
        "Nothing here needs a reviewer. The message is accurate and the diff "
        "holds together."
    ),
    "message_describes_diff": "The message does not describe what the diff does.",
    "single_purpose": "Unrelated changes are bundled into one commit.",
    "undisclosed_change": "The diff changes behaviour the message does not mention.",
    "unexplained_removal": "Something was deleted without a stated reason.",
    "debug_leftovers": "Debugging scaffolding was left in the code.",
    "new_dependency": "A new third-party dependency arrives unannounced.",
    "secret_material": "A credential is committed in the diff.",
}

HEADLINE_QUESTION = {
    "type": "choice",
    "instructions": (
        "Of these, which is the single biggest problem a reviewer would raise "
        "about this commit?"
    ),
    "criteria": HEADLINE_OPTIONS,
}


def questions() -> dict[str, dict]:
    """Every judgment for one commit, in one request."""
    qs = {rule.id: rule.question() for rule in JEV_RULES}
    qs["headline"] = HEADLINE_QUESTION
    return qs


# ---------------------------------------------------------------- code checks

MANIFESTS = (
    "package.json", "package-lock.json", "requirements.txt", "pyproject.toml",
    "Pipfile", "poetry.lock", "go.mod", "Cargo.toml", "Gemfile", "pom.xml",
    "build.gradle", "composer.json",
)

SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "secret key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), "GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*"
            r"['\"]([^'\"]{12,})['\"]"
        ),
        "assigned credential",
    ),
)

PLACEHOLDER = re.compile(
    r"(?i)(your[-_ ]?(key|token|secret)|example|placeholder|redacted|changeme"
    r"|xxx+|\.\.\.|<[^>]+>|\$\{?[A-Z_]+\}?|os\.environ|getenv|process\.env)"
)

EM_DASH = "—"
LARGE_FILE_LINES = 2000


@dataclass(frozen=True)
class CodeFinding:
    check: str
    verdict: str
    detail: str
    fix: str


def code_checks(commit) -> list[CodeFinding]:
    """Everything decided without asking the model."""
    findings: list[CodeFinding] = []

    if EM_DASH in commit.message:
        findings.append(CodeFinding(
            "em dash in message", WARN,
            "the commit message contains an em dash",
            "Use a comma or a colon, or restructure the sentence.",
        ))

    deleted = [f.path for f in commit.files if f.status.startswith("D")]
    if deleted:
        findings.append(CodeFinding(
            "file deleted", WARN,
            "deletes " + ", ".join(deleted[:3])
            + (f" and {len(deleted) - 3} more" if len(deleted) > 3 else ""),
            "Move or rename the file instead of deleting it.",
        ))

    manifests = [
        f.path for f in commit.files
        if f.path.rsplit("/", 1)[-1] in MANIFESTS and not f.status.startswith("D")
    ]
    if manifests:
        findings.append(CodeFinding(
            "manifest changed", REVIEW,
            "touches " + ", ".join(sorted(set(manifests))),
            "Check the dependency change is one you meant to make.",
        ))

    hits = _secret_hits(commit.added_lines)
    if hits:
        findings.append(CodeFinding(
            "credential pattern", WARN,
            "added line looks like a " + ", ".join(sorted(set(hits))),
            "Rotate the credential, then keep it out of the commit.",
        ))

    binaries = [f.path for f in commit.files if f.binary and not f.status.startswith("D")]
    if binaries:
        findings.append(CodeFinding(
            "binary file", REVIEW,
            "adds or changes " + ", ".join(binaries[:3]),
            "Confirm the binary belongs in the repository.",
        ))

    big = [
        f.path for f in commit.files
        if f.added is not None and f.added > LARGE_FILE_LINES
    ]
    if big:
        findings.append(CodeFinding(
            "large addition", REVIEW,
            ", ".join(f"{p} (+{LARGE_FILE_LINES}+ lines)" for p in big[:3]),
            "Confirm a generated or vendored file is meant to be here.",
        ))

    return findings


def _secret_hits(added: list[str]) -> list[str]:
    hits = []
    for line in added:
        if PLACEHOLDER.search(line):
            continue
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(line):
                hits.append(label)
                break
    return hits


# -------------------------------------------------------------------- policy


@dataclass
class RuleResult:
    rule: JevRule
    noul: float
    verdict: str


@dataclass
class CommitReport:
    commit: object
    results: list[RuleResult]
    findings: list[CodeFinding]
    headline: str | None
    headline_confidence: float
    skipped: str | None = None

    @property
    def verdict(self) -> str:
        if self.skipped:
            return OK
        worst = [r.verdict for r in self.results] + [f.verdict for f in self.findings]
        return max(worst, key=lambda v: RANK[v], default=OK)

    @property
    def flagged(self) -> list[RuleResult]:
        """Rules that did not pass, worst first, then furthest from the middle."""
        bad = [r for r in self.results if r.verdict != OK]
        return sorted(bad, key=lambda r: (-RANK[r.verdict], -abs(r.noul - 0.5)))

    @property
    def headline_text(self) -> str | None:
        if not self.headline or self.headline == "none":
            return None
        if self.headline_confidence < HEADLINE_CONFIDENCE:
            return None
        return HEADLINE_OPTIONS[self.headline]


def judge(commit, answers: dict) -> CommitReport:
    """Turn one Jev response into a verdict. No inference happens here."""
    results = []
    for rule in JEV_RULES:
        answer = answers.get(rule.id)
        if answer is None:
            continue
        noul = float(answer["noul"])
        results.append(RuleResult(rule, noul, rule.verdict(noul)))

    headline = answers.get("headline") or {}
    return CommitReport(
        commit=commit,
        results=results,
        findings=code_checks(commit),
        headline=headline.get("choice"),
        headline_confidence=float(headline.get("confidence", 0.0)),
    )
