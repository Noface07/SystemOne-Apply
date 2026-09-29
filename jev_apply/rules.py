"""Fixed rules for the fields almost every Indian application form asks.

When a field's own words clearly say what it wants ("Current CTC (in LPA)", "Notice period (days)", "Total
experience"), the fact is picked here, the same way every time, without any model. Anything a rule isn't sure
about returns None and goes to the decision model as before. A rule never invents a value: it only names one of
your profile facts or one of the special choices (ASK_USER, SKIP_FIELD, DRAFT_ANSWER).
"""

import re

from .profile import DATE, format_date, safe_id

MONEY = re.compile(r"\b(ctc|salary|compensation|package|income|pay|remuneration|stipend)\b")
EXPECTED = re.compile(r"expect|desired|asking|target|anticipat|looking for|new (ctc|salary|package)")
CURRENT = re.compile(r"current|present|existing|last drawn|drawn|at present")
MONTHLY = re.compile(r"per month|monthly|/ ?month|\bp\.?m\.?\b|a month|\bpm\b")
LPA = re.compile(r"\blpa\b|lakh|\blacs?\b|\bin l\b|\(l\)")
# Facts a profile never holds: you answer these yourself.
MAX_TAGS = 12  # items typed into a tag field that states no limit: your first (strongest) ones
ASK = re.compile(
    r"last working day|relieving date|date of (joining|leaving)|how did you (hear|find|come)|take[- ]?home|in[- ]?hand"
)
ANNUAL = re.compile(r"annual|per annum|\bp\.?a\.?\b|yearly|a year|in inr|rupees|₹|\brs\.?\b")
# School-leaving exams, which Indian forms ask about separately from your degree. 12th before 10th: "higher
# secondary" contains "secondary".
CLASS_12 = re.compile(
    r"\b(12th|xii|twelfth|hsc|higher secondary|intermediate|puc|pre-?university|senior secondary|sr\.? secondary|"
    r"class ?12|std\.? ?12)\b|\+ ?2\b"
)
CLASS_10 = re.compile(r"\b(10th|tenth|ssc|sslc|matric(ulation)?|secondary school|class ?(10|x)|std\.? ?10)\b")
# A repeating block's heading and number: "Work Experience 2", "Employment #2", "Education 1". Not "experience
# 4 years".
BLOCKS = {
    "work experience": r"(?:work experience|employment(?: history)?|experience|job|employer|position)",
    "education": r"(?:education|qualification|academic(?: details)?)",
}
NUMBERED = r"\s*#?\s*(\d{1,2})(?![\d.]|\s*(?:\+|years?|yrs?|months?|days?|lpa|%))"
PREVIOUS = re.compile(r"\b(previous|past|former|last)\b (company|employer|organi[sz]ation|job|designation|title|role)")
# A text field that wants a date in a given shape: "DD/MM/YYYY", "MM-YYYY", "Month YYYY".
DATE_SHAPE = re.compile(r"\b(dd|mm|yyyy|yy)\s*([/.\-])\s*(dd|mm|yyyy|yy)(?:\s*\2\s*(dd|mm|yyyy|yy))?\b")
# Facts nothing else in a profile can stand in for: when missing, the question goes to you, not to the model.
PERSONAL = (
    "personal.date_of_birth",
    "personal.nationality",
    "personal.father_name",
    "personal.mother_name",
    "personal.has_valid_passport",
    "self_identification.marital_status",
    "academics.active_backlogs",
    "academics.has_active_backlogs",
    "work.has_relative_at_company",
    "school.",
    "skill_years.",
)
MONTH_SHAPE = re.compile(r"\b(month|mmm+)\s*,?\s*(yyyy|year)\b")


# Words that say nothing about WHICH fact a question wants: a model's pick must share a word beyond these.
GENERIC = set(
    "you your yes have has are the what which how many much please select choose one option any this that for with "
    "and our will would can able currently current did been ever role position job team time type response answer "
    "question details information provide enter not applicable other from about into here there their they them "
    "was were who whom why when where does done make made most more less than also just like give tell share describe "
    "explain field value experience professional work years year yrs months month".split()
)
OPEN_QUESTION = re.compile(r"^(what|why|how|tell|describe|explain|share|give|walk us|talk about|briefly)\b|\bif yes\b")


