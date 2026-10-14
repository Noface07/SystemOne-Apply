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
from pathlib import Path

from . import laya_backend, rules
from .browser import PROMPT_ENTER_SITES
from .profile import MONTHS, Fact, answers, safe_id, undouble

PLACEHOLDER = re.compile(r"^\s*$|^\s*(select|choose|please|pick|--|—|-|none)", re.I)
# Workday names its dropdown button after the question and what it shows: "Phone Device Type Select One Required".
SHOWS_NOTHING = re.compile(r"\b(select one|please select|choose one)\b(\s+required)?\s*$", re.I)
COOKIES = re.compile(
    r"only (necessary|essential|required)|necessary (cookies )?only|reject( all)?\b|decline|"
    r"^\s*(dismiss|got it|ok|close)\s*$",
    re.I,
)
# A job page's way into its form, by its words ("Continue Application", "Apply", "Apply Now").
WAY_IN_TEXT = re.compile(r"^\W*(continue application|apply( now| for this job)?|start (your )?application)\W*$", re.I | re.M)
# A cookie banner's accept button: "Accept Cookies", "Accept all", "Allow all", "I agree".
ACCEPT_COOKIES = re.compile(
    r"^\W*(accept|allow|agree|i agree|i accept|yes)\b.{0,30}$|^\W*ok,? (i )?(agree|accept)\b", re.I
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
# Not a failure: the job is done (the agent records it and moves on).
ALREADY_APPLIED = "The site shows this job as already applied."
APPLIED_AGO = re.compile(
    r"\bapplied \d+ (second|minute|hour|day|week|month)s? ago\b|application status\W+application (submitted|viewed)|"
    # What a job page says, before anything is done, when you applied to it before.
    r"you('ve| have)? already (applied|submitted)|already (applied|submitted an application) (for|to)|"
    r"thank you for (applying|your application|your interest in this (job|position|role))|"
    r"(we('ve| have)|has been) received your application|your application (has been|was) (received|submitted)|"
    r"you('ve| have) applied (for|to) this (job|position|role)|application (already )?(submitted|received) on",
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
# Applying through another site's account ("Apply with LinkedIn", "Apply with Indeed"): never used. It shares that
# profile instead of yours, and needs a sign-in the agent doesn't do.
SOCIAL_APPLY = re.compile(
    r"\b(apply|sign\s*in|log\s*in|continue|auto-?fill|import|connect|easy apply)\b.{0,25}?\b(with|using|via|from|"
    r"through)\s+(linkedin|indeed|seek|xing|glassdoor|facebook)\b|^\W*(linkedin|indeed)\W*$",
    re.I,
)
# Another site's apply widget embedded in a portal (Workday's "applyWithLinkedIn" frame): never opened.
SOCIAL_FRAME = re.compile(r"linkedin|indeed|glassdoor|facebook|seek\.com", re.I)
# The ways into a company portal's application, in your order of preference (Workday, Oracle, SuccessFactors...
# ask first): reuse your last application there, else let the portal read your résumé, else fill it in by hand.
WAYS_IN = (
    re.compile(
        r"\b(use|apply with|start (from|with)|copy|reuse)\s+(my|your)?\s*"
        r"(last|previous|prior|existing|saved|most recent)\s+(application|profile)\b|\blast application\b",
        re.I,
    ),
    re.compile(
        r"auto-?fill\s+(with|from|using)?\s*(my|your|a)?\s*(r[ée]sum[ée]|cv)|(apply|start|upload)\s+(with|using|from)\s+"
        r"(my|your|a)?\s*(r[ée]sum[ée]|cv)\b|\b(r[ée]sum[ée]|cv)\s+(auto-?fill|parsing|upload)\b",
        re.I,
    ),
    re.compile(
        r"\b(apply|fill(\s+(it|this|them))?(\s+(out|in))?|enter(\s+details)?|complete(\s+the\s+form)?|start)\s+manually\b|"
        r"\bmanual\s+(application|apply|entry)\b|\bapply without (a )?(r[ée]sum[ée]|cv)\b|\bstart from scratch\b",
        re.I,
    ),
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
SITE_ERROR_REASON = 'The site shows an error ("Something went wrong"): try again later.'
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
    r"job (is )?not found|(posting|job) (has )?closed|not accepting applications|"
    r"page you are looking for (doesn.t|does not) exist",  # Workday's page for a removed posting (Thomson Reuters)
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
    # Workday's "Websites: Add any relevant websites" (your GitHub, or portfolio)
    (re.compile(r"\bwebsites?\b", re.I), "links", "github"),
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
# How Workday shows what a prompt holds: a chip "LinkedIn, press delete to clear value." Not a choice to click.
CHIP = re.compile(r",?\s*press delete to clear value\.?\s*$", re.I)
# An option already ticked ("LinkedIn checked"): clicking it again only unticks it.
TICKED = re.compile(r"(?<!\bnot)\s+checked\s*$", re.I)
TICKED_SUFFIX = re.compile(r"\s+(not\s+)?checked\s*$", re.I)  # how Workday names a result's tick box state
NO_RESULTS = re.compile(r"^\s*(no items|no results?( found)?|no matches)\.?\s*$", re.I)


def pending_above(page, actions, history, url):
    """Required questions the page says are still empty somewhere (off screen too), not worked on and not shown
    as chosen (a Workday prompt with a chip reads empty to the page's own check)."""
    chipped = {norm(a.get("context") or "")[:30] for a in actions if a.get("role") == "option" and CHIP.search(a["label"])}
    return [
        q for q in page.get("required_above") or []
        if norm(q)[:30] not in chipped and not touched(history, url, label=q) and not touched(history, url, context=q)
    ]  # fmt: skip


def downs_since_advance(history, url):
    """Scrolls down since the last Next/Save on this page."""
    count = 0
    for h in reversed(history):
        if h.get("kind") == "click" and ADVANCE.search(str(h.get("action") or "")):
            break
        if h.get("kind") == "scroll" and h.get("url") == url and str(h.get("action") or "").startswith("Scroll down"):
            count += 1
    return count


def ups_since_advance(history, url):
    """Scrolls up since the last Next/Save on this page."""
    count = 0
    for h in reversed(history):
        if h.get("kind") == "click" and ADVANCE.search(str(h.get("action") or "")):
            break
        if h.get("kind") == "scroll" and h.get("url") == url and str(h.get("action") or "").startswith("Scroll up"):
            count += 1
    return count


GENERIC_WORDS = r"(programming language|language|software|framework|protocol|platform|technology|development)"
# Only a generic tail: "Python (Programming Language)", "Kotlin Programming Language". A bracketed name is something
# else entirely: "Citizen (Czechia)" is not "Citizen" (India).
GENERIC_TAIL = re.compile(rf"\s*(\(\s*{GENERIC_WORDS}\s*\)|{GENERIC_WORDS})?\s*", re.I)


def skill_names(typed):
    """Your skill as written, and what it is also called: "Retrieval-Augmented Generation (RAG)" is
    "Retrieval-Augmented Generation" and "RAG" too."""
    head = re.sub(r"\s*\([^)]*\)\s*", " ", typed).strip()
    names = [typed, head, *re.findall(r"\(([^)]+)\)", typed)]
    return [n for n in dict.fromkeys(n.strip() for n in names) if n]


def same_skill(typed, offered):
    """Is the site's skill name the same skill as yours (or as another name of it, skill_names)?"""
    return any(norm(name) == norm(offered) or one_skill(name, offered) for name in skill_names(typed))


def one_skill(typed, offered):
    """Its own words plus a generic tail ("Programming Language", a bracketed note), or your words as its
    bracketed abbreviation ("... (OPC UA)")."""
    if norm(typed) in {norm(p) for p in re.findall(r"\(([^)]+)\)", offered)}:
        return True
    if not offered.lower().startswith(typed.lower()):
        return False
    tail = offered[len(typed):]
    if GENERIC_TAIL.fullmatch(tail):
        return True
    # The abbreviation spelled out: "SCADA (Supervisory Control and Data Acquisition)", its initials are "SCADA".
    spelled = re.fullmatch(r"\s*\(([^)]+)\)\s*", tail)
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", spelled.group(1))) if spelled else ""
    return bool(initials) and initials.lower() == re.sub(r"[^a-z]", "", typed.lower())


def search_shorter(actions, label, answer, history):
    """A search for your whole answer found nothing that fits ("Computer Science and Engineering"): its first word
    ("Computer"), once, whose results are then judged against the whole answer."""
    words = [w for w in re.findall(r"[A-Za-z0-9+#.]+", answer or "") if w.lower() not in STOP_WORDS]
    field = next((a for a in actions if a["kind"] == "fill" and a["label"] == label), None)
    if len(words) < 2 or not field or field.get("multiple"):
        return None
    short = words[0]
    if any(h.get("kind") == "fill" and h.get("action") == label and h.get("typed") == short for h in history):
        return None  # searched shorter already
    return decision("TYPE_TEXT", {**field, "retype": short})


def stems(text):
    """Meaningful words, plurals folded: "Information Sciences" -> {"information", "science"}."""
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in tokens(text) if len(w) > 2}


def opened_times(history, url, a):
    """How often this "Select One" (this label, under this question) was opened on this page."""
    context = (a.get("context") or "")[:160]
    return sum(h.get("kind") == "click" and h.get("url") == url and h.get("action") == a["label"]
               and (h.get("context") or "")[:160] == context for h in history)  # fmt: skip


def run_together(label, facts):
    """Is this chip ("Remove: OPC UA, OPC DA, Modbus, ...") three or more of your profile's list items as one?"""
    parts = [norm(p) for p in re.split(r",\s*", re.sub(r"^Remove:\s*", "", label)) if norm(p)]
    # The page state shortens long values (200 characters), so a long list's tail may be missing from it: three
    # of yours, and a quarter of the chip at least, is enough to call it your list run together.
    mine = sum(p in facts.values for p in parts)
    if len(parts) >= 3 and mine >= 3 and mine * 4 >= len(parts):
        return True
    # Typed one after another into a box that kept its text ("OPC UAOPC DAMoIMQTTBACnetDNP3..."): no commas, but
    # three or more of your items glued in one chip ("Citizen (India)" holds two of your answers and is one option).
    glued = norm(re.sub(r"^Remove:\s*", "", label))
    return len(parts) == 1 and sum(1 for v in facts.values if len(v) >= 4 and v in glued) >= 3


def chosen_items(actions, field):
    """What a prompt field already holds, from Workday's chips under its question: ["LinkedIn"]."""
    q = norm(field.get("label") or "")[:30]
    return [CHIP.sub("", a["label"]).strip() for a in actions
            if a.get("role") == "option" and CHIP.search(a["label"]) and q
            and (norm(a.get("context") or "").startswith(q) or q.startswith(norm(a.get("context") or "x")[:30]))]  # fmt: skip


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
# A degree's nearest equivalents, most exact first, for lists that name only some ("BS", "MS" and no B.Tech): an
# engineering or computing bachelor's is a bachelor of science, never an arts one.
DEGREE_EQUIVALENTS = {
    **dict.fromkeys(("btech", "bacheloroftechnology"),
                    ("btech", "be", "bachelorofengineering", "bsc", "bs", "bachelorofscience")),
    **dict.fromkeys(("be", "bachelorofengineering"),
                    ("be", "btech", "bacheloroftechnology", "bsc", "bs", "bachelorofscience")),
    "bca": ("bca", "bsc", "bs", "bachelorofscience"),
    "bsc": ("bsc", "bs", "bachelorofscience"),
    **dict.fromkeys(("mtech", "masteroftechnology"),
                    ("mtech", "me", "masterofengineering", "msc", "ms", "masterofscience")),
    "me": ("me", "mtech", "msc", "ms", "masterofscience"),
    "mca": ("mca", "msc", "ms", "masterofscience"),
    "msc": ("msc", "ms", "masterofscience"),
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
    if action["kind"] == "select":
        return action["label"].split(" → ", 1)[-1]
    return TICKED_SUFFIX.sub("", action["label"])  # Workday's "India not checked" is the option "India"


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
    # "B.Tech" in a list of levels (Workday: "Bachelors", "Masters", "Bachelor of Business & Commerce"): the one
    # option that is nothing but the level's name. A bachelor's in another field is not yours.
    level = set().union(*(LEVELS.get(w, set()) for w in wanted))
    if level:
        generic = [o for o in options
                   if tokens(option_text(o)) and tokens(option_text(o)) - {"degree", "s"} <= level]  # fmt: skip
        if len(generic) == 1:
            return generic
    # A list of abbreviations without yours (Maersk: "BA", "BS", "MS", "MBA", no B.Tech): its nearest equivalent.
    for w in wanted:
        for alias in DEGREE_EQUIVALENTS.get(w, ()):
            same = [o for o in options if norm(option_text(o)) == alias]
            if same:
                return same[:1]
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
    # A paragraph of a question (HPE's conflict-of-interest and government-officials notes) overflowed the model's
    # 2048-token window: its opening and its closing ask are kept
    if len(question) > 600:
        question = f"{question[:300]} … {question[-300:]}"
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

    try:
        answer = model.systemone(body)[0]["answers"]["option"]
    except RuntimeError as error:
        if "HTTP 4" not in str(error):
            raise  # the model is down: the run stops, as it does when Clef can't load
        return None  # it refused this one request (too long, malformed): no pick, never a guess
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
        or sign_in_to_apply(page)
    )


def sign_in_to_apply(page):
    """An application's own address (".../apply/useMyLastApplication") showing only a Sign In button, no form:
    the portal wants an account before it shows the form (Blue Yonder's Workday)."""
    if not re.search(r"/apply(/|$|\?)", page.get("url") or "", re.I) or application_fields(page):
        return False
    if re.search(r"\bcurrent step \d+ of \d+", page.get("text") or "", re.I):
        return False  # inside the form's steps (one still loading): its own sign-in step is SIGN_IN_STEP's
    # Signed in already (Workday's header shows your e-mail as its account button), or the form still drawing
    # behind a passing "Sign In": not a wall (Deluxe signed in again and again while its form said "Loading")
    if any(re.fullmatch(r"[^@\s]+@[^@\s]+\.\w+", a.get("label") or "") for a in page.get("actions") or []):
        return False
    if LOADING.search(page.get("text") or ""):
        return False

    return any(a.get("kind") == "click" and a.get("role") in {"button", "link"}
               and re.fullmatch(r"\W*(sign ?in|log ?in)\W*", a.get("label") or "", re.I)
               for a in page.get("actions") or [])  # fmt: skip


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
    if name == "links":
        return 1 if candidate.get("links.github") or candidate.get("links.portfolio") else 0  # one website: yours
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
            if name == "links":
                # One website box, for your GitHub: Add pressed once, unless a box shows in that section already.
                box = any(x["kind"] == "fill" and pattern.search(x.get("section") or "") for x in actions)
                return bool(wanted) and not box and not touched(history, url, label=a["label"], context=a.get("context"))
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
            # Blocks the page shows already ("Work Experience 1", "Work Experience 2": a draft the portal kept from
            # an earlier visit): never more blocks than your profile has entries.
            family = rules.BLOCKS["work experience" if name == "experience" else "education"]
            shown_blocks = max(
                (int(m.group(1)) for x in actions
                 for m in [re.search(family + rules.NUMBERED, x.get("section") or "", re.I)] if m),
                default=0,
            )  # fmt: skip
            if shown_blocks >= wanted:
                return False
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
        and same_block(h, a)
    )


def same_block(h, a):
    """A history step and a field in the same block of a repeating section: "Month" of Work Experience 1 and
    "Month" of Work Experience 2 are two fields."""
    return not (h.get("section") and a.get("section")) or h.get("section") == a.get("section")


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


def site_pick(actions, history, url, facts=None, page=None):
    """A single-choice search box the site filled by itself after a search (HPE: "Computer Science and Engineering"
    + Enter, and Workday chose its one result "Electrical Engineering and Computer Science"): the chip's "Remove: X"
    button, when X is not what was typed, nobody clicked X, and the name can be searched shorter. Once per box; its
    context names the box, so the shorter search knows where it goes."""
    for chip in actions:
        if chip.get("role") != "option" or not CHIP.search(chip["label"]):
            continue
        name = CHIP.sub("", chip["label"]).strip()
        if chip.get("field_id") and sum(bool(o.get("role") == "option" and CHIP.search(o["label"])
                                             and o.get("field_id") == chip["field_id"]) for o in actions) > 1:  # fmt: skip
            continue  # several picks in one field: a list (skills), each its own item, never a wrong single pick
        remove = next((a for a in actions if a["kind"] == "click" and norm(a["label"]) == norm(f"Remove: {name}")),
                      None)  # fmt: skip
        typed = next((h for h in reversed(history) if h.get("url") == url and h.get("kind") == "fill"
                      and (h.get("section") or "") == (chip.get("section") or "") and h.get("typed")), None)  # fmt: skip
        if not typed and facts is not None and chip.get("field_id"):
            # A draft's pick from an earlier run (HPE's Field of Study): your answer for its field, by the site's
            # field name
            words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", chip["field_id"]).lower()  # "fieldOfStudy": field of study
            choice = rules.match({**chip, "kind": "fill", "label": words, "context": ""}, facts, False, (),
                                 (page or {}).get("text", ""))  # fmt: skip
            fact = facts.by_id.get(choice) if choice else None
            parts = {norm(x) for x in re.split(r"[,;]", str(fact.value if fact else ""))}
            if fact and (any(k.startswith(f"{choice}__item_") for k in facts.by_id) or norm(name) in parts):
                fact = None  # a list of yours (skills): one chip is one item of it (HPE lost ChromaDB to this)
            typed = {"action": f"field:{chip['field_id']}", "typed": str(fact.value)} if fact else None
        if not remove or not typed:
            continue
        label = typed["action"]
        if any(a["kind"] == "fill" and a["label"] == label for a in actions):
            continue  # its box still shows: a list taking several items (skills), not a single choice
        if norm(name) == norm(typed["typed"]) or not shorter_name(typed["typed"]):
            continue
        if set(re.findall(r"\w+", name.lower())) <= set(re.findall(r"\w+", str(typed["typed"]).lower())):
            continue  # your answer, shorter ("Manipal University" for "Manipal University Jaipur", Cisco)
        if any(h.get("kind") == "click" and norm(option_text({"kind": "click", "label": h.get("action") or ""}))
               == norm(name) for h in history):  # fmt: skip
            continue  # your pick (a closest match the run chose), not the site's
        if any(h.get("kind") == "click" and str(h.get("action") or "").startswith("Remove: ")
               and h.get("context") == label for h in history):  # fmt: skip
            continue  # removed once already: the site's pick stands, and the review shows it
        return {**remove, "context": label, "answer": typed["typed"]}
    return None


def removed_pick(history, url, field):
    """What was typed into `field` before the site's own pick was removed from it (site_pick), or None."""
    names = {field["label"], f"field:{field.get('field_id')}"}
    removed = next((h for h in history if h.get("kind") == "click" and str(h.get("action") or "").startswith("Remove: ")
                    and h.get("context") in names and h.get("url") == url), None)  # fmt: skip
    if not removed:
        return None
    typed = next((h for h in history if h.get("kind") == "fill" and h.get("action") == field["label"]
                  and h.get("typed")), None)  # fmt: skip
    return typed["typed"] if typed else removed.get("answer")


def duplicate_upload(page, actions, history, candidate):
    """Your résumé twice in the site's file list (an earlier run's upload kept in the draft, then another: Harman,
    NCR): the "Delete <file>" of one extra copy, when it is on screen. One click per extra copy at most, so a Delete
    the site ignores is not pressed forever. Only a file of yours, only when another copy stays."""
    cards = page.get("file_cards") or []
    mine = {norm(Path(str(v)).name) for k, v in candidate.items() if k.startswith("document.") and v}
    url = page.get("url")
    for name in dict.fromkeys(cards):
        copies = sum(c == name for c in cards)
        if copies < 2 or norm(name) not in mine:
            continue
        label = next((a["label"] for a in actions if a["kind"] == "click" and norm(a["label"]) == norm(f"Delete {name}")),
                     None)  # fmt: skip
        pressed = sum(h.get("url") == url and h.get("kind") == "click" and h.get("action") == label for h in history)
        if label and pressed < copies - 1:
            return next(a for a in reversed(actions) if a["kind"] == "click" and a["label"] == label)
        where = page.get("file_card_where") or []
        if not label and not pressed and len(where) == len(cards):
            # The copies are off screen (the résumé list sits at the foot of Workday's My Experience): towards them
            last = where[max(i for i, c in enumerate(cards) if c == name)]
            scroll = next((a for a in actions if a["id"] == {"below": "scroll_down", "above": "scroll_up"}.get(last)),
                          None)  # fmt: skip
            if scroll:
                return scroll
    return None


def way_to_start(actions, history, url):
    """On a portal asking how to start: your last application there, else autofill from your résumé, else
    manually; never through LinkedIn or another site's account. A way already tried on this page is passed over."""
    ways = [a for a in actions if a["kind"] == "click" and a.get("role") in {"button", "link"}
            and not SOCIAL_APPLY.search(a["label"])]  # fmt: skip
    everything = history
    # A sign-in starts afresh: a way tried before it (it led to the sign-in) may be taken again (NCR: every way was
    # "tried" once signed in, so nothing was picked, the dialog was closed and Apply pressed three times)
    signed = [i for i, h in enumerate(history) if h.get("source") == "portal account" or h.get("kind") == "handover"
              or str(h.get("action") or "").startswith("Back to the job")]  # fmt: skip
    # Once this run went to "Apply Manually" (the last application was empty: a new account has none), that is
    # the way in from then on (Deluxe: "Use My Last Application" again after each sign-in, sign-in, home page, loop)
    manual = any(str(h.get("action") or "").startswith("Apply manually") for h in everything)
    history = history[signed[-1] + 1:] if signed else history
    for way in (WAYS_IN[2], WAYS_IN[1], WAYS_IN[0]) if manual else WAYS_IN:
        pick = next((a for a in ways if way.search(a["label"]) and not touched(history, url, label=a["label"])), None)
        if pick:
            return pick
    return None


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
            if CURRENT_JOB.search(a["label"]):
                continue  # "I currently work here" is unticked for a past job (its "*" belongs to the dates beside it)
            group = [o for o in actions if o.get("role") == "checkbox" and o.get("context") == a.get("context")]
            if len(group) < 2:
                missing.append(a["label"])
            elif a.get("context") and ("c", a["context"]) not in seen and not any(is_on(o) for o in group):
                seen.add(("c", a["context"]))
                missing.append(question_of(a["context"], group))
    return [m[:60] for m in missing]


FORM_STEP = re.compile(r"\bcurrent step (\d+) of \d+", re.I)


def form_step(page):
    """Which step of the form a page is: the step its progress bar marks as current (the page reader's `step`,
    "My Information"), else "current step 4 of 6" in its text ("4"); None where the page doesn't say."""
    if isinstance(page, dict):
        if page.get("step"):
            return page["step"]
        page = page.get("text")
    found = FORM_STEP.search(page or "")
    return found.group(1) if found else None


def stale_errors(actions, history, url, page=""):
    """Every field the site marks wrong has been answered since the last Next/Save (Workday keeps a "required"
    message from a Save pressed early until the next one): pressing it again re-checks them. Once only: errors
    still there after that Save are real. Errors on a step the last Save didn't stay on are from an earlier visit
    (NCR: its draft reopened Application Questions with an earlier run's "Errors Found"): fill first."""
    saves = [i for i, h in enumerate(history) if h.get("kind") == "click" and h.get("url") == url
             and ADVANCE.search(str(h.get("action") or ""))]  # fmt: skip
    now = form_step(page)  # the page (its progress bar) or its text
    if now and saves and history[saves[-1]].get("form_step") not in (None, now):
        return True  # the last Save was on another step: these errors were here when this step opened
    if not saves:
        # No Save pressed here by this run: a site checks only when Save is pressed, so these are from an earlier
        # visit kept in the portal's draft (NCR opened My Information with "State is required"): fill first
        return any(h.get("url") == url for h in history) or bool(history)
    since = history[saves[-1] + 1:]
    if not any(h.get("kind") in {"fill", "select", "setdate", "click", "upload"} for h in since):
        return False  # nothing answered since that Save: its errors stand
    answered = [norm(str(h.get("action") or ""))[:24] for h in since if h.get("kind") in {"fill", "select", "setdate"}]
    wrong = [a for a in actions if a.get("invalid") and a["kind"] in {"fill", "setdate", "select", "upload"}]
    if not wrong:
        # Only the banner ("Errors Found") with no field marked any more, and something answered since that Save
        # (MKS: the résumé uploaded after a Save pressed too early): one more Save re-checks.
        return any(h.get("kind") in {"fill", "select", "setdate", "upload"} for h in since)

    def settled(a):
        has = a.get("value") or SELECTED.search(a.get("context") or "")
        return bool(has) and any(norm(a["label"]).startswith(x) or x.startswith(norm(a["label"])[:24]) for x in answered)

    return all(settled(a) for a in wrong)


def invalid_fields(actions):
    """Fields the site marks invalid, with its message: 'Email: Enter a valid email'."""
    found, seen = [], set()
    for a in actions:
        if a.get("invalid") and a.get("node") not in seen and a["kind"] in {"fill", "setdate", "select", "upload"}:
            seen.add(a.get("node"))
            label = a["label"].split(" → ", 1)[0][:50]
            found.append(f"{label}: {a['error']}" if a.get("error") else label)
    return found


# Lists drawn as categories (Workday's "How Did You Hear About Us?": "Job Board >", "Social Media >"): when your
# answer isn't shown, the category it usually sits in, most likely first.
CATEGORY_HINTS = (
    (re.compile(r"linkedin|glassdoor", re.I),  # Workday files them under Job Board
     [re.compile(r"job ?boards?|job ?portal|job ?site", re.I), re.compile(r"social", re.I),
      re.compile(r"online|internet|website|\bweb\b", re.I)]),
    (re.compile(r"facebook|twitter|instagram|youtube", re.I),
     [re.compile(r"social", re.I), re.compile(r"online|internet|website|\bweb\b", re.I)]),
    (re.compile(r"naukri|indeed|monster|foundit|shine|hirist|instahyre|wellfound|job ?board|job ?portal", re.I),
     [re.compile(r"job ?board|job ?portal|job ?site", re.I), re.compile(r"online|internet|website|\bweb\b", re.I)]),
    (re.compile(r"referr|friend|employee", re.I), [re.compile(r"referr", re.I)]),
    (re.compile(r"career (fair|site|page)|company (site|website)", re.I),
     [re.compile(r"career|company (site|website)", re.I)]),
)  # fmt: skip
INSTITUTION = re.compile(r"school|college|universit|institut|alma mater", re.I)
# The way out of a school list that doesn't have yours.
OTHER_OPTION = re.compile(
    r"^\W*(others?|other\s*[-–:(/].*|not (listed|found|in (the )?list)|none of (the )?above|"
    r"my (school|university|college|institution) is not listed)\W*$",
    re.I,
)
NAME_STOP = {"of", "the", "and", "at", "for", "in"}


def name_words(text):
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if w not in NAME_STOP}


