"""Role-first search: roles instead of résumé tracks, hiring.cafe as a source, newest jobs first, live streams, and
batches from earlier sessions."""

import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")

from jev_apply.web import batches, features, hiringcafe, jobs  # noqa: E402

from .test_web import client, home  # noqa: E402,F401

AUTH = {"x-jev-token": "secret"}
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def test_roles_come_from_a_list_or_the_older_words_per_track():
    assert jobs.as_roles(["Solutions Engineer", " solutions engineer ", "", "Data Analyst"]) == [
        "Solutions Engineer", "solutions engineer", "Data Analyst"
    ]  # fmt: skip
    assert jobs.as_roles({"data/dotnet": ["C# developer"], "data/genai": ["LLM engineer", "C# developer"]}) == [
        "C# developer", "LLM engineer"
    ]  # fmt: skip


def test_jobs_posted_in_the_last_24_hours_are_fresh():
    assert jobs.freshen({"posted_text": "3 hours ago"}, NOW)["fresh"]
    assert jobs.freshen({"posted_text": "Just now"}, NOW)["fresh"]
    assert not jobs.freshen({"posted_text": "2 days ago"}, NOW)["fresh"]
    assert jobs.freshen({"posted": "2026-10-09T03:00:00+00:00"}, NOW)["age_hours"] == 9.0
    assert jobs.freshen({"posted": "2026-10-09"}, NOW)["fresh"]  # a bare date counts from the end of that day
    assert not jobs.freshen({"posted": "2026-10-01"}, NOW)["fresh"]
    unknown = jobs.freshen({}, NOW)
    assert unknown["fresh"] is False and unknown["age_hours"] is None


def test_the_same_posting_from_two_sites_is_listed_once():
    task, seen = {"jobs": []}, set()
    one = {"key": "linkedin:1", "company": "Acme Technologies Pvt Ltd", "title": "Solutions Engineer"}
    twin = {"key": "https://acme.wd1.myworkdayjobs.com/x", "company": "Acme", "title": "Solutions Engineer!"}
    other = {"key": "linkedin:2", "company": "Acme", "title": "Data Analyst"}
    assert jobs._add(task, one, seen) and not jobs._add(task, twin, seen) and jobs._add(task, other, seen)
    assert not jobs._add(task, dict(one), seen)
    assert [j["key"] for j in task["jobs"]] == ["linkedin:1", "linkedin:2"]


HIT = {
    "id": "abc123",
    "apply_url": "https://job-boards.greenhouse.io/devrev/jobs/6139498004",
    "source": "grnhse",
    "job_information": {"title": "Solution Engineer", "job_title_raw": "Solution Engineer"},
    "v5_processed_job_data": {
        "company_name": "DevRev",
        "formatted_workplace_location": "Bengaluru, Karnataka, India",
        "workplace_type": "Hybrid",
        "estimated_publish_date": "2026-10-09T06:00:00Z",
        "min_industry_and_role_yoe": 2,
        "technical_tools": ["REST", "Python"],
        "requirements_summary": "2+ years building REST integrations",
        "role_activities": ["designing solutions", "supporting implementations"],
        "listed_compensation_currency": "INR",
        "yearly_min_compensation": 1200000,
        "yearly_max_compensation": 1800000,
    },  # fmt: skip
}


def test_a_hiring_cafe_result_becomes_a_job():
    job = hiringcafe.parse(HIT)
    assert job["url"] == HIT["apply_url"] and job["title"] == "Solution Engineer" and job["company"] == "DevRev"
    assert job["location"].startswith("Bengaluru") and job["years"] == 2.0 and job["skills"] == ["REST", "Python"]
    assert job["description"] == (
        "2+ years building REST integrations. designing solutions, supporting implementations. REST, Python"
    )
    assert job["salary"] == "12-18 LPA" and not job["account"]
    assert hiringcafe.parse({**HIT, "is_expired": True}) is None
    assert job["posted"] == "2026-10-09T06:00:00+00:00"
    workday = {**HIT, "apply_url": "https://abb.wd3.myworkdayjobs.com/x/job/1", "source": "workday"}
    assert hiringcafe.parse(workday)["account"]  # Workday makes you create an account
    assert hiringcafe.parse({**HIT, "apply_url": ""}) is None
    assert hiringcafe.parse({"_source": HIT})["company"] == "DevRev"
    airbus = {**HIT, "enriched_company_data": {"name": "Airbus"}, "v5_processed_job_data": {"company_name": "Ag"}}
    assert hiringcafe.parse(airbus)["company"] == "Airbus"


