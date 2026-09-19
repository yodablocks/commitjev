# commitjev

Reads a commit the way a reviewer would, before a reviewer has to. It says
whether the message describes the diff, whether the edits belong in one
commit, and what the message leaves out. Built on TypeSafe's Jev model.

```
python3 commitjev.py HEAD~10..HEAD        # judge a range
python3 commitjev.py --staged             # judge what you are about to commit
python3 commitjev.py HEAD --markdown      # paste into a pull request
python3 calibrate.py                      # measure what it catches
python3 calibrate.py --repeat 4           # measure how much the answers move
```

Point it at any repository with `-C`, or install it as a commit-msg hook so
it runs on the commit you are about to make:

```
cp hooks/commit-msg /your/project/.git/hooks/commit-msg
chmod +x /your/project/.git/hooks/commit-msg
export COMMITJEV=$PWD/commitjev.py
```

Needs Python 3.12, `typesafe_sdk`, and a key in `TYPESAFE_API_KEY` (or `.env`,
see `.env.example`). Exit status is 0 when nothing warns, 1 when something
does, 2 when the run itself failed. The hook blocks only on a warning: not
being able to check a commit is not a reason to refuse it.

## How it works

One Jev request per commit. The state is the message, the list of changed
files, and the diff:

```json
{"commit_message": {"subject": "...", "body": "..."},
 "files_changed": [{"path": "app/client.py", "change": "modified",
                    "lines_added": 12, "lines_removed": 3}],
 "diff": "..."}
```

Seven Nouls and one Choice ride in that single request and are evaluated in
parallel (`rules.py`):

| id | good when | judgment |
|---|---|---|
| `message_describes_diff` | yes | would the message give you the right idea of what changed |
| `single_purpose` | yes | do all the edits serve one purpose |
| `undisclosed_change` | no | does the diff change behaviour the message never mentions |
| `unexplained_removal` | no | is working code gone with no reason given |
| `debug_leftovers` | no | is there a stray print, a commented-out block, a hardcoded test value |
| `new_dependency` | no | does the project now depend on a package it did not before |
| `secret_material` | no | is a real credential in the diff |
| `headline` | Choice | of these, which one would a reviewer actually raise |

Six more checks never reach the model, because a regex already settles them:
an em dash in the message, a deleted file, a touched dependency manifest, a
credential pattern in an added line, a binary file, an addition over 2000
lines. Jev cannot count and is weak on literal string matching, so asking it
those questions would only add noise and cost.

Code owns every threshold. A Noul at or past 0.65 on its good side passes; at
or past 0.65 on its bad side warns; **anything in between is reported as
"review" rather than rounded to a verdict**, because that band is a judgment
the model did not actually make. The Choice only picks the line shown first,
and only when its confidence clears 0.50. The two are never mixed: Jev's own
notes say a Choice is relative (which problem is worst) while each Noul is
absolute (is this problem there at all), so the Nouls decide every verdict.

Answers are cached in SQLite under `.commitjev/`. A commit is immutable and so
are the rules, so re-running a range costs nothing and returns instantly. That
is what makes the thresholds cheap to tune: edit `PASS`, re-run, watch the
report move. Editing a rule's wording changes the key and correctly re-asks.

## What it catches

`calibrate.py` builds a throwaway repository, branches thirteen labelled
commits off one base, and runs the real pipeline over them. Eight carry
exactly one planted defect and five are clean. Only the clean column measures
a false alarm.

```
rule                      own defect  other defects   clean  margin  fires
--------------------------------------------------------------------------
message matches diff       0.02 0.04           0.23    0.95    0.93  yes
one logical change              0.16           0.17    0.95    0.79  yes
hidden change                   0.96           0.98    0.12    0.84  yes
unexplained removal             0.76           0.83    0.07    0.69  yes
debug leftovers                 0.97           0.47    0.04    0.93  yes
new dependency                  0.98           0.07    0.06    0.92  yes
credential in diff              0.96           0.03    0.03    0.93  yes
```

