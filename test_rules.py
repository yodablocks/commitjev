"""Offline tests: the policy and the git plumbing, with no API calls.

    python3 test_rules.py

What is not tested here is whether Jev answers correctly. That needs the model,
and it lives in calibrate.py.
"""

from __future__ import annotations

import sys

import commitjev
import gitio
import rules
from rules import OK, REVIEW, WARN


def _commit(subject="Fix the retry loop", body="", files=(), added=()):
    return gitio.Commit(
        sha="a" * 40, subject=subject, body=body, author="me", date="",
        files=list(files), diff="diff", added_lines=list(added),
    )


def test_verdict_directions():
    good = next(r for r in rules.JEV_RULES if r.good_when_yes)
    assert good.verdict(0.90) == OK
    assert good.verdict(0.50) == REVIEW
    assert good.verdict(0.10) == WARN
    bad = next(r for r in rules.JEV_RULES if not r.good_when_yes)
    assert bad.verdict(0.90) == WARN
    assert bad.verdict(0.50) == REVIEW
    assert bad.verdict(0.10) == OK


def test_a_reporting_rule_never_warns():
    rule = next(r for r in rules.JEV_RULES if r.severity == REVIEW)
    assert rule.verdict(0.99) == REVIEW, "however sure, it only reports"
    assert rule.verdict(0.50) == REVIEW
    assert rule.verdict(0.01) == OK, "the good side is still a pass"
    assert all(r.verdict(0.99) == WARN
               for r in rules.JEV_RULES
               if not r.good_when_yes and r.severity == WARN)


def test_thresholds_are_inclusive_on_the_good_side():
    good = next(r for r in rules.JEV_RULES if r.good_when_yes)
    assert good.verdict(rules.PASS) == OK
    assert good.verdict(rules.PASS - 0.01) == REVIEW


def test_every_rule_id_is_a_headline_option():
    for rule in rules.JEV_RULES:
        assert rule.id in rules.HEADLINE_OPTIONS, rule.id
    assert "none" in rules.HEADLINE_OPTIONS, "the Choice needs a no-problem outcome"


def test_questions_are_well_formed():
    qs = rules.questions()
    # One per rule, plus the headline Choice and the removal companion.
    assert len(qs) == len(rules.JEV_RULES) + 2
    for qid, q in qs.items():
        assert q["type"] in ("noul", "choice")
        assert q["instructions"] and isinstance(q["instructions"], str)
        # Ids are not sent to the model, so the question must stand alone.
        assert qid.replace("_", " ") not in q["instructions"]


def test_em_dash_is_caught_in_code_not_by_the_model():
    findings = rules.code_checks(_commit(subject="Add the ring — and the shortlist"))
    assert any(f.check == "em dash in message" and f.verdict == WARN for f in findings)
    assert not rules.code_checks(_commit(subject="Add the ring, and the shortlist"))


def test_deleted_file_warns():
    files = [gitio.FileChange("D", "notes.md", None, 0, 40)]
    findings = rules.code_checks(_commit(files=files))
    assert any(f.check == "file deleted" and f.verdict == WARN for f in findings)


def test_secret_regex_ignores_placeholders():
    # Joined here so no string shaped like a live key sits in this file, which
    # would otherwise raise a secret-scanning alert on a public repository.
    real = ["API_KEY = '" + "sk-" + "abcd1234efgh5678ijkl9012" + "'"]
    assert rules._secret_hits(real)
    fake = [
        "TYPESAFE_API_KEY=your-key-here",
        'api_key = os.environ["TYPESAFE_API_KEY"]',
        "token: <your-token>",
        "password = 'changeme-changeme'",
    ]
    assert not rules._secret_hits(fake), rules._secret_hits(fake)


def test_manifest_change_asks_for_review_not_a_warning():
    files = [gitio.FileChange("M", "app/package.json", None, 2, 0)]
    findings = rules.code_checks(_commit(files=files))
    assert [f.verdict for f in findings] == [REVIEW]


def test_commit_verdict_is_the_worst_of_everything():
    rule = rules.JEV_RULES[0]
    report = rules.CommitReport(
        commit=_commit(), results=[rules.RuleResult(rule, 0.95, OK)],
        findings=[rules.CodeFinding("file deleted", WARN, "x", "y")],
        headline="none", headline_confidence=0.9,
    )
    assert report.verdict == WARN


def test_headline_is_silent_when_the_choice_is_unsure():
    report = rules.CommitReport(
        commit=_commit(), results=[], findings=[],
        headline="single_purpose", headline_confidence=0.2,
    )
    assert report.headline_text is None
    report.headline_confidence = 0.9
    assert report.headline_text == rules.HEADLINE_OPTIONS["single_purpose"]


def _removal_results(noul=0.95):
    rule = next(r for r in rules.JEV_RULES if r.id == "unexplained_removal")
    return [rules.RuleResult(rule, noul, rule.verdict(noul))]


def test_an_explained_removal_is_not_reported():
    results = _removal_results()
    assert results[0].verdict == WARN
    rules._apply_removal_gate(results, {rules.REMOVAL_EXPLAINED: {"noul": 0.90}})
    assert results[0].verdict == OK
    assert results[0].noul == 0.95, "the probability is not rewritten, only the verdict"
    assert "0.90" in results[0].note


