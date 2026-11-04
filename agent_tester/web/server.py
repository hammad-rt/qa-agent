"""Local web UI for qa-agent: start/stop runs, answer the agent's questions, watch every step live, open reports.

Start it with `python -m agent_tester web` and open http://127.0.0.1:8765. Standard library only; listens on
localhost only. Runs are started as the normal CLI in a subprocess, so the web UI and the terminal behave the same.
"""
from __future__ import annotations

import json
import mimetypes
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs"
SERVER_MTIME = Path(__file__).stat().st_mtime


def code_stale() -> bool:
    """True when the web server's own code changed on disk after it started (a restart picks it up)."""
    try:
        return Path(__file__).stat().st_mtime != SERVER_MTIME
    except OSError:
        return False
PACKS = ROOT / "packs"
MEM_FIELDS = ("id", "kind", "scope", "area", "text", "status", "source", "evidence", "linked_issue",
              "created_at", "last_confirmed_at", "success_count", "failure_count")


def run_sort_key(d) -> str:
    """Newest first by the start time in the folder name (<task>-YYYYMMDD-HHMMSS); folder mtimes change
    whenever a file inside is touched, which reshuffled the list."""
    import re as _re
    m = _re.search(r"(\d{8})-(\d{6})$", d.name)
    return m.group(1) + m.group(2) if m else "0"


def _mem_db():
    """Open the memory DB without loading the embedder. value_or_ref and secrets are never sent to the browser."""
    import os
    from ..memory.store import MemoryStore
    return MemoryStore(Path(os.environ.get("MEMORY_DB", str(ROOT / "memory" / "memory.db"))), None)


def memory_list() -> list[dict]:
    st = _mem_db()
    try:
        rows = st.db.execute(f"SELECT {', '.join(MEM_FIELDS)} FROM memories ORDER BY "
                             "CASE status WHEN 'proposed' THEN 0 WHEN 'needs_recheck' THEN 1 WHEN 'active' THEN 2 ELSE 3 END, id DESC").fetchall()
        return [dict(zip(MEM_FIELDS, r)) for r in rows]
    finally:
        st.close()


def memory_change(mid: int, action: str, text: str = "") -> dict:
    from ..memory.store import STATUSES
    st = _mem_db()
    try:
        row = st.get(mid)
        if not row:
            return {"error": "no such memory"}
        status = {"approve": "active", "reject": "retired", "retire": "retired", "restore": "active",
                  "recheck": "needs_recheck"}.get(action)
        if status in STATUSES:
            st.set_status(mid, status)
        elif action == "edit":
            text = " ".join((text or "").split())
            if not text:
                return {"error": "text is empty"}
            st.db.execute("UPDATE memories SET text = ?, embedding = NULL WHERE id = ?", (text, mid))
            st.db.commit()
        elif action == "delete":
            st.db.execute("INSERT INTO memories_fts(memories_fts, rowid, text, area, applies_when) "
                          "VALUES ('delete', ?, ?, ?, ?)", (mid, row["text"], row.get("area"), row.get("applies_when")))
            st.db.execute("DELETE FROM memories WHERE id = ?", (mid,))
            st.db.commit()
        else:
            return {"error": f"unknown action {action}"}
        return {"ok": True}
    finally:
        st.close()


def _job_on(run: str) -> bool:
    return any(j.info()["run_dir"] == run and j.proc.poll() is None for j in JOBS.values())


def knowledge() -> list[dict]:
    """Each pack's self-built knowledge base (learned.md) plus its hand-written facts.md, read-only facts."""
    out = []
    for p in sorted(PACKS.iterdir()) if PACKS.exists() else []:
        if p.name.startswith("_") or not (p / "pack.yaml").exists():
            continue
        lf, ff = p / "learned.md", p / "facts.md"
        out.append({"pack": p.name, "learned": lf.read_text() if lf.is_file() else "",
                    "facts": ff.read_text() if ff.is_file() else "",
                    "updated": lf.stat().st_mtime if lf.is_file() else None})
    return out
STATIC = Path(__file__).with_name("index.html")
JOBS: dict[str, "Job"] = {}
SECRET_WORDS = ("password", "secret", "token", "api key")