def words_of(text):
    """Content words of a text, lightly stemmed ('skills' ~ 'skill')."""
    found = set()
    for w in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        if len(w) >= 3 and w not in GENERIC:
            found.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return found


def related(fact, action):
    """Does a fact share a content word with the question? A model's pick that doesn't is never typed: that is
    how 'Python' ends up in 'Tell us about a time you...'."""
    question = words_of(text_of(action))
    return bool(question & words_of(f"{fact.key.replace('.', ' ').replace('_', ' ')} {fact.about}"))


def text_of(action):
    parts = (
        action.get("label", "").split(" → ")[0],
        action.get("context", ""),
        action.get("placeholder", ""),
        action.get("help", ""),
    )
    return " ".join(p for p in parts if p).lower()


def label_of(action):
    label = re.sub(r"[*:✱＊]", "", action.get("label", "").split(" → ")[0]).strip().lower()
    return re.sub(r"^(please )?(enter|type|provide|add|select|choose) (your |the )?|^your ", "", label).strip()


def money_unit(t, action):
    if MONTHLY.search(t):
        return "_monthly"
    if LPA.search(t):
        return "_lpa"
    if ANNUAL.search(t):
        return ""
    digits = re.sub(r"[^\d]", "", action.get("placeholder", "") or "")
    if digits:
        return "" if int(digits) >= 100000 else "_lpa" if int(digits) <= 500 else None
    # "Expected CTC" with no unit anywhere is often asked in LPA on Indian forms: too ambiguous to guess.
    return None if "ctc" in t else ""


def money(t, action):
    if re.search(r"take[- ]?home|in[- ]?hand|\bnet\b|hike|increment|percent|%", t):
        return "ASK_USER"
    when = "expected" if EXPECTED.search(t) else "current" if CURRENT.search(t) else None
    if when is None:
        return None
    part = (
        "fixed"
        if re.search(r"fixed|\bbase\b|basic", t)
        else "variable"
        if re.search(r"variable|bonus|incentive", t)
        else "total"
    )
    if part == "variable" and when == "expected":
        return None
    unit = money_unit(t, action)
    return None if unit is None else f"compensation.{when}.{part}_annual{unit}"


def next_item(profile, prefix, action, tried):
    """A tag field takes one list item at a time: the first one not tried and not already shown as added."""
    added = re.sub(r"[^0-9a-z]", "", (action.get("context") or "").lower())
    limit = re.search(r"(?:up to|max(?:imum)?(?: of)?|at most) (\d+)", text_of(action))
    if len(tried) >= (int(limit.group(1)) if limit else MAX_TAGS):
        return "SKIP_FIELD"  # the field says how many it takes; otherwise your top MAX_TAGS
    n = 1
    while safe_id(f"{prefix}.item_{n}") in profile.by_id:
        choice = safe_id(f"{prefix}.item_{n}")
        value = re.sub(r"[^0-9a-z]", "", profile.by_id[choice].value.lower())
        if choice not in tried and value not in added:
            return choice
        n += 1
    return "SKIP_FIELD"


def block_index(name, t, section, page_text):
    """Which block of a repeating section ('Work Experience 2', 'Employment #2', 'Education 1') a field is in:
    from its own words, then the heading above it, then the page when it shows exactly one such block. None
    when unclear."""
    pattern = BLOCKS.get(name, re.escape(name)) + NUMBERED
    for text in (t, section.lower()):
        found = re.search(pattern, text)
        if found:
            return int(found.group(1)) - 1
    shown = set(re.findall(pattern, page_text.lower()))
    return int(shown.pop()) - 1 if len(shown) == 1 else None


def dates_key(base, t, label):
    """From/To (or Start/End) inside a block, in the shape the field asks for."""
    part = "start" if re.search(r"\bfrom\b|start", t) else "end" if re.search(r"\bto\b|\bend\b", t) else None
    if part is None or not re.fullmatch(r"from|to|start( date)?|end( date)?|month|year|mm|yyyy", label):
        return None
    if label in {"month", "mm"}:
        return base + part + ".mm"
    if label in {"year", "yyyy"}:
        return base + part + ".year"
    return base + part + (".month_year" if re.search(r"month yyyy|mmm", t) else ".mm_yyyy")


