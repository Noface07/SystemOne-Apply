"""The app's bigger tools: safety settings per track (policy.json), search preferences and saved searches, Clef's
server controls, results by track, the review queue, drafting a profile from a résumé and the daily autopilot.
Each reads and writes the same files the command line uses."""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path

from ..policy import Policy
from . import data

# ---- 4. safety settings per track -----------------------------------------------------------------------------

SAFETY = [
    # (field, label, help)
    ("auto_submit", "Auto-submit", "Off: each stops before Submit. On: complete ones only, never twice."),
    ("submit_drafts", "Send drafts unread", "Off: an application with a drafted answer waits for you."),
    ("drafts", "Open questions", "confirm: Claude drafts the answer. ask: always ask you."),
    ("min_target_probability", "How sure the model must be to click", "0 to 1. Higher = more is left for you."),
    ("min_operation_probability", "How sure the model must be of the step", "0 to 1."),
    ("min_value_margin", "Lead over the runner-up answer", "0 to 1. A closer call is left for you."),
    ("per_company", "Applications per company", "In the window below. 0 turns the limit off."),
    ("company_gap_days", "Company window (days)", "e.g. 14: at most the number above in any 14 days."),
    ("auto_consent", "Tick plain privacy/terms boxes", "Declarations (criminal record...) are always asked."),
    ("google_sign_in", "Sign in with Google for me", "Clicks only: your account, basic profile sharing. Never types."),
    ("login_wait_s", "Wait at a sign-in page (s)", "How long a run waits for you to sign in. 0 stops at once."),
    ("confirm_wait_s", "Wait for the site's confirmation (s)", "After Submit."),
]  # fmt: skip
SAFETY_FIELDS = {s[0] for s in SAFETY}


def get_safety(track):
    folder = data.track_folder(track)
    path = folder / "policy.json"
    overrides = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    policy = Policy.load(path)
    defaults = Policy()
    return [
        {
            "field": name,
            "label": label,
            "help": about,
            "value": getattr(policy, name),
            "default": getattr(defaults, name),
            "custom": name in overrides,
        }
        for name, label, about in SAFETY
    ]


