"""How the Laya backend moves through a form: a fixed procedure walks the page, and the answers come from the
fixed rules first and Laya second.

Laya is good at small, well-posed choices ("which of these 4 options answers 'Notice period' for this candidate?")
and poor at steering a whole page, so it is only asked the first kind. The procedure, per observation:

1. Open an embedded application form when the page has no fields of its own.
2. Dismiss a cookie banner with its most private choice.
3. If suggestions or dropdown options are showing, click the one that matches what was just typed, or your
   profile's answer to the question the list belongs to.
4. Otherwise take the first unanswered field, top to bottom: text, date, file, dropdown, radio, Yes/No button
   group, tickbox group, required checkbox. Text, date and file values are chosen by agent.py (rules, then Laya);
   dropdown and radio options by the rules (exact match with your profile), then Laya. A field the site filled
   in with something other than your profile's answer is corrected once.
5. With nothing left to fill: click Next/Continue, else the submit-like button (agent.py stops before a final
   submit), else scroll down, else hand over with what is still unanswered. A page it can't move on from is never
   taken for a finished application.

The output is the same decision shape as model.choose, so every guard in agent.py still applies.
"""

import re

from . import laya_backend, rules
from .profile import MONTHS, Fact, answers, safe_id, undouble

PLACEHOLDER = re.compile(r"^\s*$|^\s*(select|choose|please|pick|--|—|-|none)", re.I)
COOKIES = re.compile(
    r"only (necessary|essential|required)|necessary (cookies )?only|reject( all)?\b|decline|"
    r"^\s*(dismiss|got it|ok|close)\s*$",
    re.I,
)
ADVANCE = re.compile(r"^\s*(next|continue|review(?!s)|save (and|&) (continue|next)|proceed|next step)\b", re.I)
# Naukri: its own Apply sends the application; its questionnaire is a chat answered one message at a time.
NAUKRI_APPLY = re.compile(r"^\s*(apply|apply now|i am interested)\s*$", re.I)
APPLIED = re.compile(
    r"successfully applied|you have (successfully )?applied|already applied|applied successfully|"
    r"application (has been )?(sent|submitted)",
    re.I,
)
ALREADY = re.compile(r"applied|already applied|application submitted", re.I)
APPLIED_AGO = re.compile(
    r"\bapplied \d+ (second|minute|hour|day|week|month)s? ago\b|application status\W+application (submitted|viewed)",
    re.I,
)
# A question about what you have done or can do ("Have you deployed ROS 2...?"): only your profile, a rule or
# your own saved answer may say Yes or No; a model's guess would be a claim about you.
CLAIM = re.compile(
    r"^\W*(have you|has your|do you (have|possess|hold|know)|did you|can you|could you|will you be able|"
    r"are you (able|experienced|familiar|proficient|comfortable|skilled|certified|trained))\b",
    re.I,
)
# A knowledge check, not a question about you: "... Do you agree?", "True or false: ..."
QUIZ = re.compile(
    r"do you agree|agree or disagree|true or false|is (this|it|the (above )?statement) (true|correct|false)|"
    r"which (of the following )?is (true|correct)",
    re.I,
)
CHAT_SEND = re.compile(r"^\s*(save|send|submit|next|done|ok)\s*$", re.I)
# "Do you have experience with X?": Yes when X is one of your skills (never No: that is yours to say).
HAS_SKILL = re.compile(
    r"(?:do you have|have you (?:worked|used)|are you (?:familiar|experienced|proficient|comfortable))"
    r"[^?]{0,30}?(?:experience |knowledge )?(?:with|in|on|using) ([^?*✱]+)",
    re.I,
)
# "I'm interested" is Zoho Recruit's Apply (and other sites'): on a posting it opens the form, as Apply does.
INTERESTED = r"i(?:['’]m| am) interested"
SUBMIT = re.compile(rf"\b(submit|apply|send application|finish|complete application|{INTERESTED})\b", re.I)
APPLY_BUTTON = re.compile(
    r"^\W*(easy\s+)?apply\b|^\W*(submit|send application|finish|complete application|continue applying|"
    rf"resume application|{INTERESTED})\b",
    re.I,
)
BACK = re.compile(r"\b(back|previous|prev|cancel)\b", re.I)
ERRORS = re.compile(
    r"errors? found|is required and must have a value|please (fill|complete)[^.]{0,40}required|"
    r"fix the errors|this field is required|please correct|cannot be (blank|empty)|enter a valid|is not valid|"
    r"there was an error|please try again",
    re.I,
)
ADD = re.compile(r"^\s*add( another| more| new)?\b", re.I)
EDIT_ENTRY = re.compile(r"^\s*(edit|remove|delete)\b", re.I)
APPLICATION_FRAME = re.compile(
    r"appl|job|career|recruit|candidate|form|greenhouse|lever|icims|workday|smartrecruiters|ashby|taleo|"
    r"successfactors|oraclecloud|jobvite|workable|zoho|keka|darwinbox",
    re.I,
)
# The site failed, not the form: retrying the same steps won't help.
SITE_ERROR = re.compile(
    r"something went wrong|try refreshing|an (unexpected )?error (has )?occurred|unexpected error|there was an error|"
    r"page (isn't|is not|could not be) (available|found|loaded)|we couldn'?t load|temporarily unavailable",
    re.I,
)
# Blocks on a careers page that are not the application: newsletters, demos, trials, product sign-ups.
NOT_APPLICATION = re.compile(
    r"newsletter|subscribe|free trial|(book|get|request) (a )?demo|contact sales|launch your|sign up for (our|free)|"
    r"stay (up to date|informed|in touch)|job alerts?",
    re.I,
)
# Words any job application page says somewhere.
APPLICATION_WORDS = re.compile(
    r"appl(y|ication|icant)|r[ée]sum[ée]|\bcv\b|career|\bjobs?\b|position|candidate|vacanc|opening|hiring|recruit",
    re.I,
)
DECORATIVE_FRAME = re.compile(r"social|share|widget|video|youtube|vimeo|chat|analytics|map|captcha|ads?\b", re.I)
CLOSED = re.compile(
    r"no longer (available|accepting|open)|job (has )?expired|(position|job|role) (has been |is )?(filled|closed)|"
    r"job (is )?not found|(posting|job) (has )?closed|not accepting applications",
    re.I,
)
# Buttons that do something to the page rather than answer a question.
UTILITY = re.compile(
    r"\b(upload|attach|browse|remove|delete|edit|cancel|close|search|clear|sign ?in|log ?in|register|menu|help|"
    r"view|show|hide|more|less|save|next|back|continue|submit|apply|dropbox|google drive|onedrive|box|"
    r"manually|paste|locate|toggle|flyout|copy)\b",
    re.I,
)
# Repeating sections the procedure may add blocks to, and the profile list that says how many.
# Languages and certifications are left out on purpose: their blocks ask for things your profile doesn't hold
# (proficiency levels, issue dates), and a half-filled block blocks the whole page.
SECTIONS = (
    (re.compile(r"work experience|employment|work history", re.I), "experience", "company"),
    (re.compile(r"education|academic", re.I), "education", "institution"),
)
PROMPT = re.compile(r"search|type to|start typing|press enter|hit enter|items? selected", re.I)
# Fields whose value is usually picked from suggestions the site looks up as you type.
LOOKUP = re.compile(r"location|\bcity\b|country|school|college|universit|institut|company|employer|skill", re.I)
LOADING = re.compile(r"\b(loading|searching)\b", re.I)
# A dropdown drawn as a text box ("Select..."): opened and chosen from, not typed into, when no rule has a value.
SELECT_LIKE = re.compile(r"(^|\s)(select|choose|pick)( one| an option)?\s*(\.\.\.|…)?\s*$", re.I)
# Voluntary self-identification (EEO) questions, and the answer that shares nothing.
EEO = re.compile(
    r"\b(gender|sex|race|racial|ethnic|hispanic|latin[oax]|veteran|disabilit|sexual orientation|pronoun|transgender)",
    re.I,
)
DECLINE = re.compile(
    r"decline|prefer not|do not wish|don'?t wish|not to (say|disclose|answer|identify)|choose not|rather not|"
    r"not specified|i don'?t want to",
    re.I,
)
CURRENT_JOB = re.compile(r"currently work here|i currently work|current(ly)? (employed|working)|still work here", re.I)
SELECTED = re.compile(r"\b[1-9]\d* items? selected", re.I)
MAX_WAITS = 25  # ~10 s for a slow page to settle after an action
NO_FORM_WAITS = 75  # ~30 s for a page that shows no form yet (Workday's first load) before handing over
QUIET_WAITS = 8  # a page with fields but nothing to do: how long it may still be drawing before handing over
MAX_LAYA_OPTIONS = 12  # above this, Laya's option budget is too small to tell options apart
STOP_WORDS = {"of", "and", "in", "the", "a", "an", "for", "to", "or", "with", "at", "on", "by"}