def test_hiring_cafe_locations_filter_by_country_then_city():
    assert hiringcafe.country("India") == ("India", "IN")
    assert hiringcafe.country("Pune") == ("India", "IN")  # an Indian city searches India
    assert hiringcafe.country("Berlin, Germany") == ("Germany", "DE")
    assert hiringcafe.country("Atlantis") is None
    assert hiringcafe.place_matches("Bengaluru, Karnataka, India", "Bangalore")
    assert hiringcafe.place_matches("Mumbai, India", "India")
    assert not hiringcafe.place_matches("Mumbai, India", "Pune")
    state = hiringcafe.search_state("Solutions Engineer", "Pune", "day", ["remote", "bogus"], include_senior=True)
    assert state["searchQuery"] == "Solutions Engineer" and state["workplaceTypes"] == ["Remote"]
    assert state["locations"][0]["address_components"][0]["short_name"] == "IN"
    assert "Senior Level" in state["seniorityLevel"] and state["dateFetchedPastNDays"] == 2


class FakeTab:
    """hiring.cafe's search answered from memory: two pages for the first role, one for the second."""

    def __init__(self):
        self.asked = []

    def search(self, state, page, should_stop=None):
        self.asked.append((state["searchQuery"], page))
        if state["searchQuery"] == "Solutions Engineer" and page == 0:
            return [{**HIT, "apply_url": f"https://x.test/{n}"} for n in range(40)], False
        if state["searchQuery"] == "Solutions Engineer" and page == 1:
            other = {**HIT["v5_processed_job_data"], "formatted_workplace_location": "Pune, India"}
            return [HIT, {**HIT, "apply_url": "https://x.test/pune", "v5_processed_job_data": other}], True
        return [HIT], False  # the second role finds a job the first already found, then has no more pages


def test_hiring_cafe_search_pages_through_and_reports_each_page():
    tab, pages = FakeTab(), []
    found, problems = hiringcafe.search(["Solutions Engineer", "Data Analyst"], ["Bengaluru"], on_jobs=pages.append,
                                        tab=tab)  # fmt: skip
    assert problems == []
    assert tab.asked[:3] == [("Solutions Engineer", 0), ("Solutions Engineer", 1), ("Data Analyst", 0)]
    assert len(tab.asked) == 2 + hiringcafe.PAGES  # stops at the last page, or at the page limit
    urls = [j["url"] for j in found]
    assert len(urls) == 41 and "https://x.test/pune" not in urls  # Pune isn't Bengaluru
    assert len(pages) == 2 and all(j["query"] == "Solutions Engineer" for j in found)
    assert "&page=2" in hiringcafe.page_url({"searchQuery": "x"}, 2) and "page=" not in hiringcafe.page_url({}, 0)


def test_hiring_cafe_without_chrome_is_a_problem_not_a_crash(monkeypatch):
    def no_chrome(*a, **k):
        raise RuntimeError("Chrome is asking to allow remote debugging")

    monkeypatch.setattr(hiringcafe, "Tab", no_chrome)
    found, problems = hiringcafe.search(["Solutions Engineer"], ["India"])
    assert found == [] and "needs your Chrome" in problems[0]


