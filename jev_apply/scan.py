"""Find open jobs on company boards whose forms need no account: Greenhouse, Lever and Ashby.

Each lists its postings through a public, keyless JSON API. `jev-apply scan boards.txt` reads every board, keeps
the jobs that match your keywords and location and don't ask for more years than you have, skips jobs you
already applied to, and writes their links for `jev-apply batch`. No model is involved: fit is plain rules.

A boards file has one board per line: `greenhouse:<token>`, `lever:<site>`, `ashby:<org>`, or the board's own
address (https://job-boards.greenhouse.io/<token>, https://jobs.lever.co/<site>, https://jobs.ashbyhq.com/<org>).
"""

import html
import re
from dataclasses import dataclass, field

import httpx

from . import applied

TIMEOUT = 20


@dataclass
class Job:
    provider: str
    company: str
    title: str
    location: str
    url: str
    description: str = ""
    min_years: float | None = None
    reasons: list = field(default_factory=list)


def board(line):
    """('greenhouse' | 'lever' | 'ashby', token) for a boards-file line, or None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    found = re.match(r"^(greenhouse|lever|ashby):\s*([\w.-]+)$", line, re.I)
    if found:
        return found[1].lower(), found[2]
    for pattern, provider in (
        (r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([\w.-]+)", "greenhouse"),
        (r"jobs\.lever\.co/([\w.-]+)", "lever"),
        (r"jobs\.ashbyhq\.com/([\w.-]+)", "ashby"),
    ):
        found = re.search(pattern, line, re.I)
        if found:
            return provider, found[1]
    return None


def text_of(markup):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(markup or "")))).strip()


def fetch(provider, token, client):
    """Every open posting on one board, as Jobs."""
    if provider == "greenhouse":
        data = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
        data.raise_for_status()
        return [
            Job(
                "greenhouse",
                token,
                j.get("title", ""),
                (j.get("location") or {}).get("name", ""),
                # Greenhouse's own hosted form, not the company page that embeds it: same form, stable address.
                f"https://job-boards.greenhouse.io/{token}/jobs/{j['id']}"
                if j.get("id")
                else j.get("absolute_url", ""),
                text_of(j.get("content")),
            )  # fmt: skip
            for j in data.json().get("jobs", [])
        ]
    if provider == "lever":
        data = client.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
        data.raise_for_status()
        return [
            Job(
                "lever",
                token,
                j.get("text", ""),
                (j.get("categories") or {}).get("location", "") or "",
                (j.get("hostedUrl") or "").rstrip("/") + "/apply",
                j.get("descriptionPlain") or text_of(j.get("description")),
            )  # fmt: skip
            for j in data.json()
        ]
    if provider == "ashby":
        data = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
        data.raise_for_status()
        return [
            Job(
                "ashby",
                token,
                j.get("title", ""),
                j.get("location", "") or "",
                j.get("applyUrl") or j.get("jobUrl", ""),
                j.get("descriptionPlain") or text_of(j.get("descriptionHtml")),
            )  # fmt: skip
            for j in data.json().get("jobs", [])
            if j.get("isListed", True)
        ]
    raise ValueError(f"unknown board provider {provider!r}")


YEARS = re.compile(r"(\d{1,2})(?:\.\d)?\s*(?:\+|-|–|to)?\s*(?:\d{1,2}\s*)?\+?\s*(?:years|yrs)", re.I)
SENIOR = re.compile(r"\b(senior|sr|lead|staff|principal|manager|director|head|architect|vp)\b", re.I)


def min_years(description):
    """The smallest 'N years' a description asks for (under 20), or None when it names none."""
    found = [int(n) for n in YEARS.findall(description or "") if int(n) < 20]
    return min(found) if found else None


# India by default: its cities, the country, or a remote role that doesn't name another country.
INDIA = (
    r"india|,\s*in\s*$|bengaluru|bangalore|mumbai|pune|hyderabad|chennai|delhi|gurugram|gurgaon|noida|kolkata|"
    r"ahmedabad|vadodara|coimbatore|kochi|jaipur|udaipur|indore|chandigarh|anywhere|worldwide|global|^\s*remote\s*$"
)
FOREIGN_REMOTE = re.compile(r"remote\s*[-–(,:]\s*(?!india)[a-z]", re.I)


def mentions(keyword, text):
    """A keyword as a whole word ("RAG" is not in "storage"); symbols like "C#" or ".NET" match as written."""
    k = re.escape(keyword.lower())
    return re.search(rf"(?<![a-z0-9]){k}(?![a-z0-9])", text) is not None


def fits(job, keywords, location, max_years, include_senior=False):
    """Why this job is a fit (non-empty list), or [] when it isn't. A keyword counts in the title, or when the
    description names at least two of them: one passing word in a long description is not the role."""
    title, description = job.title.lower(), job.description.lower()
    in_title = [k for k in keywords if k and mentions(k, title)]
    in_text = [k for k in keywords if k and mentions(k, description)]
    hits = list(dict.fromkeys(in_title + in_text))
    if keywords and not in_title and len(in_text) < 2:
        return []
    place = job.location or ""
    if location and (not re.search(location, place, re.I) or FOREIGN_REMOTE.search(place)):
        return []
    if not include_senior and SENIOR.search(job.title):
        return []
    job.min_years = min_years(job.description)
    if job.min_years is not None and max_years is not None and job.min_years > max_years:
        return []
    reasons = [f"matches {', '.join(hits)}" if hits else "any role", f"asks {job.min_years:g}+ years"
               if job.min_years is not None else "years not stated"]  # fmt: skip
    if job.provider == "lever":
        reasons.append("Lever: you click Submit (captcha)")
    return reasons


def scan(lines, keywords=(), location=INDIA, max_years=None, applied_path=None,
         include_senior=False, client=None):  # fmt: skip
    """(fitting jobs, problems) across every board in `lines`."""
    done = applied.read(applied_path) if applied_path else {}
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": "systemone-apply"})
    jobs, problems, seen = [], [], set()
    try:
        for line in lines:
            spec = board(line)
            if spec is None:
                if line.strip() and not line.strip().startswith("#"):
                    problems.append(f"not a Greenhouse, Lever or Ashby board: {line.strip()}")
                continue
            try:
                found = fetch(*spec, client)
            except (httpx.HTTPError, ValueError) as error:
                problems.append(
                    f"{spec[0]}:{spec[1]}: {str(error).splitlines()[0]} (board names can be case-sensitive)"
                )
                continue
            for job in found:
                key = applied.job_key(job.url)
                if not job.url or key in seen or key in done:
                    continue
                job.reasons = fits(job, keywords, location, max_years, include_senior)
                if job.reasons:
                    seen.add(key)
                    jobs.append(job)
    finally:
        if own:
            client.close()
    return jobs, problems