def norm(value):
    return re.sub(r"[^0-9a-z]", "", str(value or "").lower().replace("&", "and"))


def tokens(value):
    return {w for w in re.findall(r"[a-z0-9]+", str(value or "").lower().replace("&", " and ")) if w not in STOP_WORDS}


# Degree abbreviations Indian profiles use, and the names most applicant systems list.
DEGREES = {
    "btech": "bacheloroftechnology",
    "be": "bachelorofengineering",
    "mtech": "masteroftechnology",
    "me": "masterofengineering",
    "bsc": "bachelorofscience",
    "msc": "masterofscience",
    "bca": "bachelorofcomputerapplications",
    "mca": "masterofcomputerapplications",
    "mba": "masterofbusinessadministration",
    "phd": "doctorofphilosophy",
}
# The level a degree is, in the words a "Highest qualification" list uses ("Bachelor's Degree", "Post Graduate").
LEVELS = {
    **dict.fromkeys(
        ("btech", "be", "bsc", "bca", "ba", "bcom", "bba", "bs", "bachelor"),
        {"bachelor", "bachelors", "undergraduate", "graduate", "graduation", "ug"},
    ),
    **dict.fromkeys(
        ("mtech", "me", "msc", "mca", "ma", "mcom", "mba", "ms", "master"),
        {"master", "masters", "postgraduate", "post", "pg"},
    ),
    "phd": {"doctorate", "doctoral", "phd", "doctor"},
}


def option_text(action):
    return action["label"].split(" → ", 1)[-1] if action["kind"] == "select" else action["label"]


class Facts:
    """The candidate facts in the shape rules.key_for expects."""

    def __init__(self, candidate):
        self.by_id = {safe_id(k): Fact(k, str(v), "") for k, v in candidate.items()}
        self.values = {norm(item) for v in candidate.values() for item in str(v).split(", ") if norm(item)}


def decision(operation, action, p=1.0, probabilities=None, reason=None):
    target = action.get("id") if action else None
    ranked = sorted((probabilities or {}).values(), reverse=True)
    result = {
        "operation": operation,
        "action": action,
        "target": target,
        "operation_probability": 1.0,  # the procedure is certain which step comes next
        "target_probability": p if action else None,
        # How far the chosen option is ahead of the next one, when a model chose it: a close call gets confirmed.
        "target_margin": ranked[0] - ranked[1] if len(ranked) > 1 else None,
        "operation_probabilities": {operation: 1.0},
        "target_probabilities": probabilities or ({target: p} if target else {}),
        "usage": {},
        "latency_ms": 0,
    }
    if reason:
        result["reason"] = reason
    return result


def fact_values(question, facts):
    """Your answer(s) to a question: one you saved for it before, else your profile's per the fixed rules (every
    fact a rule names, in order: '+91' and 'India' for a phone code), else Yes for experience with a skill you list."""
    for k, fact in facts.by_id.items():
        if fact.key.startswith("saved: ") and answers(fact.key[7:], question):
            return [item for item in fact.value.split(", ") if norm(item)]
    key = rules.key_for({"label": question, "context": question}, facts, set())
    values = []
    for k in key if isinstance(key, tuple) else (key,):
        fact = facts.by_id.get(safe_id(k)) if k and not k.isupper() else None
        if fact:
            values += [item for item in fact.value.split(", ") if norm(item)]
    if values:
        return values
    asked = HAS_SKILL.search(question)
    if asked:
        phrase = norm(asked.group(1))
        words = {norm(w) for w in re.split(r"[\s,/()]+", asked.group(1))}
        known = [
            norm(item)
            for fact in facts.by_id.values()
            if fact.key == "skills" or (fact.key.startswith("skill_years.") and fact.key != "skill_years.default")
            for item in (fact.value.split(", ") if fact.key == "skills" else [fact.key.split(".", 1)[1]])
        ]
        # A short skill counts only as a whole word: "Git" is not in "digital", "RAG" not in "storage".
        if any(k and len(k) >= 2 and (k == phrase or (k in phrase and (len(k) > 4 or k in words))) for k in known):
            return ["Yes"]
        if any(fact.key == "skill_years.default" for fact in facts.by_id.values()):
            return ["Yes"]  # you give years for any skill (skill_years.default): so the answer is Yes too
    return []


# "Are you from Mumbai?": one place, asked about where you live. "...or nearby?", "relocate" are not this.
PLACE_QUESTION = re.compile(
    r"\bare you (?:from|based (?:in|out of)|located in|living in|residing in|a resident of|currently (?:in|based in|"
    r"located in|living in)) (?:the )?([a-z][a-z .'-]{1,40}?)\s*\??\s*$"
)


def place_answer(question, options, facts):
    """Yes when the place asked about is your city, state or country; No for any other one place."""
    asked = PLACE_QUESTION.search(question.lower().strip(" *"))
    home = [facts.by_id[k].value for k in map(safe_id, ("personal.city", "personal.state", "personal.country"))
            if k in facts.by_id]  # fmt: skip
    if not asked or not home or re.search(r"\b(or|and|near|nearby|around)\b", asked[1]):
        return []
    want = "yes" if any(norm(h) == norm(asked[1]) for h in home) else "no"
    return [o for o in options if norm(option_text(o)) == want]