def experience_key(t, label, section, page_text, i=None):
    """Fields of a 'Work Experience N' block (Workday and similar): the N-th job in your profile, newest first."""
    i = block_index("work experience", t, section, page_text) if i is None else i
    if i is None:
        return None
    base = f"experience.{i}."
    if re.fullmatch(r"(job )?title|position|role|designation", label):
        return base + "title"
    if re.fullmatch(r"company( name)?|employer|organi[sz]ation", label):
        return base + "company"
    if re.fullmatch(r"(role |job )?description|responsibilities|summary", label):
        return base + "summary"
    if re.fullmatch(r"location|city", label):
        return base + "location"
    return dates_key(base, t, label)


def education_key(t, label, section, page_text):
    """Fields of an 'Education N' block: the N-th entry of your education, newest first."""
    i = block_index("education", t, section, page_text)
    if i is None:
        return None
    base = f"education.{i}."
    if re.search(r"school|university|college|institut", label):
        return base + "institution"
    if re.search(r"field of study|major|speciali[sz]ation|branch|discipline|stream", label):
        return base + "field"
    if re.search(r"degree|qualification", label):
        return base + "degree"
    if re.search(r"gpa|overall result|grade|percentage|marks|score", label):
        return base + "grade"
    part = "start" if re.search(r"\bfrom\b|start", t) else "end" if re.search(r"\bto\b|\bend\b|graduat", t) else None
    if part and re.fullmatch(r"from|to|start( date)?|end( date)?|year|graduation (date|year)", label):
        # A year box gets the year alone, whether your profile holds "2024" or "2024-06".
        return (base + part + ".year", base + part) if "year" in label else base + part
    return None


def school_key(t, profile):
    """Class 10 / Class 12 questions: their own facts, never your degree's grade or year."""
    level = "school.class_12" if CLASS_12.search(t) else "school.class_10" if CLASS_10.search(t) else None
    if level is None:
        return None
    if re.search(r"\bboard\b", t):
        part = "board"
    elif re.search(r"percent|marks|grade|\bc?gpa\b|score|result|%|aggregate", t):
        part = "percentage"
    elif re.search(r"year|passing|pass[- ]?out|completion|completed", t):
        part = "year"
    elif re.search(r"stream|subjects?\b|speciali[sz]ation", t):
        part = "stream"
    elif re.search(r"school|college|institut", t):
        part = "school"
    else:
        return None
    key = f"{level}.{part}"
    return key if safe_id(key) in profile.by_id else "ASK_USER"


SKILL_YEARS = re.compile(
    r"(?:how many )?years?(?: of)?(?: total| work| professional| hands[- ]on| relevant| practical)* experience "
    r"(?:do you have |have you |you have )?(?:with|in|using|on|of|working with|working on) ([^?*✱]+)"
)


# "How many years of Software Development experience do you have?": the skill comes before "experience".
SKILL_BEFORE = re.compile(r"years? of ([a-z0-9 .+#/&-]{2,40}?) experience")
GENERIC_EXPERIENCE = re.compile(
    r"^(work|working|professional|total|overall|relevant|industry|it|prior|previous|full[- ]time|hands[- ]on|"
    r"practical|paid|related|corporate|job|post[- ]qualification)( work)?$"
)


