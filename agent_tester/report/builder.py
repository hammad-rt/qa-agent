"""Full report (FR-21): summary, top bugs, per-case detail with screenshots, baseline vs final, leftover data,
questions and answers, what was learned, model. Writes report.html, report.json, bug drafts, and report.pdf
when a headless browser is available."""
from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, select_autoescape

from ..redact import redact

ORDER = ["PASS", "FAIL", "PARTIAL", "BLOCKED", "NOT VERIFIED", "SKIPPED", "AGENT INCOMPLETE"]

TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>QA #{{ plan.task_id }}</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px;color:#202124}
table{border-collapse:collapse;width:100%;margin:.5rem 0}td,th{border:1px solid #ddd;padding:6px;vertical-align:top;font-size:13px}
th{background:#f5f5f5;text-align:left}h2{border-bottom:2px solid #eee;padding-bottom:4px;margin-top:2rem}
.case{page-break-inside:avoid;border:1px solid #e0e0e0;border-radius:6px;padding:12px;margin:12px 0}
.shots img{max-width:48%;border:1px solid #ccc;margin:4px}.muted{color:#666;font-size:12px}
.PASS{color:#137333}.FAIL{color:#c5221f}.PARTIAL,.NOT{color:#b06000}.BLOCKED,.AGENT,.SKIPPED{color:#5f6368}
.mean{background:#fef7e0;padding:8px;border-left:4px solid #f9ab00}</style></head><body>
<h1>QA report: #{{ plan.task_id }} ({{ plan.env }})</h1>
<p class="muted">Run {{ run_id }}{% if pack_name %} · {{ pack_name }}{% endif %}{% if model %} · model {{ model.requested }}{% if model.served %} (served: {{ model.served | join(', ') }}){% endif %}{% endif %}</p>

<h2>Summary</h2>
<table><tr>{% for v in order %}<th class="{{ v.split()[0] }}">{{ v }}</th>{% endfor %}<th>Total</th></tr>
<tr>{% for v in order %}<td>{{ counts.get(v, 0) }}</td>{% endfor %}<td>{{ results | length }} of {{ plan.cases | length }}</td></tr></table>
{% if drafts.drafts %}<h3>Top new bugs (drafts, not posted)</h3><ol>{% for d in drafts.drafts[:5] %}
<li><b>{{ d.severity }}</b> {{ d.title }} ({{ d.linked_cases | join(', ') }}){% if d.posted_issue %} · posted as #{{ d.posted_issue }}{% endif %}</li>{% endfor %}</ol>{% endif %}
{% if drafts.still_present %}<h3>Known bugs still present</h3><ul>{% for b in drafts.still_present %}
<li>Bug {{ b.n }} [{{ b.severity }}]: {{ b.text }} ({{ b.cases | join(', ') }})</li>{% endfor %}</ul>{% endif %}
{% if not_done %}<h3>Blocked, not verified or incomplete</h3><ul>{% for r in not_done %}<li>{{ r.case_id }} <b>{{ r.verdict }}</b>: {{ r.note }}</li>{% endfor %}</ul>{% endif %}
{% set inf = [] %}{% for c in plan.cases %}{% for s in c.steps if s.inferred %}{% set _ = inf.append((c.id, s)) %}{% endfor %}{% endfor %}
{% if inf %}<h3>Inferred steps (not written in the task)</h3><ul>{% for cid, s in inf %}<li>{{ cid }}: {{ s.text }} ({{ s.inferred_reason }}; {{ s.inferred_source }})</li>{% endfor %}</ul>{% endif %}

<h2>Cases</h2>
{% for c in plan.cases %}{% set r = results.get(c.id) %}<div class="case">
<h3>{{ c.id }}: {{ c.title }} · <span class="{{ (r.verdict if r else 'NOT').split()[0] }}">{{ r.verdict if r else 'not run' }}</span>
{% if r and r.known_bug %} <small>(still present: bug {{ r.known_bug }})</small>{% endif %}</h3>
<ol>{% for s in c.steps %}<li>[{{ s.app }}] {{ s.text }}{% if s.inferred %} <i>(inferred: {{ s.inferred_reason }}; {{ s.inferred_source }})</i>{% endif %}
{% if r and (loop.index|string) in done.get(c.id, []) %} ✓{% endif %}</li>{% endfor %}</ol>
<table><tr><th style="width:50%">Expected</th><th>Actual</th></tr><tr><td>{{ c.expected or (r.expected if r else '') }}</td><td>{{ r.actual if r else '' }}</td></tr></table>
{% if r and r.values %}<p>{% for k, v in r.values.items() %}<code>{{ k }} = {{ v }}</code> {% endfor %}</p>{% endif %}
{% if r and r.note %}<div class="{{ 'mean' if r.verdict != 'PASS' else '' }}"><b>{{ 'What this means' if r.verdict != 'PASS' else 'Note' }}:</b> {{ r.note }}</div>{% endif %}
{% if r and r.lessons_applied %}<p class="muted">Lessons applied: {{ r.lessons_applied | join(', ') }}</p>{% endif %}
{% if r and r.evidence %}<div class="shots">{% for e in r.evidence %}{% if e.endswith('.png') %}<img src="shots/{{ e }}" alt="{{ e }}">{% else %}<span class="muted">{{ e }}</span> {% endif %}{% endfor %}</div>{% endif %}
</div>{% endfor %}

{% if baseline or final %}<h2>Baseline vs final</h2><table><tr><th>Value</th><th>Baseline</th><th>Final</th></tr>
{% for k in snap_keys %}<tr><td>{{ k }}</td><td>{{ baseline.get(k, '') }}</td><td>{{ final.get(k, '') }}</td></tr>{% endfor %}</table>{% endif %}
{% if unrestored %}<h2 class="FAIL">Settings not restored</h2><ul>{% for u in unrestored %}<li>{{ u.app }}: {{ u.setting }} was {{ u.original }}, now {{ u.restored or 'not restored' }}</li>{% endfor %}</ul>{% endif %}
{% if ledger %}<h2>Test data</h2><table><tr><th>App</th><th>Record</th><th>Action</th><th>Final state</th><th>Case</th><th>Tag</th></tr>
{% for e in ledger %}<tr><td>{{ e.app }}</td><td>{{ e.record }}</td><td>{{ e.action }}</td><td>{{ e.final_state }}</td><td>{{ e.case_id }}</td><td>{{ e.tag }}{% if e.cleaned %} (cleaned){% endif %}</td></tr>{% endfor %}</table>{% endif %}

<h2>Questions asked and answers used</h2><table><tr><th>Needed</th><th>Status</th><th>Answer used</th><th>Source</th><th>Checked</th></tr>
{% for q in plan.requirements %}<tr><td>{{ q.key }}</td><td>{{ q.status }}</td><td>{% if q.secret_env %}{{ q.value or '' }} (password in {{ q.secret_env }}){% else %}{{ q.value or '' }}{% endif %}</td>
<td>{{ q.source or '' }}{% if q.question %}<br><span class="muted">asked: {{ q.question }}</span>{% endif %}</td><td>{{ q.sources_checked | join(', ') }}</td></tr>{% endfor %}</table>

{% if learned %}<h2>Learned this run</h2><p>Approved: {{ learned.approved | length }}, rejected: {{ learned.rejected | length }},
promoted to core: {{ learned.promoted | length }}, new open bugs saved: {{ learned.open_bugs_saved | length }}</p>{% endif %}
{% if notes %}<h2>Run notes</h2><ul>{% for n in notes %}<li>{{ n }}</li>{% endfor %}</ul>{% endif %}
</body></html>"""


def build_report(state, pack=None, pdf: bool | None = None) -> Path:
    """Returns report.html; report.pdf is written next to it when possible (REPORT_PDF=0 turns it off)."""
    import os
    if pdf is None:
        pdf = os.environ.get("REPORT_PDF", "1") != "0"
    plan = state.plan
    results = {cid: state.result(cid) for cid in state.data["results"]}
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    drafts = {"drafts": [], "still_present": []}
    if pack is not None:
        from .bug_drafts import build_drafts
        drafts = build_drafts(state, pack)
        results = {cid: state.result(cid) for cid in state.data["results"]}
    done: dict[str, list[str]] = {}
    for a in state.data.get("action_log", []):
        if a["tool"] == "mark_step":
            done.setdefault(a["case_id"], []).append(str(a["args"].get("n")))
    base, fin = state.data.get("baseline") or {}, state.data.get("final") or {}
    html = Environment(autoescape=select_autoescape(default=True)).from_string(TEMPLATE).render(
        plan=plan, results=results, counts=counts, order=ORDER, run_id=state.dir.name,
        pack_name=pack.config.get("name") if pack else None, drafts=drafts, done=done,
        not_done=[r for r in results.values() if r.verdict in ("BLOCKED", "NOT VERIFIED", "AGENT INCOMPLETE")],
        baseline=base, final=fin, snap_keys=list(dict.fromkeys([*base, *fin])),
        ledger=state.data.get("ledger", []), unrestored=[u for u in state.data.get("settings", {}).values() if not u["verified"]],
        learned=state.data.get("learned"), model=state.data.get("model"), notes=state.data.get("notes", []))
    out = state.dir / "report.html"
    out.write_text(redact(html))
    (state.dir / "report.json").write_text(redact(json.dumps(
        {"task_id": plan.task_id, "env": plan.env, "counts": counts,
         "results": {k: v.model_dump(mode="json") for k, v in results.items()},
         "bug_drafts": drafts, "ledger": state.data.get("ledger", []), "settings": state.data.get("settings", {}),
         "baseline": state.data.get("baseline"), "final": state.data.get("final"), "learned": state.data.get("learned"),
         "requirements": [r.model_dump(mode="json") for r in plan.requirements],
         "model": state.data.get("model")}, indent=2, default=str)))
    if pdf:
        from .pdf import html_to_pdf
        p = html_to_pdf(out)
        state.data["report_pdf"] = str(p) if p else None
        state.save()
    return out
