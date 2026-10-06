"""Phase 1 report: report.json plus a simple HTML page. Phase 4 adds the full layout and PDF."""
from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, select_autoescape

from ..redact import redact

TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>QA #{{ plan.task_id }}</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}table{border-collapse:collapse;width:100%}
td,th{border:1px solid #ddd;padding:6px;vertical-align:top;font-size:14px}th{background:#f5f5f5}
.PASS{color:#137333}.FAIL{color:#c5221f}.PARTIAL,.NOT{color:#b06000}.BLOCKED,.AGENT{color:#5f6368}</style></head><body>
<h1>QA run for #{{ plan.task_id }} ({{ plan.env }})</h1>
<p>{% for v, n in counts.items() %}<b class="{{ v.split()[0] }}">{{ v }}</b>: {{ n }}&nbsp;&nbsp;{% endfor %}</p>
<table><tr><th>Case</th><th>Verdict</th><th>Expected</th><th>Actual</th><th>Values</th><th>Evidence</th><th>Note</th></tr>
{% for c in plan.cases %}{% set r = results.get(c.id) %}<tr><td>{{ c.id }}: {{ c.title }}</td>
<td class="{{ (r.verdict if r else 'NOT').split()[0] }}">{{ r.verdict if r else 'not run' }}{% if r and r.known_bug %} (still present: bug {{ r.known_bug }}){% endif %}</td>
<td>{{ c.expected }}</td><td>{{ r.actual if r else '' }}</td>
<td>{% if r %}{% for k, v in r.values.items() %}{{ k }} = {{ v }}<br>{% endfor %}{% endif %}</td>
<td>{% if r %}{% for e in r.evidence %}<a href="shots/{{ e }}">{{ e }}</a><br>{% endfor %}{% endif %}</td>
<td>{{ r.note if r and r.note else '' }}</td></tr>{% endfor %}</table>
<h2>Inferred steps</h2><ul>{% for c in plan.cases %}{% for s in c.steps if s.inferred %}<li>{{ c.id }}: {{ s.text }} ({{ s.inferred_reason }}; {{ s.inferred_source }})</li>{% endfor %}{% endfor %}</ul>
<h2>Requirements</h2><ul>{% for q in plan.requirements %}<li>{{ q.key }}: {{ q.status }}{% if q.source %} from {{ q.source }}{% endif %}</li>{% endfor %}</ul>
</body></html>"""


def build_report(state) -> Path:
    plan = state.plan
    results = {cid: state.result(cid) for cid in state.data["results"]}
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    html = Environment(autoescape=select_autoescape()).from_string(TEMPLATE).render(plan=plan, results=results, counts=counts)
    out = state.dir / "report.html"
    out.write_text(redact(html))
    (state.dir / "report.json").write_text(json.dumps(
        {"task_id": plan.task_id, "env": plan.env, "counts": counts,
         "results": {k: v.model_dump(mode="json") for k, v in results.items()}}, indent=2))
    return out
