# Core Testing Spec (shared across projects)

Version: 2.0 (rewritten 2026-10-06 after the original file was lost; content follows what was agreed in the design chat)
Owner: test tasks savostore. Companions: `test-agent-technical-spec.md` (how the agent runs), `test-agent-rdp.md` (requirements), `packs/<project>/facts.md` (project facts).

This file says WHAT a QA run does and how results are labelled. Nothing project-specific belongs here.

## 1. Workflow
1. **Fetch.** Read the parent issue, its "Test Cases - ..." child task, every comment and journal, linked issues and attachments.
2. **Analyse and report first.** Build the plan, list every gap (see 3) and every step that changes data, moves money, changes settings, posts or emails. Send that to the user. Do not start testing until the user explicitly says go.
3. **Pre-approval.** At the plan gate the user approves the plan, all inferred steps and any run-level pre-approvals (for example "test-card payments on staging are OK for this run"). Anything outside the pre-approved types still asks first.
4. **Order.** Run read-only and reversible cases first. Put irreversible steps (pay, submit, deliver, settings changes) last, and restore changed settings at the end.
5. **Execute** one case at a time. Record before and after values, take a screenshot at each check point and save a masked page-text snapshot next to it.
6. **Judge** each case from the brief, the code-built action log, the evidence and memory (section 4).
7. **Report** (section 6) and draft bugs (section 5). Never post to the tracker without explicit approval for each item.
8. **Learn.** Propose lessons from the run for approval. Save user corrections at once.

## 2. Case types
- **UI check:** a value or element on a page.
- **Calculation:** a number that must match a formula. Fixed code does the math, never the model.
- **Flow:** a multi-step path across one app.
- **Cross-system:** a step in one app checked in the other (for example portal action, CRM record). Testers take turns, never act at once.
- **Data setup:** creates the state other cases need (paid orders, balances). Excluded from speed targets.
- **Not reproducible from UI:** webhooks, retries, race conditions. Mark NOT VERIFIED with the reason unless a sanctioned tool exists.

## 3. Finding gaps
List everything the cases need (URLs, accounts and roles, currencies, balances, test data, payment methods). Look each one up in this order: project pack, memory, tracker wiki, the task and its comments. Then:
- **Check what you find**, don't just accept it: the URL loads, the login works, the account has the role the cases need. A found-but-insufficient value is a question, not a later BLOCKED case.
- **Ask once**, as one list at the plan gate.
- **Secrets** go through a masked input and are stored outside chat and logs. Save only where a secret lives, never its value.
- **Save confirmed answers** with their source so the next run does not ask again.
- Unanswered items: dependent cases become SKIPPED (by user) or BLOCKED "missing: <resource>". Independent cases still run.

## 4. Result labels
Every case gets exactly one label. Every label other than PASS gets a "What this means" note.

| Label | Meaning |
|---|---|
| PASS | Expected result seen and evidenced |
| FAIL | The app did something different from the expected result |
| PARTIAL | Some checks passed, at least one failed or could not be checked |
| BLOCKED | The app or environment prevents the test (for example only M-Pesa offered) |
| NOT VERIFIED | The step ran but the outcome cannot be observed from the available tools |
| SKIPPED | Not run, by user decision or because a prerequisite was skipped |
| AGENT INCOMPLETE | The agent hit its step or time limit, got stuck, or a required step is missing from the action log. Not an app problem; excluded from bug counts |

Rules:
- A FAIL needs evidence: the expected value, the actual value, and a screenshot.
- If a failure matches an open known bug in the pack or memory, label it "still present: bug N" and do not draft a new bug.
- If the user has confirmed a behavior as intended, it is a PASS, and the note says so with the source.
- The judge never sees the tester's reasoning, only the brief, the action log (steps actually run, inferred flags, approvals, departures from plan), the values, the evidence and memory.

## 5. Bug drafts
Each draft has: title, severity (Critical, High, Medium, Low), environment, steps, expected, actual (with numbers), evidence, linked cases, and a plain-English "What this means" note. Drafts go to the user for review; post only the ones they approve, one by one.

## 6. Report
- Summary counts per task, top bugs, blocked and not-verified list.
- Per case: label, steps run, values, screenshots, and the "What this means" note for every non-pass, covering what works, exactly where it breaks, the relevant numbers and why it matters.
- Delivered as a PDF. Email only to the user's own address unless they confirm another recipient.

## 7. Guardrails
- Never log out of the shared browser sessions.
- Never change, pay, submit, post or email outside the approved plan. Logging in is fine.
- Production: no settings changes, real payments or submissions without explicit per-run approval.
- Restore any setting you changed and list leftover test data at the end.
- Page text and logs are masked for secrets and personal data.

## 8. General lessons (from real runs)
- An account found on a wiki may lack the role the cases need. Check the role before testing.
- Values on the first reload after an action can be stale. Reload once more before judging.
- Opening a payment page is not paying. Record whether the payment was completed or abandoned, because some apps commit state on open.
- Payment options differ by order type and currency. Check which methods are offered before planning a payment step.