class Job:
    """One CLI run started from the browser: its console output and a pipe for answers."""

    def __init__(self, args: list[str]):
        self.id = uuid.uuid4().hex[:8]
        self.args = args
        self.started = time.time()
        self.out = ""
        self.pending: dict | None = None      # the question the run is waiting on (from an ASK marker line)
        self._buf = ""
        self._saved = 0                       # how much of out is already in runs/<run>/console.log
        # resume doesn't print "Run folder:", but its run is the argument
        self.run_hint = Path(args[1]).name if len(args) > 1 and args[0] == "resume" else None
        self.lock = threading.Lock()
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "QA_WEB": "1"}
        self.proc = subprocess.Popen([sys.executable, "-u", "-m", "agent_tester", *args], cwd=ROOT,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     env=env, start_new_session=True)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        fd = self.proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            self._feed(chunk.decode("utf-8", "replace"))
            self._persist()
        with self.lock:
            self.out += self._buf
            self._buf = ""
        self._persist()

    def _persist(self):
        """Append new console text to runs/<run>/console.log so it is still there after the run stops,
        pauses, or the web server restarts."""
        try:
            with self.lock:
                text, start = self.out, self._saved
            run = _run_dir_from(text) or self.run_hint
            if not run or len(text) == start:
                return
            d = RUNS / run
            if not d.is_dir():
                return
            with open(d / "console.log", "a", encoding="utf-8") as f:
                if start == 0:
                    f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}  agent_tester {' '.join(self.args)} ===\n")
                f.write(text[start:])
            with self.lock:
                self._saved = len(text)
        except OSError:
            pass

    def _feed(self, text: str):
        """Marker lines become a readable question (no newline, the answer is echoed after it) and set pending.
        Other text goes straight to the console; a partial line is held only while it could be a marker."""
        from ..auto import ASK_MARK
        with self.lock:
            self._buf += text
            while self._buf:
                if self._buf.startswith(ASK_MARK):
                    if "\n" not in self._buf:
                        break
                    line, self._buf = self._buf.split("\n", 1)
                    try:
                        q = json.loads(line[len(ASK_MARK):])
                    except ValueError:
                        q = {"prompt": line[len(ASK_MARK):], "secret": False}
                    q["secret"] = bool(q.get("secret"))      # the CLI knows which questions are secret; trust it
                    self.pending = q
                    if self.out and not self.out.endswith("\n"):
                        self.out += "\n"
                    self.out += str(q.get("prompt", "")) + " "
                    continue
                i = self._buf.find(ASK_MARK)
                if i == -1:
                    keep = 0
                    for k in range(1, len(ASK_MARK)):          # a marker may be split across reads
                        if self._buf.endswith(ASK_MARK[:k]):
                            keep = k
                    self.out += self._buf[:len(self._buf) - keep]
                    self._buf = self._buf[len(self._buf) - keep:]
                    break
                self.out += self._buf[:i]
                self._buf = self._buf[i:]

    def info(self, since: int = 0) -> dict:
        with self.lock:
            text = self.out
        running = self.proc.poll() is None
        if self.pending and running:
            waiting, prompt, secret = True, str(self.pending.get("prompt", "")), bool(self.pending.get("secret"))
        else:  # fallback for any prompt that did not come through ask_line
            tail = text.rstrip().splitlines()[-1] if text.strip() else ""
            waiting = running and not text.endswith("\n") and bool(tail)
            prompt, secret = (tail if waiting else ""), waiting and any(w in tail.lower() for w in SECRET_WORDS)
        return {"id": self.id, "args": self.args, "started": self.started, "running": self.proc.poll() is None,
                "code": self.proc.poll(), "out": text[since:], "len": len(text), "waiting": waiting,
                "secret": secret, "prompt": prompt,
                "run_dir": _run_dir_from(text) or self.run_hint}

    def answer(self, text: str):
        if self.proc.poll() is None:
            info = self.info()
            secret = info["secret"]
            self.proc.stdin.write((text + "\n").encode())
            self.proc.stdin.flush()
            with self.lock:
                self.pending = None
                self.out += ("********" if secret and text else text) + "\n"
            self._persist()

    def stop(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGINT)        # same as Ctrl+C: the run pauses and can be resumed


