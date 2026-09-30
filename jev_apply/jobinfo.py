"""A job's company, title and description before its form is opened, from public, keyless sources:
LinkedIn's guest job page, and the Greenhouse, Lever and Ashby posting APIs. Used to pick the résumé, spread
applications across companies and find the skills a job will ask about. Nothing is sent anywhere.
"""

import html
import re
from dataclasses import dataclass

import httpx

TIMEOUT = 20
UA = {"User-Agent": "Mozilla/5.0 (systemone-apply)"}


@dataclass
class JobInfo:
    url: str
    provider: str
    company: str = ""
    title: str = ""
    description: str = ""


def text_of(markup):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(markup or "")))).strip()


def company_key(name):
    """One key per company, whatever the suffix: 'Zuci Systems' and 'Zuci Systems Pvt. Ltd.' are one company."""
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    noise = {"pvt", "private", "ltd", "limited", "llc", "inc", "corp", "corporation", "co", "technologies",
             "technology", "solutions", "services", "india", "the", "group", "global"}  # fmt: skip
    kept = [w for w in words if w not in noise]
    return " ".join(kept or words)


def linkedin(job, client):
    # LinkedIn's guest pages refuse quick successive requests (429, or its own 999): pace them and retry.
    import time

    for attempt in range(3):
        time.sleep(0.8 + 2.5 * attempt)
        page = client.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job}")
        if page.status_code not in {429, 999} and page.status_code < 500:
            break
    page.raise_for_status()
    s = page.text

    def grab(pattern):
        found = re.search(pattern, s, re.S)
        return text_of(found[1]) if found else ""

    return (
        grab(r"topcard__org-name-link[^>]*>(.*?)</a>") or grab(r"topcard__flavor[^>]*>(.*?)</span>"),
        grab(r"top-card-layout__title[^>]*>(.*?)</h2>"),
        grab(r"show-more-less-html__markup[^>]*>(.*?)</div>"),
    )


def fetch(url, client=None):
    """JobInfo for a LinkedIn, Greenhouse, Lever or Ashby job, or None for other sites (or when it's gone)."""
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=UA)
    try:
        found = re.search(r"linkedin\.com/jobs/(?:view/|collections/[^?]*currentJobId=)(\d+)", url or "")
        if found:
            company, title, description = linkedin(found[1], client)
            return JobInfo(url, "linkedin", company, title, description)
        found = re.search(r"(?:job-)?boards\.greenhouse\.io/([\w.-]+)/jobs/(\d+)", url or "")
        if found:
            data = client.get(f"https://boards-api.greenhouse.io/v1/boards/{found[1]}/jobs/{found[2]}").json()
            return JobInfo(url, "greenhouse", data.get("company_name") or found[1], data.get("title", ""),
                           text_of(data.get("content")))  # fmt: skip
        found = re.search(r"jobs(\.eu)?\.lever\.co/([\w.-]+)/([0-9a-f-]{36})", url or "")
        if found:
            api = "api.eu.lever.co" if found[1] else "api.lever.co"
            data = client.get(f"https://{api}/v0/postings/{found[2]}/{found[3]}").json()
            return JobInfo(url, "lever", found[2], data.get("text", ""),
                           data.get("descriptionPlain") or text_of(data.get("description")))  # fmt: skip
        found = re.search(r"jobs\.ashbyhq\.com/([\w.%-]+)/([0-9a-f-]{36})", url or "")
        if found:
            data = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{found[1]}").json()
            job = next((j for j in data.get("jobs", []) if j.get("id") == found[2]), None)
            if job:
                return JobInfo(url, "ashby", found[1], job.get("title", ""),
                               job.get("descriptionPlain") or text_of(job.get("descriptionHtml")))  # fmt: skip
        return None
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if own:
            client.close()
