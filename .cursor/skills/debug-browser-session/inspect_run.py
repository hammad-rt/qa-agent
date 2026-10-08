#!/usr/bin/env python3
"""Read-only evidence dump for qa-agent browser/login problems. Usage: inspect_run.py [runs/<run>]"""
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime


def ts(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return "missing"


def cookies(db_path):
    if not os.path.exists(db_path):
        return None
    tmp = tempfile.mkdtemp()
    try:
        for suffix in ("", "-journal", "-wal"):
            if os.path.exists(db_path + suffix):
                shutil.copy(db_path + suffix, os.path.join(tmp, "Cookies" + suffix))
        db = sqlite3.connect(os.path.join(tmp, "Cookies"))
        return db.execute("select host_key, name, is_persistent from cookies").fetchall()
    except sqlite3.Error as e:
        return f"unreadable ({e})"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def proc(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace")[:160]
    except OSError:
        return None


def profile_report(prof):
    print(f"\n  profile {prof}")
    try:
        p = json.load(open(os.path.join(prof, "Default", "Preferences")))
        print(f"    exit_type: {p.get('profile', {}).get('exit_type')}  (Crashed = killed, not closed)")
    except (OSError, ValueError):
        print("    exit_type: no Preferences")
    db = os.path.join(prof, "Default", "Cookies")
    if not os.path.exists(db):
        db = os.path.join(prof, "Default", "Network", "Cookies")
    rows = cookies(db)
    print(f"    Cookies last written: {ts(db)}")
    if isinstance(rows, list):
        hosts = {}
        for host, name, persistent in rows:
            h = hosts.setdefault(host, [0, 0])
            h[0 if persistent else 1] += 1
        print(f"    cookie rows: {len(rows)}")
        for host, (pers, sess) in sorted(hosts.items()):
            print(f"      {host}: {pers} persistent, {sess} session-only")
    else:
        print(f"    cookie rows: {rows}")
    lock = os.path.join(prof, "SingletonLock")
    if os.path.islink(lock):
        target = os.readlink(lock)
        pid = target.rsplit("-", 1)[-1]
        cmd = proc(pid) if pid.isdigit() else None
        print(f"    SingletonLock: {target} -> " + (f"ALIVE: {cmd}" if cmd else "process gone (stale lock)"))
    else:
        print("    SingletonLock: none")


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else max(glob.glob("runs/*/"), key=os.path.getmtime, default=None)
    if not run or not os.path.isdir(run):
        sys.exit("usage: inspect_run.py runs/<run>")
    print(f"run {run}")
    try:
        st = json.load(open(os.path.join(run, "state.json")))
        print(f"  status: {st.get('status')}  manual_logins: {st.get('manual_logins')}")
        for c in st.get("checkpoints", [])[-12:]:
            print(f"    {c.get('at')}  {str(c.get('label'))[:140]}")
    except (OSError, ValueError):
        print("  no state.json")
    for prof in sorted(glob.glob(os.path.join(run, "profiles", "*"))):
        profile_report(prof)
    trace = os.path.join(run, "trace.jsonl")
    if os.path.exists(trace):
        print("\n  trace (login / finish / errors):")
        for line in open(trace, errors="replace"):
            if any(k in line for k in ("needs_manual_login", '"finish"', "rror", "Target closed", "crash")):
                try:
                    d = json.loads(line)
                    if not (d.get("calls") or d.get("text")):
                        continue
                    print(f"    {d.get('at')} {d.get('author')} {json.dumps(d.get('calls') or d.get('text'))[:160]}")
                except ValueError:
                    print("    " + line.strip()[:160])
    print("\n  live Chrome / Playwright processes on run profiles:")
    out = subprocess.run(["ps", "-eo", "pid,ppid,etime,args"], capture_output=True, text=True).stdout
    hits = [l[:200] for l in out.splitlines()
            if "cursorsandbox" not in l and ("user-data-dir" in l and "runs/" in l or "@playwright/mcp" in l)]
    print("\n".join("    " + h for h in hits) or "    none")


if __name__ == "__main__":
    main()
