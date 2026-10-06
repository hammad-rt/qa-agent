# Redmine Test Agent: Requirements Document (RDP)

Status: draft for review, v1.3 (rebuilt 6 Oct 2026 after the original folder was lost; adds gap resolution). Nothing built. Owner: Sidney Karanja.
Related: `test-agent-technical-spec.md` (architecture), `core-testing-spec.md` (shared testing rules), `packs/savostore/facts.md` (first project pack).

Version history: v1 (1 Oct) first draft · v1.1 (1 Oct) real-run review: pre-approval, irreversible ordering, AGENT INCOMPLETE, payments scope, speed target, known bugs, replay test · v1.2 (1 Oct) multi-agent roles, judge action log · v1.3 (6 Oct) gap resolution.

---

## 1. Problem
Manual QA of Redmine test tasks is slow and repetitive: read the parent issue, the "Test Cases" child and every comment, work out missing preconditions, find URLs and accounts, click through two apps, record numbers before and after, screenshot, judge each case, and write a PDF report with bug drafts. The same UI quirks, "intended behavior" questions and "where is staging / which login" questions return every time, and that knowledge lives in people's heads.

## 2. Goal
A local agent that takes a Redmine task ID from any onboarded project and runs its test cases end to end on staging, producing an evidence-backed report. It finds what it's missing, asks only for what it can't find, and gets better every run by remembering what it learned.

## 3. Users
| User | Needs |
|---|---|
| QA / developer running tests (Sidney first) | Give an issue ID, answer one batch of questions, review a plan, say "go", get a trustworthy report |
| Project lead | Read the report and bug drafts, decide what goes to Redmine |
| Whoever onboards a new project | Create a project pack without touching the engine |

## 4. Scope
**In scope (v1)**
- Redmine-driven test tasks; web UI testing via a browser; one or more apps per project (e.g. portal + CRM).
- Staging by default; prod read-only and only when approved per run.
- Staging test-card payments (e.g. Stripe 4242) with approval.
- Savostore first; at least one more project onboarded to prove reuse.
- Local run on the user's laptop with the Rolustech-hosted LLM.

**Out of scope (v1)**
- Mobile apps, API/load/performance testing.
- Payment gateways the UI can't exercise (e.g. M-Pesa): marked NOT VERIFIED / SKIPPED.
- Posting to Redmine or emailing anyone without approval; fixing bugs or code; parallel test runs.

## 5. Functional requirements

### Intake and planning
| ID | Requirement | Priority |
|---|---|---|
| FR-1 | Fetch a Redmine issue with children, comments and attachments, plus its "Test Cases" child, using fixed code | Must |
| FR-2 | Pick the project pack from the Redmine project ID; if none matches, stop and offer onboarding | Must |
| FR-3 | Check the feature is deployed on the target environment before testing; stop with a clear message if not | Must |
| FR-4 | Structured plan per case: type, steps, expected result, preconditions, dependencies, evidence needed, approval needed | Must |
| FR-5 | Infer missing steps and preconditions from the expected result, pack facts, memory and earlier cases; mark each `inferred` with reason and source | Must |
| FR-6 | Flag conflicts between the parent task, test-case child, comments and live configuration | Must |
| FR-7 | A reviewer pass checks the plan before the user sees it | Should |
| FR-8 | Never start testing until the user says "go" | Must |
| FR-8a | Run-level pre-approval of action types at the plan gate (e.g. pay staging orders with the test card, move orders to delivered), for that run only. Settings changes, Redmine posts, emails and prod writes always need one-off approval | Must |
| FR-8b | Irreversible steps (e.g. opening checkout, which commits rewards; paying, which closes an order to further tests) run last on a given record, or use a fresh record | Must |

