"""What the UI shows, read from the same files the command line uses: your tracks (data/<name>/profile.json),
data/applied.json, the run reports in runs/ and data/QUESTIONS.md. Nothing here changes a profile."""

import argparse
import json
import os
import re
from functools import lru_cache
from pathlib import Path

from .. import applied, inbox
from ..jobinfo import company_key

ROOT = Path.cwd()


def data_dir():
    return ROOT / "data"


def runs_dir():
    return ROOT / "runs"


def track_folders():
    """data/<name> folders holding a profile.json: your résumé tracks (data itself when there are none)."""
    base = data_dir()
    found = sorted(p for p in base.iterdir() if p.is_dir() and (p / "profile.json").is_file()) if base.is_dir() else []
    return found or ([base] if (base / "profile.json").is_file() else [])


def rel(path):
    try:
        return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


@lru_cache(maxsize=8)
def _load(folder, stamp):
    from ..cli import load

    return load(argparse.Namespace(data=folder, profile=None, answers=None, policy=None))


def load_track(folder):
    """(profile, policy) for a track, reloaded when its files change."""
    folder = Path(folder) if Path(folder).is_absolute() else ROOT / folder
    stamp = tuple(
        (p.name, p.stat().st_mtime) for p in sorted(folder.glob("*.json")) + [data_dir() / "QUESTIONS.md"] if p.exists()
    )
    return _load(str(folder.resolve()), stamp)


def tracks():
    """Each track with its résumé, headline and the skills it lists."""
    from ..jobplan import knows

    out = []
    for folder in track_folders():
        profile, policy = load_track(folder)
        resume = profile.documents.get("resume")
        headline = next((f.value for f in profile.facts.values() if f.key == "headline"), "")
        out.append(
            {
                "id": rel(folder),
                "name": folder.name,
                "resume": Path(resume.value).name if resume else None,
                "headline": headline,
                "skills": sorted(knows(profile))[:40],
                "auto_submit": bool(getattr(policy, "auto_submit", False)),
            }
        )
    return out


def resume_track():
    """{normalised résumé file stem: track id}, to tell which résumé a run used."""
    out = {}
    for t in tracks():
        if t["resume"]:
            out[norm(Path(t["resume"]).stem)] = t["id"]
    return out


def norm(text):
    return re.sub(r"[^0-9a-z]", "", str(text or "").lower())


_reports = {}  # report.json path -> ((mtime, size), parsed report)


def read_report(path):
    """A run report, parsed once and reused until its file changes. The Inbox, History and Overview all read every
    report; parsing them all on each call (and once more per job) made the Inbox take seconds."""
    try:
        info = path.stat()
    except OSError:
        return None
    stamp = (info.st_mtime_ns, info.st_size)
    cached = _reports.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    _reports[path] = (stamp, report)
    return report


def run_reports():
    """Every run report, newest first, in a light shape for lists."""
    out = []
    folder = runs_dir()
    if not folder.is_dir():
        return out
    stems = resume_track()
    for path in sorted(folder.glob("*/report.json"), reverse=True):
        report = read_report(path)
        if report is not None:
            out.append(summary(path.parent.name, report, stems))
    return out


def summary(run_id, report, stems=None):
    stems = stems if stems is not None else resume_track()
    used = None
    for h in report.get("history") or []:
        if h.get("kind") in {"click", "upload"}:
            label = norm(h.get("action"))
            used = next((t for s, t in stems.items() if s and s in label), used)
    if used is None:
        for f in report.get("fills") or []:
            if f.get("kind") == "upload":
                used = next((t for s, t in stems.items() if s and s in norm(f.get("value"))), used)
    left = [x.get("what") for x in report.get("left_for_you") or []] + [
        a.get("question") for a in report.get("asked") or [] if not a.get("answered")
    ]
    reason = next((h.get("reason") for h in report.get("handovers") or []), None)
    return {
        "id": run_id,
        "at": run_at(run_id),
        "status": report.get("status"),
        "url": report.get("start_url"),
        "job": applied.board_job(report.get("start_url") or ""),
        "fills": len(report.get("fills") or []),
        "drafts": sum(1 for f in report.get("fills") or [] if f.get("source") == "draft"),
        "left": [x for x in left if x][:8],
        "reason": reason,
        "track": used,
        "elapsed_s": report.get("elapsed_s"),
        "receipt": bool(report.get("receipt")),
        "claude_cost_usd": round(sum(c.get("cost_usd") or 0 for c in report.get("claude") or []), 4) or None,
    }


