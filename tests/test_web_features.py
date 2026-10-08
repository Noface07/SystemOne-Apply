"""The app's tools: safety settings, search preferences and ranking, the review queue, résumé drafts, autopilot."""

import json

import pytest

pytest.importorskip("fastapi")

from jev_apply import jobplan  # noqa: E402
from jev_apply.web import data, features  # noqa: E402

from .test_web import client, home  # noqa: E402,F401

AUTH = {"x-jev-token": "secret"}


def test_safety_settings_write_only_overrides_and_refuse_nonsense(home):  # noqa: F811
    c = client()
    rows = {r["field"]: r for r in c.get("/api/safety", params={"track": "data/dotnet"}, headers=AUTH).json()}
    assert rows["auto_submit"]["value"] is False and not rows["auto_submit"]["custom"]
    res = c.put(
        "/api/safety",
        params={"track": "data/dotnet"},
        json={"values": {"auto_submit": True, "min_target_probability": 0.85}},
        headers=AUTH,
    )
    assert res.status_code == 200
    written = json.loads((home / "data" / "dotnet" / "policy.json").read_text(encoding="utf-8"))
    assert written == {"auto_submit": True, "min_target_probability": 0.85}
    c.put("/api/safety", params={"track": "data/dotnet"}, json={"values": {"auto_submit": False}}, headers=AUTH)
    assert "auto_submit" not in json.loads((home / "data" / "dotnet" / "policy.json").read_text(encoding="utf-8"))
    for bad in ({"min_target_probability": 3}, {"submit_patterns": []}, {"drafts": "yolo"}, {"per_company": -1}):
        assert (
            c.put("/api/safety", params={"track": "data/dotnet"}, json={"values": bad}, headers=AUTH).status_code == 400
        )


def test_blocked_companies_and_words_rule_a_job_out_everywhere():
    prefs = {"blocked_companies": ["Talentgigs"], "blocked_keywords": ["sales", "night shift"]}
    assert jobplan.blocked("TalentGigs Pvt Ltd", "AI Engineer", "", prefs) == "you blocked Talentgigs"
    assert "night shift" in jobplan.blocked("Acme", "Engineer", "Rotational night shift required", prefs)
    assert jobplan.blocked("Acme", "Wholesale engineer", "", prefs) is None  # whole words only
    assert jobplan.blocked("Acme", "Engineer", "", {}) is None


def test_salary_and_score_rank_what_fits_your_resume_first():
    assert features.salary("CTC: 8 - 12 LPA plus bonus") == "8-12 LPA"
    assert features.salary("₹6 to 9.5 lakhs per annum") == "6-9.5 LPA"
    assert features.salary("competitive salary") is None
    mine = {"C#", "ASP.NET Core", "SQL Server", "Azure"}
    good = {
        "title": ".NET Developer",
        "skills": ["C#", "ASP.NET MVC", "SQL Server"],
        "min_years": 2,
        "location": "Pune",
    }
    poor = {"title": "Java Developer", "skills": ["Java", "Spring Boot", "Kafka"], "min_years": 5, "location": "Pune"}
    prefs = {"preferred_cities": ["Pune"]}
    assert features.score(good, mine, prefs, 2) > 80 > features.score(poor, mine, prefs, 2)


def test_the_review_queue_lists_jobs_left_for_you_and_remembers_what_you_cleared(home):  # noqa: F811
    from datetime import datetime

    run = home / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")  # recent: older stops drop out of the queue
    run.mkdir(parents=True)
    draft = {
        "label": "Why do you want to join?",
        "value": "I like building backends.",
        "source": "draft",
        "kind": "fill",
    }
    report = {
        "status": "review",
        "start_url": "https://www.linkedin.com/jobs/view/123/",
        "fills": [draft],
        "left_for_you": [{"what": "Answer 'Current CTC'", "why": "required, still empty"}],
        "asked": [],
        "handovers": [],
        "history": [],
    }
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    c = client()
    items = c.get("/api/queue", headers=AUTH).json()["items"]
    assert {i["kind"] for i in items} == {"draft", "job"}
    draft = next(i for i in items if i["kind"] == "draft")
    assert draft["text"] == "I like building backends." and not draft["sent"]
    left = c.post(f"/api/queue/{draft['id']}", json={}, headers=AUTH).json()["items"]
    assert [i["kind"] for i in left] == ["job"]
    assert [i["kind"] for i in client().get("/api/queue", headers=AUTH).json()["items"]] == ["job"]  # kept