def category_for(question, options, facts, history):
    """A list drawn as categories: the one your answer sits in, when your answer itself isn't among the options."""
    ruled = rules.match({"label": question, "context": question}, facts)
    fact = facts.by_id.get(ruled) if ruled and ruled not in {"ASK_USER", "SKIP_FIELD", "DRAFT_ANSWER"} else None
    leaf = lambda o: norm(re.split(r"[>›»]", option_text(o))[-1])  # noqa: E731  "Job Boards > LinkedIn" is LinkedIn
    if not fact or any(norm(fact.value) in {norm(option_text(o)), leaf(o)} for o in options):
        return None
    clicked = {h.get("action") for h in history[-15:] if h.get("kind") == "click"}
    groups = [o for o in options if not re.search(r"[>›»]", option_text(o))]  # a result's path isn't a category
    for answer, categories in CATEGORY_HINTS:
        if answer.search(fact.value):
            for category in categories:
                pick = next((o for o in groups if category.search(option_text(o)) and o["label"] not in clicked), None)
                if pick:
                    return decision("CLICK", pick, 0.9)
    return None


def school_fallback(typed, options):
    """Your school isn't offered as typed: a suggestion that is part of its name ("Manipal University" for
    "Manipal University Jaipur"), the longest such; else the list's own way out ("Other", "Not listed")."""
    words = name_words(typed)
    within = [o for o in options if len(name_words(option_text(o))) >= 2 and name_words(option_text(o)) <= words]
    if within:
        return decision("CLICK", max(within, key=lambda o: len(name_words(option_text(o)))), 0.8)
    other = next((o for o in options if OTHER_OPTION.search(option_text(o))), None)
    return decision("CLICK", other, 0.8) if other else None