def _subtasks_of(d: Path) -> bool:
    """Whether a run was started with its subtasks (feature mode). Older runs didn't record it: default on."""
    st = _read_json(d / "state.json", {}) or {}
    return (st.get("mode") or {}).get("subtasks", True) is not False


def _run_dir_from(text: str) -> str | None:
    for line in text.splitlines():
        if "Run folder:" in line:
            return Path(line.split("Run folder:")[1].strip()).name
    return None


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def run_summary(d: Path) -> dict:
    st = _read_json(d / "state.json", {}) or {}
    plan = st.get("plan") or {}
    results = st.get("results") or {}
    counts: dict = {}
    for r in results.values():
        v = (r or {}).get("verdict")
        if v:
            counts[v] = counts.get(v, 0) + 1
    cps = st.get("checkpoints") or []
    live = any(j.info()["run_dir"] == d.name and j.proc.poll() is None for j in JOBS.values())
    return {"id": d.name, "task": plan.get("task_id") or d.name.split("-")[0], "env": plan.get("env"),
            "status": "running" if live else st.get("status", "unknown"), "cases": len(plan.get("cases") or []),
            "done": len(results), "counts": counts,
            "current": next((c.get("id") for c in plan.get("cases") or [] if c.get("id") not in results
                             and (not (st.get("mode") or {}).get("cases") or c.get("id") in (st.get("mode") or {}).get("cases"))), None) if live else None, "started": cps[0]["at"] if cps else None,
            "last": cps[-1]["label"] if cps else None, "mtime": d.stat().st_mtime,
            "label": (d / "label.txt").read_text().strip() if (d / "label.txt").exists() else "",
            "report": next((f.name for f in (d / "report.html", d / "report.pdf") if f.exists()), None)}


def run_detail(d: Path) -> dict:
    st = _read_json(d / "state.json", {}) or {}
    shots = sorted((p.name for p in (d / "shots").glob("*.png")), key=lambda n: (d / "shots" / n).stat().st_mtime) \
        if (d / "shots").exists() else []
    progress = (d / "progress.log").read_text() if (d / "progress.log").exists() else ""
    reports = [f.name for f in d.iterdir() if f.name.startswith(("report", "bug")) or f.suffix == ".md"]
    return {"summary": run_summary(d), "plan": st.get("plan"), "results": st.get("results"),
            "values": st.get("values"), "checkpoints": st.get("checkpoints"), "ledger": st.get("ledger"),
            "action_log": (st.get("action_log") or [])[-400:], "shots": shots, "progress": progress,
            "reports": sorted(reports), "model": st.get("model"), "mode": st.get("mode")}


def trace_lines(d: Path, since: int) -> dict:
    p = d / "trace.jsonl"
    if not p.exists():
        return {"lines": [], "next": since}
    rows = p.read_text(errors="replace").splitlines()
    out = []
    for raw in rows[since:]:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return {"lines": out, "next": len(rows)}


