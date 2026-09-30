"""data/applied.json: the jobs the agent submitted (auto_submit), so no batch ever applies to one twice."""

import json
import re
from datetime import datetime
from pathlib import Path

# The same job under different addresses (tracking parameters, search pages) has one key.
KEYS = (
    (re.compile(r"linkedin\.com/jobs/(?:view|collections/[^?]*currentJobId=)/?(\d+)"), "linkedin"),
    (re.compile(r"currentJobId=(\d+)"), "linkedin"),
    (re.compile(r"indeed\.[a-z.]+/.*[?&](?:jk|vjk)=([0-9a-f]{16})"), "indeed"),
    (re.compile(r"naukri\.com/job-listings-[^?#]*?-(\d{9,})"), "naukri"),
    (re.compile(r"greenhouse\.io/.*?/jobs/(\d+)"), "greenhouse"),
    (re.compile(r"(?:lever\.co|ashbyhq\.com)/[^/]+/([0-9a-f-]{36})"), "board"),
)


def board_job(url):
    """'site:id' for a job board or applicant-system posting, else None (a plain page has no job identity)."""
    for pattern, site in KEYS:
        found = pattern.search(url or "")
        if found:
            return f"{site}:{found.group(1)}"
    return None


def job_key(url):
    for pattern, site in KEYS:
        found = pattern.search(url or "")
        if found:
            return f"{site}:{found.group(1)}"
    return re.sub(r"[?#].*$", "", (url or "").lower()).rstrip("/")


def read(path):
    path = Path(path) if path else None
    return json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else {}


def submitted(path, *urls):
    """The record of an earlier submission of this job, or None."""
    done = read(path)
    return next((done[job_key(u)] for u in urls if u and job_key(u) in done), None)


def record(path, urls, status, title="", company=""):
    if not path:
        return
    done = read(path)
    entry = {"status": status, "title": title[:120], "url": urls[0], "at": datetime.now().isoformat(timespec="seconds")}
    if company:
        entry["company"] = company
    for url in urls:
        if url:
            done[job_key(url)] = entry
    Path(path).write_text(json.dumps(done, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def company_of(entry):
    """The company of a recorded application: saved with it, or read from a LinkedIn title ('Role | Company |
    LinkedIn')."""
    if entry.get("company"):
        return entry["company"]
    parts = [p.strip() for p in (entry.get("title") or "").split("|")]
    return parts[-2] if len(parts) >= 3 and parts[-1].lower() == "linkedin" else ""


def recent_by_company(path, days, now=None):
    """{company key: number of applications in the last `days` days}, from applied.json."""
    from .jobinfo import company_key

    now = now or datetime.now()
    counts = {}
    seen = set()
    for entry in read(path).values():
        key = (entry.get("url"), entry.get("at"))  # one application can be stored under several job keys
        if key in seen:
            continue
        seen.add(key)
        try:
            when = datetime.fromisoformat(entry.get("at", ""))
        except ValueError:
            continue
        company = company_key(company_of(entry))
        if company and (now - when).days < days:
            counts[company] = counts.get(company, 0) + 1
    return counts