def shorter_name(value):
    """A school's name cut to what its search usually knows: before a comma or dash, else its first two words."""
    head = re.split(r"\s*[,(]\s*|\s+[-–]\s+", value or "")[0].strip()
    if head and head != value.strip():
        return head
    words = (value or "").split()
    return " ".join(words[:2]) if len(words) > 2 else None


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
    typed_text, name = "", ""
    if last and last.get("kind") == "fill":
        typed_text = last.get("typed") or ""
    elif last and last.get("kind") == "enter":
        # Enter searched for what was typed just before it (Workday's skills box, its Field of Study).
        name = re.sub(r"^Press Enter in '(.*)'$", r"\1", last.get("action") or "")
        typed_text = next((h.get("typed") or "" for h in reversed(history)
                           if h.get("kind") == "fill" and h.get("action") == name), "")  # fmt: skip
    typed = norm(typed_text)
    options = [o for o in options if not NO_RESULTS.match(o["label"])]  # "No Items." is not a result
    if not options:
        # Nothing found: a skill stays unmatched (the next one is typed); one answer is searched again shorter.
        searched = name if last and last.get("kind") == "enter" else (last or {}).get("action")
        single = any(a["kind"] == "fill" and a["label"] == searched and not a.get("multiple") for a in actions)
        return search_shorter(actions, searched, typed_text, history) if single and typed_text else None
    if typed:
        # Suggestions for what was just typed: the one that is that text, else the one that starts with it.
        # Never another suggestion: a skill or city the site doesn't offer stays unmatched and is reported.
        shown = {id(o): norm(TICKED_SUFFIX.sub("", o["label"])) for o in options}  # "Python IDLE not checked"
        exact = [o for o in options if typed in {shown[id(o)], norm(re.split(r"[>›»]", o["label"])[-1])}]
        if exact:
            return decision("CLICK", exact[0])
        typed_field = name if last.get("kind") == "enter" else last.get("action")
        tag_box = any(a["kind"] == "fill" and a["label"] == typed_field and a.get("multiple") for a in actions)
        if tag_box:
            # A skill: the same skill under the site's name ("Python (Programming Language)", "Kotlin Programming
            # Language", "OPC Unified Architecture (OPC UA)"), never a neighbour ("Siemens S7-200" for "Siemens S7",
            # "Siemens WinCC SCADA" for "SCADA"). None of that: this skill stays unmatched, and the next one is typed.
            same = [o for o in options if same_skill(typed_text, TICKED_SUFFIX.sub("", o["label"]))]
            # Several fit ("Citizen (India)", "Permanent Resident (India)" for "India"): the one also naming another
            # answer of yours (your citizenship status), then the shortest name.
            mine = lambda o: sum(v in shown[id(o)] for v in facts.values if len(v) >= 4 and v != typed)  # noqa: E731
            return decision("CLICK", min(same, key=lambda o: (-mine(o), len(shown[id(o)]))), 0.9) if same else None
        # Searched again shorter ("Computer" for "Computer Science and Engineering"): the results are judged against
        # your whole answer, not the short search.
        original = max((h.get("typed") or "" for h in history if h.get("kind") == "fill"
                        and h.get("action") == typed_field), key=len, default="")  # fmt: skip
        shorter = len(norm(original)) > len(typed)
        starts = [] if shorter else [o for o in options if shown[id(o)].startswith(typed)]
        if len(starts) == 1:
            return decision("CLICK", starts[0], 0.9)
        if starts:
            # "Udaipur, Rajasthan, India" over "Udaipur Airport": the one that also names your state or country.
            # Equally good ones: the site's own first suggestion.
            # A suggestion whose name is exactly what was typed ("Udaipur, Rajasthan, India" for "Udaipur") counts
            # double over one that only starts with it ("Udaipurwati, Rajasthan, India").
            scores = [
                sum(v in norm(o["label"]) for v in facts.values if len(v) >= 4)
                + 2 * (norm(re.split(r"[,(|>›-]", o["label"])[0]) == typed)
                for o in starts
            ]
            best = max(scores)
            return decision("CLICK", starts[scores.index(best)], 0.85 if scores.count(best) == 1 else 0.65)
        field = f"{last.get('action') or ''} {last.get('context') or ''}"
        if INSTITUTION.search(field):
            return school_fallback(typed_text, options)  # your college under a shorter name, or "Other"
        # The site's name for it carries what was typed as its abbreviation ("OPC Unified Architecture (OPC UA)" for
        # "OPC UA"). Merely containing the word is not enough: "SCADA" is not "Siemens WinCC SCADA".
        inside = [o for o in options if typed in {norm(p) for p in re.findall(r"\(([^)]+)\)", o["label"])}]
        if inside:
            pick = min(inside, key=lambda o: len(shown[id(o)]))
            return decision("CLICK", pick, 0.9 if len(inside) == 1 else 0.85)
        # Its own words differ ("Computer and Information Sciences" for "Computer Science and Engineering"): the
        # result sharing most of the words, when it shares at least two and half of them, and is the clear best.
        words = stems(original if shorter else typed_text)
        if len(words) >= 2:
            scored = []
            for o in options:
                theirs = stems(TICKED_SUFFIX.sub("", o["label"]))
                shared = len(words & theirs)
                scored.append((shared, shared / len(words | theirs) if theirs else 0, o))
            shared, overlap, best = max(scored, key=lambda s: (s[0], s[1]))
            rivals = [s for s in scored if (s[0], s[1]) == (shared, overlap)]
            if shared >= 2 and shared * 2 >= len(words):
                return decision("CLICK", best, 0.85 if len(rivals) == 1 else 0.6)
        # A search that shows categories, not results (Workday): the category your answer sits in.
        category = category_for(last.get("action") or options[0].get("context") or "", options, facts, history)
        return category or search_shorter(actions, typed_field, original or typed_text, history)
    question = options[0].get("context") or ""
    if not question and last and last.get("kind") == "click":
        # A dropdown's list is usually drawn apart from it: the question is the one of the control just opened.
        # Workday's button says it itself ("Phone Device Type Select One Required"), sharper than its section.
        label = last.get("action") or ""
        named = SHOWS_NOTHING.sub("", label).strip() if SHOWS_NOTHING.search(label) else ""
        question = named or last.get("context") or re.sub(r"^Open ", "", label)
    if not question:
        return None
    return category_for(question, options, facts, history) or answer_group(question, options, candidate, facts, "CLICK")


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
        return decision("BLOCKED", None, reason=ALREADY_APPLIED)
    if not has_fields and SITE_ERROR.search(page.get("text", "")) and history:
        # Often a passing hiccup (Workday right after signing in): a few moments first, then the agent reloads.
        if wait and waiting(history) < 5:
            return decision("WAIT", wait)
        return decision("BLOCKED", None, reason=SITE_ERROR_REASON)
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
    # A portal asking how to start (Workday's "Autofill with Resume / Apply Manually / Use My Last Application"
    # dialog) comes before any embedded frame: that dialog also holds Workday's "applyWithLinkedIn" frame.
    way_in = None if has_fields else way_to_start(actions, history, url)
    if way_in:
        return decision("CLICK", way_in)
    frame = max(
        (a for a in actions if a["kind"] == "frame" and not SOCIAL_FRAME.search(f"{a['label']} {a.get('value', '')}")),
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
    def banner(a, words):
        return (
            a["kind"] == "click"
            and a.get("role") in {"button", "link"}
            and words.search(a["label"])
            and not touched(history, url, label=a["label"])  # tried once: a banner that stays isn't clicked forever
            and re.search(
                r"cookie|tracking|consent manager|privacy preference", f"{a['label']} {a.get('context', '')}", re.I
            )
        )  # "Decline" in a cookie banner, not "I decline to self-identify"

    # Cookies accepted, as you asked (12 Oct 2026); a banner offering no Accept is declined, to get it out of the way.
    cookie = first(lambda a: banner(a, ACCEPT_COOKIES)) or first(lambda a: banner(a, COOKIES))
    if cookie:
        return decision("CLICK", cookie)

    # A chat questionnaire (Naukri): after answering the latest question, send the answer with its Save.
    answered = next((h for h in reversed(history) if h.get("kind") not in {"wait", "scroll"}), None)
    if answered and answered.get("chat") and answered.get("kind") in {"fill", "click", "select"}:
        send = first(lambda a: a.get("chat") and a["kind"] == "click" and CHAT_SEND.search(a["label"]))
        if send and not CHAT_SEND.search(answered.get("action", "")):
            return decision("CLICK", send)

    # Workday's prompt ("How Did You Hear About Us?") keeps showing its categories while you type; Enter searches,
    # and a single match is then chosen by Workday itself. So right after typing there: Enter, before any list.
    just = next((h for h in reversed(history) if h.get("kind") not in {"wait", "scroll"}), None)
    if just and just.get("kind") == "enter" and just.get("url") == url and PROMPT_ENTER_SITES.search(url or ""):
        # Enter pressed there: Workday searches, then shows the pick as a chip. Give it a moment, or the agent
        # retypes and wanders into the categories while the answer is on its way.
        name = re.sub(r"^Press Enter in '(.*)'$", r"\1", just.get("action") or "")
        field = next((a for a in actions if a["kind"] == "fill" and a["label"] == name), None)
        # Results of this search (options naming what was typed: "OPC UA" for "OPC UA"), not a category list
        # that stays open from before (Job Board, Social Media): those are what to pick from now.
        searched = next((norm(h.get("typed") or "") for h in reversed(history) if h.get("kind") == "fill"
                         and h.get("action") == name), "")  # fmt: skip
        results = bool(searched) and any(
            a.get("role") == "option" and not CHIP.search(a["label"]) and searched in norm(a["label"]) for a in actions
        )
        if field and not chosen_items(actions, field) and not results and wait and waiting(history) < 4:
            return decision("WAIT", wait)
    if just and just.get("kind") == "fill" and just.get("url") == url and PROMPT_ENTER_SITES.search(url or ""):
        # Just typed there (each skill of a "Type to Add Skills" box too): Enter searches for that one.
        typed = next((a for a in actions if a["kind"] == "fill" and a["label"] == just.get("action")), None)
        # Workday's page has no <form> at all: in a form or not, its prompt searches on Enter.
        if typed and (typed.get("value") or just.get("typed")) and is_prompt(typed):
            return decision("PRESS_ENTER", {
                "id": f"enter_{typed['node']}", "kind": "enter", "node": typed["node"], "role": typed.get("role"),
                "label": f"Press Enter in '{typed['label']}'", "context": typed.get("context") or "",
                "section": typed.get("section") or "", "in_form_ok": True,
            })  # fmt: skip

    # A chip holding several of your list's items run together ("OPC UA, OPC DA, Modbus, ..."): a whole list typed
    # as one entry (an earlier run's mistake, kept in the portal's draft). Removed, so the items go in one by one.
    blob = first(lambda a: a.get("remove_chip") and run_together(a["label"], facts)
                 and not touched(history, url, label=a["label"]))  # fmt: skip
    if blob:
        return decision("CLICK", blob)

    # Workday's chips ("LinkedIn, press delete to clear value.") show a pick; they are not a list to choose from.
    options = [a for a in actions if a["kind"] == "click" and a.get("role") == "option" and not CHIP.search(a["label"])]
    if options:
        picked = answer_options(options, actions, candidate, facts, history)
        if picked and picked.get("action") and TICKED.search(picked["action"].get("label") or ""):
            picked = None  # already ticked: a click would untick it
        if picked:
            return picked
        last = opener(history)
        escape = first(lambda a: a.get("id") == "escape")
        if escape and last and last.get("kind") == "click" and str(last.get("action", "")).startswith("Open "):
            return decision("KEY", escape)  # opened a list with no answer for you in it: close it, move on

    last = next((h for h in reversed(history) if h.get("kind") not in {"wait", "scroll"}), None)
    if not options and last and last.get("kind") == "enter" and wait and waiting(history) < 4:
        return decision("WAIT", wait)  # results of a search are loading
    opened = last and last.get("kind") == "click" and (
        str(last.get("action", "")).startswith("Open ") or SHOWS_NOTHING.search(str(last.get("action") or ""))
    )  # also a Workday "Select One" whose list draws late
    if not options and opened and wait and waiting(history) < 4:
        # A list just opened and nothing shows yet (some portals fetch it): wait. Clicking anything else now
        # would close it again.
        return decision("WAIT", wait)
    if not options and last and last.get("kind") in {"fill", "enter"}:
        # The field just typed into (or searched with Enter, "Press Enter in '<field>'").
        name = re.sub(r"^Press Enter in '(.*)'$", r"\1", last.get("action") or "")
        typed = next((a for a in actions if a["kind"] == "fill" and a["label"] == name), None)
        looks_up = typed and typed.get("value") and (is_prompt(typed) or LOOKUP.search(typed["label"]))
        if looks_up and wait and waiting(history) < (6 if LOADING.search(page.get("text", "")) else 2):
            return decision("WAIT", wait)  # suggestions fetched over the network can take a moment
        where = f"{typed['label']} {typed.get('context') or ''}" if typed else ""
        if typed and typed.get("value") and INSTITUTION.search(where):
            # Your college's full name found nothing: its search may know it shorter ("Manipal University").
            short = shorter_name(last.get("typed") or typed["value"])
            if short and not any(h.get("kind") == "fill" and h.get("typed") == short for h in history):
                return decision("TYPE_TEXT", {**typed, "retype": short})
        pressed = any(h.get("kind") == "enter" and h.get("url") == url and typed and
                      h.get("action") == f"Press Enter in '{typed['label']}'" for h in history)  # fmt: skip
        if (
            typed
            and typed.get("value")
            and is_prompt(typed)
            and typed.get("in_form")
            and not pressed
            and (PROMPT_ENTER_SITES.search(url or ""))
        ):
            # A portal whose search box shows its values only after Enter, and where Enter there only searches.
            return decision("PRESS_ENTER", {
                "id": f"enter_{typed['node']}", "kind": "enter", "node": typed["node"], "role": typed.get("role"),
                "label": f"Press Enter in '{typed['label']}'", "context": typed.get("context") or "",
                "section": typed.get("section") or "", "in_form_ok": True,
            })  # fmt: skip
        if typed and typed.get("value") and is_prompt(typed) and typed.get("in_form"):
            # Its search wants Enter, which inside a form could send it: open its list and choose there.
            label = f"Open {typed['label']}"
            opens = next((o for o in actions if o.get("node") == typed["node"] and o["label"] == label), None)
            if not touched(history, url, label=label):
                return decision("CLICK", opens or {
                    "id": f"open_{typed['node']}", "kind": "click", "node": typed["node"],
                    "role": typed.get("role") or "combobox", "label": label, "context": typed.get("context") or "",
                    "section": typed.get("section") or "",
                })  # fmt: skip
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

    wrong = site_pick(actions, history, url, facts, page)
    if wrong:
        return decision("CLICK", wrong)
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
        if kind == "fill" and a.get("multiple") and chosen_items(actions, a):
            # Workday's prompt shows its picks as chips: done once everything your profile gives is there.
            shown = [norm(x) for x in chosen_items(actions, a)]
            choice = rules.match(a, facts, False, (), page.get("text", ""))
            fact = facts.by_id.get(choice) if choice else None
            # One answer (How Did You Hear: LinkedIn) chipped: done. A list added item by item (skills) is the
            # agent's to finish: it knows which items it typed, and stops at the field's limit.
            wanted = [norm(v) for v in re.split(r"[,;]", str(fact.value)) if norm(v)] if fact else []
            if wanted and all(any(w == s or w in s for s in shown) for w in wanted):
                continue
        if kind == "fill" and not a.get("multiple") and not a.get("value") and removed_pick(history, url, a):
            # The site's own pick for the full name was removed: search a shorter one, whose list holds the answer
            short = shorter_name(removed_pick(history, url, a))
            if short and not any(h.get("kind") == "fill" and h.get("typed") == short for h in history):
                return decision("TYPE_TEXT", {**a, "retype": short})
        if kind == "fill" and (a.get("multiple") or not a.get("value")):
            return decision("TYPE_TEXT", a)
        if kind == "fill" and not typed_times(history, url, a):
            # Filled in by the site (résumé parsing, autofill, an earlier visit): correct it once if your profile
            # says something else. This very field (its block and its From/To), not any field sharing its label.
            choice = rules.match(a, facts, False, (), page.get("text", ""))
            fact = facts.by_id.get(choice) if choice else None
            # In the shape this box takes: a "Month" box showing "10" holds 2026-10-26's month already.
            wanted = rules.fit(fact, a, facts) if fact else None
            if fact and not same_value(a["value"], fact.value) and not (wanted and same_value(a["value"], wanted)):
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
                    [page.get("text", ""), *(o["label"] for o in actions), *(o.get("context") or "" for o in actions),
                     *(page.get("files") or []), *(page.get("file_cards") or [])]  # also those out of view
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
        dropdown = role == "button" and SHOWS_NOTHING.search(a["label"])  # Workday's "Select One Required"
        if is_choice(a) and a.get("context") and ("q", a["context"]) not in seen and not dropdown:
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
        if kind == "click" and role == "checkbox" and a.get("checked") == "false" and not touched(history, url, a["label"]):
            # A single box a fact of yours answers Yes ("I am fluent in this language." for English: Fluent): ticked
            # once. Never unticked, and a box no fact speaks for stays as it is.
            choice = rules.match(a, facts, False, (), page.get("text", ""))
            fact = facts.by_id.get(choice) if choice else None
            if fact and norm(fact.value) in {"yes", "true"}:
                return decision("CLICK", a)
        if kind == "click" and role in {"button", "link"} and ADD.search(a["label"]):
            if add_block(a, candidate, history, url, page["actions"]):
                return decision("CLICK", a)
            continue
        custom = role == "combobox" or (role == "button" and PLACEHOLDER.search(a["label"]) and a["label"].strip())
        # Workday's dropdown button has no aria-expanded: opened once per page, never again (no loop).
        # Several of them read just "Select One Required" (Application Questions): each one by its own question.
        # Still showing "Select One" after it was answered: the pick didn't take (its list drew late), so it is
        # opened once more; twice at most.
        opened = opened_times(history, url, a)
        untried = any(o is not a and o.get("role") == "button" and SHOWS_NOTHING.search(o["label"])
                      and not opened_times(history, url, o) for o in actions)  # fmt: skip
        workday_pick = (
            kind == "click" and role == "button" and SHOWS_NOTHING.search(a["label"]) and a.get("expanded") is None
            and (opened == 0 or (opened == 1 and not untried))  # questions not yet tried come first
        )  # fmt: skip
        if workday_pick and a.get("node") not in input_nodes:
            return decision("CLICK", a)  # open it; its options are answered in step 3
        if kind == "click" and role == "button" and a.get("context") and not opened and a.get("node") not in input_nodes:
            # A Workday dropdown showing Yes or No against your answer (HPE's draft held "YES" for its list of
            # export-control countries from before the rule): opened once, and answered again from your profile.
            shown = norm(re.sub(r"\brequired\b", "", a["label"], flags=re.I))
            choice = rules.match({**a, "label": a["context"]}, facts, False, (), page.get("text", "")) \
                if shown in {"yes", "no"} else None  # fmt: skip
            fact = facts.by_id.get(choice) if choice else None
            if fact and norm(fact.value) in {"yes", "no"} and norm(fact.value) != shown:
                return decision("CLICK", a)
        if kind == "click" and custom and a.get("expanded") == "false" and a.get("node") not in input_nodes:
            if role == "button" or not a.get("value") or PLACEHOLDER.search(a.get("value")):
                return decision("CLICK", a)  # a custom dropdown ("Select One"): open it, answer it in step 3

    # A second copy of your résumé in the site's list: deleted once this page's own fields are done (scrolling to it
    # first would pass fields by, as Harman's "I am fluent in this language." was)
    extra = duplicate_upload(page, actions, history, candidate)
    if extra:
        return decision({"scroll_down": "SCROLL_DOWN", "scroll_up": "SCROLL_UP"}.get(extra["id"], "CLICK"), extra)
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
    if has_fields and (invalid or ERRORS.search(page.get("text", ""))) and not stale_errors(actions, history, url, page):
        # The site says fields are missing or wrong (after a Next/Save click): don't press it again blindly.
        return decision("BLOCKED", None, reason=stuck("The site reports problems on this page.", actions, invalid))
    button = {"button", "link"}
    way_in = None if has_fields else way_to_start(actions, history, url)
    if way_in:
        return decision("CLICK", way_in)
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
    # Worked on this step: since its last Save / Next that moved on. Workday keeps one address for every step, so a
    # step still "Loading" counted the earlier steps' fields and was saved unseen (GEA's Application Questions).
    moved = [i for i, h in enumerate(history) if h.get("url") == url and h.get("kind") == "click"
             and h.get("page_changed") and ADVANCE.search(str(h.get("action") or ""))]  # fmt: skip
    step_worked = any(h.get("url") == url and h.get("kind") in {"fill", "select", "setdate", "upload"}
                      for h in history[(moved[-1] + 1) if moved else 0:])  # fmt: skip
    # Worked on this address at all (any step): a page whose fields were all tried or left looks empty, and is
    # never waited on at length (NCR: 75 waits after each Save, three times, until the run's budget ran out).
    worked = any(h.get("url") == url and h.get("kind") in {"fill", "select", "setdate", "upload"} for h in history)
    # The way in is on the page but above the screen (Maersk's "Continue Application" at the top, scrolled past
    # while looking further down): back up for it, a few times.
    up = first(lambda a: a.get("id") == "scroll_up")
    named = WAY_IN_TEXT.search(page.get("text", ""))
    way = first(lambda a: a["kind"] == "click" and a.get("role") in {"button", "link"} and WAY_IN_TEXT.search(a["label"])
                and not touched(history, url, label=a["label"]))  # fmt: skip
    if not has_fields and not worked and way:
        # A job page's own way into its form, in view: clicked now. ("Continue Application" was read as a form's
        # Continue, so the run waited for fields, then scrolled away from it.)
        return decision("CLICK", way)
    in_view = any(a["kind"] == "click" and a.get("role") in {"button", "link"} and WAY_IN_TEXT.search(a["label"])
                  for a in actions)  # fmt: skip
    ups = sum(h.get("kind") == "scroll" and h.get("url") == url and str(h.get("action", "")).startswith("Scroll up")
              for h in history)  # fmt: skip
    downs = sum(h.get("kind") == "scroll" and h.get("url") == url and str(h.get("action", "")).startswith("Scroll down")
                for h in history)  # fmt: skip
    # Only what is on screen is read: scrolled down from the way in, the page no longer names it. Back up as far.
    if not has_fields and not worked and not in_view and up and ups < 3 and (named or ups < downs):
        return decision("SCROLL_UP", up)
    loading = bool(LOADING.search(page.get("text", "")))  # Workday says "Loading" while it draws each field
    patience = NO_FORM_WAITS * 2 if loading else (NO_FORM_WAITS if advance else 8)
    # A new step still drawing ("Loading", nothing done on it yet) is waited for even on Workday's one address
    if not has_fields and (not worked or (loading and not step_worked)) and wait and waiting(history) < patience:
        if advance and scroll and waiting(history) >= 3 and not settling(history):
            return decision("SCROLL_DOWN", scroll)
        return decision("WAIT", wait)
    if advance and scroll and form_step(page) and downs_since_advance(history, url) < 8:
        # A step of a multi-step form is read to its end before Save, fields on screen or not (HPE's and Deluxe's
        # required consent box sat under a long block of terms: one scroll, Save, three errors, a rescue)
        return decision("SCROLL_DOWN", scroll)
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
        # A list still open over the form (a dropdown Workday was loading) hides its fields: the page then looks
        # done. Close it first, once; Next/Save comes after the form shows again.
        escape = first(lambda a: a.get("id") == "escape")
        listed = any(a.get("role") == "option" and not CHIP.search(a["label"]) for a in actions)
        if escape and listed and not (last and last.get("kind") == "key" and last.get("url") == url):
            return decision("KEY", escape)
        # Required questions the page reports empty off screen (Workday adds an Education block above where the
        # agent is, and its form only grows downward in our reading): look up for them, a few times, then go on.
        up = first(lambda a: a.get("id") == "scroll_up")
        if up and pending_above(page, actions, history, url) and ups_since_advance(history, url) < 4:
            return decision("SCROLL_UP", up)
        # An answer just given may open more of the form below it (Deluxe: "employed by Deluxe?" No, and its name,
        # address and phone fields drew only then; Save went first and came back with eight errors): one look first.
        wait = first(lambda a: a.get("id") == "wait")
        answered = last and last.get("url") == url and last.get("kind") in {"click", "select"} and last is history[-1]
        if wait and answered and last.get("action") != advance["label"]:
            return decision("WAIT", wait)
        return decision("CLICK", advance)
    submit = first(
        lambda a: (
            a["kind"] == "click"
            and a.get("role") in button
            and SUBMIT.search(a["label"])
            and APPLY_BUTTON.search(a["label"])
            and not SOCIAL_APPLY.search(a["label"])  # "Apply with LinkedIn" is never the way in
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