Every rule fires on its defect, with 0.69 to 0.93 of margin against the clean
commits, and nothing fires on a clean one. Two of the five clean cases carry a
one-line message over the same diff as a longer one, because a short accurate
message is the commonest real commit and the likeliest false alarm for the
message rule. Both pass.

The Choice names the planted defect on six of the eight defective commits and
says "none" on all five clean ones at 0.91 confidence and above.

## A run over real history

Its own nine commits, `jev-1.13.0`, 8 workers:

| commits | warnings | to review | time | cost |
|---|---|---|---|---|
| 9 | 3 | 1 | 3.0 s | $0.0011 |

Two of those warnings are `single_purpose` on commits that really did two
things: "Record that Jev's answers move between runs" also corrected a
filename in the same README, and "Keep the diff out of the message" also fixed
a stale sentence. They score 0.31 and 0.19. Both should have been two commits,
and the tool is right about both.

The third warning is a false positive worth understanding, and it has its own
entry below.

## What it is not

- **Not a precise diagnosis.** The Nouls each answer in isolation, so one real
  problem trips several. The commit with a deliberately wrong message trips
  four rules. Read the report as how many ways a commit is off, not as four
  separate findings, and read the headline Choice for the one that matters.

- **`unexplained_removal` misreads a changed constant as a deletion.** On the
  wrong-message case, which changes two constants and removes nothing, it
  answers 0.83. Jev is documented as reading literally, and an old value
  vanishing from a line apparently looks enough like a removal. Treat that
  rule as the weakest of the seven.

- **`new_dependency` fires whether or not the message names the dependency.**
  The first commit here adds `typesafe_sdk` and its message says so in as many
  words, and the rule still answers 0.77. The question it asks is "does this
  add a dependency", which is honestly yes; the fix text it prints is about
  disclosure, which is a different question. Read it as a notification, not a
  violation, or reword the rule so it asks what the fix text implies.

- **The synthetic defects are easier than real ones, by an amount that
  varies.** The planted bundled commit scores 0.16 on `single_purpose`, and
  the two genuinely bundled commits in this repository score 0.19 and 0.31,
  which transfers well. But a bundled commit in another repository, whose
  subject joined two changes with an "and", scored 0.66 and passed by a
  hundredth. The calibration margins are an upper bound, not a floor.

- **Five clean commits is a small control group.** Zero false alarms across
  five hand-written commits is weak evidence. Run `calibrate.py` with cases of
  your own before trusting the thresholds on a codebase that matters.

- **Large commits are judged on a fraction of themselves.** The diff is capped
  at 120 lines per file and 400 overall, because Jev loses accuracy as the
  state fills with detail. The commit that added this tool was 1363 lines, so
  under a third of it reached the model. The state says when it was truncated,
  but a rule cannot see what was cut.

- **Jev is not deterministic, and the cache freezes whichever answer came
  first.** Over four runs of the thirteen calibration cases the spread per
  rule is 0.01 to 0.09, and two case-and-rule pairs change verdict between
  runs, both of them `unexplained_removal`. On the much larger commit that
  added this tool, `new_dependency` ranged 0.58 to 0.76 across six runs of
  byte-identical state, straddling the threshold, so that commit warns on some
  runs and passes on others. Variance looks worse on large truncated diffs
  than on small focused ones. `calibrate.py --repeat N` measures it.

- **The credential regex cannot tell a fixture from a key.** It fires on the
  credential-shaped test data in `test_rules.py` and `cases.py`, correctly by
  its own lights. That is the right failure direction for a credential check,
  so it is left alone rather than weakened to quiet the tool's own tests.

- **Your diffs go to TypeSafe.** Read their data terms before pointing this at
  a private repository.

## Files

- `commitjev.py` the CLI and the pipeline
- `rules.py` the seven judgments, the six code checks, and the thresholds
- `gitio.py` reading commits out of git and capping the diff
- `client.py` the SDK wrapper, the SQLite cache, the thread pool
- `report.py` terminal, markdown, and JSON output
- `cases.py` thirteen labelled commits, `calibrate.py` the measurement
- `test_rules.py` offline tests, `python3 test_rules.py`
- `hooks/commit-msg` a sample hook, copy it in yourself
