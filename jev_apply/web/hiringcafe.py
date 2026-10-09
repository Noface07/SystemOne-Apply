"""hiring.cafe as a job source: the "hiring.cafe" switch in Find jobs.

hiring.cafe collects postings from companies' own careers pages (Workday, Greenhouse, Lever, SmartRecruiters...) and
links each to the employer's application. github.com/umur957/hiring-cafe-job-scraper (MIT) read it by POSTing a
searchState to /api/search-jobs; that endpoint is gone (405), and plain HTTP to the site now gets Cloudflare's
"Just a moment" page. The site renders a search server-side instead: hiringcafe.com/?searchState=<json>&page=<n>
carries that page's results in its __NEXT_DATA__ (pageProps.ssrHits, about 40 jobs a page, with
ssrIsLastPage). So each page is opened in a background tab of your Chrome (Browser Harness), where Cloudflare's
check passes, and its data is read. Only public search pages are read; nothing is sent (AGENTS rule 18)."""

import json
import re
import time
import urllib.parse
from datetime import datetime, timezone

from ..scan import INDIA

SITE = "https://hiringcafe.com"
PAGES = 5  # per role and location: about 200 jobs, most relevant first
LOAD_S = 40  # Cloudflare's check plus a page load
POSTED_DAYS = {"day": 2, "week": 7, "month": 30}  # "day" asks for two: hiring.cafe dates by when it fetched a job
WORKPLACES = {"onsite": "Onsite", "hybrid": "Hybrid", "remote": "Remote"}
COUNTRIES = {
    "india": ("India", "IN"), "united states": ("United States", "US"), "usa": ("United States", "US"),
    "united kingdom": ("United Kingdom", "GB"), "uk": ("United Kingdom", "GB"), "germany": ("Germany", "DE"),
    "canada": ("Canada", "CA"), "singapore": ("Singapore", "SG"), "netherlands": ("Netherlands", "NL"),
    "united arab emirates": ("United Arab Emirates", "AE"), "uae": ("United Arab Emirates", "AE"),
    "australia": ("Australia", "AU"), "ireland": ("Ireland", "IE"),
}  # fmt: skip
# Company portals that make you create an account before applying (the agent waits for you to sign in once).
ACCOUNT_PORTALS = re.compile(
    r"workday|icims|oracle|successfactors|taleo|eightfold|avature|zoho|paradox|ttcportals|phenom|brassring", re.I
)
SENIORITY = ["No Prior Experience Required", "Entry Level", "Mid Level"]


def country(location):
    """(name, ISO code) of the country a location names, India for an Indian city, else None."""
    low = (location or "").lower()
    for key, found in COUNTRIES.items():
        if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", low):
            return found
    return COUNTRIES["india"] if re.search(INDIA, low) and "remote" not in low else None


def place_matches(place, wanted):
    """Whether a job's location fits a location you searched: any place in the country when you named a country,
    else the city (or region) by name."""
    want = (wanted or "").strip().lower()
    if not want or want in COUNTRIES or want == "remote":
        return True
    city = re.split(r",", want)[0].strip()
    aliases = {"bangalore": "bengaluru", "bengaluru": "bangalore", "gurgaon": "gurugram", "gurugram": "gurgaon",
               "bombay": "mumbai", "madras": "chennai"}  # fmt: skip
    low = (place or "").lower()
    return city in low or (aliases.get(city, "\0") in low)


def search_state(role, location, posted, workplaces=(), include_senior=False):
    """hiring.cafe's searchState for one role. Fields left out keep the site's own defaults."""
    state = {
        "searchQuery": role,
        "dateFetchedPastNDays": POSTED_DAYS.get(posted, 7),
        "workplaceTypes": [WORKPLACES[w] for w in workplaces if w in WORKPLACES] or list(WORKPLACES.values()),
        "seniorityLevel": SENIORITY + (["Senior Level"] if include_senior else []),
        "roleYoeRange": [0, 20],
        "excludeIfRoleYoeIsNotSpecified": False,
        "sortBy": "default",
    }
    found = country(location)
    if found:
        name, code = found
        state["locations"] = [{
            "formatted_address": name, "types": ["country"], "id": "user_country",
            "address_components": [{"long_name": name, "short_name": code, "types": ["country"]}],
            "options": {"flexible_regions": []},  # the scraper's "anywhere_in_continent/world" would leave the country
        }]  # fmt: skip
    return state