def profile_answer(question, options, facts):
    """Options that are exactly what your profile says for this question (via the fixed rules)."""
    placed = place_answer(question, options, facts)
    if placed:
        return placed
    values = fact_values(question, facts)
    # A decimal ("2.2" years) is never matched as text: without its point it would read "22 years".
    wanted = [norm(v) for v in values if not re.fullmatch(r"\s*\d+\.\d+\s*", v)]
    wanted += [DEGREES[w] for w in wanted if w in DEGREES]  # "B.Tech" also matches "Bachelor of Technology"
    if not wanted:
        return range_answer(question, options, facts)
    matches = [o for o in options if norm(option_text(o)) in wanted]
    if matches:
        return matches
    # "+91" for "India (+91)": a phone code inside the option's text.
    for code in [v.strip() for v in values if re.fullmatch(r"\+\d{1,4}", v.strip())]:
        coded = [o for o in options if re.search(re.escape(code) + r"(?!\d)", option_text(o))]
        if len(coded) == 1:
            return coded
    # "Job Boards > LinkedIn": a list grouped into categories, answered by its last part.
    leaves = [o for o in options if norm(re.split(r"[>›»]", option_text(o))[-1]) in wanted]
    if leaves:
        return leaves
    # "India" for an option "India (+91)": accepted when exactly one option starts that way, or when one of several
    # also names your state or country ("Bengaluru, Karnataka, IND" over "Bengaluru, Tamil Nadu").
    for w in wanted:
        starts = [o for o in options if norm(option_text(o)).startswith(w)]
        if len(starts) == 1:
            return starts
        if starts:
            others = [v for v in facts.values if len(v) >= 4 and v != w]
            scores = [sum(v in norm(option_text(o)) for v in others) for o in starts]
            if max(scores) > 0 and scores.count(max(scores)) == 1:
                return [starts[scores.index(max(scores))]]
    return range_answer(question, options, facts)


def bounds(label):
    """The numbers an option covers: '2-5 Years' -> (2, 5), 'More than 5' / '5+' -> (5, inf), 'Less than 1' ->
    (0, 1), '30 days' -> (30, 30). None when the option names no number."""
    text = label.lower().replace("–", "-").replace("—", "-").replace(",", "")
    number = r"(\d+(?:\.\d+)?)"
    if re.search(r"\b(fresher|no experience|immediate(ly)?|none)\b", text) and not re.search(r"\d", text):
        return 0.0, 0.0
    found = re.search(number + r"\s*(?:-|to)\s*" + number, text)
    if found:
        return float(found[1]), float(found[2])
    found = re.search(r"(?:more than|above|over|greater than|>|at least|min(?:imum)?)\s*" + number, text) or re.search(
        number + r"\s*(?:\+|(?:and|or) (?:above|more))", text
    )
    if found:
        return float(found[1]), float("inf")
    found = re.search(r"(?:less than|below|under|<|up ?to|within|max(?:imum)?)\s*" + number, text)
    if found:
        return 0.0, float(found[1])
    found = re.fullmatch(r"\D*" + number + r"\D*", text)
    return (float(found[1]), float(found[1])) if found else None


def range_answer(question, options, facts):
    """The option whose range holds your number: '2-5 Years' for 4.5 years, '16-20 LPA' for a CTC of 17 LPA."""
    ranges = [bounds(option_text(o)) for o in options]
    if sum(r is not None for r in ranges) < 2:
        return []
    shown = " ".join(option_text(o) for o in options)
    key = rules.key_for({"label": question, "context": f"{question} {shown}"}, facts, set())
    fact = next((facts.by_id.get(safe_id(k)) for k in (key if isinstance(key, tuple) else (key,)) if k), None)
    try:
        value = float(str(fact.value).replace(",", "")) if fact else None
    except ValueError:
        value = None
    if value is None:
        return []
    if fact.key.endswith("_days") and re.search(r"month", shown, re.I) and not re.search(r"day", shown, re.I):
        value /= 30  # notice in days, options in months
    inside = [o for o, r in zip(options, ranges) if r and r[0] <= value < r[1]]
    inside = inside or [o for o, r in zip(options, ranges) if r and r[0] <= value <= r[1]]
    if not inside and re.search(r"(experience_years|^skill_years\.)", fact.key):
        # One whole number per option ("0 year", "1 year", "2 years"): your completed years, 2.2 -> "2 years".
        inside = [o for o, r in zip(options, ranges) if r and r[0] == r[1] == int(value)]
    return inside[:1] if len(inside) >= 1 else []


# Words in fact names that only structure the profile ("experience.0.end"): they relate to nothing by themselves.
STRUCTURAL = set(
    "end start first last name title summary total annual fixed variable current expected years year item days text "
    "local code personal work preferences education experience compensation links saved self identification".split()
)


def relates(question, candidate):
    """Is the question about something your profile holds at all (a skill, a place, a preference it names)? If not,
    a model has nothing to choose from, and its pick would be invented."""
    words = set()
    for key, value in candidate.items():
        if key.startswith("saved: "):
            words |= rules.words_of(key[7:])
            continue
        words |= rules.words_of(key.rsplit(".", 1)[-1].replace("_", " ")) - STRUCTURAL
        if not re.fullmatch(r"(yes|no|true|false|[\d.,\s%-]+)", str(value).strip().lower()):
            words |= rules.words_of(value)
    return bool(rules.words_of(question) & words)


def eeo_decline(question, option, candidate):
    """Is this the 'decline to self-identify' answer to a voluntary EEO question, and does your profile allow
    choosing it for you? (self_identification.decline_eeo: false turns it off.)"""
    allowed = str(candidate.get("self_identification.decline_eeo", "Yes")).lower() not in {"no", "false"}
    return allowed and bool(EEO.search(question or "")) and bool(DECLINE.search(option or ""))


def narrow(question, options, candidate, facts):
    """The options that share words with your answer to this question (its rule fact, else the most relevant
    facts): a long list (degrees, universities, cities) cut to what Laya can compare. Best first."""
    values = fact_values(question, facts)
    if not values and candidate:
        relevant = laya_backend.shortlist(question, {k: f"{k}: {v}" for k, v in candidate.items()}, 3)
        values = [str(candidate[k]) for k in relevant]
    words = set()
    for value in values:
        found = tokens(value)
        words |= found
        for w in found | {norm(value)}:
            words |= LEVELS.get(w, set())
            if re.fullmatch(r"0?[1-9]|1[0-2]", w) and re.search(r"month", question, re.I):
                words.add(MONTHS[int(w) - 1].lower())
    scored = [(len(words & tokens(option_text(o))), i, o) for i, o in enumerate(options)]
    return [o for score, _, o in sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1]))][
        :MAX_LAYA_OPTIONS
    ]


