"""Rebuild a replay set (tech spec section 11) from a past QA report PDF: case ids, steps performed, expected,
actual, verdict, figures, and which known bugs (facts.md numbering) each case is linked to."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..redact import redact

VERDICTS = ["AGENT INCOMPLETE", "NOT VERIFIED", "PARTIAL", "BLOCKED", "SKIPPED", "PASS", "FAIL"]
_HEAD = re.compile(r"^\s*([A-Z]{1,2}\d{1,3}|Master)\s+-\s+(.+?)\s{2,}(PASS|FAIL|PARTIAL|BLOCKED|NOT VERIFIED|SKIPPED)\b(.*)$")
_HEAD_NOVERDICT = re.compile(r"^\s*([A-Z]{1,2}\d{1,3})\s+-\s+(\S.+?)\s*$")
_FIELD = re.compile(r"^\s*(Steps|performed|Expected|Actual|Result)\b\s*(.*)$")
_TASK = re.compile(r"^\s*Task\s+#(\d+)")
_BUG = re.compile(r"^\s*Bug\s+(\d+):\s*(.+?)(?:\s{2,}(Critical|High|Medium|Low))?\s*$")
_AFFECTED = re.compile(r"Affected cases:\s*(.+)$")
_COUNTED = re.compile(r"\b([A-Z]\d{1,3})\s+counted\s+(Partial|Fail|Pass|Blocked)\b", re.I)
_FIG = re.compile(r"Fig\.\s*\d+\s*-\s*([\w.-]+\.png)")


def pdf_text(pdf: Path) -> str:
    return subprocess.run(["pdftotext", "-layout", str(pdf), "-"], check=True, capture_output=True, text=True).stdout


def normalize_verdict(text: str) -> str:
    """'PASS (flow) / FAIL (final W = L - U)' has two verdicts: the case is PARTIAL. 'FAIL (CRITICAL)' is FAIL."""
    found = [v for v in VERDICTS if re.search(rf"\b{v}\b", text or "")]
    if len(found) > 1 and not (set(found) <= {"FAIL"}):
        return "PARTIAL"
    return found[0] if found else "NOT VERIFIED"


_STOP = {"the", "a", "on", "of", "to", "is", "and", "not", "when", "after", "an", "with", "by", "in", "for"}


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _words(t: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9$]+", t.lower())} - _STOP


def match_known_bug(title: str, known_bugs: list[dict]) -> int | None:
    """Report bug numbering can differ from facts.md, so map by text overlap."""
    tw = _words(title)
    best, score = None, 0.0
    for b in known_bugs:
        bw = _words(b["text"])
        s = len(tw & bw) / max(1, min(len(tw), len(bw)))
        if s > score:
            best, score = b["n"], s
    return best if score >= 0.35 else None


def extract(text: str, known_bugs: list[dict]) -> dict:
    lines = text.splitlines()
    cases: dict[str, dict] = {}
    cur, field, task = None, None, None
    counted = {m.group(1).upper(): m.group(2).upper() for m in _COUNTED.finditer(re.sub(r"\s+", " ", text))}
    bugs: list[dict] = []
    for raw in lines:
        if m := _TASK.match(raw):
            task = int(m.group(1))
            continue
        if m := _BUG.match(raw):
            bugs.append({"report_n": int(m.group(1)), "title": m.group(2).strip(), "severity": m.group(3), "cases": []})
            cur = None
            continue
        if bugs and (m := _AFFECTED.search(raw)):
            bugs[-1]["cases"] = [c.strip() for c in m.group(1).split(",") if c.strip()]
            continue
        m = _HEAD.match(raw) or _HEAD_NOVERDICT.match(raw)
        if m and (m.re is _HEAD or not _FIELD.match(raw)) and " - " in raw and len(m.group(1)) <= 4:
            cid = m.group(1)
            key = f"{task}:{cid}" if task else cid
            if key in cases and cases[key]["expected"]:
                cur = None
                continue
            verdict_text = (m.group(3) + m.group(4)) if m.re is _HEAD else ""
            cur = cases.setdefault(key, {"id": cid, "task": task, "title": m.group(2).strip(), "verdict_text": verdict_text.strip(),
                                         "steps": "", "expected": "", "actual": "", "figures": []})
            field = None
            continue
        if cur is None:
            continue
        if f := _FIG.search(raw):
            cur["figures"].append(f.group(1))
            continue
        if m := _FIELD.match(raw):
            name, rest = m.group(1), m.group(2)
            field = {"Steps": "steps", "performed": "steps", "Expected": "expected", "Actual": "actual", "Result": "result"}[name]
            if field == "result":
                cur["verdict_text"] = cur["verdict_text"] or rest.strip()
                field = None
            elif rest.strip():
                cur[field] = (cur[field] + " " + rest.strip()).strip()
            continue
        if field and raw.strip() and not raw.strip().startswith(("See also", "Fig.")):
            cur[field] = (cur[field] + " " + raw.strip()).strip()
        elif not raw.strip() and field == "actual":
            field = None
    for b in bugs:
        b["n"] = match_known_bug(b["title"], known_bugs)
    out = []
    for key, c in cases.items():
        if not (c["expected"] or c["actual"]):
            continue
        v = normalize_verdict(c["verdict_text"])
        if c["id"].upper() in counted:
            v = {"PARTIAL": "PARTIAL", "FAIL": "FAIL", "PASS": "PASS", "BLOCKED": "BLOCKED"}[counted[c["id"].upper()]]
        linked = sorted({b["n"] for b in bugs if b["n"] and c["id"] in b["cases"]})
        out.append({"key": key, "id": c["id"], "task": c["task"], "title": c["title"], "steps": redact(c["steps"]),
                    "expected": redact(c["expected"]), "actual": redact(c["actual"]), "verdict": v,
                    "verdict_text": c["verdict_text"], "known_bugs": linked if v != "PASS" else [],
                    "figures": c["figures"]})
    return {"cases": out, "report_bugs": bugs}


def build_known_answers(pack, sources: list[Path] | None = None, corrections: list[dict] | None = None) -> dict:
    """Write packs/<slug>/evals/known_answers.json from every PDF in evals/source/. User corrections
    (kept in evals/corrections.json) override the report's verdicts, e.g. a behaviour confirmed intended."""
    import json
    src_dir = pack.root / "evals" / "source"
    sources = sources or sorted(src_dir.glob("*.pdf"))
    corr_path = pack.root / "evals" / "corrections.json"
    corrections = corrections if corrections is not None else (json.loads(corr_path.read_text()) if corr_path.exists() else [])
    all_cases, bugs = [], []
    for pdf in sources:
        got = extract(pdf_text(pdf), pack.known_bugs)
        for c in got["cases"]:
            c["source"] = pdf.name
        all_cases += got["cases"]
        bugs += got["report_bugs"]
    for corr in corrections:
        for c in all_cases:
            if c["key"] == corr.get("key") or c["id"] == corr.get("id"):
                c["verdict"] = corr.get("verdict", c["verdict"])
                if "known_bugs" in corr:
                    c["known_bugs"] = corr["known_bugs"]
                c["correction"] = corr.get("why", "")
    data = {"pack": pack.slug, "sources": [p.name for p in sources], "cases": all_cases, "report_bugs": bugs,
            "note": "Rebuilt from report PDFs: no page text, so replay judges from recorded actual text and figure names."}
    out = pack.root / "evals" / "known_answers.json"
    out.write_text(json.dumps(data, indent=2))
    return data