def test_search_takes_roles_and_sources(home, monkeypatch):  # noqa: F811
    started = {}
    monkeypatch.setattr(jobs, "start_search", lambda roles, **kw: started.update(roles=roles, **kw) or "t1")
    c = client()
    res = c.post("/api/search", json={"roles": ["Solutions Engineer"], "locations": ["Pune", "Remote"],
                                      "sources": ["linkedin", "hiringcafe", "evil"], "workplaces": ["remote", "x"]},
                 headers=AUTH)  # fmt: skip
    assert res.json() == {"id": "t1"}
    assert started["roles"] == ["Solutions Engineer"] and started["locations"] == ["Pune", "Remote"]
    assert started["sources"] == ["linkedin", "hiringcafe"] and started["workplaces"] == ["remote"]
    assert started["easy_apply"] is False  # every job, not only Easy Apply, unless you ask
    assert c.post("/api/search", json={"roles": []}, headers=AUTH).status_code == 400
    assert c.post("/api/search", json={"roles": ["x"], "sources": ["evil"]}, headers=AUTH).status_code == 400
    defaults = c.get("/api/search/defaults", headers=AUTH).json()
    assert "data/dotnet" in defaults["suggestions"] and defaults["roles"]


def test_a_search_streams_its_state_then_only_changes(home, monkeypatch):  # noqa: F811
    task = {"id": "s1", "state": "done", "progress": [1, 1], "problems": [], "params": {},
            "jobs": [{"key": "linkedin:1", "title": "Solutions Engineer", "fresh": True}]}  # fmt: skip
    monkeypatch.setitem(jobs._tasks, "s1", task)
    c = client()
    assert c.get("/api/search/s1/events").status_code == 401  # the token is needed here too
    assert c.get("/api/search/s1", params={"token": "secret"}).status_code == 401  # only streams take it in the query
    with c.stream("GET", "/api/search/s1/events", params={"token": "secret"}) as res:
        assert res.headers["content-type"].startswith("text/event-stream")
        body = "".join(res.iter_text())
    first = json.loads(body.split("data: ", 1)[1].split("\n", 1)[0])
    assert first["full"] and first["jobs"][0]["key"] == "linkedin:1" and first["state"] == "done"
    assert "event: end" in body
    assert c.get("/api/search/nope/events", params={"token": "secret"}).status_code == 404


def test_batches_from_earlier_sessions_are_rebuilt_from_their_files(home):  # noqa: F811
    folder = batches.folder()
    (folder / "20261008-093000-abcd.txt").write_text("https://a.test/1\nhttps://a.test/2\n", encoding="utf-8")
    (folder / "20261008-093000-abcd.log").write_text(
        "[1/2] https://a.test/1\n  · [1/2] submitted: 4 fields filled\n[2/2] https://a.test/2\n"
        "  · [2/2] review: 1 left for you\nBatch done\n",
        encoding="utf-8",
    )
    (folder / "20261008-120000-beef.txt").write_text("https://a.test/3\n", encoding="utf-8")  # no log: never ran
    (folder / "notes.txt").write_text("not a batch", encoding="utf-8")
    rows = batches.listing()
    assert [r["id"] for r in rows] == ["20261008-120000-beef", "20261008-093000-abcd"]
    done = rows[1]
    assert done["state"] == "done" and done["past"] and done["counts"] == {"submitted": 1, "review": 1}
    assert rows[0]["state"] == "stopped" and rows[0]["counts"] == {"not_run": 1}
    full = batches.get("20261008-093000-abcd", log_all=True)
    assert full["log_n"] == 5 and full["jobs"][0]["state"] == "submitted"
    assert batches.get("../../etc") is None and batches.get("notes") is None


def test_autopilot_runs_the_saved_role_search(home):  # noqa: F811
    features.save_prefs({"saved_searches": [
        {"name": "old", "queries": {"data/dotnet": ["C# developer"]}, "autopilot": False},
        {"name": "daily", "roles": ["Solutions Engineer", "Data Analyst"], "locations": ["Pune"],
         "sources": ["hiringcafe", "evil"], "workplaces": ["remote"], "autopilot": True},
    ]})  # fmt: skip
    search = features.autopilot_search(["data/dotnet"])
    assert search["name"] == "daily" and search["roles"] == ["Solutions Engineer", "Data Analyst"]
    assert (
        search["locations"] == ["Pune"] and search["sources"] == ["hiringcafe"] and search["workplaces"] == ["remote"]
    )
    features.save_prefs(
        {"saved_searches": [{"name": "old", "queries": {"data/dotnet": ["C# dev"]}, "autopilot": True}]}
    )
    assert features.autopilot_search()["roles"] == ["C# dev"]  # an older saved search still works
    features.save_prefs({"saved_searches": []})
    assert features.autopilot_search(["data/dotnet"])["sources"] == ["linkedin"]