def laya_pick(question, options, candidate):
    """Ask Laya which option answers the question. Returns (option, probability, probabilities) or None."""
    if not 2 <= len(options) <= MAX_LAYA_OPTIONS or not question:
        return None
    facts = {k: f"{k}: {v}" for k, v in candidate.items()}
    relevant = laya_backend.shortlist(question, facts, 6)
    criteria = {str(i): option_text(o) for i, o in enumerate(options, 1)}
    body = {
        "state": {"field": {"label": question}, "candidate": {k: candidate[k] for k in relevant}},
        "questions": {
            "option": {
                "type": "choice",
                "instructions": f"Which option is the candidate's answer to the question '{question}'?",
                "criteria": criteria,
            }
        },
    }
    from . import model  # the configured backend (Laya by default, or Jev): model imports this module

    answer = model.systemone(body)[0]["answers"]["option"]
    chosen = options[int(answer["choice"]) - 1]
    by_target = {options[int(k) - 1].get("id"): v for k, v in answer["probabilities"].items()}
    return chosen, answer["probabilities"][answer["choice"]], by_target


def answer_group(question, options, candidate, facts, operation):
    exact = profile_answer(question, options, facts)
    if exact:
        return decision(operation, exact[0])
    declines = [o for o in options if eeo_decline(question, option_text(o), candidate)]
    if declines:
        return decision(operation, declines[0])  # EEO: the answer that shares nothing, unless your profile says more
    ruled = rules.match({"label": question, "context": question}, facts)
    if ruled == "ASK_USER":
        return None  # a question only you can answer (a relative working here, your birth date): never guessed
    binary = all(re.fullmatch(r"yes|no|true|false|agree|disagree", norm(option_text(o))) for o in options)
    if ruled is None and binary and (not re.search(r"\byou|\byour", question, re.I) or QUIZ.search(question)):
        return None  # a quiz statement ("An HTTP 200 guarantees..."), not a question about you: never guessed
    if ruled is None and CLAIM.search(question):
        return None  # yours to say: it goes to QUESTIONS.md, answered once for every later form
    if ruled is None and not relates(question, candidate):
        return None  # "Have you managed onboarding?": nothing in your profile says; a model's pick would be invented
    if len(options) > MAX_LAYA_OPTIONS:
        options = narrow(question, options, candidate, facts)
        if len(options) == 1:
            return decision(operation, options[0], 0.7)  # one option shares words with your answer: likely, not sure
    picked = laya_pick(question, options, candidate)
    if picked:
        return decision(operation, picked[0], picked[1], picked[2])
    return None


def question_of(context, group):
    """A choice group's question: its context without the options' own words ("Relocate? Yes No" -> "Relocate?")."""
    text = f" {context} "
    for o in group:
        text = text.replace(f" {option_text(o)} ", " ", 1)
    return undouble(re.sub(r"\s+", " ", text).strip()) or context


# Workday's application opens on a "Create Account/Sign In" step, sometimes before its form (and password box)
# has rendered: that step is a sign-in wall too.
SIGN_IN_STEP = re.compile(r"current step \d+ of \d+\W*(create account\s*/\s*sign in|sign in)\b", re.I)


# "Continue with Google" and its kind: a sign-in even with no password box on the page. "Apply with LinkedIn"
# (an autofill button on Greenhouse and Lever forms) is not one.
SSO = re.compile(
    r"^\s*(?:sign\s*in|log\s*in|continue|sign\s*up|register)\s+(?:with|using|via)\s+(google|microsoft|linkedin|apple)\b",
    re.I,
)
# The identity providers' own sign-in pages (Google's account chooser, Microsoft's login...).
IDP = re.compile(
    r"^https://(accounts\.google\.com|login\.microsoftonline\.com|login\.live\.com|appleid\.apple\.com)/", re.I
)
ACCOUNT_FIELD = re.compile(r"e-?mail|password|user\s*name|phone|mobile|search|otp|code|captcha", re.I)


def sso_buttons(page):
    """'Sign in / Continue with Google' (Microsoft, LinkedIn, Apple) buttons on this page."""
    return [a for a in page.get("actions") or [] if a.get("kind") == "click" and SSO.search(a.get("label") or "")]


def application_fields(page):
    """Does the page hold anything of an application (not just the e-mail and password of an account)?"""
    for a in page.get("actions") or []:
        if a.get("kind") in {"select", "setdate", "upload"}:
            return True
        if a.get("kind") == "fill" and not ACCOUNT_FIELD.search(f"{a.get('label', '')} {a.get('input_type', '')}"):
            return True
    return False


def sign_in_wall(page):
    """A page that wants you to sign in or create an account first: a visible password box, Workday's sign-in
    step, an identity provider's page, or only "Continue with Google"-style buttons and no application fields.
    The agent never types there: it waits for you (or, when allowed, signs in with Google by clicks only)."""
    return bool(
        page.get("login")
        or SIGN_IN_STEP.search(page.get("text") or "")
        or IDP.match(page.get("url") or "")
        or (sso_buttons(page) and not application_fields(page))
    )


def declaration_question(action):
    """How a single checkbox is asked in QUESTIONS.md: 'Tick this box? <its words>'. A short label ('I agree') is
    read with the text it sits under, so two different 'I agree' boxes stay two questions."""
    label = undouble(re.sub(r"\s+", " ", action.get("label") or "")).strip()
    context = undouble(re.sub(r"\s+", " ", action.get("context") or "")).strip()
    words = label if len(label) >= 40 or not context else f"{context[:240]} ({label})"
    return f"Tick this box? {words}"


def waiting(history):
    """How many of the latest steps in a row were waits."""
    count = 0
    for entry in reversed(history):
        if entry.get("kind") != "wait":
            break
        count += 1
    return count


def entries(candidate, name, key):
    count = 0
    while f"{name}.{count}.{key}" in candidate:
        count += 1
    return count


def add_block(a, candidate, history, url, actions=()):
    """Should this Add / Add Another button be pressed for a repeating section, given your profile?"""
    where = " ".join((a["label"], a.get("context") or "", a.get("section") or ""))
    for pattern, name, key in SECTIONS:
        if pattern.search(where):
            wanted = entries(candidate, name, key)
            # Entries the page already lists, each with its own Edit / Remove button (LinkedIn's work experience
            # step, taken from your profile there): the section is filled; a new entry would change that profile.
            if any(
                x.get("kind") == "click"
                and EDIT_ENTRY.search(x["label"])
                and pattern.search(" ".join((x["label"], x.get("section") or "")))
                for x in actions
            ):
                return False
            # ... or the button's own context already shows one of your entries ("School Example Institute ...").
            shown = norm(a.get("context") or "")
            if any(
                len(norm(candidate[f"{name}.{i}.{key}"])) >= 4 and norm(candidate[f"{name}.{i}.{key}"]) in shown
                for i in range(wanted)
            ):
                return False
            # The heading above the button changes as blocks are added ("Work Experience 1", then "... 2"), so
            # count every press of this button within the same section family on this page.
            pressed = sum(
                1
                for h in history
                if h.get("kind") == "click"
                and h.get("action") == a["label"]
                and pattern.search(" ".join((h.get("section") or "", h.get("context") or "")))
                and h.get("url") == url
            )
            another = bool(re.search(r"another|more|new", a["label"], re.I))
            # "Add" creates the first block; "Add Another" the rest (one block usually shows already).
            return pressed < (wanted - 1 if another else min(wanted, 1))
    return False