### Gap resolution
| ID | Requirement | Priority |
|---|---|---|
| FR-40 | During planning, list every resource the cases need: environment URLs, accounts and roles, test data, config values | Must |
| FR-41 | Look each one up in a fixed order: project pack, project memory, Redmine wiki pages named in the pack (then wiki search), the task's description, comments and attachments; ask the user only for what's still missing | Must |
| FR-42 | Check what it finds, not just that it exists: URL reachable and in the allowlist, login works, account has the role the cases need, test data in the needed state. A found value that falls short becomes a question | Must |
| FR-43 | Ask all open questions in one batch at the plan gate, each with why it's needed, which cases depend on it, and what was found. Mid-run questions only for gaps that couldn't be known earlier | Must |
| FR-44 | Never accept passwords as chat text: use masked input, store the secret in `.env` (or keyring), and keep only its name in memory and reports | Must |
| FR-45 | Save a confirmed answer to project memory with its source once it passes its check, so it isn't asked again; re-ask if it later fails | Must |
| FR-46 | Cases that depend on an unanswered resource are SKIPPED or BLOCKED with "missing: <resource>"; independent cases still run | Should |

### Execution
| ID | Requirement | Priority |
|---|---|---|
| FR-9 | Log in with confirmed accounts; never log out of shared sessions | Must |
| FR-10 | Baseline snapshot of relevant values and settings before the first case | Must |
| FR-11 | One case at a time; project helpers first, free browser navigation when needed | Must |
| FR-12 | Record every number from the page; all math in code | Must |
| FR-13 | Screenshots named `<case>-<nn>-<what>.png`, each with a masked page-text snapshot; check each shows what the verdict claims | Must |
| FR-14 | Approval before any step that changes data, moves money, changes settings, posts or emails, including inferred steps, unless covered by FR-8a | Must |
| FR-15 | Admin-config cases record, restore and verify the original value | Must |
| FR-16 | Track every record created, with final state and run-ID tag | Must |
| FR-17 | Per-case step and time limits; on hitting one, mark AGENT INCOMPLETE (not BLOCKED, which means the app prevents the test); excluded from bug counts | Must |
| FR-18 | Resume an interrupted run from the first unfinished case; if the LLM endpoint stays down, pause there | Should |

### Verdicts and reporting
| ID | Requirement | Priority |
|---|---|---|
| FR-19 | Verdicts: PASS, FAIL, PARTIAL, BLOCKED, NOT VERIFIED, SKIPPED, AGENT INCOMPLETE, with a note on every non-pass | Must |
| FR-20 | Use project memory to tell intended behavior from bugs, and say which lesson was applied | Must |
| FR-21 | HTML-to-PDF report: summary, per-case steps, expected vs actual, screenshots, notes, baseline vs final, leftover data, questions asked and answers used, model used | Must |
| FR-22 | Bug drafts in the core format, never posted without approval | Must |
| FR-22a | A failure matching a known open bug is reported as "still present: bug N", not drafted as new | Must |
| FR-23 | Optionally email the report and milestone updates to the user's own inbox only | Should |

### Modes
| ID | Requirement | Priority |
|---|---|---|
| FR-24 | Full run, rerun selected cases, retest cases linked to a fixed bug, plan-only dry run (includes the question list) | Must |
| FR-25 | Onboard a new project: draft `pack.yaml` and `facts.md` from Redmine, its wiki and one batch of user answers, for approval | Should |

### Learning
| ID | Requirement | Priority |
|---|---|---|
| FR-26 | Save user corrections immediately | Must |
| FR-27 | Propose lessons, UI recipes and open bugs at end of run; save only approved ones | Must |
| FR-28 | Save to the current project by default; never load another project's memory | Must |
| FR-29 | Promote to the shared core only when the user marks it general or it appears in a second project, with approval | Should |
| FR-30 | Flag lessons for re-check when their linked bug closes, recipes that fail twice, and saved resources that fail their check | Should |
| FR-31 | Turn recipes that keep working into fixed helpers | Could |

### Agent roles
| ID | Requirement | Priority |
|---|---|---|
| FR-32 | Role agents: planner, gap resolver, reviewer, coordinator, one tester per app, judge, reporter, memory curator | Must |
| FR-33 | Test cases strictly one at a time; only read-only work runs in parallel | Must |
| FR-34 | The coordinator is fixed code holding the plan and run state | Must |
| FR-35 | Each tester drives only its own app and session and returns values, screenshots and snapshots; raw page content stays out of the coordinator's context | Must |
| FR-36 | The judge decides from evidence, memory and a code-built action log (steps run, `inferred` flags, approvals, deviations), never the tester's reasoning. A missing required step means AGENT INCOMPLETE, not FAIL | Must |
| FR-37 | The reporter writes notes and drafts in the background | Should |
| FR-38 | The memory curator proposes lessons, recipes, known bugs, confirmed answers and promotions at end of run | Should |
| FR-39 | Cap parallel LLM calls until the endpoint's rate limits are known | Must |

