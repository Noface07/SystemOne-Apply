"""Batch planning: skills from job descriptions, résumé per job, answers from the form, spread per company."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from jev_apply import applied, jobplan, skills
from jev_apply.jobinfo import JobInfo, company_key
from jev_apply.policy import Policy
from jev_apply.preflight import Question
from jev_apply.profile import Profile, answers

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8"))


def profile(**extra):
    return Profile({**BASE, **extra}, ROOT / "data")


def test_skills_in_a_description_by_their_common_names():
    text = "2+ years with React.js, Node and SQL Server. Go-getter attitude. C# and .NET Core; golang a plus. SQL."
    assert skills.mentioned(text) == ["React", "Node.js", "SQL Server", "C#", ".NET Core", "Go", "SQL"]
    assert skills.asked_skill("How many years of experience do you have in React.js?") == "React"
    assert answers("How many years of work experience do you have with React?", "Years of experience with ReactJS?")
    assert not answers("How many years of work experience do you have with React?", "...do you have with Angular?")


def test_each_job_gets_the_resume_whose_skills_it_asks_for():
    dotnet = profile(headline="C# / .NET developer", skills=["C#", "ASP.NET Core", "SQL Server"])
    genai = profile(headline="GenAI engineer", skills=["Python", "LangChain", "RAG", "PyTorch"])
    tracks = {"data/dotnet": dotnet, "data/genai": genai}
    job = JobInfo("u", "linkedin", "Acme", "LLM Engineer", "Build RAG pipelines with LangChain and PyTorch")
    assert jobplan.choose_track(job, tracks, "data/dotnet")[0] == "data/genai"
    job = JobInfo("u", "linkedin", "Acme", ".NET Developer", "C#, ASP.NET Core Web API, SQL Server")
    assert jobplan.choose_track(job, tracks, "data/genai")[0] == "data/dotnet"
    assert jobplan.choose_track(None, tracks, "data/genai")[0] == "data/genai"  # nothing known: your --data


def test_skill_questions_are_only_prominent_ones_nothing_answers():
    mine = profile(skill_years={"Python": 2})
    description = "Python and Angular daily. Angular 16, Kubernetes on AWS. Python tooling. HTML once."
    job = JobInfo("u", "linkedin", "Acme", "Kubernetes Engineer", description)
    asked = [g["question"] for g in jobplan.skill_gaps(job, mine)]
    # Kubernetes is in the title, Angular named twice: asked. Python is answered; AWS and HTML only in passing.
    assert asked == [skills.years_question("Kubernetes"), skills.years_question("Angular")]


def test_form_answers_are_the_exact_options_your_profile_gives():
    questions = [
        Question("Notice Period", "choice", True, ["Immediate", "15 Days", "30 Days", "60 Days"]),
        Question("Do you like cricket?", "choice", True, ["Yes", "No"]),
    ]
    got = jobplan.form_answers(
        questions, profile(work={**BASE["work"], "notice_period_days": 15, "notice_period_text": "15 days"})
    )
    assert got == {"Notice Period": "15 Days"}


def test_applications_are_spread_across_companies(tmp_path):
    path = tmp_path / "applied.json"
    recent = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    old = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
    path.write_text(json.dumps({
        "linkedin:1": {"title": "Dev | Zuci Systems | LinkedIn", "url": "a", "at": recent},
        "linkedin:2": {"title": "Dev | Old Corp | LinkedIn", "url": "b", "at": old},
    }))  # fmt: skip
    assert company_key("Zuci Systems Pvt. Ltd.") == company_key("Zuci Systems")
    plans = [
        jobplan.Plan("x1", JobInfo("x1", "linkedin", "Zuci Systems Pvt. Ltd."), "d"),
        jobplan.Plan("x2", JobInfo("x2", "linkedin", "Old Corp"), "d"),
        jobplan.Plan("x3", JobInfo("x3", "linkedin", "New Co"), "d"),
        jobplan.Plan("x4", JobInfo("x4", "linkedin", "New Co Private Limited"), "d"),
    ]
    jobplan.spread(plans, path, Policy())
    assert [bool(p.deferred) for p in plans] == [True, False, False, True]
    fresh = [jobplan.Plan("x1", JobInfo("x1", "linkedin", "Zuci Systems"), "d")]
    assert all(not p.deferred for p in jobplan.spread(fresh, path, Policy(per_company=0)))  # 0 turns it off
    assert applied.company_of({"title": "C# Developer | Tata Electronics | LinkedIn"}) == "Tata Electronics"