def is_prompt(a):
    # Workday names its skills box "Type to Add Skills": the label alone can say it is a search prompt.
    return a.get("role") in {"combobox", "searchbox"} or bool(
        PROMPT.search(" ".join(str(a.get(k) or "") for k in ("placeholder", "help", "context")))
        or re.match(r"\s*(type to|start typing|search)\b", a.get("label") or "", re.I)
    )


def settling(history):
    """True while the page may still be drawing itself: at the very start, right after a click that changed the
    page (Next, opening a form), or while the last wait still saw changes. Typing into a half-drawn form (Workday
    and other JavaScript apps) gets wiped when the app finishes, so wait until one wait sees no change."""
    if not history:
        return True
    last = history[-1]
    if last.get("kind") == "wait":
        return bool(last.get("page_changed")) and waiting(history) < MAX_WAITS
    return last.get("kind") in {"click", "frame"} and bool(last.get("page_changed"))


def typed_times(history, url, a):
    """Typed into this field for this question (a chat reply box asks many questions under one label)."""
    context = (a.get("context") or "")[:160]
    return sum(
        1
        for h in history
        if h.get("kind") == "fill"
        and h.get("action") == a["label"]
        and h.get("url") == url
        and (h.get("context") or "")[:160] == context
    )


def is_button_choice(a):
    """A button that answers a question ("Yes", "No", "Hybrid") without aria-pressed: it shows its chosen state
    some other way, its label is short, and its context asks something."""
    context = a.get("context") or ""
    return (
        a["kind"] == "click"
        and a.get("role") == "button"
        and a.get("chosen") in {"true", "false"}
        and 0 < len(a["label"]) <= 40
        and bool(re.search(r"[?*✱]", context))
        and not (UTILITY.search(a["label"]) or ADD.search(a["label"]) or COOKIES.search(a["label"]))
    )


FILE_NAME = re.compile(r"\.(pdf|docx?|rtf|odt)\b|\b(pdf|docx?)\b.*\b(kb|mb)\b", re.I)


def resume_cards(group):
    """A list of files already on the site (LinkedIn's saved résumés): which one to send is the upload step's
    choice (your profile's own résumé), never a question for a model."""
    return any(FILE_NAME.search(o.get("label", "")) for o in group)


def is_choice(a):
    return a["kind"] == "click" and (
        a.get("role") == "radio" or a.get("pressed") in {"true", "false"} or is_button_choice(a)
    )


def is_on(a):
    return "true" in (a.get("checked"), a.get("pressed"), a.get("chosen"))


def same_value(shown, wanted):
    """Does a field's current value already say what your profile says (formatting aside)?"""
    a, b = norm(shown), norm(wanted)
    if a == b or (min(len(a), len(b)) >= 4 and (a.endswith(b) or b.endswith(a))):
        return True
    try:
        return float(str(shown).replace(",", "")) == float(str(wanted).replace(",", ""))
    except ValueError:
        return False


def touched(history, url, label=None, context=None, question=None):
    """Has the agent already worked on this field or question on this page (typed, chosen, skipped)?"""
    for h in history:
        if h.get("url") != url or h.get("kind") not in {"fill", "select", "setdate", "click", "skip"}:
            continue
        if label is not None and h.get("action") == label:
            return True
        if context and h.get("context") == context[:160]:
            return True
        if question and str(h.get("action", "")).startswith(question + " → "):
            return True
    return False


def unanswered(actions):
    """Required questions on this page that still have no answer: labels, for you to see."""
    missing, seen = [], set()
    for a in actions:
        kind, key = a["kind"], a.get("node")
        if not a.get("required"):
            continue
        if kind in {"fill", "setdate", "upload"} and not a.get("value") and key not in seen:
            seen.add(key)
            missing.append(a["label"])
        elif kind == "select" and key not in seen:
            seen.add(key)
            if PLACEHOLDER.search(a.get("current_value") or ""):
                missing.append(a["label"].split(" → ", 1)[0])
        elif is_choice(a) and a.get("context") and ("q", a["context"]) not in seen:
            seen.add(("q", a["context"]))
            group = [o for o in actions if is_choice(o) and o.get("context") == a["context"]]
            if not any(is_on(o) for o in group):
                missing.append(question_of(a["context"], group))
        elif kind == "click" and a.get("role") == "checkbox" and a.get("checked") == "false":
            group = [o for o in actions if o.get("role") == "checkbox" and o.get("context") == a.get("context")]
            if len(group) < 2:
                missing.append(a["label"])
            elif a.get("context") and ("c", a["context"]) not in seen and not any(is_on(o) for o in group):
                seen.add(("c", a["context"]))
                missing.append(question_of(a["context"], group))
    return [m[:60] for m in missing]


def invalid_fields(actions):
    """Fields the site marks invalid, with its message: 'Email: Enter a valid email'."""
    found, seen = [], set()
    for a in actions:
        if a.get("invalid") and a.get("node") not in seen and a["kind"] in {"fill", "setdate", "select", "upload"}:
            seen.add(a.get("node"))
            label = a["label"].split(" → ", 1)[0][:50]
            found.append(f"{label}: {a['error']}" if a.get("error") else label)
    return found


def opener(history):
    """The last thing done before a list of options showed: typing into a field, or clicking a dropdown open."""
    return next((h for h in reversed(history) if h.get("kind") not in {"wait", "scroll", "skip"}), None)


def answer_options(options, actions, candidate, facts, history):
    """Which of the options showing (suggestions, an open dropdown) to click, or None."""
    last = opener(history)
    if (
        last
        and last.get("kind") == "click"
        and any(
            last.get("action") == o["label"] and last.get("context") == (o.get("context") or "")[:160] for o in options
        )
    ):
        return None  # one of these was just clicked and the list stayed (a multi-select): don't click again
    typed = norm(last.get("typed")) if last and last.get("kind") in {"fill", "enter"} else ""
    if typed:
        # Suggestions for what was just typed: the one that is that text, else the one that starts with it.
        # Never another suggestion: a skill or city the site doesn't offer stays unmatched and is reported.
        exact = [o for o in options if typed in {norm(o["label"]), norm(re.split(r"[>›»]", o["label"])[-1])}]
        if exact:
            return decision("CLICK", exact[0])
        starts = [o for o in options if norm(o["label"]).startswith(typed)]
        if len(starts) == 1:
            return decision("CLICK", starts[0], 0.9)
        if starts:
            # "Udaipur, Rajasthan, India" over "Udaipur Airport": the one that also names your state or country.
            # Equally good ones: the site's own first suggestion.
            scores = [sum(v in norm(o["label"]) for v in facts.values if len(v) >= 4) for o in starts]
            best = max(scores)
            return decision("CLICK", starts[scores.index(best)], 0.85 if scores.count(best) == 1 else 0.65)
        return None
    question = options[0].get("context") or ""
    if not question and last and last.get("kind") == "click":
        # A dropdown's list is usually drawn apart from it: the question is the one of the control just opened.
        question = last.get("context") or re.sub(r"^Open ", "", last.get("action") or "")
    return answer_group(question, options, candidate, facts, "CLICK") if question else None


