"""Batches started from the UI: the same `jev-apply batch` the command line runs, as its own process, so every
guard (auto_submit gate, applied.json, captcha, unattended defaults) applies unchanged. Its output is read line by
line into a live state per job: the plan, the résumé used, events, and the result."""

import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

from . import data

JOB = re.compile(r"^\[(\d+)/(\d+)\] (\S+)")
EVENT = re.compile(r"^\s*· \[(\d+)/(\d+)\] (.*)$")
PLAN = re.compile(r"^\s*(\d+)\. (.+?)(?:\s+résumé (\S+)|\s{2,})\s*(.*)$")
RESULT = re.compile(r"^(submitted|review|stopped|failed|ready to submit|deferred|needs you)[^:]*:\s*(.*)$")
SUMMARY = re.compile(r"^(\d+)\. (SUBMITTED|REVIEW|STOPPED|DEFERRED|READY TO CHECK|FAILED|READY TO SUBMIT[^ ]*)\s+(\S+)")

_batches = {}
_lock = threading.Lock()


def folder():
    path = data.runs_dir() / "batches"
    path.mkdir(parents=True, exist_ok=True)
    return path


def active():
    with _lock:
        return next((b["id"] for b in _batches.values() if b["state"] in {"starting", "planning", "running"}), None)


def start(urls, tracks, submit=True):
    """Start a batch for `urls` with résumé `tracks`; returns its id. One batch at a time (one browser)."""
    if active():
        raise RuntimeError("A batch is already running: stop it or wait for it to finish.")
    urls = [u.strip() for u in urls if u and u.strip().startswith("http")]
    if not urls:
        raise ValueError("No job links to apply to.")
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    jobs_file = folder() / f"{batch_id}.txt"
    jobs_file.write_text("\n".join(urls) + "\n", encoding="utf-8")
    command = [sys.executable, "-m", "jev_apply.cli", "batch", str(jobs_file), "--browser", "chrome"]
    if tracks:
        command += ["--tracks", ",".join(tracks)]
    if submit:
        command.append("--submit")
    batch = {
        "id": batch_id,
        "state": "starting",
        "phase": "Reading the jobs",
        "submit": submit,
        "started": datetime.now().isoformat(timespec="seconds"),
        "ended": None,
        "jobs": [{"n": n, "url": u, "state": "waiting", "events": []} for n, u in enumerate(urls, 1)],
        "log": [],
        "exit": None,
    }
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    log = (folder() / f"{batch_id}.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=data.ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    with _lock:
        _batches[batch_id] = {**batch, "_process": process}
    threading.Thread(target=_follow, args=(batch_id, process, log), daemon=True).start()
    return batch_id


def get(batch_id):
    with _lock:
        b = _batches.get(batch_id)
        if b is None:
            return None
        return {k: v for k, v in b.items() if not k.startswith("_")} | {
            "jobs": [dict(j, events=list(j["events"][-12:])) for j in b["jobs"]],
            "log": b["log"][-400:],
        }


def listing():
    with _lock:
        return [
            {k: b[k] for k in ("id", "state", "started", "ended", "submit")} | {"jobs": len(b["jobs"])}
            for b in sorted(_batches.values(), key=lambda b: b["started"], reverse=True)
        ]


def stop(batch_id):
    with _lock:
        b = _batches.get(batch_id)
    if not b:
        return False
    process = b["_process"]
    if process.poll() is None:
        if os.name == "nt":  # the batch and anything it started (the Clef server is stopped by its own exit hook)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
        else:
            process.terminate()
    with _lock:
        b["state"] = "stopped"
        b["phase"] = "Stopped by you"
    return True


def _follow(batch_id, process, log):
    with _lock:
        b = _batches[batch_id]
    current = None
    for raw in process.stdout:
        line = raw.rstrip("\n")
        log.write(raw)
        log.flush()
        with _lock:
            b["log"].append(line)
            current = _apply(b, line, current)
    process.wait()
    log.close()
    with _lock:
        b["exit"] = process.returncode
        b["ended"] = datetime.now().isoformat(timespec="seconds")
        if b["state"] != "stopped":
            b["state"] = "done" if process.returncode == 0 else "failed"
            b["phase"] = "Done" if process.returncode == 0 else f"Ended with an error (exit {process.returncode})"
        for job in b["jobs"]:
            if job["state"] in {"waiting", "running"}:
                job["state"] = "not run"


def _apply(b, line, current):
    """Fold one output line into the batch state; returns the job now running (its number)."""
    text = line.strip()
    if text.startswith("Plan for"):
        b["state"], b["phase"] = "planning", "Planning: résumé per job"
    elif text.startswith(("Starting Clef", "Loading Laya")):
        b["state"], b["phase"] = "running", "Loading the decision model"
    elif text.startswith("Batch done"):
        b["phase"] = "Finishing"
    plan = PLAN.match(line) if b["state"] in {"starting", "planning"} else None
    if plan and plan[1].isdigit() and int(plan[1]) <= len(b["jobs"]):
        job = b["jobs"][int(plan[1]) - 1]
        title = plan[2].strip()
        if " · " in title:
            job["company"], job["title"] = [x.strip() for x in title.split(" · ", 1)]
        job["track"] = plan[3]
        extra = plan[4] or ""
        if "DEFERRED" in extra:
            job["state"], job["result"] = "deferred", re.sub(r".*DEFERRED \((.*)\).*", r"\1", extra)
        return current
    started = JOB.match(text)
    if started:
        n = int(started[1])
        if n <= len(b["jobs"]):
            b["state"], b["phase"] = "running", f"Job {n} of {started[2]}"
            if b["jobs"][n - 1]["state"] == "waiting":
                b["jobs"][n - 1]["state"] = "running"
                b["jobs"][n - 1]["started"] = time.time()
        return n
    event = EVENT.match(line)
    if event:
        n = int(event[1])
        if n > len(b["jobs"]):
            return current
        job, message = b["jobs"][n - 1], event[3].strip()
        job["events"].append(message)
        if message.startswith("résumé:"):
            job["resume"] = message.split("(", 1)[-1].rstrip(")")
            job["track"] = message.split()[1]
        result = RESULT.match(message)
        if result:
            kind = result[1]
            if kind == "needs you":
                job["needs"] = result[2]
            else:
                job["state"] = kind.replace(" ", "_")
                job["result"] = result[2]
                if job.get("started"):
                    job["seconds"] = round(time.time() - job["started"])
        return current
    final = SUMMARY.match(text)
    if final and int(final[1]) <= len(b["jobs"]):
        job = b["jobs"][int(final[1]) - 1]
        job["final"] = final[2].lower()
    return current
