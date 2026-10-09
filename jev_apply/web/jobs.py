"""Finding jobs for the UI, role first: you name the roles you'd take and the filters (locations, workplace, date
posted, years), and every source is searched for them: LinkedIn's public job search (no login), hiring.cafe (when
switched on) and the Greenhouse, Lever and Ashby boards `jev-apply scan` reads. Your résumés don't narrow the search;
each job only shows which of them fits it best and what it names that they don't. Only public pages are read;
nothing is sent (AGENTS rule 18).

A search runs in the background: the result cards come first, then each job's description is read (paced, as
LinkedIn refuses quick successive requests) for the years it asks, the skills it names, whether it has closed and
which résumé fits it best. The UI follows it live (server.py streams the changes)."""

import html
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .. import applied, jobplan, skills
from ..jobinfo import JobInfo, company_key
from ..scan import SENIOR, min_years
from . import data

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"}
SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
CARD = re.compile(
    r'data-entity-urn="urn:li:jobPosting:(\d+)".*?base-search-card__title">\s*(.*?)\s*</h3>'
    r'.*?base-search-card__subtitle">.*?>\s*(.*?)\s*</a>.*?job-search-card__location">\s*(.*?)\s*</span>'
    r'(?:.*?<time[^>]*datetime="([^"]+)"[^>]*>\s*(.*?)\s*</time>)?',
    re.S,
)
POSTED = {"day": "r86400", "week": "r604800", "month": "r2592000"}
WORKPLACE = {"onsite": "1", "remote": "2", "hybrid": "3"}  # LinkedIn's f_WT values
SOURCES = {"linkedin", "hiringcafe"}
PAGES = 4  # LinkedIn result pages (25 jobs each) per role and location
# Role suggestions per track name: a starting point the UI offers, not a limit.
DEFAULT_QUERIES = {
    "dotnet": ["C# .NET developer", "ASP.NET Core developer", ".NET backend engineer"],
    "genai": ["GenAI engineer", "LLM engineer", "AI engineer LangChain", "Generative AI developer"],
    "automation": ["SCADA engineer", "Industrial IoT engineer", "OPC UA", "IIoT software engineer"],
}

_tasks = {}
_lock = threading.Lock()


def default_queries():
    out = {}
    for t in data.tracks():
        out[t["id"]] = DEFAULT_QUERIES.get(t["name"]) or [w for w in re.split(r"[,/|]", t["headline"]) if w.strip()][:3]
    return out


def as_roles(queries):
    """A role list from either shape: a list of roles, or the older {track: [search words]}."""
    if isinstance(queries, dict):
        queries = [q for words in queries.values() for q in words or []]
    return list(dict.fromkeys(str(q).strip() for q in queries or [] if str(q).strip()))


AGO = re.compile(r"(\d+)\s*(second|minute|hour|day|week|month)")
PER = {"second": 1 / 3600, "minute": 1 / 60, "hour": 1, "day": 24, "week": 168, "month": 720}


def age_hours(job, now=None):
    """How long ago a job was posted, in hours, or None when it doesn't say. LinkedIn's card says "3 hours ago" (and
    a date); hiring.cafe gives a timestamp."""
    text = (job.get("posted_text") or "").lower()
    found = AGO.search(text)
    if found:
        return int(found[1]) * PER[found[2]]
    if re.search(r"just now|moments? ago", text):
        return 0.0
    posted = str(job.get("posted") or "")
    if not posted:
        return None
    try:
        when = datetime.fromisoformat(posted.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    if len(posted) <= 10:  # a bare date: count from the end of that day, so today's jobs are fresh
        when = when.replace(hour=23, minute=59)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (now - when).total_seconds() / 3600)


def freshen(job, now=None):
    """Mark a job posted in the last 24 hours: those come first in results and in autopilot."""
    hours = age_hours(job, now)
    job["age_hours"] = None if hours is None else round(hours, 1)
    job["fresh"] = hours is not None and hours <= 24
    return job