def choose(page, candidate, exclude=(), history=()):
    actions = [a for a in page["actions"] if not a.get("nav")]  # a header's search box is not the application
    url = page.get("url")
    facts = Facts(candidate)
    has_fields = any(a["kind"] in {"fill", "setdate", "upload", "select"} for a in actions)
    input_nodes = {a.get("node") for a in actions if a["kind"] in {"fill", "select", "setdate", "upload"}}

    def first(test):
        return next((a for a in actions if test(a)), None)

    wait = first(lambda a: a.get("id") == "wait")
    if wait and has_fields and settling(history):
        return decision("WAIT", wait)
    if sign_in_wall(page):
        return decision(
            "BLOCKED",
            None,
            reason="Sign in or create an account here (portals like Workday need one per company). The agent never "
            "types passwords; once you're in, run the job again and it continues from the form.",
        )
    if (
        "naukri.com" in (url or "")
        and APPLIED.search(page.get("text", ""))
        and any(h.get("kind") == "click" for h in history)
    ):
        return decision("APPLIED", None, reason="Naukri shows the application as sent.")
    # Applied already (by you, or by following "Apply on company site", which Naukri counts as applying).
    if (
        not has_fields
        and not history
        and (
            any(a["kind"] == "click" and ALREADY.fullmatch(a["label"].strip()) for a in actions)
            or APPLIED_AGO.search(page.get("text", ""))
        )
    ):
        return decision("BLOCKED", None, reason="The site shows this job as already applied.")
    if not has_fields and SITE_ERROR.search(page.get("text", "")) and history:
        return decision("BLOCKED", None, reason='The site shows an error ("Something went wrong"): try again later.')
    if not has_fields and CLOSED.search(page.get("text", "")):
        return decision("BLOCKED", None, reason="This job is no longer open (the page says it has closed or expired).")
    # The application's frame, not a header, video or ad: an applicant system's page, or one that says so, else the
    # biggest.
    # A form on a page that says nothing about jobs (a product sign-up the Apply link landed on) is not filled.
    shown = " ".join([page.get("text", ""), page.get("title", ""), url or ""] + [a["label"] for a in actions])
    fields_here = [a for a in actions if a["kind"] in {"fill", "setdate", "upload", "select"}]
    if fields_here and not APPLICATION_WORDS.search(shown) and not any(h.get("url") == url for h in history):
        return decision(
            "BLOCKED",
            None,
            reason="This page doesn't look like a job application (nothing on it mentions a job, "
            "an application or a résumé), so nothing was filled.",
        )
    frame = max(
        (a for a in actions if a["kind"] == "frame"),
        key=lambda a: (
            bool(APPLICATION_FRAME.search(f"{a['label']} {a.get('value', '')}"))
            and not DECORATIVE_FRAME.search(f"{a['label']} {a.get('value', '')}"),
            (a.get("rect") or {}).get("w", 0) * (a.get("rect") or {}).get("h", 0),
        ),
        default=None,
    )
    if frame and not has_fields:
        # A frame that is the application (its address or title says so) is opened. Any other frame (a header, a
        # video) only when the page offers no Apply or Next of its own: "Apply for this job online" beats a banner.
        named = f"{frame['label']} {frame.get('value', '')}"
        looks_like_form = APPLICATION_FRAME.search(named) and not DECORATIVE_FRAME.search(named)
        own_way_in = any(
            a["kind"] == "click"
            and a.get("role") in {"button", "link"}
            and (SUBMIT.search(a["label"]) or ADVANCE.search(a["label"]))
            for a in actions
        )
        if looks_like_form or not own_way_in:
            return decision("OPEN_FORM", frame)
    cookie = first(
        lambda a: (
            a["kind"] == "click"
            and a.get("role") in {"button", "link"}
            and COOKIES.search(a["label"])
            and not touched(history, url, label=a["label"])  # tried once: a banner that stays isn't clicked forever
            and re.search(
                r"cookie|tracking|consent manager|privacy preference", f"{a['label']} {a.get('context', '')}", re.I
            )
        )
    )  # "Decline" in a cookie banner, not "I decline to self-identify"
    if cookie:
        return decision("CLICK", cookie)

    # A chat questionnaire (Naukri): after answering the latest question, send the answer with its Save.
    answered = next((h for h in reversed(history) if h.get("kind") not in {"wait", "scroll"}), None)
    if answered and answered.get("chat") and answered.get("kind") in {"fill", "click", "select"}:
        send = first(lambda a: a.get("chat") and a["kind"] == "click" and CHAT_SEND.search(a["label"]))
        if send and not CHAT_SEND.search(answered.get("action", "")):
            return decision("CLICK", send)

    options = [a for a in actions if a["kind"] == "click" and a.get("role") == "option"]
    if options:
        picked = answer_options(options, actions, candidate, facts, history)
        if picked:
            return picked
        last = opener(history)
        escape = first(lambda a: a.get("id") == "escape")
        if escape and last and last.get("kind") == "click" and str(last.get("action", "")).startswith("Open "):
            return decision("KEY", escape)  # opened a list with no answer for you in it: close it, move on

    last = next((h for h in reversed(history) if h.get("kind") not in {"wait", "scroll"}), None)
    if not options and last and last.get("kind") == "enter" and wait and waiting(history) < 4:
        return decision("WAIT", wait)  # results of a search are loading
    if not options and last and last.get("kind") == "fill":
        typed = next((a for a in actions if a["kind"] == "fill" and a["label"] == last.get("action")), None)
        looks_up = typed and typed.get("value") and (is_prompt(typed) or LOOKUP.search(typed["label"]))
        if looks_up and wait and waiting(history) < (6 if LOADING.search(page.get("text", "")) else 1):
            return decision("WAIT", wait)  # suggestions fetched over the network can take a moment
        # Enter in a <form> could submit it: only in a search box of its own ("type and press Enter").
        if typed and typed.get("value") and is_prompt(typed) and not typed.get("in_form"):
            enter = {
                "id": f"enter_{typed['node']}",
                "kind": "enter",
                "node": typed["node"],
                "role": typed.get("role"),
                "label": f"Press Enter in '{typed['label']}'",
                "context": typed.get("context") or "",
                "section": typed.get("section") or "",
            }
            return decision("PRESS_ENTER", enter)  # "type and press Enter to find a matching value"

    seen = set()
    for a in actions:
        kind, role = a["kind"], a.get("role")
        if NOT_APPLICATION.search(" ".join((a["label"], a.get("context") or "", a.get("section") or ""))):
            continue  # a newsletter box or demo form beside the application
        if kind == "fill" and not a.get("multiple") and SELECTED.search(a.get("context") or ""):
            continue  # a single-choice prompt that already shows "1 item selected"
        if kind == "fill" and not a.get("multiple") and not a.get("value") and typed_times(history, url, a) >= 2:
            continue  # typed twice and the site cleared it both times (it wants a pick from its list): yours
        if kind == "fill" and not a.get("value") and role == "combobox" and not a.get("multiple"):
            shown = " ".join((a.get("placeholder") or "", a["label"]))
            opens = next(
                (o for o in actions if o.get("node") == a["node"] and o["label"] == f"Open {a['label']}"), None
            )
            if opens and SELECT_LIKE.search(shown) and not touched(history, url, opens["label"]):
                # A dropdown drawn as a text box: typed into when a rule has your value (its search finds it),
                # else opened, and answered from its list.
                if rules.match(a, facts, False, (), page.get("text", "")) not in facts.by_id:
                    return decision("CLICK", opens)
        if kind == "fill" and (a.get("multiple") or not a.get("value")):
            return decision("TYPE_TEXT", a)
        if kind == "fill" and not touched(history, url, label=a["label"]):
            # Filled in by the site (résumé parsing, autofill, an earlier visit): correct it once if your profile
            # says something else.
            choice = rules.match(a, facts, False, (), page.get("text", ""))
            fact = facts.by_id.get(choice) if choice else None
            if fact and not same_value(a["value"], fact.value):
                return decision("TYPE_TEXT", {**a, "replaces": a["value"]})
            continue
        if kind == "setdate" and not a.get("value"):
            return decision("SET_DATE", a)
        if kind == "upload" and not a.get("value"):
            # Your résumé is already on the site (LinkedIn, Indeed keep uploaded ones as cards): choose it rather
            # than uploading a copy. Otherwise upload the one from your profile.
            names = [norm(v) for k, v in candidate.items() if k.startswith("document.") and "cover" not in k]
            card = next((o for o in actions if is_choice(o) and any(n and n in norm(o["label"]) for n in names)), None)
            if card:
                if not is_on(card) and not touched(history, url, label=card["label"]):
                    return decision("CLICK", card)
                continue
            # Workday and others empty the file input once the file is in their list ("x.pdf successfully
            # uploaded", "Delete x.pdf"): an empty input then is not a missing file. Never upload it twice.
            stems = [norm(re.sub(r"\.\w{2,4}$", "", n)) for n in names]
            # The list around a file field names saved files even when their cards are scrolled out of view.
            shown = norm(
                " ".join(
                    [page.get("text", ""), *(o["label"] for o in actions), *(o.get("context") or "" for o in actions)]
                )
            )
            uploaded = any(
                h.get("url") == url and h.get("kind") == "upload" and h.get("action") == a["label"] for h in history
            )
            if uploaded or any(s and s in shown for s in stems):
                continue
            return decision("UPLOAD", a)
        if kind == "select" and a["node"] not in seen:
            seen.add(a["node"])
            group = [o for o in actions if o["kind"] == "select" and o["node"] == a["node"]]
            question = a["label"].split(" → ", 1)[0]
            if a.get("multiple"):
                wanted = profile_answer(question, group, facts)
                if wanted:
                    return decision("SELECT", wanted[0])
            elif PLACEHOLDER.search(a.get("current_value") or ""):
                picked = answer_group(question, group, candidate, facts, "SELECT")
                if picked:
                    return picked
            elif not touched(history, url, question=question):
                # A preset choice (the site's default, a parsed résumé) that isn't your profile's answer.
                wanted = profile_answer(question, group, facts)
                if wanted and not any(norm(option_text(w)) == norm(a.get("current_value")) for w in wanted):
                    return decision("SELECT", {**wanted[0], "replaces": a.get("current_value")})
            continue
        if is_choice(a) and a.get("context") and ("q", a["context"]) not in seen:
            seen.add(("q", a["context"]))
            group = [o for o in actions if o.get("context") == a["context"] and is_choice(o)]
            if len(group) < 2 and not (role == "radio" or a.get("pressed") in {"true", "false"}):
                continue  # a lone button is not a choice between answers
            if (
                role == "button"
                and a.get("pressed") is None
                and any(o["kind"] == "upload" and o.get("context") == a["context"] for o in actions)
            ):
                continue  # buttons around a file field (Dropbox, Google Drive) are ways to attach, not answers
            if resume_cards(group):
                # The site's saved résumés: select the one that is your profile's résumé (LinkedIn prefixes the
                # name, "v4_..."). None of them: it is uploaded, or, if the page offers no upload, stop before
                # sending one you didn't pick.
                names = [norm(v) for k, v in candidate.items() if k.startswith("document.") and "cover" not in k]
                ours = [o for o in group if any(n and n in norm(o["label"]) for n in names)]
                # The same file uploaded twice shows as two cards: either one chosen is yours chosen.
                mine = next((o for o in ours if is_on(o)), ours[0] if ours else None)
                if mine and not is_on(mine) and not touched(history, url, label=mine["label"]):
                    return decision("CLICK", mine)
                # LinkedIn's cards report none chosen, even the one it has selected: there a click is all there is.
                readable = any(is_on(o) for o in group)
                if mine and not is_on(mine) and readable:
                    # Clicked and another card shows as chosen: going on would send that résumé.
                    return decision(
                        "BLOCKED",
                        None,
                        reason=f"Your résumé ({mine['label'][:60]}) didn't stay selected among the site's saved "
                        "résumés: select it yourself before sending, or another résumé goes with this application.",
                    )
                listed = [n for n in names if n and n in norm(a["context"])]
                if not mine and listed:
                    # The list names your résumé but its card is out of view (LinkedIn moves the selected one to the
                    # top, above the scrolled panel): bring it back into view, never upload another copy.
                    picked = [
                        i
                        for i, h in enumerate(history)
                        if h.get("url") == url
                        and h.get("kind") == "click"
                        and any(n in norm(str(h.get("action"))) for n in listed)
                    ]
                    ups = [
                        i
                        for i, h in enumerate(history)
                        if h.get("url") == url
                        and h.get("kind") == "scroll"
                        and str(h.get("action")).startswith("Scroll up")
                    ]
                    mine_here = [h for h in history if h.get("url") == url]
                    revisited = any(
                        h.get("kind") == "scroll"
                        and str(h.get("action")).startswith("Scroll up")
                        and n.get("kind") in {"wait", "scroll"}
                        and not str(n.get("action")).startswith("Scroll up")
                        for h, n in zip(mine_here, mine_here[1:])
                    )
                    if picked or revisited:
                        # Clicked already, or seen after scrolling back to it with nothing to do (not chosen, it
                        # would have been clicked; another card chosen would have stopped the run above): carry on
                        # rather than scroll back and forth (LinkedIn keeps the box scrolled from the step before).
                        continue
                    up = next((o for o in actions if o.get("id") == "scroll_up"), None)
                    if up and len(ups) < 4:
                        return decision("SCROLL_UP", up)
                    return decision(
                        "BLOCKED",
                        None,
                        reason="Your résumé is in the site's saved list but its card couldn't be brought into view: "
                        "select it yourself.",
                    )
                button = next(
                    (
                        o
                        for o in actions
                        if o["kind"] == "click"
                        and o.get("role") == "button"
                        and re.search(r"\b(upload|attach)\b.{0,20}\b(r[ée]sum[ée]|cv|file|document)", o["label"], re.I)
                    ),
                    None,
                )
                if not mine and names and button and not touched(history, url, label=button["label"]):
                    return decision(
                        "UPLOAD", {**button, "kind": "upload", "role": "file", "chooser": True, "value": ""}
                    )
                if not mine and names and not any(o["kind"] == "upload" for o in actions):
                    return decision(
                        "BLOCKED",
                        None,
                        reason="None of the résumés saved on this site is your profile's résumé, and there is no "
                        "upload here: attach it once on the site, then run again.",
                    )
                continue
            question = question_of(a["context"], group)
            if not any(is_on(o) for o in group) and not touched(history, url, context=a["context"]):
                # Answered once already and still showing nothing chosen: the site shows it some other way, or
                # refused it. Never clicked again (it would loop); unanswered required ones are listed for you.
                picked = answer_group(question, group, candidate, facts, "CLICK")
                if picked:
                    return picked
            elif not touched(history, url, context=a["context"]):
                wanted = profile_answer(question, group, facts)
                if wanted and not any(is_on(w) for w in wanted):
                    return decision("CLICK", {**wanted[0], "replaces": next(o["label"] for o in group if is_on(o))})
            continue
        if kind == "click" and role == "checkbox" and a.get("context"):
            group = [
                o
                for o in actions
                if o["kind"] == "click" and o.get("role") == "checkbox" and o.get("context") == a["context"]
            ]
            if len(group) >= 2:
                # "Select all that apply": tick the ones your profile lists; never untick; no guessing.
                if ("c", a["context"]) not in seen:
                    seen.add(("c", a["context"]))
                    wanted = profile_answer(question_of(a["context"], group), group, facts)
                    todo = [o for o in wanted if o.get("checked") == "false"]
                    if todo:
                        return decision("CLICK", todo[0])
                continue
        if kind == "click" and role == "checkbox" and CURRENT_JOB.search(a["label"]):
            # Only the box of Work Experience 1 (your current job), known from its heading or because the page
            # shows a single block. Other blocks' boxes stay unticked.
            where = " ".join((a["label"], a.get("context") or "")).lower()
            block = rules.block_index("work experience", where, a.get("section") or "", page.get("text", ""))
            first_job = "experience.0.company" in candidate and "experience.0.end" not in candidate
            if block == 0 and first_job and a.get("checked") == "false":
                return decision("CLICK", a)
            continue
        if kind == "click" and role == "checkbox" and a.get("checked") == "false" and a.get("required"):
            return decision("CLICK", a)
        if kind == "click" and role in {"button", "link"} and ADD.search(a["label"]):
            if add_block(a, candidate, history, url, page["actions"]):
                return decision("CLICK", a)
            continue
        custom = role == "combobox" or (role == "button" and PLACEHOLDER.search(a["label"]) and a["label"].strip())
        if kind == "click" and custom and a.get("expanded") == "false" and a.get("node") not in input_nodes:
            if role == "button" or not a.get("value") or PLACEHOLDER.search(a.get("value")):
                return decision("CLICK", a)  # a custom dropdown ("Select One"): open it, answer it in step 3

    scroll = first(lambda a: a.get("id") == "scroll_down")
    stuck_scrolls = 0
    for h in reversed(history):
        if h.get("kind") == "wait":
            continue
        if h.get("kind") != "scroll" or h.get("page_changed"):
            break
        stuck_scrolls += 1
    if stuck_scrolls >= 2:
        scroll = None  # scrolled twice and nothing new showed: the rest of the page isn't coming
    if has_fields and scroll:
        # A form continues below: see all of it before pressing Next/Save (Workday keeps those buttons in a
        # footer that is always on screen, so they are visible long before the last field).
        return decision("SCROLL_DOWN", scroll)
    invalid = invalid_fields(actions)
    if has_fields and (invalid or ERRORS.search(page.get("text", ""))):
        # The site says fields are missing or wrong (after a Next/Save click): don't press it again blindly.
        return decision("BLOCKED", None, reason=stuck("The site reports problems on this page.", actions, invalid))
    button = {"button", "link"}
    advance = first(
        lambda a: (
            a["kind"] == "click"
            and a.get("role") in button
            and ADVANCE.search(a["label"])
            and not BACK.search(a["label"])
        )
    )
    # No fields yet: the form may still be loading behind an always-visible Next/Save footer. Give it time,
    # much more before Next/Save (pointless on a blank form) than before 'Apply' on a job posting. A form below
    # the fold looks the same, so once the page has stopped changing, look further down too.
    worked = any(h.get("url") == url and h.get("kind") in {"fill", "select", "setdate", "upload"} for h in history)
    loading = bool(LOADING.search(page.get("text", "")))  # Workday says "Loading" while it draws each field
    patience = NO_FORM_WAITS * 2 if loading else (NO_FORM_WAITS if advance else 8)
    if not has_fields and not worked and wait and waiting(history) < patience:
        if advance and scroll and waiting(history) >= 3 and not settling(history):
            return decision("SCROLL_DOWN", scroll)
        return decision("WAIT", wait)
    if advance:
        last = next((h for h in reversed(history) if h.get("kind") not in {"wait", "scroll", "skip"}), None)
        if (
            last
            and last.get("kind") == "click"
            and last.get("action") == advance["label"]
            and last.get("url") == url
            and last.get("page_changed") is False
        ):
            # Pressed it already, the page didn't change, and nothing new needed filling: the site is refusing
            # (errors, often shown at the top, off screen). Pressing again would loop, so hand over. A step that
            # changed (LinkedIn's steps share one address; a résumé step may need nothing) is pressed on.
            return decision(
                "BLOCKED", None, reason=stuck(f"'{advance['label']}' didn't move the form on.", actions, invalid)
            )
        return decision("CLICK", advance)
    submit = first(
        lambda a: (
            a["kind"] == "click"
            and a.get("role") in button
            and SUBMIT.search(a["label"])
            and APPLY_BUTTON.search(a["label"])
        )  # "Easy Apply", "Submit application"; not a job card mentioning them
    )
    if submit:
        return decision("CLICK", submit)
    if scroll:
        return decision("SCROLL_DOWN", scroll)
    if has_fields:
        # Fields, nothing the procedure can still fill, and no Next/Submit: never "done". A spinner may cover the
        # footer (give it time), or the page has controls the procedure doesn't know: hand over, saying what's left.
        if wait and waiting(history) < QUIET_WAITS:
            return decision("WAIT", wait)
        return decision(
            "BLOCKED",
            None,
            reason=stuck(
                "Nothing more the agent can fill here, and no Next/Continue or Submit button.", actions, invalid
            ),
        )
    if wait and waiting(history) < (NO_FORM_WAITS * 2 if LOADING.search(page.get("text", "")) else NO_FORM_WAITS):
        return decision("WAIT", wait)  # a page still loading looks empty: give it time before handing over
    return decision(
        "BLOCKED",
        None,
        reason="Nothing here leads to an application form the agent recognises (no fields, no Apply or Next): "
        "maybe a CAPTCHA, a login, or an Apply button with an unusual name.",
    )


def stuck(why, actions, invalid=()):
    missing = unanswered(actions)
    parts = [why]
    if invalid:
        parts.append("Marked invalid: " + "; ".join(invalid[:6]) + ".")
    if missing:
        parts.append("Still unanswered: " + "; ".join(missing[:8]) + ".")
    return " ".join(parts)