def test_applying_a_resume_draft_keeps_descriptions_and_never_touches_documents(home):  # noqa: F811
    folder = home / "data" / "dotnet"
    profile = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
    profile["headline"] = {"value": "old", "about": "One-line title"}
    (folder / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    draft = {"headline": "C# backend engineer", "skills": ["C#", "Redis"], "documents": {"resume": {"path": "/x.pdf"}}}
    (folder / "profile.learned.json").write_text(json.dumps(draft), encoding="utf-8")
    res = client().post(
        "/api/learn-apply",
        params={"track": "data/dotnet"},
        json={"keys": ["headline", "skills", "documents.resume"]},
        headers=AUTH,
    )
    assert res.status_code == 200
    saved = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
    assert saved["headline"] == {"value": "C# backend engineer", "about": "One-line title"}
    assert (
        saved["skills"] == ["C#", "Redis"]
        and "documents" not in saved
        or saved.get("documents") == profile.get("documents")
    )


def test_autopilot_settings_are_checked_and_scheduled(home, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(features, "schedule", lambda config: calls.append(config))
    monkeypatch.setattr(features, "task_scheduled", lambda: None)
    c = client()
    ok = c.put(
        "/api/autopilot",
        json={"values": {"enabled": True, "time": "08:30", "tracks": ["data/dotnet", "data/nope"], "max_jobs": 99}},
        headers=AUTH,
    )
    assert ok.status_code == 200
    config = ok.json()["config"]
    assert config["tracks"] == ["data/dotnet"] and config["max_jobs"] == 50 and calls[-1]["time"] == "08:30"
    assert c.put("/api/autopilot", json={"values": {"time": "25:00"}}, headers=AUTH).status_code == 400
    assert c.put("/api/autopilot", json={"values": {"rm": "-rf"}}, headers=AUTH).status_code == 400


def test_insights_count_outcomes_per_track(home):  # noqa: F811
    for n, status in enumerate(["submitted", "stopped", "submitted"]):
        run = home / "runs" / f"20261008-10101{n}"
        run.mkdir(parents=True)
        url = f"https://www.linkedin.com/jobs/view/{n}/"
        empty = {"fills": [], "left_for_you": [], "asked": [], "history": [], "handovers": []}
        report = {"status": status, "start_url": url, "elapsed_s": 60, **empty}
        (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    found = client().get("/api/insights", headers=AUTH).json()
    [track] = found["tracks"]
    assert track["runs"] == 3 and track["submitted"] == 2 and track["rate"] == 0.67 and track["avg_seconds"] == 60
    assert data.run_reports()  # still readable


def test_keyword_coverage_and_match_reasons_explain_a_job():
    from jev_apply.web import jobs

    cov = jobs.coverage(["C#", "ASP.NET MVC", "SQL Server", "Kafka"], {"C#", "ASP.NET Core", "SQL Server"})
    assert cov["covered"] == ["C#", "SQL Server"] and cov["related"] == ["ASP.NET MVC"] and cov["missing"] == ["Kafka"]
    assert cov["pct"] == 62  # (2 + half of 1) / 4
    job = {"title": ".NET Developer", "coverage": cov, "min_years": 2, "location": "Pune", "salary": "8-12 LPA"}
    said = [r["text"] for r in jobs.reasons(job, {"preferred_cities": ["Pune"]}, 2)]
    assert "Your résumé covers 2 of 4 skills it names (+1 related)" in said
    assert "Missing: Kafka" in said and "In Pune, a city you prefer" in said and "Pays 8-12 LPA" in said


def test_dismissed_jobs_stay_hidden_until_you_undo(home):  # noqa: F811
    c = client()
    c.post("/api/dismiss", json={"key": "linkedin:42", "title": "Sales Engineer", "company": "Acme"}, headers=AUTH)
    assert "linkedin:42" in features.dismissed()
    c.post("/api/dismiss", json={"key": "linkedin:42", "undo": True}, headers=AUTH)
    assert features.dismissed() == {}


def test_the_answer_bank_lists_every_saved_answer_with_where_it_lives_and_its_use(home):  # noqa: F811
    folder = home / "data" / "dotnet"
    (folder / "answers.json").write_text(json.dumps({"answers": [{"id": "why", "question": "Why do you want to join us?",  # noqa: E501
                                                                  "answer": "I like the product."}]}), encoding="utf-8")  # fmt: skip  # noqa: E501
    run = home / "runs" / "20261008-090000"
    run.mkdir(parents=True)
    history = [
        {
            "kind": "fill",
            "source": "saved",
            "action": "Why do you want to join us?",
            "context": "Why do you want to join us?*",
        }
    ]
    (run / "report.json").write_text(
        json.dumps({"status": "submitted", "start_url": "u", "history": history}), encoding="utf-8"
    )
    c = client()
    bank = c.get("/api/answers", headers=AUTH).json()
    why = next(a for a in bank if a["question"].startswith("Why"))
    assert why["source"] == "answers.json" and why["times_used"] == 1 and why["last_used"].startswith("2026-10-08")
    c.put(f"/api/answers/{why['id']}", json={"answer": "I use it every day."}, headers=AUTH)
    stored = json.loads((folder / "answers.json").read_text(encoding="utf-8"))["answers"][0]["answer"]
    assert stored == "I use it every day."
    c.delete(f"/api/answers/{why['id']}", headers=AUTH)
    assert json.loads((folder / "answers.json").read_text(encoding="utf-8"))["answers"] == []
    assert c.put("/api/answers/answers:../../x:0", json={"answer": "x"}, headers=AUTH).status_code == 400


def test_claude_usage_sums_time_tokens_and_cost_per_stage(home):  # noqa: F811
    from datetime import datetime

    now = datetime.now().isoformat(timespec="seconds")
    log = home / "runs" / "claude-usage.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"at": now, "stage": "draft", "latency_ms": 4000, "input_tokens": 1000, "output_tokens": 100, "cost_usd": 0.01, "ok": True},  # noqa: E501
            {"at": now, "stage": "draft", "latency_ms": 6000, "input_tokens": 3000, "output_tokens": 300, "cost_usd": 0.03, "ok": True},  # noqa: E501
            {"at": now, "stage": "learn", "latency_ms": 20000, "reason": "timeout", "ok": False}]  # fmt: skip
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    used = client().get("/api/claude-usage", headers=AUTH).json()
    assert used["total"]["calls"] == 3 and used["total"]["failed"] == 1 and used["total"]["cost_usd"] == 0.04
    assert used["stages"]["draft"]["avg_seconds"] == 5.0 and used["stages"]["draft"]["input_tokens"] == 4000
