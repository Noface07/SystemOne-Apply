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


def test_a_resume_that_merely_lists_a_skill_loses_to_the_one_built_around_it():
    # Both list C# and .NET; only one is a .NET résumé. A tie used to go to the first track (automation).
    automation = profile(
        headline="Industrial IoT engineer: SCADA, OPC UA, Modbus", skills=["C#", ".NET", "SCADA", "OPC UA"]
    )
    dotnet = profile(
        headline="C# / .NET backend developer", skills=["C#", ".NET 8", "ASP.NET Core Web API", "Entity Framework"]
    )
    tracks = {"data/automation": automation, "data/dotnet": dotnet}
    job = JobInfo("u", "linkedin", "Acme", "Dotnet Developer", ".NET Core, ASP.NET MVC and SQL Server; C# a must.")
    assert jobplan.choose_track(job, tracks, "data/automation")[0] == "data/dotnet"
    job = JobInfo("u", "linkedin", "Acme", "SCADA Engineer", "OPC UA, Modbus, PLC integration; some C#.")
    assert jobplan.choose_track(job, tracks, "data/dotnet")[0] == "data/automation"
    assert skills.family("ASP.NET MVC") == ".NET" and skills.family("Python") == "Python"


def test_a_skill_named_with_its_abbreviation_matches_either_name():
    assert skills.canonical("Natural Language Processing (NLP)") == "NLP"
    assert skills.canonical("Large Language Models (LLM)") == "Large Language Models"
    assert answers("How many years of work experience do you have with NLP?",
                   "How many years of work experience do you have with Natural Language Processing (NLP)?")  # fmt: skip


def test_a_saved_skill_answer_serves_a_question_with_more_words_around_the_skill():
    saved = "How many years of hands-on experience do you have with Microsoft Azure in a cloud-native environment?"
    assert answers(saved, "How many years of work experience do you have with Microsoft Azure?")
    assert not answers(saved, "How many years of work experience do you have with AWS?")
    # Two skills named: no single skill, so no match by skill.
    assert skills.asked_skill("How many years of experience do you have with Python and Django?") != "Python"


def test_batch_planning_defers_blocked_companies(tmp_path, monkeypatch):
    from jev_apply import cli, jobinfo

    (tmp_path / "search.json").write_text(json.dumps({"blocked_companies": ["Talentgigs"]}), encoding="utf-8")
    track = tmp_path / "dotnet"
    track.mkdir()
    (track / "profile.json").write_text(json.dumps({**BASE, "skills": ["C#"]}), encoding="utf-8")
    (tmp_path / "profile.example.json").write_text("{}", encoding="utf-8")
    loaded = {str(track): cli.load(cli.argparse.Namespace(data=str(track), profile=None, answers=None, policy=None))}
    info = {
        "u1": JobInfo("u1", "linkedin", "Talentgigs", ".NET Developer", "C#"),
        "u2": JobInfo("u2", "linkedin", "Acme", ".NET Developer", "C#"),
    }
    monkeypatch.setattr(jobinfo, "fetch", lambda url, client=None: info[url])
    plans = cli.plan_batch(["u1", "u2"], loaded, str(track))
    assert plans[0].deferred == "you blocked Talentgigs" and not plans[1].deferred