def same_job(job):
    """One key for the same posting found on two sites: company and title, ignoring punctuation and suffixes."""
    title = re.sub(r"[^a-z0-9]+", " ", (job.get("title") or "").lower()).strip()
    return f"{company_key(job.get('company'))}|{title}"


def known_jobs():
    """Job keys already applied to or tried (applied.json, applied.md, earlier job lists)."""
    keys = set()
    path = data.data_dir() / "applied.json"
    if path.is_file():
        import json

        keys |= set(json.loads(path.read_text(encoding="utf-8")))
    for name in ("applied.md",):
        p = data.ROOT / name
        if p.is_file():
            keys |= {f"linkedin:{j}" for j in re.findall(r"jobs/view/(\d+)", p.read_text(encoding="utf-8"))}
    return keys


def recent_companies(days=14):
    path = data.data_dir() / "applied.json"
    return applied.recent_by_company(path, days) if path.is_file() else {}


def start_search(roles, location="India", posted="week", max_years=None, easy_apply=False, include_senior=False,
                 workplaces=(), sources=("linkedin",), locations=None, pages=PAGES):  # fmt: skip
    """Begin a search for `roles` (a list of role names; the older {track: [words]} also works); returns its id."""
    roles = as_roles(roles)
    locations = [x.strip() for x in (locations or [location]) if x and x.strip()] or ["India"]
    sources = [x for x in sources if x in SOURCES] or ["linkedin"]
    task = {
        "id": uuid.uuid4().hex[:10],
        "state": "searching",
        "progress": [0, 0],
        "jobs": [],
        "problems": [],
        "params": {
            "roles": roles,
            "locations": locations,
            "posted": posted,
            "max_years": max_years,
            "easy_apply": easy_apply,
            "workplaces": list(workplaces),
            "sources": sources,
        },  # fmt: skip
    }
    with _lock:
        _tasks[task["id"]] = task
    args = (task, roles, locations, posted, max_years, easy_apply, include_senior, list(workplaces), sources, pages)
    threading.Thread(target=_run_search, args=args, daemon=True).start()
    return task["id"]


def get_search(task_id):
    with _lock:
        task = _tasks.get(task_id)
        return None if task is None else {**task, "jobs": [dict(j) for j in task["jobs"]]}


_stopping = set()  # searches you stopped: each loop checks between requests


def stop_search(task_id):
    """Stop a running search where it is: what it found so far stays listed. False when there's no such search."""
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return False
        if task["state"] != "done":
            _stopping.add(task_id)
            task["state"] = "stopping"
    return True


def _get(client, url, **params):
    for attempt in range(3):
        time.sleep(0.9 + 2.5 * attempt)
        response = client.get(url, params=params or None)
        if response.status_code not in {429, 999} and response.status_code < 500:
            return response
    return response


def _add(task, job, seen):
    """Add a found job unless the same posting is already listed (by its link, or by company and title when two
    sites list it). Returns whether it was added."""
    twin = same_job(job)
    if job["key"] in seen or (twin != "|" and twin in seen):
        return False
    seen.update({job["key"], twin})
    with _lock:
        task["jobs"].append(job)
    return True


def _run_search(task, roles, locations, posted, max_years, easy_apply, include_senior, workplaces, sources, pages):
    seen, known, recent = set(), known_jobs(), recent_companies()
    profiles = {t["id"]: data.load_track(t["id"])[0] for t in data.tracks()}
    stopped = lambda: task["id"] in _stopping  # noqa: E731
    try:
        with httpx.Client(headers=UA, timeout=20, follow_redirects=True) as client:
            if "linkedin" in sources and not stopped():
                _linkedin_cards(task, client, roles, locations, posted, easy_apply, workplaces, pages, seen, known,
                                recent, stopped)  # fmt: skip
            if "hiringcafe" in sources and not stopped():
                task["state"] = "searching hiring.cafe"
                _hiringcafe(task, roles, locations, posted, workplaces, include_senior, profiles, max_years, seen,
                            known, recent, stopped)  # fmt: skip
            if not stopped():
                task["state"] = "reading"
            # Newest first, so stopping early still leaves the jobs posted in the last 24 hours read.
            todo = sorted((j for j in task["jobs"] if j["state"] == "listed" and not j["known"]),
                          key=lambda j: not j.get("fresh"))  # fmt: skip
            task["progress"] = [0, len(todo)]
            for job in todo:
                if stopped():
                    break
                _enrich(job, client, profiles, max_years, include_senior)
                task["progress"][0] += 1
    except httpx.HTTPError as error:
        task["problems"].append(f"network: {error}")
    unread = 0
    for job in task["jobs"]:
        if job["state"] == "listed":
            unread += not job["known"] and stopped()
            job["state"] = "read" if job["known"] else "unread" if stopped() else "unreadable"
    if stopped():
        task["problems"].append(
            "Stopped by you. Everything found so far is listed"
            + (f"; {unread} job(s) weren't read yet (no score or years)." if unread else ".")
        )
        _stopping.discard(task["id"])
    task["state"] = "done"


