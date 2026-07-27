#!/usr/bin/env python
"""Supervisor for every long-running memebot process.

Why this exists: Task Scheduler registration on this machine needs elevation,
and Scheduler only resurrects processes IT started. This runs entirely in user
space and owns every process directly, so crash recovery works for all
strategies the moment it starts - no admin, no reboot.

Track record it is fixing: the 2026-07-23..25 BASE trial ran 58h wall-clock with
14.18h dark (24.5%), one gap unbroken for 10.74h, during which a position was
held 13.4h against an 8h max_hold. On 2026-07-26 HOMERUN died at 14:49 and sat
dead ~9h before anyone noticed. Every one of those was a process that stopped
and stayed stopped.

Behaviour:
  - On start, kills any stray copy of a supervised job so exactly one runs.
  - Polls every POLL_SEC; a dead job is relaunched immediately.
  - Backs off after repeated fast crashes so a broken config doesn't spin.
  - Logs every death and restart to reports/watchdog.log.
  - Writes reports/watchdog.json for the dashboard.

Usage:
  python watchdog.py              supervise (foreground; Ctrl-C stops all)
  python watchdog.py --status     print what is running, then exit
  python watchdog.py --no-adopt   skip the startup stray-kill
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
WORKTREE = os.path.join(ROOT, ".claude", "worktrees", "paper-bounce-holder")
PY = sys.executable

LOG_PATH = os.path.join("reports", "watchdog.log")
STATE_PATH = os.path.join("reports", "watchdog.json")

POLL_SEC = 15.0
FAST_CRASH_SEC = 60.0      # a job dying inside this window counts as flapping
MAX_BACKOFF_SEC = 300.0

JOBS = [
    {"name": "asym",    "args": ["run.py", "--config", "config.asym.json"]},
    {"name": "homerun", "args": ["run.py", "--config", "config.homerun.json"]},
    {"name": "grinder", "args": ["run.py", "--config", "config.grinder.json"]},
    {"name": "bounce",  "args": ["run.py", "--config", "config.bounce.json",
                                 "--workdir", ROOT], "cwd": WORKTREE},
    {"name": "holder",  "args": ["run.py", "--config", "config.holder.json",
                                 "--workdir", ROOT], "cwd": WORKTREE},
    {"name": "dash",    "args": ["dashboard/server.py", "--config", "config.asym.json"]},
    {"name": "pages",   "args": ["pages_publish.py", "--loop", "5"]},
]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        os.makedirs("reports", exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def running_pids() -> dict:
    """pid -> command line, for every python process on the box. Uses CIM
    because psutil is not a dependency of this repo."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60).stdout
        data = json.loads(out) if out.strip() else []
    except Exception:
        return {}
    if isinstance(data, dict):
        data = [data]
    return {int(d["ProcessId"]): (d.get("CommandLine") or "")
            for d in data if d.get("ProcessId")}


def job_matches(job: dict, cmd: str) -> bool:
    """Does this command line belong to this job? BOTH the script and the
    config must match: `dashboard/server.py --config config.asym.json` and
    `run.py --config config.asym.json` share a config, so matching on the
    config alone would make the watchdog kill the dashboard as a stray asym."""
    args = job["args"]
    script = os.path.basename(args[0])
    if script not in cmd:
        return False
    # run.py backs every strategy, so the config is what separates them
    if "--config" in args:
        return args[args.index("--config") + 1] in cmd
    return True


def kill_strays(jobs) -> None:
    """One process per job, always. A stray from a previous manual launch is
    unsupervised by definition, so it gets replaced rather than adopted."""
    pids = running_pids()
    me = os.getpid()
    for job in jobs:
        for pid, cmd in pids.items():
            if pid == me or "watchdog.py" in cmd or not job_matches(job, cmd):
                continue
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=30)
                log(f"adopt: killed stray {job['name']} pid {pid}")
            except Exception as exc:
                log(f"adopt: could not kill pid {pid}: {exc}")


def spawn(job: dict) -> subprocess.Popen:
    cwd = job.get("cwd", ROOT)
    p = subprocess.Popen([PY] + job["args"], cwd=cwd,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    job["proc"] = p
    job["started"] = time.time()
    return p


def write_state(jobs) -> None:
    try:
        os.makedirs("reports", exist_ok=True)
        doc = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "watchdog_pid": os.getpid(),
               "jobs": [{"name": j["name"],
                         "pid": j["proc"].pid if j.get("proc") else None,
                         "alive": bool(j.get("proc") and j["proc"].poll() is None),
                         "restarts": j.get("restarts", 0),
                         "uptime_sec": round(time.time() - j.get("started", time.time()))}
                        for j in jobs]}
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def status() -> None:
    pids = running_pids()
    print(f"{'job':10s} {'pid':>7s}  command")
    for job in JOBS:
        found = [(p, c) for p, c in pids.items()
                 if "watchdog.py" not in c and job_matches(job, c)]
        if found:
            for p, _c in found:
                print(f"{job['name']:10s} {p:7d}  {' '.join(job['args'])}")
        else:
            print(f"{job['name']:10s} {'-':>7s}  DOWN")


def main() -> None:
    ap = argparse.ArgumentParser(description="memebot process supervisor")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    ap.add_argument("--no-adopt", action="store_true", help="skip the startup stray-kill")
    args = ap.parse_args()
    if args.status:
        status()
        return

    jobs = [dict(j) for j in JOBS]
    if not os.path.isdir(WORKTREE):
        log(f"WARNING: worktree missing ({WORKTREE}) - bounce/holder will flap")

    log(f"watchdog starting (pid {os.getpid()}, {len(jobs)} jobs, poll {POLL_SEC:.0f}s)")
    if not args.no_adopt:
        kill_strays(jobs)
        time.sleep(2)

    for job in jobs:
        job["restarts"] = 0
        job["backoff"] = 0.0
        job["next_try"] = 0.0
        spawn(job)
        log(f"started {job['name']} pid {job['proc'].pid}")
    write_state(jobs)

    try:
        while True:
            time.sleep(POLL_SEC)
            now = time.time()
            for job in jobs:
                p = job.get("proc")
                if p is not None and p.poll() is None:
                    if now - job["started"] > FAST_CRASH_SEC:
                        job["backoff"] = 0.0      # stable again; clear the penalty
                    continue
                if now < job.get("next_try", 0):
                    continue
                ran = now - job.get("started", now)
                code = p.returncode if p is not None else "?"
                log(f"DIED {job['name']} (exit {code}) after {ran / 60:.1f}m - restarting")
                if ran < FAST_CRASH_SEC:
                    job["backoff"] = min(max(job["backoff"] * 2, 15.0), MAX_BACKOFF_SEC)
                    job["next_try"] = now + job["backoff"]
                    log(f"  {job['name']} is flapping; next attempt in {job['backoff']:.0f}s")
                job["restarts"] += 1
                try:
                    spawn(job)
                    log(f"  restarted {job['name']} pid {job['proc'].pid} "
                        f"(restart #{job['restarts']})")
                except Exception as exc:
                    log(f"  FAILED to restart {job['name']}: {exc}")
            write_state(jobs)
    except KeyboardInterrupt:
        log("watchdog stopping - terminating supervised jobs")
        for job in jobs:
            p = job.get("proc")
            if p is not None and p.poll() is None:
                p.terminate()
        log("stopped")


if __name__ == "__main__":
    main()