def test_autopilot_takes_fresh_jobs_first(home, monkeypatch):  # noqa: F811
    found = {"state": "done", "jobs": [
        {"url": "u1", "fit": True, "score": 95, "fresh": False},
        {"url": "u2", "fit": True, "score": 70, "fresh": True},
        {"url": "u3", "fit": True, "score": 40, "fresh": True},
        {"url": "u4", "fit": False, "score": 99, "fresh": True},
    ]}  # fmt: skip
    monkeypatch.setattr(jobs, "start_search", lambda *a, **k: "t")
    monkeypatch.setattr(jobs, "get_search", lambda t: found)
    monkeypatch.setattr(features, "schedule", lambda config: None)  # never touch the real Windows task
    features.save_autopilot({"min_score": 50, "max_jobs": 5})
    summary = features.run_autopilot(dry=True)
    assert [p["url"] for p in summary["picked"]] == ["u2", "u1"]


class FlakyTab:
    """A tab that fails in a given way on its first `fails` calls, then answers one page per role."""

    def __init__(self, error, fails=1):
        self.error, self.fails, self.calls = error, fails, 0

    def search(self, state, page, should_stop=None):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.error
        return [{**HIT, "apply_url": f"https://x.test/{state['searchQuery']}"}], True

    def close(self):
        pass


def test_a_closed_hiring_cafe_tab_is_reopened_once_then_skipped():
    opened = []

    def new_tab():
        opened.append(1)
        return FlakyTab(hiringcafe.TabLost("Session with given id not found."), fails=0)

    found, problems = hiringcafe.search(["A", "B"], ["India"], tab=FlakyTab(hiringcafe.TabLost("gone")),
                                        new_tab=new_tab)  # fmt: skip
    assert len(opened) == 1 and problems == [] and [j["query"] for j in found] == ["A", "B"]

    def always_gone():
        return FlakyTab(hiringcafe.TabLost("gone"), fails=99)

    found, problems = hiringcafe.search(["A", "B", "C"], ["India"], tab=always_gone(), new_tab=always_gone)
    assert found == [] and len(problems) == 1 and "closed again" in problems[0]  # one message, not one per role


def test_a_hiring_cafe_page_that_never_loads_skips_the_rest_at_once():
    tab = FlakyTab(hiringcafe.PageStuck("its page didn't load"), fails=99)
    found, problems = hiringcafe.search(["A", "B", "C"], ["India"], tab=tab)
    assert tab.calls == 1 and len(problems) == 1 and "rest of hiring.cafe was skipped" in problems[0]


def test_a_lost_tab_is_told_apart_from_other_errors():
    class Gone:
        def call(self, *a, **k):
            raise RuntimeError("{'code': -32001, 'message': 'Session with given id not found.'}")

    with pytest.raises(hiringcafe.TabLost):
        hiringcafe.Tab(Gone()).evaluate("1")


def test_stopping_a_search_keeps_what_it_found(home, monkeypatch):  # noqa: F811
    from threading import Event

    started, release = Event(), Event()

    def slow_cards(task, *args):
        jobs._add(task, {"key": "linkedin:1", "title": "A", "company": "X", "state": "listed", "known": False}, set())
        started.set()
        release.wait(5)

    monkeypatch.setattr(jobs, "_linkedin_cards", slow_cards)
    monkeypatch.setattr(jobs, "_enrich", lambda *a: pytest.fail("a stopped search reads nothing more"))
    task_id = jobs.start_search(["A"], sources=["linkedin"])
    assert started.wait(5)
    c = client()
    assert c.post(f"/api/search/{task_id}/stop", headers=AUTH).json() == {"stopping": True}
    assert jobs.get_search(task_id)["state"] == "stopping"
    release.set()
    for _ in range(50):
        found = jobs.get_search(task_id)
        if found["state"] == "done":
            break
        import time

        time.sleep(0.05)
    assert found["state"] == "done" and found["jobs"][0]["state"] == "unread"
    assert "Stopped by you" in found["problems"][-1]
    assert c.post("/api/search/nope/stop", headers=AUTH).status_code == 404


