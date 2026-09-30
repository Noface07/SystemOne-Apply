"""Planning a batch before any form opens: which résumé (profile track) each job gets, which skills its
description will ask about that your profile has no years for, which answers its published form already
settles, and which jobs wait because you applied to that company recently. Plain rules; nothing is sent.
"""

from dataclasses import dataclass, field

from . import applied, planner, rules, skills
from .jobinfo import JobInfo, company_key


@dataclass
class Plan:
    url: str
    info: JobInfo | None
    track: str  # the data folder whose profile and résumé this job uses
    scores: dict = field(default_factory=dict)
    skill_gaps: list = field(default_factory=list)  # questions for QUESTIONS.md
    answers: dict = field(default_factory=dict)  # form-definition question -> exact option your profile answers
    deferred: str = ""  # why this job waits (applied to the company recently)


def knows(profile):
    """The skills a track's résumé lists (its skills list and headline), by vocabulary name. Years per skill are
    left out: they describe you, not the résumé, and are the same in every track."""
    names = set()
    for key, fact in profile.facts.items():
        if fact.key == "skills":
            names |= {skills.canonical(s) for s in fact.value.split(", ")}
            names |= set(skills.mentioned(fact.value))
        elif fact.key in {"headline", "work.current_title"}:
            names |= set(skills.mentioned(fact.value))
    return names


def scores_for(info, tracks):
    """Each track's fit: every skill of the job its résumé lists, weighted by how few tracks list it (a skill all
    of them list says nothing), and the job title's skills five times as much (the title says what the role is)."""
    known = {folder: knows(p) for folder, p in tracks.items()}
    in_title = set(skills.mentioned(info.title))
    out = {}
    for folder in tracks:
        points = 0.0
        for skill in skills.mentioned(f"{info.title} {info.description}"):
            if skill in known[folder]:
                sharing = sum(1 for k in known.values() if skill in k)
                points += (5.0 if skill in in_title else 1.0) / sharing
        out[folder] = round(points, 2)
    return out


def choose_track(info, tracks, default):
    """(best data folder, {folder: score}); the default folder when nothing is known or nothing scores."""
    if not info or len(tracks) < 2:
        return default, {}
    scores = scores_for(info, tracks)
    best = max(scores, key=lambda f: (scores[f], f == default))
    return (best if scores[best] > 0 else default), scores


def skill_gaps(info, profile):
    """The years-with-skill questions this job's description implies that nothing in your profile answers."""
    if not info:
        return []
    gaps = []
    for skill in skills.prominent(info.title, info.description):
        question = skills.years_question(skill)
        if profile.saved_answer(question):
            continue
        field_ = {"kind": "fill", "role": "textbox", "label": question, "required": True}
        if rules.match(field_, profile) == "ASK_USER":
            gaps.append({"question": question, "options": [], "urls": [info.url]})
    return gaps


def form_answers(questions, profile):
    """{question: option} for the choice questions of a published form that your profile answers exactly."""
    facts = planner.Facts(profile.candidate_state())
    out = {}
    for q in questions:
        if q.kind not in {"choice", "multi", "boolean"} or not q.label:
            continue
        options = q.options or (["Yes", "No"] if q.kind == "boolean" else [])
        group = [{"kind": "select", "label": f"{q.label} → {o}", "value": o} for o in options]
        exact = planner.profile_answer(q.label, group, facts) if group else []
        if exact:
            out[q.label] = planner.option_text(exact[0])
    return out


def spread(plans, applied_path, policy):
    """Mark the jobs that would go over `per_company` applications to one company within `company_gap_days`
    (counting applied.json and the jobs earlier in this batch)."""
    if policy.per_company <= 0:
        return plans
    counts = applied.recent_by_company(applied_path, policy.company_gap_days)
    for plan in plans:
        company = company_key(plan.info.company) if plan.info else ""
        if not company:
            continue
        if counts.get(company, 0) >= policy.per_company:
            plan.deferred = (
                f"{plan.info.company}: already {counts[company]} application(s) in {policy.company_gap_days} days"
            )
            continue
        counts[company] = counts.get(company, 0) + 1
    return plans
