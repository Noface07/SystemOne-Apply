"""The local web app: its guards (token, host), the questions inbox and the live batch state."""

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from jev_apply import inbox  # noqa: E402
from jev_apply.web import batches, data  # noqa: E402
from jev_apply.web.server import create_app  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A data folder with one track and a QUESTIONS.md, as the app's working directory."""
    track = tmp_path / "data" / "dotnet"
    track.mkdir(parents=True)
    (tmp_path / "data" / "profile.example.json").write_text("{}", encoding="utf-8")
    profile = {"personal": {"first_name": "Aarav", "email": "aarav@mail.test"}, "headline": "C# / .NET developer"}
    (track / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    inbox.write(
        tmp_path / "data" / "QUESTIONS.md",
        [
            {
                "question": "Are you open to night shifts?",
                "options": ["Yes", "No"],
                "urls": ["https://x.test/1"],
                "answer": "",
            }
        ],
    )
    monkeypatch.setattr(data, "ROOT", tmp_path)
    data._load.cache_clear()
    return tmp_path


def client():
    return TestClient(create_app(token="secret"))


def test_the_api_needs_the_session_token_and_this_host():
    c = client()
    assert c.get("/api/tracks").status_code == 401
    assert c.get("/api/tracks", headers={"x-jev-token": "wrong"}).status_code == 401
    assert c.get("/api/tracks", headers={"x-jev-token": "secret", "host": "evil.example"}).status_code == 403
    assert 'content="secret"' in c.get("/").text  # the page carries it; other sites can't read the page


def test_answering_a_question_saves_it_to_questions_md(home):
    c, auth = client(), {"x-jev-token": "secret"}
    [q] = c.get("/api/questions", headers=auth).json()
    assert q["answer"] == "" and q["options"] == ["Yes", "No"]
    assert c.post("/api/questions/0", json={"answer": "No"}, headers=auth).status_code == 200
    assert inbox.read(home / "data" / "QUESTIONS.md")[0]["answer"] == "No"
    assert c.post("/api/questions/5", json={"answer": "x"}, headers=auth).status_code == 404


def test_tracks_and_search_refuse_unknown_tracks(home):
    c, auth = client(), {"x-jev-token": "secret"}
    [track] = c.get("/api/tracks", headers=auth).json()
    assert track["id"] == "data/dotnet" and track["headline"] == "C# / .NET developer"
    bad = c.post("/api/search", json={"queries": {"../../etc": ["x"]}}, headers=auth)
    assert bad.status_code == 400


def test_batch_output_becomes_live_job_state():
    b = {
        "state": "starting",
        "phase": "",
        "jobs": [{"n": n, "url": f"u{n}", "state": "waiting", "events": []} for n in (1, 2, 3)],
    }
    lines = [
        "Plan for 3 job(s):",
        "   1. Dexter Solutions Inc · Dotnet Developer                            résumé dotnet 1 skill question(s)",
        "   2. Gainserv · Software Engineer (.NET | AI | Backend)                 résumé genai ",
        "   3. Varahi Technologies · C# .NET Developer                            résumé dotnet DEFERRED (Varahi"
        " Technologies: already 1 application(s) in 14 days)",
        "Starting Clef (llama-server; loading 6 GB onto the GPU takes a minute)...",
        "[1/3] https://www.linkedin.com/jobs/view/1",
        "  · [1/3] résumé: data/dotnet (Yuvraj_Soni_Resume_CSharp_DotNET.pdf)",
        "  · [1/3] needs you: Your résumé is in the site's saved list but its card couldn't be brought into view.",
        "  · [1/3] stopped: 0 fields filled, 0 draft(s) to read, 0 left for you",
        "[2/3] https://www.linkedin.com/jobs/view/2",
        "  · [2/3] submitted: 4 fields filled, 0 draft(s) to read, 0 left for you",
    ]
    current = None
    for line in lines:
        current = batches._apply(b, line, current)
    one, two, three = b["jobs"]
    assert one["company"] == "Dexter Solutions Inc" and one["title"] == "Dotnet Developer"
    assert one["state"] == "stopped" and one["track"] == "data/dotnet" and "saved list" in one["needs"]
    assert two["state"] == "submitted" and two["track"] == "genai"
    assert three["state"] == "deferred" and "already 1 application" in three["result"]
    assert b["phase"] == "Job 2 of 3"


def test_only_one_batch_at_a_time(monkeypatch):
    monkeypatch.setitem(batches._batches, "x", {"id": "x", "state": "running", "started": "", "jobs": []})
    with pytest.raises(RuntimeError, match="already running"):
        batches.start(["https://www.linkedin.com/jobs/view/1"], [])


def test_switching_the_decision_model_writes_env_and_waits_for_a_running_batch(home, monkeypatch):
    import os

    (home / ".env").write_text("# keep me\nDECISION_BACKEND=clef\nCLEF_MODEL_PATH=x.gguf\n", encoding="utf-8")
    monkeypatch.setenv("DECISION_BACKEND", "clef")
    c, auth = client(), {"x-jev-token": "secret"}
    models = {m["id"]: m for m in c.get("/api/models", headers=auth).json()}
    assert models["clef"]["current"] and set(models) == {"clef", "laya", "typesafe", "llm"}
    assert c.post("/api/models", json={"id": "laya"}, headers=auth).status_code == 200
    assert (home / ".env").read_text(encoding="utf-8") == "# keep me\nDECISION_BACKEND=laya\nCLEF_MODEL_PATH=x.gguf\n"
    assert os.environ["DECISION_BACKEND"] == "laya"
    assert c.post("/api/models", json={"id": "gpt-x"}, headers=auth).status_code == 400
    monkeypatch.setitem(batches._batches, "x", {"id": "x", "state": "running", "started": "", "jobs": []})
    assert c.post("/api/models", json={"id": "clef"}, headers=auth).status_code == 409


def test_saving_a_profile_keeps_a_backup_and_copies_only_shared_facts(home):
    genai = home / "data" / "genai"
    genai.mkdir()
    theirs = {"personal": {"first_name": "Aarav"}, "headline": "GenAI engineer", "work": {"notice_period_days": 30}}
    (genai / "profile.json").write_text(json.dumps(theirs), encoding="utf-8")
    c, auth = client(), {"x-jev-token": "secret"}
    mine = c.get("/api/profile", params={"track": "data/dotnet"}, headers=auth).json()["profile"]
    mine["headline"] = "Senior-ish .NET developer"  # this résumé only
    mine["work"] = {"notice_period_days": 15}  # a shared fact
    res = c.put(
        "/api/profile",
        params={"track": "data/dotnet"},
        json={"profile": mine, "apply_to": ["data/genai"]},
        headers=auth,
    )
    assert res.status_code == 200
    saved = json.loads((home / "data" / "dotnet" / "profile.json").read_text(encoding="utf-8"))
    copied = json.loads((genai / "profile.json").read_text(encoding="utf-8"))
    assert saved["headline"] == "Senior-ish .NET developer" and saved["work"]["notice_period_days"] == 15
    assert copied["work"]["notice_period_days"] == 15 and copied["headline"] == "GenAI engineer"
    assert len(list((home / "data" / "dotnet" / ".backups").glob("profile-*.json"))) == 1
    assert c.get("/api/profile", params={"track": "../secrets"}, headers=auth).status_code == 404
    bad = c.put(
        "/api/profile", params={"track": "data/dotnet"}, json={"profile": ["not", "a", "profile"]}, headers=auth
    )
    assert bad.status_code == 400


def test_profile_is_not_saved_while_a_batch_runs(home, monkeypatch):
    monkeypatch.setitem(batches._batches, "x", {"id": "x", "state": "running", "started": "", "jobs": []})
    res = client().put(
        "/api/profile", params={"track": "data/dotnet"}, json={"profile": {}}, headers={"x-jev-token": "secret"}
    )
    assert res.status_code == 409


def test_a_resume_upload_must_be_a_real_document_and_becomes_the_tracks_resume(home):
    c, auth = client(), {"x-jev-token": "secret"}
    put = lambda name, body, kind="resume": c.put(  # noqa: E731
        "/api/documents", params={"track": "data/dotnet", "kind": kind, "filename": name}, content=body, headers=auth
    )
    assert put("cv.pdf", b"MZ\x90 not a pdf").status_code == 400
    assert put("cv.exe", b"%PDF-1.7").status_code == 400
    assert put("cv.pdf", b"%PDF-1.7 tiny", kind="../x").status_code == 400
    res = put("../../My CV.pdf", b"%PDF-1.7 tiny")
    assert res.status_code == 200 and res.json()["resume"]["exists"]
    assert (home / "data" / "dotnet" / "documents" / "My CV.pdf").read_bytes() == b"%PDF-1.7 tiny"
    saved = json.loads((home / "data" / "dotnet" / "profile.json").read_text(encoding="utf-8"))
    assert saved["documents"]["resume"]["path"] == "documents/My CV.pdf"


def test_a_new_track_starts_from_shared_facts_with_its_own_headline_and_skills(home):
    c, auth = client(), {"x-jev-token": "secret"}
    res = c.post("/api/tracks", json={"name": "Embedded Systems", "base": "data/dotnet"}, headers=auth)
    assert res.status_code == 200 and res.json()["track"] == "data/embedded-systems"
    made = json.loads((home / "data" / "embedded-systems" / "profile.json").read_text(encoding="utf-8"))
    assert made["personal"]["first_name"] == "Aarav" and made["skills"] == [] and made["documents"] == {}
    assert c.post("/api/tracks", json={"name": "embedded systems"}, headers=auth).status_code == 400  # exists
    assert {t["id"] for t in c.get("/api/tracks", headers=auth).json()} == {"data/dotnet", "data/embedded-systems"}


def test_keys_are_saved_to_env_but_never_sent_back(home, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    (home / ".env").write_text("DECISION_BACKEND=clef\n", encoding="utf-8")
    c, auth = client(), {"x-jev-token": "secret"}
    res = c.put("/api/settings", json={"values": {"TYPESAFE_API_KEY": "ts_live_1234567890abcd"}}, headers=auth)
    assert res.status_code == 200
    assert "TYPESAFE_API_KEY=ts_live_1234567890abcd" in (home / ".env").read_text(encoding="utf-8")
    shown = {s["key"]: s for s in c.get("/api/settings", headers=auth).json()}["TYPESAFE_API_KEY"]
    assert shown["set"] and shown["hint"] == "…abcd" and "ts_live" not in json.dumps(shown)
    assert next(m for m in res.json()["models"] if m["id"] == "typesafe")["problem"] is None  # Jev is ready
    c.put("/api/settings", json={"values": {"TYPESAFE_API_KEY": ""}}, headers=auth)
    assert "TYPESAFE_API_KEY" not in (home / ".env").read_text(encoding="utf-8")
    assert c.put("/api/settings", json={"values": {"PATH": "x"}}, headers=auth).status_code == 400
    assert c.put("/api/settings", json={"values": {"TEXT_MODEL": "a\nEVIL=1"}}, headers=auth).status_code == 400


def test_company_boards_are_checked_before_they_are_added(home, monkeypatch):
    from jev_apply import scan

    (home / "data" / "boards.example.txt").write_text("greenhouse:devrev\n", encoding="utf-8")
    monkeypatch.setattr(
        scan, "fetch", lambda provider, token, client: [] if token != "ghost" else (_ for _ in ()).throw(ValueError())
    )
    c, auth = client(), {"x-jev-token": "secret"}
    listed = c.get("/api/boards", headers=auth).json()
    assert [b["entry"] for b in listed["boards"]] == ["greenhouse:devrev"] and not listed["own"]
    res = c.post("/api/boards", json={"board": "https://jobs.lever.co/allata/abc"}, headers=auth)
    assert res.status_code == 200 and res.json()["added"] == "lever:allata"
    assert (home / "data" / "boards.txt").read_text(encoding="utf-8").splitlines()[1:] == [
        "greenhouse:devrev",
        "lever:allata",
    ]
    assert c.post("/api/boards", json={"board": "lever:allata"}, headers=auth).status_code == 400  # twice
    assert c.post("/api/boards", json={"board": "ashby:ghost"}, headers=auth).status_code == 400  # doesn't exist
    assert c.post("/api/boards", json={"board": "https://example.com/careers"}, headers=auth).status_code == 400
    assert c.delete("/api/boards", params={"entry": "greenhouse:devrev"}, headers=auth).status_code == 200
    assert [b["entry"] for b in c.get("/api/boards", headers=auth).json()["boards"]] == ["lever:allata"]
