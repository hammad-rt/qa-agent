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
            with self.lock:
                self.out += chunk.decode("utf-8", "replace")

    def info(self, since: int = 0) -> dict:
        with self.lock:
            text = self.out
        tail = text.rstrip().splitlines()[-1] if text.strip() else ""
        waiting = self.proc.poll() is None and not text.endswith("\n") and bool(tail)
        return {"id": self.id, "args": self.args, "started": self.started, "running": self.proc.poll() is None,
                "code": self.proc.poll(), "out": text[since:], "len": len(text), "waiting": waiting,
                "secret": waiting and any(w in tail.lower() for w in SECRET_WORDS), "prompt": tail if waiting else "",
                "run_dir": _run_dir_from(text)}

    def answer(self, text: str):
        if self.proc.poll() is None:
            self.proc.stdin.write((text + "\n").encode())
            self.proc.stdin.flush()
            secret = any(w in self.info()["prompt"].lower() for w in SECRET_WORDS)
            with self.lock:
                self.out += ("********" if secret else text) + "\n"

    def stop(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGINT)        # same as Ctrl+C: the run pauses and can be resumed


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
            "done": len(results), "counts": counts, "started": cps[0]["at"] if cps else None,
            "last": cps[-1]["label"] if cps else None, "mtime": d.stat().st_mtime,
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
            "reports": sorted(reports), "model": st.get("model")}


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
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

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
                            key=lambda d: d.stat().st_mtime, reverse=True) if RUNS.exists() else []
                return self._send(200, [run_summary(d) for d in ds[:100]])
            if parts == ["api", "jobs"]:
                return self._send(200, [j.info(len(j.out)) for j in JOBS.values()])
            if len(parts) >= 3 and parts[:2] == ["api", "runs"]:
                d = self._run(parts[2])
                if not d:
                    return self._send(404, {"error": "no such run"})
                if len(parts) == 3:
                    return self._send(200, run_detail(d))
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
            return self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        try:
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            b = self._body()
            if parts == ["api", "start"]:
                if any(j.proc.poll() is None for j in JOBS.values()):
                    return self._send(409, {"error": "a run is already going; stop it or wait for it to finish"})
                if b.get("resume"):
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
            return self._send(404, {"error": "not found"})
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