def skill_years_key(t, profile):
    """'How many years of work experience do you have with Python?' (LinkedIn, Indeed, Naukri): your
    skill_years entry for that skill, else you are asked (once: `jev-apply answer` keeps it)."""
    t = re.sub(r"\byear'?s'?\b", "years", t)  # "How many year's of experience..."
    asked = SKILL_YEARS.search(t)
    if not asked:
        before = SKILL_BEFORE.search(t)
        asked = before if before and not GENERIC_EXPERIENCE.match(before.group(1).strip()) else None
    if not asked or re.search(r"\b(total|overall)\b", t):
        return None
    phrase = re.sub(r"[^0-9a-z+#]", "", asked.group(1).lower())
    listed = [
        (re.sub(r"[^0-9a-z+#]", "", fact.key.split(".", 1)[1].lower()), fact.key)
        for key, fact in profile.by_id.items()
        if key.startswith("skill_years__") and fact.key != "skill_years.default"
    ]
    listed = [(skill, key) for skill, key in listed if skill]

    def at_a_word(key):  # ".NET" starts a word of "ASP.NET Core", but not of "BACnet"
        parts = re.split(r"[\s./-]+", key.split(".", 1)[1].lower())
        return any(re.sub(r"[^0-9a-z+#]", "", "".join(parts[i:])).startswith(phrase) for i in range(len(parts)))

    # Where a skill may start inside the question: its first words, or the first words after "/", "(", ",", "and"
    # ("C (Programming Language)", "IIoT/IoT/Industrial Automation", "... (RAG)"). A skill in the middle of a
    # phrase is another skill: "PLC Programming" is neither "C programming" nor "Programming", "digital" not "Git".
    starts = {}
    whole = re.sub(r"[()]", " ", asked.group(1).lower())  # "C (Programming Language)" reads "C Programming ..."
    for n, segment in enumerate([whole] + re.split(r"[/(),]+|\band\b|\bor\b", asked.group(1).lower())):
        words = [re.sub(r"[^0-9a-z+#]", "", w) for w in segment.split()]
        for i in range(1, len(words) + 1):
            starts.setdefault("".join(words[:i]), n)
    # The skill itself, else the longest skill a segment starts with (the earliest segment first), else the
    # closest listed skill the question abbreviates.
    exact = [key for skill, key in listed if skill == phrase]
    inside = [(starts[skill], -len(skill), key) for skill, key in listed if skill in starts]
    # A shorter name for a longer listed skill: an abbreviation anywhere in it (".NET", "PLC"), or its start
    # ("ASP.NET" for "ASP.NET Core"). "Engineering" is not "Prompt engineering".
    around = [
        (skill, key)
        for skill, key in listed
        if len(phrase) >= 2 and (phrase in skill if len(phrase) <= 5 else skill.startswith(phrase))
    ]
    if exact:
        return exact[0]
    if inside:
        return min(inside)[2]
    if around:
        return min(around, key=lambda s: (not at_a_word(s[1]), len(s[0])))[1]
    # A skill you didn't list: your skill_years.default when you set one ("when in doubt, say 2"), else asked.
    return "skill_years.default" if safe_id("skill_years.default") in profile.by_id else "ASK_USER"


def experience_years_key(t, label):
    """Total / relevant experience in the unit the field asks: years, months, or a years box + months box."""
    plain = re.sub(
        r"(years?|months?) of (work |professional |total )?experience|\b(in|no\.? of) (years|yrs|months)\b|"
        r"\((in )?(years|yrs|months)\)",
        " ",
        t,
    )
    asks_yes_no = re.search(r"\b(do|have|are|did|will) you\b", t) and not re.search(
        r"how (many|much|long)|years? of|number of (years|months)|no\.? of (years|months)", t
    )
    if not re.search(r"years?|yrs|months?|total|overall|how (long|much)|\bexp\b", t) or asks_yes_no:
        return None  # "Do you have experience with X?" asks yes/no, not a duration
    if "relevant" in t:
        base = "work.relevant_experience"
    elif re.search(r"\b(with|within|using|on|of|in|at)\b (a |an )?[a-z]", plain) and not re.search(
        r"\b(total|overall)\b", t
    ):
        return None  # years with one specific skill or tool: not a profile fact
    elif re.search(r"total|overall|professional|work|years|yrs|\bexp\b|month", t):
        base = "work.total_experience"
    else:
        return None
    if label in {"years", "year", "yrs", "yr"}:
        return base + "_whole_years"
    if label in {"months", "month", "mths"} or re.search(r"(additional|extra|remaining) months|months beyond", t):
        return base + "_extra_months"  # the months box beside a years box: 2.2 years -> 2 years + 2 months
    if re.search(r"month", t):
        # "(years and months)" in one box is neither unit alone.
        return None if re.search(r"year|yrs", t) else base + "_months"
    return base + "_years"


