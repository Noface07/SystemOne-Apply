"""Planning a batch before any form opens: which résumé (profile track) each job gets, which skills its
description will ask about that your profile has no years for, which answers its published form already
settles, and which jobs wait because you applied to that company recently. Plain rules; nothing is sent.
"""

import re
from collections import Counter
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


RESUME_FACTS = re.compile(r"(skills|headline|work\.current_title|experience\.|projects\.|summary)")


def emphasis(profile):
    """How much a track's résumé makes of each line of work: how often its skills, headline and job summaries name
    each skill family. Two résumés may both list ".NET"; the one that names it a dozen times is the .NET one."""
    text = " ".join(f.value for k, f in profile.facts.items() if RESUME_FACTS.match(k))
    found = Counter()
    for name, n in skills.counts(text).items():
        found[skills.family(name)] += n
    for name in knows(profile):  # listed once at least, whatever the wording
        found[skills.family(name)] += 0 if found[skills.family(name)] else 1
    return found


def scores_for(info, tracks):
    """Each track's fit: for every skill of the job, that track's share of the emphasis the résumés put on the
    skill's family (the résumé naming it most gets 1, one naming it a fifth as often 0.2), and the job title's
    skills five times as much (the title says what the role is)."""
    made = {folder: emphasis(p) for folder, p in tracks.items()}
    in_title = {skills.family(s) for s in skills.mentioned(info.title)}
    out = {folder: 0.0 for folder in tracks}
    for skill in skills.mentioned(f"{info.title} {info.description}"):
        key = skills.family(skill)
        most = max(m[key] for m in made.values())
        if not most:
            continue
        for folder in tracks:
            out[folder] += (5.0 if key in in_title else 1.0) * made[folder][key] / most
    return {folder: round(points, 2) for folder, points in out.items()}


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


def blocked(company, title, text, prefs):
    """Why your search preferences (data/search.json) rule a job out, a company or a word you blocked, or None."""
    key = company_key(company)
    for name in prefs.get("blocked_companies") or []:
        if company_key(name) and company_key(name) == key:
            return f"you blocked {name}"
    low = f"{title} {text}".lower()
    for word in prefs.get("blocked_keywords") or []:
        if word.strip() and re.search(rf"(?<![a-z0-9]){re.escape(word.strip().lower())}(?![a-z0-9])", low):
            return f"mentions '{word.strip()}', which you blocked"
    return None


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