def _linkedin_cards(task, client, roles, locations, posted, easy_apply, workplaces, pages, seen, known, recent,
                    stopped=lambda: False):  # fmt: skip
    for location in locations:
        for role in roles:
            for start in range(0, 25 * pages, 25):
                if stopped():
                    return
                params = {"keywords": role, "location": location, "start": start}
                if posted in POSTED:
                    params["f_TPR"] = POSTED[posted]
                if easy_apply:
                    params["f_AL"] = "true"
                if workplaces:
                    params["f_WT"] = ",".join(WORKPLACE[w] for w in workplaces if w in WORKPLACE)
                response = _get(client, SEARCH, **params)
                if response.status_code != 200:
                    task["problems"].append(f"LinkedIn '{role}' in {location}: answered {response.status_code}")
                    break
                cards = CARD.findall(response.text)
                for job_id, title, company, where, when, when_text in cards:
                    title, company = html.unescape(title).strip(), html.unescape(company).strip()
                    if "<" in company:
                        continue
                    key = f"linkedin:{job_id}"
                    job = freshen({
                        "id": job_id, "key": key, "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                        "title": title, "company": company, "location": html.unescape(where).strip(),
                        "posted": when or None, "posted_text": html.unescape(when_text or "").strip() or None,
                        "query": role, "known": key in known, "recent_company": recent.get(company_key(company), 0),
                        "senior": bool(SENIOR.search(title)), "state": "listed", "provider": "linkedin",
                    })  # fmt: skip
                    _add(task, job, seen)
                if len(cards) < 25:
                    break


def _hiringcafe(task, roles, locations, posted, workplaces, include_senior, profiles, max_years, seen, known, recent,
                stopped=lambda: False):  # fmt: skip
    """hiring.cafe's results come with their description, so each is assessed as it lands."""
    from . import hiringcafe

    def land(found):
        for hit in found:
            key = applied.job_key(hit["url"]) or hit["url"]
            company = hit["company"]
            job = freshen({
                "id": None, "key": key, "url": hit["url"], "title": hit["title"], "company": company,
                "location": hit["location"], "posted": hit["posted"], "query": hit["query"], "known": key in known,
                "recent_company": recent.get(company_key(company), 0), "senior": bool(SENIOR.search(hit["title"])),
                "state": "listed", "provider": "hiringcafe", "portal": hit["portal"], "account": hit["account"],
                "workplace": hit["workplace"],
            })  # fmt: skip
            if _add(task, job, seen):
                text = hit["description"] or " ".join(hit["skills"])
                assess(job, text, False, profiles, max_years, include_senior, years=hit["years"])
                job["salary"] = job.get("salary") or hit["salary"]

    _, problems = hiringcafe.search(roles, locations, posted, workplaces, include_senior, on_jobs=land,
                                    should_stop=stopped)  # fmt: skip
    task["problems"] += problems