def test_the_inbox_parses_each_run_report_once(home, monkeypatch):  # noqa: F811
    from jev_apply.web import data

    for n in range(3):
        run = home / "runs" / f"20261009-10000{n}"
        run.mkdir(parents=True)
        report = {"status": "stopped", "start_url": f"https://x.test/{n}", "handovers": [{"reason": "needs you"}]}
        (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    reads = []
    real = data.json.loads
    monkeypatch.setattr(data.json, "loads", lambda text, *a, **k: reads.append(1) or real(text, *a, **k))
    features.queue()
    first = len(reads)
    features.queue()
    assert len(reads) - first <= 1  # the second build reuses every parsed report (applied.json may be read)


def test_a_batch_job_waiting_for_your_answers_shows_until_it_moves_on(monkeypatch):
    b = {"id": "b1", "state": "running", "phase": "", "started": "", "jobs": [{"n": 1, "url": "u1", "state": "running",
         "events": []}]}  # fmt: skip
    current = batches._apply(b, "  · [1/1] waiting for your answers (up to 5 min, in Questions): Notice? | Shifts?", 1)
    assert b["jobs"][0]["waiting"] == "Notice? | Shifts?"
    monkeypatch.setitem(batches._batches, "b1", b)
    assert batches.waiting() == [
        {"batch": "b1", "n": 1, "title": "u1", "company": None, "questions": ["Notice?", "Shifts?"]}
    ]
    batches._apply(b, "  · [1/1] answered: carrying on with this form", current)
    assert "waiting" not in b["jobs"][0] and batches.waiting() == []


def test_jobs_stopped_on_questions_you_have_answered_are_offered_again(home):  # noqa: F811
    from jev_apply import inbox

    def run(name, url, status, waiting=None):
        folder = home / "runs" / name
        folder.mkdir(parents=True)
        report = {"status": status, "start_url": url, "final_url": url}
        if waiting is not None:
            report["waiting_on"] = waiting
        (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")

    now = datetime.now().strftime("%Y%m%d")
    run(f"{now}-100000", "https://x.test/a", "stopped", ["Notice period?"])  # answered: offered
    run(f"{now}-100001", "https://x.test/b", "stopped", ["Notice period?", "Night shifts?"])  # one still open
    run(f"{now}-100002", "https://x.test/c", "stopped")  # an older run: QUESTIONS.md says what it asked
    run(f"{now}-100003", "https://x.test/d", "submitted", [])
    run(f"{now}-090000", "https://x.test/d", "stopped", ["Notice period?"])  # since submitted: not offered
    entries = inbox.read(home / "data" / "QUESTIONS.md") + [
        {"question": "Notice period?", "options": [], "urls": ["https://x.test/c"], "answer": "15 days"},
        {"question": "Night shifts?", "options": ["Yes", "No"], "urls": [], "answer": ""},
    ]
    inbox.write(home / "data" / "QUESTIONS.md", entries)
    ready = features.rerun_ready()
    assert sorted(j["url"] for j in ready) == ["https://x.test/a", "https://x.test/c"]
    assert client().get("/api/rerun", headers=AUTH).json()["jobs"] == ready


def test_an_answer_is_saved_to_its_question_even_when_the_list_moved(home):  # noqa: F811
    from jev_apply import inbox
    from jev_apply.web import data

    path = home / "data" / "QUESTIONS.md"
    shown = data.questions()  # the page lists them...
    entries = inbox.read(path)
    inbox.write(
        path, [{"question": "A new one a batch just added?", "options": [], "urls": [], "answer": ""}] + entries
    )
    target = shown[0]  # ...a batch adds a question in front, and the numbers move
    c = client()
    c.post(f"/api/questions/{target['index']}", json={"answer": "No", "question": target["question"]}, headers=AUTH)
    saved = {e["question"]: e["answer"] for e in inbox.read(path)}
    assert saved[target["question"]] == "No" and saved["A new one a batch just added?"] == ""