def key_for(action, profile, tried, dates_only=False, page_text=""):
    """Profile key (dotted), special choice, or None. Order matters: specific questions before generic ones."""
    t, label = text_of(action), label_of(action)
    section = (action.get("section") or "").lower()
    where = " ".join((page_text.lower(), t, section))
    school = school_key(t, profile)
    if school:
        return school
    if re.search(BLOCKS["work experience"] + NUMBERED, where):
        key = experience_key(t, label, section, page_text)
        if key:
            return key
    if PREVIOUS.search(f"{t} {section}") and not re.search(r"\b(current|present)\b", t):
        # "Previous employer": your job before the current one (or your last job, when you have none now).
        current = safe_id("experience.0.company") in profile.by_id and safe_id("experience.0.end") not in profile.by_id
        return experience_key(t, label, section, page_text, i=1 if current else 0)
    if re.search(BLOCKS["education"] + NUMBERED, where):
        key = education_key(t, label, section, page_text)
        if key:
            return key
    if re.search(r"\blocal (given|first|middle|family|last) name|native script|name in local", t):
        return None if action.get("required") else "SKIP_FIELD"  # a name in another script: yours to type
    if re.search(
        r"(have|did) you (ever )?(previously )?(work|worked|been employed)\b(?! (on|with|in|using|as)\b)|"
        r"former employee|"
        r"worked (for|at) .* before",
        t,
    ):
        worked_at = [
            f.value.lower()
            for k, f in profile.by_id.items()
            if k.startswith("experience__") and k.endswith("__company")
        ]
        return "ASK_USER" if any(c and c in t for c in worked_at) else "work.worked_for_this_company_before"
    if MONEY.search(t) and not re.search(r"pay ?slip|payroll|paypal|pay scale", t):
        return money(t, action)
    if re.search(r"how did you (hear|find|come)|where did you (hear|find)|source of (application|referral)", t):
        return "application.how_heard" if safe_id("application.how_heard") in profile.by_id else "ASK_USER"
    if ASK.search(t):
        return "ASK_USER"
    if re.search(
        r"(start|join)\w*\s+(right away|immediately|at once|asap)|immediate(ly)? (joiner|joining|available|start)", t
    ):
        return "work.can_join_immediately"
    if "notice" in t:
        if re.search(r"serving|currently on notice|are you on", t):
            return "work.serving_notice"
        if re.search(r"negotiable|buy ?out|early", t):
            return None
        if re.search(r"\bdays?\b", t) or action.get("input_type") == "number":
            return "work.notice_period_days"
        if re.search(r"month|week", t):
            return None
        return "work.notice_period_text"
    skill = skill_years_key(t, profile)
    if skill:
        return skill
    if re.search(r"experience|\bexp\b", t) and not re.search(r"describe|tell|explain|summar", t):
        return experience_years_key(t, label)
    if re.search(r"backlog|arrear|\bkt\b", t):
        return (
            "academics.active_backlogs"
            if re.search(r"number|how many|count|no\.? of", t) or action.get("input_type") == "number"
            else "academics.has_active_backlogs"
        )
    if re.search(r"graduat|passing|pass[- ]?out|year of completion", t):
        if re.search(r"percent|marks|\bc?gpa\b|grade|score|aggregate", t):
            return "education.0.grade"
        if re.search(r"month|date", t) or action.get("input_type") in {"date", "month"}:
            return None
        if re.search(r"year", t) or action.get("input_type") == "number":
            return ("education.0.end.year", "education.0.end")  # the year alone, from "2024" or "2024-06"
        return None
    if re.search(r"(relative|family member|friend|anyone you know).{0,60}(work|employ)", t):
        return "work.has_relative_at_company"
    if re.search(r"date of birth|\bdob\b|birth ?date|born on", t):
        return "personal.date_of_birth"
    if re.search(r"nationality|citizenship", t):
        return "personal.nationality"
    if re.search(r"father'?s?( or guardian'?s?)? name|guardian'?s? name", t):
        return "personal.father_name"
    if re.search(r"mother'?s? name", t):
        return "personal.mother_name"
    if "marital" in t:
        return "self_identification.marital_status"
    if re.search(r"passport", t) and not re.search(r"number|\bno\b|expiry|issue", t):
        return "personal.has_valid_passport"
    if re.search(r"\bwhy\b|motivat|what (excites|interests|attracts)|cover (letter|note)", t):
        return "DRAFT_ANSWER"
    if re.search(r"about (yourself|you)\b|introduce yourself|professional summary|brief intro", t):
        return "answer.about_me"
    if "middle name" in t:
        return None if action.get("required") else "SKIP_FIELD"
    if re.search(r"first name|given name|forename", t):
        return "personal.first_name"
    if re.search(r"last name|surname|family name", t):
        return "personal.last_name"
    if label in {"name", "full name", "your name", "candidate name", "applicant name"}:
        return "personal.full_name"
    if re.search(r"e-?mail", t):
        return "personal.email"
    if re.search(r"phone code|country code|dial(l?ing)? code|calling code", t):
        return ("personal.phone_country_code", "personal.country")
    if label in {"country", "country/region", "country / region"} and re.search(r"phone|mobile", t):
        return ("personal.country", "personal.phone_country_code")  # a searchable picker: "India" finds "India +91"
    if re.search(r"(phone|device) type|type of (phone|number)", t):
        return "personal.phone_type"
    if re.search(r"alternate|alternative|emergency|extension", t):
        return None if action.get("required") else "SKIP_FIELD"
    if re.search(r"phone|mobile|contact (number|no)|whatsapp", t):
        # A separate country-code box on the page means the number goes in without +91.
        page = page_text.lower()
        if re.search(
            r"phone code|country code|dial(l?ing)? code|calling code|phone\W{1,20}country|country\W{1,20}phone", page
        ):
            return ("personal.phone_local", "personal.phone")
        return "personal.phone"
    if "linkedin" in t:
        return "links.linkedin"
    if "github" in t:
        return "links.github"
    link = None
    if re.search(r"twitter|\bx\.com\b|\bx (profile|handle)", t):
        link = "links.twitter"
    elif re.search(r"portfolio|personal (web)?site|website|\bblog\b|other (url|link|website|profile)", t):
        link = "links.portfolio"
    if link:
        # A link your profile doesn't have is left empty (or asked, when required), never filled with another fact.
        return link if safe_id(link) in profile.by_id else ("ASK_USER" if action.get("required") else "SKIP_FIELD")
    if re.search(r"pin ?code|postal|\bzip\b", t):
        return "personal.pincode"
    if re.search(
        r"preferred (work )?location|location preference|preferred cit|location.{0,20}applying for|"
        r"applying for.{0,20}location",
        t,
    ):
        return (
            next_item(profile, "preferences.preferred_locations", action, tried)
            if action.get("multiple")
            else "preferences.preferred_locations"
        )
    if re.search(r"current (city|location)|city of residence|where are you (based|located)", t) or re.sub(
        r"\s*\(.*?\)", "", label
    ) in {
        "city",
        "location",
        "current city",
        "current location",
    }:
        return "personal.city"
    if re.search(r"(what|which) country|country (are you|do you) (based|live|reside|located)", t):
        return "personal.country"
    if re.fullmatch(r"country( ?/ ?territory| or region)?|country of residence|current country", label):
        return "personal.country"
    if re.search(r"address line ?2|address 2", t):
        if safe_id("personal.address_line2") in profile.by_id:
            return "personal.address_line2"
        return None if action.get("required") else "SKIP_FIELD"
    if re.search(r"address line ?3|address 3|apartment|suite", t):
        return None if action.get("required") else "SKIP_FIELD"
    if re.search(r"address line ?1|address 1", t):
        # Split over lines only when the form has a second line; otherwise the whole street goes in one.
        two_lines = re.search(r"address line ?2|address 2", page_text.lower())
        return ("personal.address_line1", "personal.address") if two_lines else "personal.address"
    if re.search(r"street address|house|flat no|building", t):
        return "personal.address"
    if re.fullmatch(r"((current|permanent|full|residential|postal|correspondence|home) )?address", label):
        return ("personal.full_address", "personal.address")  # one box for the whole address
    if re.fullmatch(r"state( ?/ ?province)?|province|region|state of residence", label):
        return "personal.state"
    if re.search(r"relocat", t):
        return "preferences.willing_to_relocate"
    if re.search(r"shift", t):
        return "preferences.open_to_rotational_shifts"
    if re.search(r"work ?mode|work arrangement|work model", t):
        return "preferences.work_mode"
    if re.search(r"authori[sz]ed to work|work authori[sz]ation|legally (eligible|allowed|permitted)", t):
        return "work_authorization.authorized_to_work_in_india"
    if re.search(r"sponsor", t):
        return "work_authorization.requires_visa_sponsorship"
    if re.search(r"earliest|joining date|start date|availability|when can you (start|join)|date of availability", t):
        # A calendar date when the field asks for one (a date input, "DD/MM/YYYY", "...date"), else your words.
        wants_date = dates_only or date_shape(action) or re.search(r"\bdate\b", label)
        return (
            ("work.earliest_start_date", "work.availability")
            if wants_date
            else ("work.availability", "work.earliest_start_date")
        )
    if re.search(r"current (company|employer|organi[sz]ation)|present (employer|company)", t) or label in {
        "company",
        "company name",
        "employer",
        "current company",
    }:
        return "work.current_company"
    if re.search(r"current (designation|title|role|position)|job title|designation", t):
        return "work.current_title"
    if re.search(r"\bdegree\b|qualification", t):
        return "education.0.degree"
    if re.search(r"college|university|institut", t):
        return "education.0.institution"
    if re.search(r"branch|speciali[sz]ation|\bmajor\b|field of study|\bstream\b|discipline", t):
        return "education.0.field"
    if re.search(r"\bc?gpa\b|percentage|\bgrade\b|\bmarks\b", t):
        return "education.0.grade"
    if "skill" in t:
        return next_item(profile, "skills", action, tried) if action.get("multiple") else "skills"
    if re.search(r"languages?( known| spoken)?\b", label):
        return "languages"
    if re.search(r"headline|profile title", t):
        return "headline"
    if re.search(r"\bgender\b", t):
        return "self_identification.gender"
    question = label_of(action)
    if OPEN_QUESTION.search(question) and (action.get("input_type") == "textarea" or len(question) > 50):
        return "DRAFT_ANSWER"  # an open question: written for you (or asked), never answered with one fact
    return None