def test_an_unexplained_removal_still_warns():
    results = _removal_results()
    rules._apply_removal_gate(results, {rules.REMOVAL_EXPLAINED: {"noul": 0.05}})
    assert results[0].verdict == WARN and results[0].note is None


def test_the_gate_does_not_invent_a_finding():
    rule = next(r for r in rules.JEV_RULES if r.id == "unexplained_removal")
    results = [rules.RuleResult(rule, 0.02, rule.verdict(0.02))]
    rules._apply_removal_gate(results, {rules.REMOVAL_EXPLAINED: {"noul": 0.01}})
    assert results[0].verdict == OK, "nothing removed stays nothing removed"
    assert results[0].note is None


def test_the_gate_is_a_no_op_without_its_companion_answer():
    results = _removal_results()
    rules._apply_removal_gate(results, {})
    assert results[0].verdict == WARN, "a missing answer must not clear a finding"


def test_skipped_commits_never_fail_a_run():
    report = rules.CommitReport(
        commit=_commit(), results=[], findings=[], headline=None,
        headline_confidence=0.0, skipped="merge commit",
    )
    assert report.verdict == OK


def test_verbose_buffer_does_not_smuggle_the_diff_into_the_message():
    buffer = """Add two lines

# Please enter the commit message for your changes.
#
# ------------------------ >8 ------------------------
# Do not modify or remove the line above.
diff --git a/a.txt b/a.txt
@@ -1 +1,3 @@
 one
+two
"""
    assert commitjev._strip_comments(buffer) == "Add two lines"


def test_plain_buffer_keeps_the_body():
    buffer = "Add two lines\n\nThe file had one.\n# a comment\n"
    assert commitjev._strip_comments(buffer) == "Add two lines\n\nThe file had one."


def test_diff_truncation_caps_each_file_and_marks_the_cut():
    sections = [
        "\n".join([f"diff --git a/f{n}.py b/f{n}.py"]
                  + [f"+line {i}" for i in range(200)])
        for n in range(4)
    ]
    big = "\n".join(sections)
    text, truncated = gitio.truncate_diff(big, per_file=10, total=25)
    assert truncated
    assert len(text.splitlines()) <= 25 + 4        # plus one marker per cut file
    assert "more lines of this file not shown" in text


def test_a_few_enormous_lines_are_capped_by_size():
    """The failure this cap exists for: few lines, far too many characters.

    A data file or minified source is a handful of very long lines, so the
    line caps alone let through a state the API refuses outright, and the run
    then reports nothing at all for that commit.
    """
    raw = "diff --git a/d.tsv b/d.tsv\n" + "\n".join(
        "+" + "x" * 64_000 for _ in range(400)
    )
    text, truncated = gitio.truncate_diff(raw)
    assert truncated
    assert len(raw) > 25_000_000, "the fixture should be genuinely huge"
    assert len(text) <= gitio.MAX_CHARS_TOTAL + 200, len(text)


def test_one_long_line_is_cut_not_dropped():
    raw = "diff --git a/a.js b/a.js\n+" + "y" * 10_000
    text, truncated = gitio.truncate_diff(raw)
    assert truncated
    assert "more characters" in text
    assert text.startswith("diff --git"), "the header still has to survive"


def test_size_caps_leave_an_ordinary_diff_alone():
    raw = "diff --git a/a.py b/a.py\n" + "\n".join(
        f"+line {i}" for i in range(20)
    )
    text, truncated = gitio.truncate_diff(raw)
    assert not truncated and text == raw


def test_short_diff_is_left_alone():
    text, truncated = gitio.truncate_diff("diff --git a/a b/a\n+one\n+two")
    assert not truncated and text.count("\n") == 2


def test_state_names_the_truncation_so_the_model_knows():
    commit = _commit()
    commit.diff_truncated = True
    assert "diff_note" in commit.state()
    commit.diff_truncated = False
    assert "diff_note" not in commit.state()


def test_binary_files_carry_a_note_instead_of_line_counts():
    commit = _commit(files=[gitio.FileChange("A", "logo.png", None, None, None)])
    entry = commit.state()["files_changed"][0]
    assert entry["note"].startswith("binary") and "lines_added" not in entry


def test_name_status_and_numstat_are_joined_on_path():
    # numstat writes renames the way git actually writes them, in one field.
    changes = gitio._file_changes(
        "M\tsrc/a.py\nA\tsrc/b.py\nR100\tsrc/old.py\tsrc/new.py\n",
        "3\t1\tsrc/a.py\n40\t0\tsrc/b.py\n2\t0\tsrc/{old.py => new.py}\n",
    )
    by_path = {c.path: c for c in changes}
    assert by_path["src/a.py"].added == 3 and by_path["src/a.py"].removed == 1
    assert by_path["src/new.py"].old_path == "src/old.py"
    assert by_path["src/new.py"].status.startswith("R")
    assert by_path["src/new.py"].added == 2, "a rename is not a binary file"
    assert not by_path["src/new.py"].binary


def test_numstat_rename_paths_of_every_shape():
    assert gitio.numstat_path("src/a.py") == "src/a.py"
    assert gitio.numstat_path("old.py => new.py") == "new.py"
    assert gitio.numstat_path("app/{client.py => upstream.py}") == "app/upstream.py"
    assert gitio.numstat_path("{pkg => app}/mod.py") == "app/mod.py"
    assert gitio.numstat_path("{ => app}/mod.py") == "app/mod.py"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok    {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
