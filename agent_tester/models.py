"""Typed data passed between agents and fixed code (tech spec section 5)."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["PASS", "FAIL", "PARTIAL", "BLOCKED", "NOT VERIFIED", "SKIPPED", "AGENT INCOMPLETE"]


class CaseType(str, Enum):
    UI = "ui"
    CROSS_SYSTEM = "cross"
    NUMERIC = "numeric"
    DATA_SETUP = "setup"
    ADMIN_CONFIG = "admin"
    NOT_UI = "not_ui"


class Step(BaseModel):
    text: str
    app: str = "none"                 # app key from the pack, or "redmine" / "none"
    changes_data: bool = False
    costs_money: bool = False
    irreversible: bool = False
    action_type: str | None = None
    needs_approval: bool = False
    inferred: bool = False
    inferred_reason: str | None = None
    inferred_source: str | None = None
    restores: bool = False            # marks the restore step of an admin case
    record: str | None = None         # test record this step uses (e.g. "order:A1158"), for irreversible ordering


class Requirement(BaseModel):
    key: str                          # "env.staging.portal", "account.crm.admin", "data.rewards_balance"
    kind: Literal["url", "account", "data", "config", "other"] = "other"
    needed_by: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    value: str | None = None          # non-secret value, or env var name for a secret
    secret_env: str | None = None
    source: str | None = None
    sources_checked: list[str] = Field(default_factory=list)
    status: Literal["resolved", "unverified", "failed_check", "missing"] = "missing"
    check_note: str | None = None
    question: str | None = None
    default: str | None = None        # suggested answer shown at the plan gate (Enter keeps it), e.g. last-used username


class PlannedCase(BaseModel):
    id: str
    title: str
    type: CaseType = CaseType.UI
    area: str | None = None           # memory area (rewards, parcels...) for loading lessons
    preconditions: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    expected: str = ""
    evidence_required: list[str] = Field(default_factory=list)
    values_to_record: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    skip_reason: str | None = None
    generated: bool = False           # written by the planner from the task text, not taken from a Test Cases task
    expected_source: str | None = None  # where the expected result comes from: 'description: "..."', 'comment 2: "..."', 'facts.md: "..."', 'user'
    open_question: str | None = None  # asked at the plan gate when the task does not say what should happen
    source_task: int | None = None    # feature mode: the subtask this case came from
    covers: list[int] = Field(default_factory=list)   # feature mode: every subtask this (deduplicated) case checks


class TestPlan(BaseModel):
    task_id: int
    env: Literal["staging", "preprod", "prod", "local"] = "staging"
    cases_source: Literal["task", "generated"] = "task"
    cases: list[PlannedCase] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    global_conflicts: list[str] = Field(default_factory=list)
    data_to_create: list[str] = Field(default_factory=list)
    preapprovable_actions: list[str] = Field(default_factory=list)
    baseline_steps: list[Step] = Field(default_factory=list)   # read-only reads before the first case and after the last


class ActionLogEntry(BaseModel):
    """Built by fixed code from tool calls, never from the tester's own words."""
    seq: int
    case_id: str
    app: str
    tool: str
    args: dict = Field(default_factory=dict)
    plan_step: int | None = None
    inferred: bool = False
    approval: Literal["not_needed", "approved", "pre_approved", "denied", "held"] = "not_needed"
    outcome: str = "ok"
    deviation: str | None = None


class StepResult(BaseModel):
    case_id: str
    app: str
    values: dict[str, float | str] = Field(default_factory=dict)
    screenshots: list[str] = Field(default_factory=list)
    snapshots: list[str] = Field(default_factory=list)
    steps_done: list[int] = Field(default_factory=list)
    notes: str = ""
    stuck: bool = False
    limit_hit: bool = False
    loop: bool = False                # stopped because the same call repeated without progress
    needs_login: str | None = None    # tester hit a CAPTCHA/robot check at login: a person has to log in once


class CaseResult(BaseModel):
    case_id: str
    verdict: Verdict
    known_bug: int | None = None
    steps_done: list[str] = Field(default_factory=list)
    expected: str = ""
    actual: str = ""
    values: dict[str, float | str] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    note: str | None = None
    lessons_applied: list[int] = Field(default_factory=list)
    bug_draft_ids: list[str] = Field(default_factory=list)
    severity: Literal["Critical", "High", "Medium", "Low"] | None = None   # for a new bug draft
    bug_title: str | None = None                                           # short bug title for the draft