def run_at(run_id):
    found = re.fullmatch(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", run_id)
    return f"{found[1]}-{found[2]}-{found[3]}T{found[4]}:{found[5]}:{found[6]}" if found else None


def run_detail(run_id, apps=None, stems=None):
    """One run report in full, with each filled field's value, source and the model's confidence. `apps` and
    `stems` are applications() and resume_track() when the caller already has them (the Inbox reads many runs)."""
    if not re.fullmatch(r"[\w.-]+", run_id or ""):
        return None
    report = read_report(runs_dir() / run_id / "report.json")
    if report is None:
        return None
    steps = []
    for h in report.get("history") or []:
        if h.get("kind") == "wait":
            continue
        steps.append(
            {
                "step": h.get("step"),
                "kind": h.get("kind"),
                "action": h.get("action"),
                "context": h.get("context"),
                "source": h.get("source"),
                "typed": h.get("typed"),
                "p": h.get("target_probability"),
                "changed": h.get("page_changed"),
            }
        )
    sent = next((a for a in (applications() if apps is None else apps) if a["run"] == run_id), None)
    return {
        **summary(run_id, report, stems),
        "title": sent["title"] if sent else None,
        "company": sent["company"] if sent else None,
        "final_url": report.get("final_url"),
        "fields": [
            {k: f.get(k) for k in ("label", "value", "source", "kind", "confirmed")} for f in report.get("fills") or []
        ],
        "left_for_you": report.get("left_for_you") or [],
        "asked": report.get("asked") or [],
        "optional_skipped": report.get("optional_skipped") or [],
        "handovers": report.get("handovers") or [],
        "steps": steps,
        "decisions": report.get("decisions"),
        "claude": report.get("claude") or [],
        "receipt": report.get("receipt"),
    }


def applications():
    """Every job in data/applied.json, with the run that sent it and the résumé track it used."""
    path = data_dir() / "applied.json"
    entries = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    runs = {}
    for r in run_reports():  # newest first: the first run per job is its latest
        if r["job"] and r["job"] not in runs:
            runs[r["job"]] = r
    out = []
    for key, e in entries.items():
        run = runs.get(key) or {}
        title = re.sub(r"\s*\|\s*LinkedIn\s*$", "", e.get("title") or "")
        role, _, last = title.rpartition("|")  # "Software Engineer (.NET | AI | Backend) | Gainserv"
        company = e.get("company") or last.strip()
        role = role.strip() if role else title
        if company and role.endswith(f"| {company}"):
            role = role[: -len(company) - 2].strip(" |")
        out.append(
            {
                "key": key,
                "site": key.split(":")[0],
                "title": role,
                "company": company,
                "company_key": company_key(company),
                "url": e.get("url"),
                "at": e.get("at"),
                "status": e.get("status"),
                "confirmed": "no confirmation" not in (e.get("status") or ""),
                "track": run.get("track"),
                "run": run.get("id"),
                "receipt": run.get("receipt", False),
            }
        )
    return sorted(out, key=lambda a: a.get("at") or "", reverse=True)


def questions():
    path = data_dir() / "QUESTIONS.md"
    entries = inbox.read(path)
    return [{"index": i, **e} for i, e in enumerate(entries)]


def answer_question(index, answer, question=None):
    """Save your answer to one question in QUESTIONS.md (every later form reuses it). `question` (its text) finds
    it even when a running batch has added questions since the page listed them and the numbers moved; the save
    is read back and retried, as that batch may write the file at the same moment."""
    path = data_dir() / "QUESTIONS.md"
    answer = " ".join(str(answer or "").split())
    for _ in range(3):
        entries = inbox.read(path)
        found = next((i for i, e in enumerate(entries) if question and e["question"] == question), None)
        if found is None:
            if question or not 0 <= index < len(entries):
                raise IndexError("no such question")
            found = index
        entries[found]["answer"] = answer
        inbox.write(path, entries)
        _load.cache_clear()
        kept = next((e for e in inbox.read(path) if e["question"] == entries[found]["question"]), None)
        if kept and kept["answer"] == answer:
            return kept
    return entries[found]


MODELS = [
    {
        "id": "clef",
        "name": "Clef-Flash",
        "maker": "Cloudflare · 9B · runs on your GPU",
        "about": "Decision model that scores every option in one pass. On your profile it answered 18 of 26 field "
        "wordings right (Laya: 3). Needs about 9 GB of free memory while a batch runs.",
    },
    {
        "id": "laya",
        "name": "Laya",
        "maker": "Convai · 441M · runs on this PC",
        "about": "Small and light. Unsure about most fields, so it asks you more often.",
    },
    {
        "id": "typesafe",
        "name": "Jev",
        "maker": "TypeSafe · hosted API",
        "about": "TypeSafe's decision model through its API (TYPESAFE_API_KEY). Paid per decision.",
    },
    {
        "id": "llm",
        "name": "Chat model",
        "maker": "Any OpenAI-compatible API",
        "about": "A general chat model (DECISION_MODEL) that also steers through the page. Not recommended.",
    },
]


def models():
    """Every decision model, whether it can run now (and what it still needs), and which one is set."""
    from ..cli import backend_problem

    current = (os.environ.get("DECISION_BACKEND") or "laya").strip().lower()
    return [{**m, "current": m["id"] == current, "problem": backend_problem(m["id"])} for m in MODELS]


def set_model(name):
    """Make `name` the decision model: DECISION_BACKEND in .env (what the terminal reads) and this process."""
    if name not in {m["id"] for m in MODELS}:
        raise ValueError("unknown decision model")
    from .manage import set_env

    set_env({"DECISION_BACKEND": name})  # the next batch inherits it


def system():
    """The decision model, Clef's server and memory, the browser connection: what a run needs."""
    from .. import clef_backend
    from ..cli import backend_problem

    backend = (os.environ.get("DECISION_BACKEND") or "laya").strip().lower()
    out = {"backend": backend, "problem": backend_problem(), "drafting": os.environ.get("TEXT_MODEL_PROVIDER") or None}
    folders = track_folders()
    if folders:
        profile, _ = load_track(folders[0])
        first = profile.facts.get("personal.first_name")
        years = profile.facts.get("work.total_experience_whole_years") or profile.facts.get(
            "work.total_experience_years"
        )
        out["name"] = first.value if first else None
        out["years"] = float(years.value) if years else None
    if backend == "clef":
        s = clef_backend.settings()
        free = clef_backend.commit_free_gb()
        need = clef_backend.needed_gb(s) if s["path"] and Path(s["path"]).is_file() else None
        out["clef"] = {
            "running": clef_backend.healthy(s["base"]),
            "base": s["base"],
            "model": s["model"],
            "path": s["path"],
            "memory_free_gb": round(free, 1) if free is not None else None,
            "memory_needed_gb": round(need + 1, 1) if need else None,
        }
    return out


# Parts of a profile that belong to one résumé (its own headline, skills, jobs as told for that track, file):
# never copied to the other tracks when you save a change "to all tracks".
TRACK_ONLY = {"_readme", "headline", "skills", "experience", "documents"}


def track_folder(track):
    """The folder of a known track id ('data/dotnet'), or ValueError: never a path the caller made up."""
    found = next((f for f in track_folders() if rel(f) == track), None)
    if found is None:
        raise ValueError("unknown track")
    return found


def get_profile(track):
    from .manage import documents

    folder = track_folder(track)
    return {
        "track": track,
        "path": rel(folder / "profile.json"),
        "profile": _read(folder / "profile.json"),
        "documents": documents(track),
    }


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def changes(old, new, path=()):
    """The paths whose value differs between two profiles (a list counts as one value)."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in dict.fromkeys([*old, *new]):
            if key not in new:
                out.append((*path, key))
            elif key not in old:
                out.append((*path, key))
            else:
                out += changes(old[key], new[key], (*path, key))
        return out
    return [] if old == new else [path]


def _set(target, path, source):
    """Copy the value at `path` in `source` into `target` (removing it when `source` no longer has it)."""
    for key in path[:-1]:
        source = source.get(key) if isinstance(source, dict) else None
        if not isinstance(target.get(key), dict):
            target[key] = {}
        target = target[key]
    if isinstance(source, dict) and path[-1] in source:
        target[path[-1]] = source[path[-1]]
    else:
        target.pop(path[-1], None)


def save_profile(track, profile, apply_to=()):
    """Save a track's profile (checked by loading it, the old one kept in .backups/), and copy the changed shared
    facts to the `apply_to` tracks. Returns {track: [changed paths]}."""
    from ..profile import Profile

    if not isinstance(profile, dict):
        raise ValueError("a profile is a JSON object")
    folder = track_folder(track)
    old = _read(folder / "profile.json")
    changed = changes(old, profile)
    targets = {track: (folder, profile)}
    for other in apply_to:
        if other == track:
            continue
        other_folder = track_folder(other)
        theirs = _read(other_folder / "profile.json")
        shared = [p for p in changed if p and p[0] not in TRACK_ONLY]
        for p in shared:
            _set(theirs, p, profile)
        targets[other] = (other_folder, theirs)
    for name, (where, data_) in targets.items():
        try:
            Profile(data_, where)
        except (SystemExit, Exception) as error:  # a profile the agent can't read must never be written
            raise ValueError(f"{name}: this profile wouldn't load ({error})") from None
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = {}
    for name, (where, data_) in targets.items():
        target = where / "profile.json"
        before = target.read_text(encoding="utf-8")
        backups = where / ".backups"
        backups.mkdir(exist_ok=True)
        (backups / f"profile-{stamp}.json").write_text(before, encoding="utf-8")
        target.write_text(json.dumps(data_, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        saved[name] = [".".join(map(str, p)) for p in changes(json.loads(before), data_)]
    _load.cache_clear()
    return saved
