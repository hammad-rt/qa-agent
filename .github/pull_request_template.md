## Summary

<!-- What this PR changes and why (one concern only). -->

## Diff scope

```text
git diff --stat origin/fixes...HEAD
```

<!-- Paste the stat. Confirm only task-needed files. -->

## Regression audit

| Behaviour / element | Touched? | Old behaviour preserved? | Covered by |
|---|---|---|---|
| Classic tabs under run title | | | |
| Always-visible guidance (`#guideIn`) | | | |
| Equal card heights / in-card icons | | | |
| Test-user ask each run | | | |
| Live polling focus / typed text | | | |
| Ask shows outcome / doing-now / need | | | |
| Stuck ask @3 + ladder; Continue/Skip/Stop | | | |
| Guidance after go changes next action | | | |
| Discovery read-only (no submit) | | | |
| Read-only from cases-file heading | | | |
| PREP create approval | | | |
| TC0 Quick Repair (CRM packs) | | | |
| App-agnostic core / pack isolation | | | |
| Env-bound identity ban | | | |
| CLI flags / env switches touched | | | |

## Protected behaviours touched

<!-- List ids from protected-behaviours.mdc that this PR intentionally changes. -->

## Env-leak check

<!-- CLEAN / LEAKS. Include fixtures scan. -->

## Tests

```bash
REPORT_PDF=0 QA_TEST=1 QA_INTERACTIVE=0 python3 -m unittest discover -s tests -t .
REPORT_PDF=0 QA_TEST=1 QA_INTERACTIVE=1 python3 -m unittest discover -s tests -t .
```

<!-- Paste Ran N / OK for both. -->

Leave unmerged until laptop verification.