def _safe(base: Path, rel: str) -> Path | None:
    p = (base / unquote(rel)).resolve()
    return p if str(p).startswith(str(base.resolve()) + os.sep) and p.is_file() else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body, default=str) if ctype == "application/json"
                                                      else str(body)).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True      # the browser left (reload, tab closed, newer poll) before the reply; harmless

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _run(self, name: str) -> Path | None:
        d = (RUNS / name).resolve()
        return d if d.parent == RUNS.resolve() and d.is_dir() else None

    def do_GET(self):
        try:
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not parts:
                return self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
            if parts == ["api", "runs"]:
                ds = sorted((d for d in RUNS.iterdir() if d.is_dir() and not d.name.startswith(".")),
                            key=run_sort_key, reverse=True) if RUNS.exists() else []
                return self._send(200, [run_summary(d) for d in ds[:100]])
            if parts == ["api", "version"]:
                return self._send(200, {"stale": code_stale()})
            if parts == ["api", "jobs"]:
                return self._send(200, [j.info(len(j.out)) for j in JOBS.values()])
            if parts == ["api", "knowledge"]:
                return self._send(200, knowledge())
            if parts == ["api", "memory"]:
                return self._send(200, memory_list())
            if len(parts) >= 3 and parts[:2] == ["api", "runs"]:
                d = self._run(parts[2])
                if not d:
                    return self._send(404, {"error": "no such run"})
                if len(parts) == 3:
                    return self._send(200, run_detail(d))
                if parts[3] == "console":
                    f = d / "console.log"
                    text = f.read_text(errors="replace")[-300_000:] if f.is_file() else ""
                    return self._send(200, {"text": text})
                if parts[3] == "trace":
                    return self._send(200, trace_lines(d, int(q.get("since", 0))))
                if parts[3] == "file":
                    f = _safe(d, q.get("path", ""))
                    if not f:
                        return self._send(404, {"error": "no such file"})
                    return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "text/plain")
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "out":
                j = JOBS.get(parts[2])
                return self._send(200, j.info(int(q.get("since", 0)))) if j else self._send(404, {"error": "no job"})
            return self._send(404, {"error": "unknown endpoint: this server is older than the page. Stop agent_tester web and start it again."})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        try:
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            b = self._body()
            if len(parts) == 4 and parts[:2] == ["api", "memory"] and parts[2].isdigit():
                r = memory_change(int(parts[2]), parts[3], str(b.get("text", "")))
                return self._send(400 if "error" in r else 200, r)
            if len(parts) == 4 and parts[:2] == ["api", "runs"]:
                d = self._run(parts[2])
                if not d:
                    return self._send(404, {"error": "no such run"})
                if parts[3] == "guide":            # a note for the agent while the run goes; read at its next tool call
                    from ..redact import redact, scrub_secrets
                    text = scrub_secrets(redact(str(b.get("text", "")).strip()))[:2000]
                    if not text:
                        return self._send(400, {"error": "write something for the agent first"})
                    if not _job_on(d.name):
                        return self._send(409, {"error": "this run isn't going, so nothing would read it"})
                    from datetime import datetime
                    with open(d / "guidance.jsonl", "a") as f:
                        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "text": text}) + "\n")
                    return self._send(200, {"ok": True})
                if _job_on(d.name):
                    return self._send(409, {"error": "this run is still going; stop it first"})
                if parts[3] == "delete":
                    import shutil
                    shutil.rmtree(d)
                    return self._send(200, {"ok": True})
                if parts[3] == "label":
                    (d / "label.txt").write_text(str(b.get("label", "")).strip()[:200] + "\n")
                    return self._send(200, {"ok": True})
                if parts[3] == "report":           # build report.html from the cases finished so far
                    if not (_read_json(d / "state.json", {}) or {}).get("plan"):
                        return self._send(400, {"error": "this run has no plan yet, so there is nothing to report"})
                    try:
                        r = subprocess.run([sys.executable, "-m", "agent_tester", "report", str(d)], cwd=ROOT,
                                           capture_output=True, text=True, timeout=300)
                    except subprocess.TimeoutExpired:
                        return self._send(504, {"error": "building the report took too long"})
                    if r.returncode != 0 or not (d / "report.html").exists():
                        tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
                        return self._send(500, {"error": "report not built: " + " ".join(tail)})
                    return self._send(200, {"ok": True})
                if parts[3] == "delete_shot":
                    f = _safe(d, "shots/" + Path(str(b.get("name", ""))).name)
                    if not f or f.suffix.lower() != ".png":
                        return self._send(404, {"error": "no such screenshot"})
                    f.unlink()
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "unknown endpoint: this server is older than the page. Stop agent_tester web and start it again."})
            if parts == ["api", "runs", "delete_many"]:
                done, skipped = [], []
                import shutil
                for name in b.get("ids") or []:
                    d = self._run(str(name))
                    if d and not _job_on(d.name):
                        shutil.rmtree(d); done.append(d.name)
                    else:
                        skipped.append(str(name))
                return self._send(200, {"deleted": done, "skipped": skipped})
            if parts == ["api", "knowledge"]:
                name = str(b.get("pack", ""))
                d = (PACKS / name).resolve()
                if not name or d.parent != PACKS.resolve() or not (d / "pack.yaml").exists():
                    return self._send(404, {"error": "no such pack"})
                text = str(b.get("learned", ""))
                if len(text) > 200_000:
                    return self._send(400, {"error": "too long"})
                (d / "learned.md").write_text(text if text.endswith("\n") else text + "\n")
                return self._send(200, {"ok": True})
            if parts == ["api", "start"]:
                if any(j.proc.poll() is None for j in JOBS.values()):
                    return self._send(409, {"error": "a run is already going; stop it or wait for it to finish"})
                if b.get("rerun"):
                    d = self._run(str(b["rerun"]))
                    if not d:
                        return self._send(404, {"error": "no such run"})
                    task = d.name.split("-")[0]
                    st = _read_json(d / "state.json", {}) or {}
                    args = ["rerun", task, "--from", str(d)]
                    if not st.get("plan"):          # cancelled before planning: start the same task again
                        env = st.get("env") or "staging"
                        args = ["test", task, "--env", str(env)]
                        only = (st.get("mode") or {}).get("cases") or []
                        if only:
                            args += ["--cases", ",".join(only)]
                    elif str(b.get("cases") or "").strip():
                        args += ["--cases", str(b["cases"]).replace(" ", "")]
                    elif b.get("failed"):
                        args.append("--failed")
                elif b.get("fresh"):
                    d = self._run(str(b["fresh"]))
                    if not d:
                        return self._send(404, {"error": "no such run"})
                    env = "staging"
                    try:
                        env = (json.loads((d / "state.json").read_text()).get("plan") or {}).get("env") or env
                    except (OSError, ValueError):
                        pass
                    args = ["test", d.name.split("-")[0], "--env", env, "--fresh-plan"]
                    if not _subtasks_of(d):
                        args.append("--no-subtasks")
                    if (d / "cases_file.md").exists():
                        args += ["--cases-file", str(d / "cases_file.md")]
                elif b.get("execute"):                # run the plan of a plan-only run
                    d = self._run(str(b["execute"]))
                    if not d:
                        return self._send(404, {"error": "no such run"})
                    st = _read_json(d / "state.json", {}) or {}
                    plan = st.get("plan") or {}
                    if st.get("status") != "dry_run" or not plan.get("cases"):
                        return self._send(400, {"error": "this run has no plan-only draft to execute"})
                    args = ["test", str(plan.get("task_id") or d.name.split("-")[0]), "--env", plan.get("env") or "staging",
                            "--from-draft", d.name]
                    if not _subtasks_of(d):
                        args.append("--no-subtasks")
                elif b.get("resume"):
                    d = self._run(str(b["resume"]))
                    if not d:
                        return self._send(404, {"error": "no such run"})
                    args = ["resume", str(d)]
                else:
                    task = str(b.get("task", "")).strip()
                    if not task.isdigit():
                        return self._send(400, {"error": "enter a Redmine task number"})
                    args = ["test", task, "--env", str(b.get("env") or "staging")]
                    if str(b.get("cases") or "").strip():
                        args += ["--cases", str(b["cases"]).replace(" ", "")]
                    if b.get("dry_run"):
                        args.append("--dry-run")
                    if b.get("subtasks") is False:
                        args.append("--no-subtasks")
                    cf = str(b.get("cases_file") or "").strip()
                    if cf:
                        p = Path(cf).expanduser()
                        if p.suffix.lower() not in (".md", ".txt", ".markdown") or not p.is_file():
                            return self._send(400, {"error": f"no .md or .txt file at {cf}"})
                        args += ["--cases-file", str(p.resolve())]
                j = Job(args)
                JOBS[j.id] = j
                return self._send(200, j.info())
            if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                j = JOBS.get(parts[2])
                if not j:
                    return self._send(404, {"error": "no job"})
                if parts[3] == "input":
                    j.answer(str(b.get("text", "")))
                elif parts[3] == "stop":
                    j.stop()
                return self._send(200, j.info(len(j.out)))
            return self._send(404, {"error": "unknown endpoint: this server is older than the page. Stop agent_tester web and start it again."})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def serve(host: str = "127.0.0.1", port: int = 8765) -> int:
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"qa-agent web UI: http://{host}:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        for j in JOBS.values():
            j.stop()
        print("\nStopped.")
    return 0