def date_shape(action):
    """(order, separator) when a text field asks for a date in a given shape, e.g. (['dd','mm','yyyy'], '/')."""
    t = " ".join(str(action.get(k) or "") for k in ("placeholder", "help", "label")).lower()
    found = DATE_SHAPE.search(t)
    if found:
        return [g for g in (found[1], found[3], found[4]) if g], found[2]
    if MONTH_SHAPE.search(t):
        return ["month", "yyyy"], " "
    return None


def expected_kind(action):
    """What a field's value must look like, from its type or its words: 'url', 'email', 'tel' or None."""
    kind = action.get("input_type")
    if kind in {"url", "email", "tel"}:
        return kind
    label = label_of(action)
    if re.search(r"years?|experience|how many|how long", label):
        return None  # "How many years of experience with GitHub?" wants a number, not a link
    if re.search(r"\burl\b|website|linkedin|github|portfolio|\blink\b", label):
        return "url"
    if re.search(r"^e-?mail\b|\be-?mail (address|id)\b", label):
        return "email"
    if re.search(r"\b(phone|mobile|contact) (number|no\.?)\b|^(phone|mobile)$", label):
        return "tel"
    return None


def fit(fact, action, profile):
    """The fact's value in the shape this field accepts, or None when it can't be given without inventing
    something (a day your profile doesn't hold, a decimal in a whole-number box, text over the length limit)."""
    value = fact.value
    shape = date_shape(action) if action.get("input_type") not in {"date", "month", "week", "time"} else None
    if shape:
        base = re.sub(r"\.(mm_yyyy|month_year|year|mm|dd_mm_yyyy|mm_dd_yyyy)$", "", fact.key)
        iso = getattr(profile, "facts", {}).get(base, fact).value
        return format_date(iso, *shape) if DATE.match(iso or "") else None
    step = str(action.get("step") or "1")
    fractional = action.get("input_type") == "number" and (step == "any" or not float(step).is_integer())
    if re.fullmatch(r"\d+\.\d+", value) and not fractional:
        # Years as a whole number unless the box takes fractions: job boards reject "1.5" in a years box
        # (LinkedIn: "Invalid input"). Completed years, as for a years box: 1.5 -> 1, never rounded up.
        if fact.key.startswith("skill_years."):
            return str(int(float(value)))
        if fact.key.endswith("_experience_years") and re.search(r"how many years", text_of(action)):
            whole = profile.by_id.get(safe_id(fact.key.replace("_years", "_whole_years")))
            return whole.value if whole else str(int(float(value)))
    wants = expected_kind(action)
    if wants == "url" and not re.match(r"^(https?://|www\.)\S+$|^[\w-]+(\.[\w-]+)+(/\S*)?$", value, re.I):
        return None
    if wants == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return None
    if wants == "tel" and not re.fullmatch(r"\+?[\d\s().-]{6,20}", value):
        return None
    if action.get("input_type") == "number":
        try:
            number = float(value.replace(",", ""))
        except ValueError:
            return None
        step = str(action.get("step") or "1")
        if step != "any" and float(step).is_integer() and not number.is_integer():
            if fact.key.startswith("skill_years."):
                return str(int(number))  # completed whole years with that skill, as for total experience
            whole = re.sub(r"_years$", "_whole_years", fact.key)
            found = profile.by_id.get(safe_id(whole)) if whole != fact.key else None
            return found.value if found else None
    limit = action.get("maxlength")
    if limit and len(value) > int(limit):
        local = profile.by_id.get(safe_id("personal.phone_local"))
        if fact.key == "personal.phone" and local and len(local.value) <= int(limit):
            return local.value
        return None
    return value


