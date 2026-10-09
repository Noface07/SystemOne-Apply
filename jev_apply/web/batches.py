"""Batches started from the UI: the same `jev-apply batch` the command line runs, as its own process, so every
guard (auto_submit gate, applied.json, captcha, unattended defaults) applies unchanged. Its output is read line by
line into a live state per job: the plan, the résumé used, events, and the result.

Every batch leaves its job list and its output in runs/batches/ (<id>.txt, <id>.log). Batches from earlier sessions
are rebuilt from those files by replaying the log, so the Batches page lists them all and opens any of them."""

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


def get(batch_id, log_all=False):
    """One batch's state (this session's, or an earlier one rebuilt from its files), or None."""
    with _lock:
        b = _batches.get(batch_id)
        if b is not None:
            return _view(b, log_all)
    b = from_disk(batch_id)
    return _view(b, log_all) if b else None


def _view(b, log_all=False):
    return {k: v for k, v in b.items() if not k.startswith("_")} | {
        "jobs": [dict(j, events=list(j["events"][-12:])) for j in b["jobs"]],
        "log": list(b["log"]) if log_all else b["log"][-400:],
        "log_n": len(b["log"]),
    }


ID = re.compile(r"^(\d{8})-(\d{6})-[0-9a-f]{4}$")
_disk = {}  # batch id -> (file stamps, rebuilt batch): the list doesn't replay every log on every visit


def from_disk(batch_id):
    """A batch from an earlier session, rebuilt from runs/batches/<id>.txt and .log by replaying its output."""
    found = ID.match(batch_id or "")
    jobs_file = folder() / f"{batch_id}.txt"
    if not found or not jobs_file.is_file():
        return None
    log_file = folder() / f"{batch_id}.log"
    stamp = tuple((f.stat().st_mtime, f.stat().st_size) for f in (jobs_file, log_file) if f.is_file())
    cached = _disk.get(batch_id)
    if cached and cached[0] == stamp:
        return cached[1]
    urls = [u.strip() for u in jobs_file.read_text(encoding="utf-8").splitlines() if u.strip()]
    started = datetime.strptime(found[1] + found[2], "%Y%m%d%H%M%S")
    b = {
        "id": batch_id, "state": "starting", "phase": "", "submit": None, "started": started.isoformat(),
        "ended": None, "jobs": [{"n": n, "url": u, "state": "waiting", "events": []} for n, u in enumerate(urls, 1)],
        "log": [], "exit": None, "past": True,
    }  # fmt: skip
    current, finished = None, False
    if log_file.is_file():
        for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
            b["log"].append(line)
            current = _apply(b, line, current)
            finished = finished or line.strip().startswith("Batch done")
        b["ended"] = datetime.fromtimestamp(log_file.stat().st_mtime).isoformat(timespec="seconds")
    b["state"] = "done" if finished else "stopped"
    b["phase"] = "Done" if finished else "Ended before it finished (stopped, or the app was closed)"
    for job in b["jobs"]:
        if job["state"] in {"waiting", "running"}:
            job["state"] = "not run"
    _disk[batch_id] = (stamp, b)
    return b


def waiting():
    """The running batch's jobs waiting for your answers: [{n, title, company, questions}]."""
    with _lock:
        live = [b for b in _batches.values() if b["state"] in {"starting", "planning", "running"}]
        return [
            {"batch": b["id"], "n": j["n"], "title": j.get("title") or j["url"], "company": j.get("company"),
             "questions": j["waiting"].split(" | ")}
            for b in live
            for j in b["jobs"]
            if j.get("waiting")
        ]  # fmt: skip


def counts(b):
    """How many of a batch's jobs ended each way."""
    out = {}
    for job in b["jobs"]:
        state = job["state"].replace(" ", "_")
        out[state] = out.get(state, 0) + 1
    return out


def _row(b, past):
    return {k: b[k] for k in ("id", "state", "phase", "started", "ended", "submit")} | {
        "jobs": len(b["jobs"]),
        "counts": counts(b),
        "past": past,
    }


def listing():
    """Every batch, newest first: this session's (live) and earlier ones from runs/batches/."""
    with _lock:
        live = {b["id"]: _row(b, False) for b in _batches.values()}
    rows = list(live.values())
    for path in folder().glob("*.txt"):
        if path.stem not in live:
            b = from_disk(path.stem)
            if b:
                rows.append(_row(b, True))
    return sorted(rows, key=lambda r: r["started"], reverse=True)


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
        if message.startswith("waiting for your answers"):
            job["waiting"] = message.split("): ", 1)[-1]  # the questions, " | " between them
        elif message.startswith(("answered:", "no answer in time")):
            job.pop("waiting", None)
        if message.startswith("résumé:"):
            job["resume"] = message.split("(", 1)[-1].rstrip(")")
            job["track"] = message.split()[1]
        result = RESULT.match(message)
        if result:
            kind = result[1]
            if kind == "needs you":
                job["needs"] = result[2]
            else:
                job.pop("waiting", None)  # the job ended: nothing waits any more
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