def page_url(state, page=0):
    return f"{SITE}/?searchState={urllib.parse.quote(json.dumps(state))}" + (f"&page={page}" if page else "")


def posted_at(value):
    """ISO timestamp (UTC) from hiring.cafe's date: an ISO string or epoch seconds/milliseconds."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            n = float(value)
            return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, timezone.utc).isoformat(timespec="seconds")
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


def salary(v5):
    """'12-18 LPA' for a yearly salary range in rupees, else None."""
    low, high = v5.get("yearly_min_compensation"), v5.get("yearly_max_compensation")
    if v5.get("listed_compensation_currency") != "INR" or not isinstance(low, (int, float)) or not low:
        return None
    high = high if isinstance(high, (int, float)) and high >= low else low
    return f"{low / 1e5:g}-{high / 1e5:g} LPA" if high != low else f"{low / 1e5:g} LPA"


def parse(hit):
    """One search result as a job: title, company, location, the employer's application link, what the job asks
    (hiring.cafe's summary of its requirements, activities and tools: the result carries no full description), when
    it was posted, the years it asks and the skills it names. None without an application link."""
    hit = hit.get("_source", hit) if isinstance(hit, dict) else {}
    v5 = hit.get("v5_processed_job_data") or {}
    info = hit.get("job_information") or {}
    url = hit.get("apply_url") or info.get("apply_url") or ""
    if not url.startswith("http") or hit.get("is_expired"):
        return None
    # The enriched name: v5's company_name is sometimes the board's token ("Ag" for Airbus, "Hpe").
    company = (hit.get("enriched_company_data") or {}).get("name") or v5.get("company_name") or ""
    years = v5.get("min_industry_and_role_yoe")
    portal = str(hit.get("source") or "")
    tools = [str(s) for s in v5.get("technical_tools") or []]
    about = [v5.get("requirements_summary") or "", ", ".join(v5.get("role_activities") or []), ", ".join(tools)]
    return {
        "url": url,
        "title": (info.get("title") or v5.get("core_job_title") or "").strip(),
        "company": str(company).strip(),
        "location": v5.get("formatted_workplace_location") or "",
        "workplace": v5.get("workplace_type") or "",
        "posted": posted_at(v5.get("estimated_publish_date") or v5.get("estimated_publish_date_millis")),
        "years": float(years) if isinstance(years, (int, float)) else None,
        "skills": tools[:16],
        "salary": salary(v5),
        "portal": portal,
        "account": bool(ACCOUNT_PORTALS.search(f"{portal} {url}")),
        "description": ". ".join(a for a in about if a),
        "hc_id": str(hit.get("id") or hit.get("objectID") or "") or None,
    }


# This page's search results from its server-rendered data, once the page for that search and page number is in.
READ = """(() => {
  const node = document.querySelector('#__NEXT_DATA__');
  const p = node && JSON.parse(node.textContent).props?.pageProps;
  if (!p || !Array.isArray(p.ssrHits)) return null;
  return JSON.stringify({page: p.ssrPage, last: p.ssrIsLastPage, total: p.ssrTotalCount, error: p.ssrError,
                         query: p.initialSearchState?.searchQuery, hits: p.ssrHits});
})()"""


LOST = re.compile(r"session with given id not found|no target with given id|target closed|session closed", re.I)


LOST_AGAIN = (
    "hiring.cafe: its Chrome tab closed again, so the rest of hiring.cafe was skipped. Leave that background tab "
    "open while a search runs (or press Stop search)."
)


class TabLost(RuntimeError):
    """The tab is gone: closed by you, or Chrome discarded it."""


class PageStuck(RuntimeError):
    """A page never showed its results: most likely Cloudflare is asking Chrome to prove it's a person."""


class Tab:
    """A background tab in your Chrome that opens hiring.cafe's search pages and reads their data."""

    def __init__(self, transport=None):
        if transport is None:
            import os

            os.environ.setdefault("JEV_BACKGROUND_TABS", "1")  # read in the background, don't steal your screen
            from ..transport import ChromeTransport

            transport = ChromeTransport()
        self.transport = transport

    def call(self, method, **params):
        try:
            return self.transport.call(method, **params)
        except Exception as error:  # noqa: BLE001 - Browser Harness raises plain exceptions carrying CDP's error
            if LOST.search(str(error)):
                raise TabLost(str(error)) from None
            raise RuntimeError(str(error)[:160]) from None

    def evaluate(self, expression):
        out = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        return (out or {}).get("result", {}).get("value")

    def search(self, state, page, should_stop=None):
        """(results, whether this was the last page) for one page of a search."""
        self.call("Page.navigate", url=page_url(state, page))
        deadline = time.monotonic() + LOAD_S
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                return [], True
            time.sleep(0.8)
            try:
                found = json.loads(self.evaluate(READ) or "null")
            except (ValueError, RuntimeError):  # the page is between documents (a closed tab raises TabLost)
                found = None
            if found and found.get("page") == page and found.get("query") == state["searchQuery"]:
                if found.get("error"):
                    raise RuntimeError(f"hiring.cafe: {str(found['error'])[:120]}")
                return found["hits"] or [], bool(found.get("last"))
        raise PageStuck("its page didn't load in Chrome (a Cloudflare check?). Open hiringcafe.com in Chrome once.")

    def close(self):
        try:
            self.transport.close()
        except Exception:  # the tab is already gone
            pass


def search(roles, locations, posted="week", workplaces=(), include_senior=False, on_jobs=None, tab=None, pages=PAGES,
           should_stop=None, new_tab=None):  # fmt: skip
    """Every hiring.cafe job for each role in each location: (jobs, problems). `on_jobs(list)` gets each page's
    new jobs as they arrive, so the UI shows them before the whole search ends; `should_stop()` ends it early.

    A closed tab is reopened once; if it goes again, or a page never loads (Cloudflare), the rest of hiring.cafe
    is skipped with one message, instead of failing (and waiting) once per role."""
    jobs, problems, seen = [], [], set()
    new_tab = new_tab or Tab
    own = tab is None
    try:
        tab = tab or new_tab()
    except Exception as error:  # noqa: BLE001 - Browser Harness missing, Chrome closed, debugging not allowed
        return [], [f"hiring.cafe needs your Chrome (Browser Harness): {str(error).splitlines()[0][:160]}"]
    reopened = False
    stop = should_stop or (lambda: False)
    try:
        for location in locations or [""]:
            for role in roles:
                state = search_state(role, location, posted, workplaces, include_senior)
                page = 0
                while page < pages:
                    if stop():
                        return jobs, problems
                    try:
                        hits, last = tab.search(state, page, stop)
                    except TabLost:
                        if reopened:
                            problems.append(LOST_AGAIN)
                            return jobs, problems
                        reopened = True
                        try:
                            tab, own = new_tab(), True
                        except Exception as error:  # noqa: BLE001
                            problems.append(f"hiring.cafe: its tab closed and Chrome couldn't open another: {error}")
                            return jobs, problems
                        continue  # the same page again, in the new tab
                    except PageStuck as error:
                        problems.append(f"hiring.cafe: {error} The rest of hiring.cafe was skipped.")
                        return jobs, problems
                    except (RuntimeError, ValueError) as error:
                        problems.append(f"hiring.cafe '{role}': {error}")
                        break
                    page += 1
                    fresh = []
                    for hit in hits:
                        job = parse(hit)
                        if not job or job["url"] in seen or not place_matches(job["location"], location):
                            continue
                        seen.add(job["url"])
                        job["query"] = role
                        fresh.append(job)
                    jobs += fresh
                    if fresh and on_jobs:
                        on_jobs(fresh)
                    if last or not hits:
                        break
    finally:
        if own:
            tab.close()
    return jobs, problems