def match(action, profile, dates_only=False, tried=(), page_text=""):
    """A choice id for value_source (a fact id or a special choice), or None when no rule is sure."""
    key = key_for(action, profile, set(tried), dates_only, page_text)
    if key is None or key in {"ASK_USER", "SKIP_FIELD", "DRAFT_ANSWER"}:
        return None if (dates_only and key == "DRAFT_ANSWER") else key
    keys = key if isinstance(key, tuple) else (key,)  # first one your profile has
    choice = next((safe_id(k) for k in keys if safe_id(k) in profile.by_id), None)
    fact = profile.by_id.get(choice)
    if fact is None and choice is None and any(k.startswith(PERSONAL) for k in keys):
        return "ASK_USER"  # no other fact can stand in for your birth date, nationality, marks...
    if fact is None or choice in tried:
        return None
    if dates_only and fact.kind != "date":
        return "ASK_USER"  # the rule knows what the field wants, but you have no calendar date for it
    return choice


def document(action, documents):
    """Which listed document an upload field wants, when its wording says so; None to let the model decide."""
    t = text_of(action)
    names = {name: f"{name} {doc.about}".lower() for name, doc in documents.items()}
    if re.search(r"cover", t):
        match = next((n for n, text in names.items() if "cover" in text), None)
        return match or ("SKIP_FIELD" if not action.get("required") else "ASK_USER")
    resume = next((n for n, text in names.items() if re.search(r"r[ée]sum[ée]|\bcv\b", text)), None)
    if re.search(r"r[ée]sum[ée]|\bcv\b|curriculum", t):
        return resume
    if re.search(
        r"portfolio|certificat|transcript|photo|picture|id proof|identity|additional|other|supporting|work sample", t
    ):
        return None  # a file field for something else: the model (or you) decides, never the résumé by default
    if re.search(r"upload|attach|select files?|drop files?|document|browse|\bfiles?\b", t):
        return resume  # an application's file field that isn't for a cover letter: your résumé
    return None