## 6. Non-functional requirements
| ID | Requirement |
|---|---|
| NFR-1 Accuracy | No invented numbers; every figure traceable to a page read or screenshot |
| NFR-2 Safety | Allowlist and risky-action rules in code; page, wiki and Redmine text treated as data; URLs found outside the pack used only after user confirmation |
| NFR-3 Privacy | No passwords in prompts, memory, reports, screenshots, traces or chat; card numbers, emails and phones masked before the LLM |
| NFR-4 Footprint | Runs on the i7 laptop with about 4 GB free RAM; LLM remote; only `nomic-embed-text` local |
| NFR-5 Disk | Keep last 5 runs or 14 days; warn below 5 GB free |
| NFR-6 Traceability | Every model and tool step logged; model id in the report |
| NFR-7 Reliability | Retries with backoff; crash-safe resume; no local fallback model |
| NFR-8 Extensibility | A new project needs only a pack, no engine changes |
| NFR-9 Speed | Under 10 minutes per typical UI case; data-setup cases excluded and timed separately |
| NFR-10 Durability | Specs, packs and memory backed up after every change (git repo or laptop copy) |

## 7. Acceptance criteria (v1)
1. #180966 TC3 on Savostore staging runs end to end, stops before submit, PASS with correct evidence.
2. Replay eval on #165177 / #165348 using screenshots and values re-extracted from the surviving report PDFs (no page text in this first set): at least 90% verdict agreement with ground truth including user corrections (bug 2 real; "new unpaid shipping only" intended), and no known FAIL reported as PASS.
3. No data-changing step happens without a logged approval or matching pre-approval.
4. A Savostore-only lesson is never loaded in another project's run.
5. A correction in run N changes the verdict in run N+1 for the same situation.
6. A second project is onboarded and runs at least one case without engine changes.
7. A repeat known bug is reported as "still present: bug N", not drafted as new.
8. A pre-approved action type runs without prompting; an action outside it still prompts.
9. No two testers act at the same time, and the judge's input has no tester reasoning (checked from the trace).
10. With staging URLs removed from the pack and memory, the agent finds them on the Redmine wiki, checks them, lists them for confirmation in one batch with any other gaps, and doesn't ask again on the next run.
11. A found account without the needed role (e.g. a non-admin CRM login for admin cases) is raised as a question before testing, not reported as a BLOCKED case.
12. No password appears in chat, memory, reports or traces after a run that asked for one.

## 8. Success metrics
Time per task vs manual; verdict agreement with human review; user override rate (falling); questions asked per run (falling as memory fills); unnecessary approval prompts; lessons reused.

## 9. Milestones
As in the technical spec: (1) skeleton, gap resolver and TC3 trial, (2) memory, (3) safety and reliability, (4) report and modes, (5) replay self-test, (6) helpers, (7) second project. Roughly 7–10 working days.

## 10. Risks
| Risk | Mitigation |
|---|---|
| Wrong verdict | Evidence check, action log, replay evals, human review |
| Changes shared staging data | Approvals, run-ID tags, leftover list, cleanup offer |
| Wrong lesson or stale answer becomes a habit | Approval, scoping, staleness and check-on-use |
| Wiki holds wrong or outdated values | Check every found value; user confirms at the gate |
| Credentials leak | Masked input, `.env` only, redaction, never stored in memory |
| LLM endpoint down | Retries, then pause and resume |
| Page content tries to steer the agent | Code-enforced policy and allowlist |
| Laptop disk or RAM | Retention, remote LLM, one run at a time |
| Docs or memory lost again | Backup after every change (NFR-10) |

## 11. Open questions
- Rate limits on `ai.rolustech.com`, and approval to send masked staging content there?
- Interface: CLI chat only, or a small local web page too?
- Milestone emails, or only the final report?
- Is prod allowed in v1?
- Which project is second?
- Backup location: GitLab repo on `git.rolustech.com`, or a laptop folder?