def _enrich(job, client, profiles, max_years, include_senior):
    """Read a LinkedIn job's description (guest page), then assess it."""
    try:
        response = _get(client, POSTING.format(job["id"]))
    except httpx.HTTPError:
        job["state"] = "unreadable"
        return
    if response.status_code != 200:
        job["state"] = "unreadable"
        return
    found = re.search(r"show-more-less-html__markup[^>]*>(.*?)</div>", response.text, re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", found[1])) if found else ""
    closed = "No longer accepting applications" in response.text
    if not job.get("title") or not job.get("company"):
        company = re.search(r"topcard__org-name-link[^>]*>(.*?)</a>", response.text, re.S)
        title = re.search(r"top-card-layout__title[^>]*>(.*?)</h2>", response.text, re.S)
        job["company"] = job.get("company") or (
            html.unescape(re.sub(r"<[^>]+>", "", company[1])).strip() if company else ""
        )
        job["title"] = job.get("title") or (html.unescape(re.sub(r"<[^>]+>", "", title[1])).strip() if title else "")
    assess(job, text, closed, profiles, max_years, include_senior)


def coverage(job_skills, profile_skills):
    """Which of the skills a job names your résumé lists: covered (the same skill), related (another skill of the
    same line of work, e.g. ASP.NET Core for ASP.NET MVC) and missing."""
    mine = {s.lower() for s in profile_skills}
    families = {skills.family(s) for s in profile_skills}
    out = {"covered": [], "related": [], "missing": []}
    for skill in dict.fromkeys(job_skills):
        if skill.lower() in mine:
            out["covered"].append(skill)
        elif skills.family(skill) != skill and skills.family(skill) in families:
            out["related"].append(skill)
        else:
            out["missing"].append(skill)
    total = len(out["covered"]) + len(out["related"]) + len(out["missing"])
    out["pct"] = round(100 * (len(out["covered"]) + 0.5 * len(out["related"])) / total) if total else None
    return out


def assess(job, text, closed, profiles, max_years, include_senior, years=None):
    """Years asked, skills named, keyword coverage, the résumé that fits, score, reasons, and whether it's hidden.
    `years` is the source's own reading of the years asked (hiring.cafe), used when the text names none."""
    from . import features

    job["closed"] = closed
    job["min_years"] = min_years(f"{job['title']} {text}")  # "Agent Developer 5+ years experience"
    if job["min_years"] is None and years is not None:
        job["min_years"] = int(years) if float(years).is_integer() else years
    job["skills"] = skills.mentioned(f"{job['title']} {text}")[:16]
    info = JobInfo(job["url"], job.get("provider") or "linkedin", job["company"], job["title"], text)
    if len(profiles) > 1:
        track, scores = jobplan.choose_track(info, profiles, next(iter(profiles)))
        job["track"], job["scores"] = track, scores
    else:
        job["track"] = next(iter(profiles), None)
    prefs = features.get_prefs()
    job["salary"] = features.salary(text)
    track_profile = profiles.get(job.get("track")) if job.get("track") else None
    known = jobplan.knows(track_profile) if track_profile else set()
    job["coverage"] = coverage(job["skills"], known)
    job["score"] = features.score(job, known, prefs, max_years)
    too_many = max_years is not None and job["min_years"] is not None and job["min_years"] > max_years
    remote_miss = prefs.get("remote_only") and "remote" not in f"{job['location']} {text[:4000]}".lower()
    job["blocked"] = features.blocked(job["company"], job["title"], text, prefs)
    job["dismissed"] = job["key"] in features.dismissed()
    hidden = [
        (job["closed"], "closed"),
        (job["dismissed"], "dismissed by you"),
        (job["blocked"], job["blocked"]),
        (too_many, f"asks {job['min_years']}+ years"),
        (job["senior"] and not include_senior, "senior title"),
        (remote_miss, "not remote"),
    ]
    job["why_not"] = next((why for hit, why in hidden if hit), None)
    job["fit"] = job["why_not"] is None and not job["known"]
    job["reasons"] = reasons(job, prefs, max_years)
    job["state"] = "read"


def reasons(job, prefs, max_years):
    """Why a job scores as it does, good and bad, in plain words."""
    out = []
    cov = job.get("coverage") or {}
    named = len(cov.get("covered", [])) + len(cov.get("related", [])) + len(cov.get("missing", []))
    if named:
        related = f" (+{len(cov['related'])} related)" if cov["related"] else ""
        text = f"Your résumé covers {len(cov['covered'])} of {named} skills it names{related}"
        out.append(("+" if cov["pct"] >= 60 else "-", text))
    title = {skills.family(s) for s in skills.mentioned(job.get("title") or "")}
    if title:
        on = title & {skills.family(s) for s in cov.get("covered", []) + cov.get("related", [])}
        out.append(
            ("+", f"Title is on your {', '.join(sorted(on))} track")
            if on
            else ("-", "Title names skills you don't list")
        )
    years = job.get("min_years")
    if years is not None and max_years is not None:
        out.append(
            ("+", f"Asks {years}+ years (you have {max_years})")
            if years <= max_years
            else ("-", f"Asks {years}+ years, more than your {max_years}")
        )
    cities = [c for c in prefs.get("preferred_cities") or [] if c.lower() in (job.get("location") or "").lower()]
    if cities:
        out.append(("+", f"In {cities[0]}, a city you prefer"))
    if job.get("salary"):
        out.append(("+", f"Pays {job['salary']}"))
    if cov.get("missing"):
        out.append(("-", "Missing: " + ", ".join(cov["missing"][:6])))
    return [{"good": sign == "+", "text": text} for sign, text in out]


def start_links(urls, max_years=None, include_senior=True):
    """Assess pasted job links (LinkedIn, Greenhouse, Lever, Ashby; other sites are listed as they are)."""
    from .. import jobinfo

    task = {
        "id": uuid.uuid4().hex[:10],
        "state": "reading",
        "progress": [0, 0],
        "jobs": [],
        "problems": [],
        "links": True,
    }
    known = known_jobs()
    for url in dict.fromkeys(u.strip() for u in urls if u.strip().startswith("http")):
        key = applied.job_key(url) or url
        found = re.search(r"linkedin\.com/jobs/(?:view/|collections/[^?]*currentJobId=)(\d+)", url) or re.search(
            r"currentJobId=(\d+)", url
        )
        task["jobs"].append({"id": found[1] if found else None, "key": key, "url": url, "title": "", "company": "",
                             "location": "", "known": key in known, "senior": False,
                             "state": "listed", "provider": "linkedin" if found else None})  # fmt: skip
    task["progress"] = [0, len(task["jobs"])]
    with _lock:
        _tasks[task["id"]] = task
    profiles = {t["id"]: data.load_track(t["id"])[0] for t in data.tracks()}

    def run():
        with httpx.Client(headers=UA, timeout=20, follow_redirects=True) as client:
            for job in task["jobs"]:
                try:
                    if job["id"]:
                        _enrich(job, client, profiles, max_years, include_senior)
                    else:
                        info = jobinfo.fetch(job["url"], client)
                        if info:
                            job.update(title=info.title, company=info.company, provider=info.provider)
                            assess(job, info.description, False, profiles, max_years, include_senior)
                        else:
                            note = "A site jev-apply can't read ahead: it is assessed when it opens"
                            job.update(title=job["url"].split("//")[-1][:70], state="read", fit=not job["known"],
                                       reasons=[{"good": False, "text": note}])  # fmt: skip
                except (httpx.HTTPError, ValueError) as error:
                    job["state"] = "unreadable"
                    task["problems"].append(f"{job['url'][:60]}: {error}")
                task["progress"][0] += 1
        task["state"] = "done"

    threading.Thread(target=run, daemon=True).start()
    return task["id"]


def scan_boards(lines, keywords=(), max_years=None):
    """Greenhouse, Lever and Ashby jobs through the existing scanner (`jev-apply scan`). The roles you search for
    are the keywords: a job matches when its title names one (or its description names two)."""
    from .. import scan as scanner

    jobs, problems = scanner.scan(lines, keywords, scanner.INDIA, max_years, data.data_dir() / "applied.json")
    return [
        {"url": j.url, "title": j.title, "company": j.company, "location": j.location, "reasons": j.reasons}
        for j in jobs
    ], problems


def boards_file():
    for name in ("boards.txt", "boards.example.txt"):
        p = data.data_dir() / name
        if p.is_file():
            return Path(p)
    return None
