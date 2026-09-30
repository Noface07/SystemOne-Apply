"""Learning a profile from a résumé: code extracts exact facts, Claude's additions only fill gaps."""

import json
from datetime import date

from jev_apply import learn, model

RESUME = """AARAV SHARMA
Software Engineer · Backend
Bengaluru, India | aarav.sharma@example.com | 9876543210 | linkedin.com/in/example | github.com/example
PROFESSIONAL EXPERIENCE
Example Tech — Software Engineer II Jul 2022 – Current
• Built billing workers in Go
Intern Corp — Software Intern Jan 2022 – Jun 2022
EDUCATION
Example Institute of Technology — B.Tech, Computer Science 2018 – 2022
CGPA: 8.4/10
SKILLS
Languages: Python, Go, SQL
Tools: Docker, Kubernetes (EKS,
AKS), Git
"""


def test_code_extracts_the_exact_facts():
    got = learn.extract(RESUME)
    assert got["personal"] == {"first_name": "Aarav", "last_name": "Sharma", "email": "aarav.sharma@example.com",
                               "phone": "+91 98765 43210", "city": "Bengaluru", "country": "India"}  # fmt: skip
    assert got["links"] == {"linkedin": "https://linkedin.com/in/example", "github": "https://github.com/example"}
    assert got["experience"][0] == {"company": "Example Tech", "title": "Software Engineer II", "start": "2022-07",
                                    "end": ""}  # fmt: skip
    assert got["experience"][1]["end"] == "2022-06"
    assert got["education"][0]["institution"] == "Example Institute of Technology"
    assert got["education"][0]["grade"] == "8.4/10 CGPA" and got["education"][0]["end"] == "2022"
    assert got["skills"] == ["Python", "Go", "SQL", "Docker", "Kubernetes (EKS, AKS)", "Git"]  # a wrapped line joined


def test_claude_fills_gaps_but_never_overrides_what_code_read(tmp_path, monkeypatch):
    reply = {"personal": {"email": "wrong@example.com", "nationality": "Indian"}, "headline": "Backend engineer",
             "experience": [{"company": "X", "summary": "Built billing workers in Go"}],
             "work": {"total_experience_years": 3.2}, "_review": ["counted from Jul 2022"]}  # fmt: skip
    monkeypatch.setattr(model, "claude_code", lambda system, payload, **k: (json.dumps(reply), {}))
    resume = tmp_path / "cv.md"
    resume.write_text(RESUME, encoding="utf-8")
    path, draft, notes = learn.learn(resume, tmp_path, today=date(2026, 9, 30))
    assert path.name == "profile.learned.json" and json.loads(path.read_text(encoding="utf-8"))["headline"]
    assert draft["personal"]["email"] == "aarav.sharma@example.com"  # code's exact value wins
    assert draft["personal"]["nationality"] == "Indian"  # a gap Claude filled
    assert draft["experience"][0]["company"] == "Example Tech" and draft["experience"][0]["summary"]
    assert any("counted from" in n for n in notes)
    assert not (tmp_path / "profile.json").exists()  # never written


def test_without_claude_the_draft_holds_what_code_read(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "claude_code", lambda *a, **k: (None, {"reason": "no CLI"}))
    resume = tmp_path / "cv.md"
    resume.write_text(RESUME, encoding="utf-8")
    _, draft, notes = learn.learn(resume, tmp_path)
    assert draft["experience"][0]["company"] == "Example Tech" and "work" not in draft
    assert any("skipped" in n for n in notes)