def save_safety(track, values):
    """Write the changed safety settings into data/<track>/policy.json (overrides only), checked by loading."""
    folder = data.track_folder(track)
    path = folder / "policy.json"
    overrides = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    types = {f.name: f.type for f in fields(Policy)}
    for name, value in values.items():
        if name not in SAFETY_FIELDS:
            raise ValueError(f"{name} isn't a safety setting")
        kind = types[name]
        if kind in (bool, "bool"):
            value = bool(value)
        elif kind in (int, "int"):
            value = int(value)
        elif kind in (float, "float"):
            value = float(value)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if name.endswith(("_s", "_days")) or name == "per_company":
            if value < 0:
                raise ValueError(f"{name} can't be negative")
        if value == getattr(Policy(), name):
            overrides.pop(name, None)  # the default again: no override needed
        else:
            overrides[name] = value
    Policy(**{k: v for k, v in overrides.items() if not k.startswith("_") and k in types})  # raises if invalid
    path.write_text(json.dumps(overrides, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    data._load.cache_clear()
    return get_safety(track)


# ---- 5. search preferences, saved searches, ranking --------------------------------------------------------------

PREFS_DEFAULT = {
    "blocked_companies": [],
    "blocked_keywords": [],
    "preferred_cities": [],
    "remote_only": False,
    "saved_searches": [],
}


def prefs_path():
    return data.data_dir() / "search.json"


def get_prefs():
    path = prefs_path()
    stored = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {**PREFS_DEFAULT, **{k: v for k, v in stored.items() if k in PREFS_DEFAULT}}


def save_prefs(values):
    prefs = get_prefs()
    for key, value in values.items():
        if key not in PREFS_DEFAULT:
            raise ValueError(f"{key} isn't a search preference")
        if key == "remote_only":
            prefs[key] = bool(value)
        elif key == "saved_searches":
            if not isinstance(value, list) or not all(isinstance(s, dict) and s.get("name") for s in value):
                raise ValueError("saved searches need a name each")
            prefs[key] = value[:30]
        else:
            prefs[key] = [str(v).strip() for v in value if str(v).strip()][:200]
    prefs_path().write_text(json.dumps(prefs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return prefs


def blocked(company, title, text, prefs):
    from ..jobplan import blocked as rule

    return rule(company, title, text, prefs)


SALARY = re.compile(
    r"(?:₹|inr|rs\.?)?\s*(\d{1,2}(?:\.\d)?)\s*(?:-|–|to)\s*(\d{1,2}(?:\.\d)?)\s*(?:lpa|lakhs?|l\b|lacs?)", re.I
)


def salary(text):
    """'8-12 LPA' when a description states a range in lakhs, else None."""
    found = SALARY.search(text or "")
    if not found:
        return None
    low, high = float(found[1]), float(found[2])
    return f"{low:g}-{high:g} LPA" if 0 < low <= high <= 200 else None


def score(job, profile_skills, prefs, max_years):
    """0-100: how much of what the job names your matched résumé lists, its title on your track, the years it
    asks against yours, and your preferred cities. A ranking aid; the fixed filters still decide what is hidden."""
    from .. import skills as vocab

    wanted = {vocab.family(s) for s in job.get("skills") or []}
    mine = {vocab.family(s) for s in profile_skills}
    match = len(wanted & mine) / len(wanted) if wanted else 0.4
    title = {vocab.family(s) for s in vocab.mentioned(job.get("title") or "")}
    points = 60 * match + (25 if title & mine else 10 if not title else 0)
    years = job.get("min_years")
    if years is not None and max_years is not None:
        points += 15 if years <= max_years else max(-30, -10 * (years - max_years))
    else:
        points += 8
    cities = [c.lower() for c in prefs.get("preferred_cities") or []]
    if cities and any(c in (job.get("location") or "").lower() for c in cities):
        points += 10
    return max(0, min(100, round(points)))


# ---- 7. Clef controls -------------------------------------------------------------------------------------------

_clef = {"state": "idle", "error": None}


def gpu():
    """The GPU's name and memory from nvidia-smi, or None without one."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    name, used, total, util = [x.strip() for x in out.splitlines()[0].split(",")]
    return {"name": name, "used_mb": int(used), "total_mb": int(total), "util": int(util)}


def clef_status():
    from .. import clef_backend

    s = clef_backend.settings()
    free = clef_backend.commit_free_gb()
    need = clef_backend.needed_gb(s) if s["path"] and Path(s["path"]).is_file() else None
    running = clef_backend.healthy(s["base"])
    state = "running" if running else _clef["state"] if _clef["state"] == "starting" else "stopped"
    return {
        "state": state,
        "error": _clef["error"],
        "owned": clef_backend._server is not None,
        "memory_free_gb": round(free, 1) if free is not None else None,
        "memory_needed_gb": round(need + 1, 1) if need else None,
        "gpu": gpu(),
    }


def clef_start():
    from .. import clef_backend

    if clef_backend.healthy() or _clef["state"] == "starting":
        return clef_status()
    _clef.update(state="starting", error=None)

    def run():
        try:
            clef_backend.prepare()
            _clef["state"] = "running"
        except Exception as error:  # shown on the page
            _clef.update(state="stopped", error=str(error))

    threading.Thread(target=run, daemon=True).start()
    return clef_status()


def clef_stop():
    from .. import clef_backend

    clef_backend.stop()
    _clef.update(state="stopped", error=None)
    return clef_status()


# ---- 8. results by track ----------------------------------------------------------------------------------------


def insights():
    """Per résumé track: runs, outcomes, time per job, why runs stopped; applications per week per track."""
    runs = data.run_reports()
    apps = data.applications()
    by_track = {}
    for r in runs:
        t = by_track.setdefault(r["track"] or "unknown", {"runs": 0, "outcomes": {}, "seconds": [], "reasons": {}})
        t["runs"] += 1
        status = str(r["status"])
        outcome = (
            "submitted"
            if status.startswith("submitted")
            else "already applied"
            if NOTHING_TO_DO.search(r.get("reason") or "") or status.startswith("already")
            else status.split(" ")[0]
        )
        t["outcomes"][outcome] = t["outcomes"].get(outcome, 0) + 1
        if r.get("elapsed_s"):
            t["seconds"].append(r["elapsed_s"])
        why = r.get("reason") or (r["left"][0] if r["left"] else None)
        if why and outcome != "submitted":
            key = re.sub(r"\s+", " ", re.sub(r"(?<![a-z])'[^']{1,80}'(?![a-z])", "'…'", why))[:70]
            t["reasons"][key] = t["reasons"].get(key, 0) + 1
    tracks = []
    for name, t in by_track.items():
        sent = t["outcomes"].get("submitted", 0)
        tracks.append(
            {
                "track": name,
                "runs": t["runs"],
                "submitted": sent,
                "rate": round(sent / t["runs"], 2) if t["runs"] else 0,
                "outcomes": t["outcomes"],
                "avg_seconds": round(sum(t["seconds"]) / len(t["seconds"])) if t["seconds"] else None,
                "reasons": sorted(t["reasons"].items(), key=lambda kv: -kv[1])[:5],
            }
        )
    weeks = {}
    for a in apps:
        if not a["at"]:
            continue
        day = datetime.fromisoformat(a["at"][:19])
        week = (day - timedelta(days=day.weekday())).date().isoformat()
        weeks.setdefault(week, {})
        weeks[week][a["track"] or "unknown"] = weeks[week].get(a["track"] or "unknown", 0) + 1
    companies = {}
    for a in apps:
        companies[a["company"] or "?"] = companies.get(a["company"] or "?", 0) + 1
    return {
        "tracks": sorted(tracks, key=lambda t: -t["runs"]),
        "weeks": dict(sorted(weeks.items())[-12:]),
        "companies": sorted(companies.items(), key=lambda kv: -kv[1])[:12],
        "total_runs": len(runs),
        "total_sent": len(apps),
    }


# ---- 3. review queue ------------------------------------------------------------------------------------------


def _inbox_state():
    path = data.data_dir() / "inbox.json"
    return path, (json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {})


# Stops that need nothing from you: the job is gone or already done.
NOTHING_TO_DO = re.compile(r"no longer open|closed or expired|already applied|already submitted", re.I)


def queue():
    """What needs you, newest first: jobs a run left for you (with why), drafted answers to read and the open
    questions. Jobs you've since applied to and items you've marked done are left out."""
    _, done = _inbox_state()
    applied_keys = {a["key"] for a in data.applications()}
    seen, items = set(), []
    for r in data.run_reports():
        if not r["job"] or r["job"] in seen:
            continue
        seen.add(r["job"])  # the latest run of each job only
        status = str(r["status"])
        if r["job"] in applied_keys and not status.startswith("submitted"):
            continue
        detail = data.run_detail(r["id"]) or {}
        drafts = [f for f in detail.get("fields") or [] if f.get("source") == "draft"]
        for f in drafts:
            iid = f"draft:{r['id']}:{hashlib.md5(f['label'].encode()).hexdigest()[:10]}"
            if iid not in done:
                items.append({"id": iid, "kind": "draft", "run": r["id"], "url": r["url"], "track": r["track"],
                              "at": r["at"], "title": f["label"][:140], "text": f["value"],
                              "sent": status.startswith("submitted")})  # fmt: skip
        recent = (r["at"] or "") >= (datetime.now() - timedelta(days=14)).isoformat()
        if (
            recent
            and status.startswith(("review", "stopped", "ready"))
            and (r["left"] or r["reason"])
            and not NOTHING_TO_DO.search(r["reason"] or "")
        ):
            iid = f"job:{r['id']}"
            if iid not in done:
                items.append({"id": iid, "kind": "job", "run": r["id"], "url": r["url"], "track": r["track"],
                              "at": r["at"], "status": status, "reason": r["reason"], "left": r["left"]})  # fmt: skip
    questions = [q for q in data.questions() if not q["answer"]]
    return {"items": items[:200], "open_questions": len(questions)}


def mark_done(item_id, done=True):
    path, state = _inbox_state()
    if done:
        state[item_id] = datetime.now().isoformat(timespec="seconds")
    else:
        state.pop(item_id, None)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return queue()


# ---- 6. draft a profile from a résumé ---------------------------------------------------------------------------

_learning = {}


def start_learn(track, use_claude=True):
    """Read the track's résumé into profile.learned.json in the background (profile.json is never touched)."""
    folder = data.track_folder(track)
    profile = data._read(folder / "profile.json")
    resume = (profile.get("documents") or {}).get("resume") or {}
    path = folder / str(resume.get("path") or "")
    if not resume or not path.is_file():
        raise ValueError("this track has no résumé file yet: upload it first")
    task = {"id": uuid.uuid4().hex[:8], "track": track, "state": "reading", "notes": [], "diffs": []}
    _learning[task["id"]] = task

    def run():
        from .. import learn

        try:
            _, draft, notes = learn.learn(path, folder, use_claude=use_claude)
            current = data._read(folder / "profile.json")
            task["diffs"] = [
                {"key": k, "learned": new, "current": old}
                for k, new, old in learn.differences(draft, current)
                if not k.startswith("documents")
            ]
            task["notes"] = notes
            task["state"] = "done"
        except Exception as error:  # shown on the page
            task.update(state="failed", notes=[str(error)])

    threading.Thread(target=run, daemon=True).start()
    return task["id"]


def learn_status(task_id):
    return _learning.get(task_id)


def apply_learned(track, keys):
    """Copy the chosen keys of profile.learned.json into profile.json (old one kept in .backups/)."""
    from .manage import write_profile

    folder = data.track_folder(track)
    learned_path = folder / "profile.learned.json"
    if not learned_path.is_file():
        raise ValueError("no draft yet: read the résumé first")
    learned = data._read(learned_path)
    profile = data._read(folder / "profile.json")
    for key in keys:
        if key.startswith(("documents", "_")):
            continue
        parts = key.split(".")
        source, target = learned, profile
        for p in parts[:-1]:
            source = source.get(p) if isinstance(source, dict) else None
            target = target.setdefault(p, {}) if isinstance(target, dict) else None
        if not isinstance(source, dict) or parts[-1] not in source or not isinstance(target, dict):
            raise ValueError(f"{key} isn't in the draft")
        value = source[parts[-1]]
        mine = target.get(parts[-1])
        if (
            isinstance(mine, dict)
            and set(mine) <= {"value", "about"}
            and "value" in mine
            and not isinstance(value, dict)
        ):
            mine["value"] = value  # keep its description
        else:
            target[parts[-1]] = value
    from ..profile import Profile

    Profile(profile, folder)  # raises if the result wouldn't load
    write_profile(folder, profile)
    return data.get_profile(track)


# ---- 1. autopilot: search and apply every day -------------------------------------------------------------------

AUTOPILOT_DEFAULT = {
    "enabled": False, "time": "09:00", "tracks": [], "max_jobs": 10, "min_score": 60, "submit": True, "posted": "day"
}  # fmt: skip
TASK_NAME = "jev-apply-autopilot"


def autopilot_path():
    return data.data_dir() / "autopilot.json"


def get_autopilot():
    path = autopilot_path()
    stored = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    config = {**AUTOPILOT_DEFAULT, **{k: v for k, v in stored.items() if k in AUTOPILOT_DEFAULT}}
    return {"config": config, "scheduled": task_scheduled(), "last": last_autopilot()}


def save_autopilot(values):
    config = get_autopilot()["config"]
    for key, value in values.items():
        if key not in AUTOPILOT_DEFAULT:
            raise ValueError(f"{key} isn't an autopilot setting")
        config[key] = value
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(config["time"])):
        raise ValueError("time: HH:MM, 24-hour (e.g. 09:00)")
    known = {t["id"] for t in data.tracks()}
    config["tracks"] = [t for t in config["tracks"] if t in known]
    config["max_jobs"] = max(1, min(50, int(config["max_jobs"])))
    config["min_score"] = max(0, min(100, int(config["min_score"])))
    if config["posted"] not in {"day", "week"}:
        raise ValueError("posted: day or week")
    config["enabled"] = bool(config["enabled"])
    config["submit"] = bool(config["submit"])
    autopilot_path().write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    schedule(config)
    return get_autopilot()


def schedule(config):
    """Create, update or remove the Windows scheduled task that runs `jev-apply autopilot` every day."""
    if os.name != "nt":
        return
    if not config["enabled"]:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True)
        return
    script = data.ROOT / "runs" / "autopilot.cmd"
    script.parent.mkdir(exist_ok=True)
    script.write_text(
        f'@echo off\r\ncd /d "{data.ROOT}"\r\n"{sys.executable}" -m jev_apply.cli autopilot '
        f'>> "{data.ROOT / "runs" / "autopilot.log"}" 2>&1\r\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", f'"{script}"', "/SC", "DAILY", "/ST", config["time"], "/F"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Windows didn't accept the schedule: {(result.stderr or result.stdout).strip()[:200]}")


def task_scheduled():
    if os.name != "nt":
        return None
    result = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    nxt = re.search(r"Next Run Time:\s*(.+)", result.stdout)
    return {"next_run": nxt[1].strip() if nxt else None}


def last_autopilot():
    folder = data.runs_dir() / "autopilot"
    found = sorted(folder.glob("*.json")) if folder.is_dir() else []
    return json.loads(found[-1].read_text(encoding="utf-8")) if found else None


def run_autopilot(dry=False, wait_s=900):
    """One autopilot round: search each track (your saved preferences apply), take the best-scoring good fits up
    to max_jobs, apply to them in one batch and write a summary to runs/autopilot/. Returns the summary."""
    from . import jobs

    config = get_autopilot()["config"]
    prefs = get_prefs()
    tracks = config["tracks"] or [t["id"] for t in data.tracks()]
    defaults = jobs.default_queries()
    # The saved search you marked for autopilot (its words per track), else each track's default words.
    chosen = next((s for s in reversed(prefs.get("saved_searches") or []) if s.get("autopilot")), {})
    saved = chosen.get("queries") or {}
    queries = {t: saved.get(t) or defaults.get(t) or [] for t in tracks}
    started = datetime.now()
    system = data.system()
    years = int(system["years"]) if system.get("years") is not None else None
    location = chosen.get("location") or "India"
    task = jobs.start_search(queries, location=location, posted=config["posted"], max_years=years)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        found = jobs.get_search(task)
        if found["state"] == "done":
            break
        time.sleep(2)
    fits = sorted(
        (j for j in found["jobs"] if j.get("fit") and (j.get("score") or 0) >= config["min_score"]),
        key=lambda j: -(j.get("score") or 0),
    )
    picked = fits[: config["max_jobs"]]
    summary = {
        "started": started.isoformat(timespec="seconds"),
        "found": len(found["jobs"]),
        "fits": len(fits),
        "picked": [{k: j.get(k) for k in ("url", "title", "company", "track", "score")} for j in picked],
        "dry": dry,
    }
    if picked and not dry:
        jobs_file = data.runs_dir() / "autopilot" / f"{started:%Y%m%d-%H%M}.txt"
        jobs_file.parent.mkdir(parents=True, exist_ok=True)
        jobs_file.write_text("\n".join(j["url"] for j in picked) + "\n", encoding="utf-8")
        command = [sys.executable, "-m", "jev_apply.cli", "batch", str(jobs_file), "--browser", "chrome",
                   "--tracks", ",".join(tracks)] + (["--submit"] if config["submit"] else [])  # fmt: skip
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        subprocess.run(command, cwd=data.ROOT, env=env, stdin=subprocess.DEVNULL)
        sent = [a for a in data.applications() if (a["at"] or "") >= summary["started"]]
        summary["submitted"] = [{"title": a["title"], "company": a["company"], "url": a["url"]} for a in sent]
        summary["needs_you"] = len(queue()["items"])
    summary["ended"] = datetime.now().isoformat(timespec="seconds")
    out = data.runs_dir() / "autopilot" / f"{started:%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


# ---- dismissed jobs ---------------------------------------------------------------------------------------------


def _dismissed_path():
    return data.data_dir() / "dismissed.json"


def dismissed():
    """{job key: {title, company, at}} for jobs you said you don't want: hidden from searches and autopilot."""
    path = _dismissed_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def dismiss(key, title="", company="", undo=False):
    if not key or len(key) > 300:
        raise ValueError("which job?")
    found = dismissed()
    if undo:
        found.pop(key, None)
    else:
        found[key] = {
            "title": str(title)[:160],
            "company": str(company)[:80],
            "at": datetime.now().isoformat(timespec="seconds"),
        }
    _dismissed_path().write_text(json.dumps(found, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return found


# ---- answer bank ------------------------------------------------------------------------------------------------


def _answer_files():
    """(source label, track or None, path) of every file holding saved answers."""
    from ..policy import Policy

    out = []
    for t in data.tracks():
        folder = data.track_folder(t["id"])
        out.append(("answers", t["id"], folder / "answers.json"))
        out.append(("learned", t["id"], folder / Policy.load(folder / "policy.json").learned_answers))
    return out


def _usage():
    """[(question asked, date)] for every field a run filled from a saved answer."""
    out = []
    for path in sorted(data.runs_dir().glob("*/report.json")) if data.runs_dir().is_dir() else []:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        when = data.run_at(path.parent.name)
        for h in report.get("history") or []:
            if h.get("source") == "saved":
                out.append((h.get("context") or h.get("action") or "", when))
    return out


def answer_bank():
    """Every saved answer, where it lives (QUESTIONS.md, a track's answers.json, answers saved while applying) and
    how often runs typed it, newest use first."""
    from ..profile import answers as same

    used = _usage()
    items = []
    for q in data.questions():
        if q["answer"]:
            items.append({"id": f"questions:{q['index']}", "source": "You, in QUESTIONS.md", "track": None,
                          "question": q["question"], "answer": q["answer"], "options": q["options"]})  # fmt: skip
    for kind, track, path in _answer_files():
        if not path.is_file():
            continue
        try:
            stored = json.loads(path.read_text(encoding="utf-8")).get("answers") or []
        except ValueError:
            continue
        label = "answers.json" if kind == "answers" else "Saved while applying"
        for n, entry in enumerate(stored):
            if entry.get("question") and entry.get("answer") is not None:
                items.append({"id": f"{kind}:{track}:{n}", "source": label, "track": track,
                              "question": entry["question"], "answer": entry["answer"], "options": []})  # fmt: skip
    for item in items:
        hits = [when for asked, when in used if asked and same(item["question"], asked)]
        item["times_used"] = len(hits)
        item["last_used"] = max((w for w in hits if w), default=None)
    return sorted(items, key=lambda i: (-(i["times_used"]), i["question"].lower()))


def _answer_target(answer_id):
    kind, _, rest = answer_id.partition(":")
    if kind == "questions":
        return kind, None, int(rest), None
    track, _, n = rest.rpartition(":")
    path = next((p for k, t, p in _answer_files() if k == kind and t == track), None)
    if path is None or not path.is_file():
        raise ValueError("no such answer")
    return kind, track, int(n), path


def update_answer(answer_id, answer):
    kind, _, n, path = _answer_target(answer_id)
    text = " ".join(str(answer or "").split()) if kind == "questions" else str(answer or "").strip()
    if kind == "questions":
        data.answer_question(n, text)
    else:
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["answers"][n]["answer"] = text
        path.write_text(json.dumps(stored, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    data._load.cache_clear()
    return answer_bank()


def delete_answer(answer_id):
    kind, _, n, path = _answer_target(answer_id)
    if kind == "questions":
        data.answer_question(n, "")  # back to an open question
    else:
        stored = json.loads(path.read_text(encoding="utf-8"))
        del stored["answers"][n]
        path.write_text(json.dumps(stored, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    data._load.cache_clear()
    return answer_bank()


# ---- Claude usage -----------------------------------------------------------------------------------------------


def claude_usage(days=30):
    """Claude calls from runs/claude-usage.jsonl: per stage and per day, time, tokens and cost; the latest calls."""
    path = data.runs_dir() / "claude-usage.jsonl"
    calls = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                calls.append(json.loads(line))
            except ValueError:
                continue
    since = (datetime.now() - timedelta(days=days)).isoformat()
    calls = [c for c in calls if (c.get("at") or "") >= since]

    def total(group):
        times = [c["latency_ms"] for c in group if c.get("latency_ms")]
        return {
            "calls": len(group),
            "failed": sum(1 for c in group if not c.get("ok", True)),
            "input_tokens": sum(c.get("input_tokens") or 0 for c in group),
            "output_tokens": sum(c.get("output_tokens") or 0 for c in group),
            "cache_read_tokens": sum(c.get("cache_read_tokens") or 0 for c in group),
            "cost_usd": round(sum(c.get("cost_usd") or 0 for c in group), 4),
            "avg_seconds": round(sum(times) / len(times) / 1000, 1) if times else None,
        }

    stages, days_ = {}, {}
    for c in calls:
        stages.setdefault(c.get("stage") or "other", []).append(c)
        days_.setdefault((c.get("at") or "")[:10], []).append(c)
    return {
        "days": days,
        "total": total(calls),
        "stages": {k: total(v) for k, v in stages.items()},
        "per_day": {k: total(v) for k, v in sorted(days_.items())},
        "recent": list(reversed(calls[-25:])),
    }
