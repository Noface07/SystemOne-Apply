"""Your data as named facts. The decision model chooses WHICH fact answers a field; it never writes one."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# A choice question offers a field's facts: TypeSafe takes up to 255 options and Laya a handful, so both backends
# get a shortlist of the facts sharing the field's words (model.fit_for_typesafe, laya_backend.shortlist). Saved
# answers can keep growing; this is only a sanity bound on a profile.
MAX_SOURCES = 240  # per question sent to TypeSafe, leaving room for DRAFT_ANSWER / ASK_USER / SKIP_FIELD
MAX_SOURCES_SHORTLISTED = 1000


def source_limit():
    return MAX_SOURCES_SHORTLISTED


MAX_ITEMS = 25  # lists up to this long also become one-item facts, for tag / multi-select fields
ITEM = re.compile(r"\.item_\d+$")
DATE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")
MONTHS = "January February March April May June July August September October November December".split()
INDEX = re.compile(r"\.(\d+)(?=\.|$)")

# Descriptions carry the wording a form might use. Precise, contrasting descriptions are what let a
# choice model tell "current CTC" from "expected CTC"; the key name alone is not enough.
KNOWN = {
    "personal.first_name": "First name / given name",
    "personal.last_name": "Last name / surname / family name",
    "personal.full_name": "Full name (first and last)",
    "personal.email": "Email address",
    "personal.phone": "Mobile / phone number with country code",
    "personal.city": "Current city / current location / city of residence",
    "personal.state": "State / province of residence",
    "personal.country": "Country of residence",
    "personal.pincode": "PIN code / postal code / ZIP",
    "personal.address": "Street address",
    "links.linkedin": "LinkedIn profile URL",
    "links.github": "GitHub profile URL",
    "links.portfolio": "Portfolio / personal website URL",
    "work.current_title": "Current job title / designation / role",
    "work.current_company": "Current employer / company / organisation",
    "work.total_experience_years": "TOTAL professional experience in years (all roles)",
    "work.relevant_experience_years": "RELEVANT experience in years (for this kind of role only), not total",
    "work.notice_period_days": "Notice period as a number of days",
    "work.notice_period_text": "Notice period as text, e.g. 'Immediate', '30 days', '2 months'",
    "work.serving_notice": "Currently serving notice period? (Yes/No)",
    "work.earliest_start_date": "Earliest joining / start / availability date",
    "compensation.currency": "Currency of the compensation figures",
    "compensation.current.fixed_annual": (
        "CURRENT FIXED annual salary only: base pay, EXCLUDING variable/bonus. Also called fixed CTC, "
        "base salary, fixed pay. Not the total CTC and not the expected amount"
    ),
    "compensation.current.variable_annual": "CURRENT annual variable pay / bonus / incentives only",
    "compensation.current.total_annual": (
        "CURRENT CTC: total CURRENT annual cost to company = fixed + variable. Also called current CTC, "
        "present CTC, current package, current salary, current compensation, current annual income. "
        "NOT the expected amount"
    ),
    "compensation.expected.total_annual": (
        "EXPECTED CTC: total annual compensation asked for in the NEW job. Also called expected CTC, "
        "expected salary, desired salary, salary expectation, expected package, expected income. "
        "NOT the current amount"
    ),
    "compensation.expected.negotiable": "Is the expected CTC negotiable? (Yes/No)",
    "preferences.willing_to_relocate": "Willing to relocate? (Yes/No)",
    "preferences.preferred_locations": "Preferred work locations",
    "preferences.work_mode": "Preferred work mode: remote / hybrid / onsite",
    "preferences.open_to_rotational_shifts": "Open to rotational / night shifts? (Yes/No)",
    "work_authorization.authorized_to_work_in_india": "Legally authorised to work in India? (Yes/No)",
    "work_authorization.requires_visa_sponsorship": "Requires visa sponsorship now or in future? (Yes/No)",
    "education.*.degree": "Education entry {n} (1 = most recent): degree / qualification",
    "education.*.field": "Education entry {n}: field of study / specialisation / branch / major",
    "education.*.institution": "Education entry {n}: college / university / institute name",
    "education.*.start": "Education entry {n}: start date",
    "education.*.end": "Education entry {n}: end / graduation / passing-out date (year of graduation)",
    "education.*.grade": "Education entry {n}: grade / CGPA / percentage",
    "experience.*.company": "Work experience entry {n} (1 = most recent): company name",
    "experience.*.title": "Work experience entry {n}: job title",
    "experience.*.start": "Work experience entry {n}: start date",
    "experience.*.end": "Work experience entry {n}: end date (empty = current job)",
    "experience.*.summary": "Work experience entry {n}: description of responsibilities / achievements",
    "skills": "Skills (comma-separated)",
    "languages": "Languages spoken",
    "personal.phone_local": "Mobile number WITHOUT the country code (for a form with a separate code box)",
    "personal.phone_country_code": "Phone country code, e.g. +91",
    "personal.nationality": "Nationality / citizenship",
    "personal.date_of_birth": "Date of birth",
    "personal.father_name": "Father's / guardian's name",
    "personal.mother_name": "Mother's name",
    "personal.has_valid_passport": "Holds a valid passport? (Yes/No)",
    "work.can_join_immediately": "Can join / start immediately? (Yes/No, from the notice period)",
    "skill_years.default": "Years of experience to give for a skill not listed in skill_years",
    "work.has_relative_at_company": "Has a relative / family member working at the company applied to? (Yes/No)",
    "work.worked_for_this_company_before": "Worked for the company applied to before? (Yes/No)",
    "self_identification.decline_eeo": "Decline voluntary EEO questions (gender, race, veteran, disability)? (Yes/No)",
    "self_identification.marital_status": "Marital status",
    "work.total_experience_months": "TOTAL professional experience in MONTHS",
    "work.total_experience_whole_years": "TOTAL experience: completed whole years only (a years box)",
    "work.total_experience_extra_months": "TOTAL experience: months beyond the whole years (a months box)",
    "work.relevant_experience_months": "RELEVANT experience in MONTHS",
    "work.relevant_experience_whole_years": "RELEVANT experience: completed whole years only",
    "work.relevant_experience_extra_months": "RELEVANT experience: months beyond the whole years",
    "academics.active_backlogs": "Number of active backlogs / arrears (0 = none)",
    "academics.has_active_backlogs": "Any active backlogs / arrears? (Yes/No)",
    "school.class_10.percentage": "Class 10 / SSC / matriculation: percentage or grade",
    "school.class_10.year": "Class 10 / SSC: year of passing",
    "school.class_10.board": "Class 10 / SSC: board (CBSE, ICSE, state board...)",
    "school.class_10.school": "Class 10 / SSC: school name",
    "school.class_12.percentage": "Class 12 / HSC / intermediate / PUC: percentage or grade",
    "school.class_12.year": "Class 12 / HSC: year of passing",
    "school.class_12.board": "Class 12 / HSC: board (CBSE, ICSE, state board...)",
    "school.class_12.school": "Class 12 / HSC: school or junior college name",
    "school.class_12.stream": "Class 12 / HSC: stream (Science, Commerce, Arts / PCM...)",
}


@dataclass
class Fact:
    key: str
    value: str
    about: str
    kind: str = "text"  # text | date


def safe_id(key):
    """Choice ids stay [A-Za-z0-9_]; the readable key survives with dots replaced."""
    return re.sub(r"[^A-Za-z0-9_]", "_", key.replace(".", "__"))


def as_text(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def describe(key):
    if key in KNOWN:
        return KNOWN[key]
    pattern = INDEX.sub(".*", key)
    if pattern in KNOWN:
        return KNOWN[pattern].format(n=int(INDEX.search(key).group(1)) + 1)
    return key.replace("_", " ").replace(".", " › ")


def flatten(node, prefix=""):
    if isinstance(node, dict):
        if "value" in node and set(node) <= {"value", "about"}:
            yield prefix, node["value"], node.get("about")
            return
        for key, child in node.items():
            if not key.startswith("_"):
                yield from flatten(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(node, list):
        if all(not isinstance(item, (dict, list)) for item in node):
            items = [as_text(item) for item in node if item not in (None, "")]
            yield prefix, ", ".join(items), f"{describe(prefix)} — all of them as ONE comma-separated text"
            if 1 < len(items) <= MAX_ITEMS:  # tag fields take one value per entry
                for n, item in enumerate(items, 1):
                    about = (
                        f"{describe(prefix)} — ONE item ({n} of {len(items)}), for fields that add items "
                        "one at a time (tags, chips, multi-select)"
                    )
                    yield f"{prefix}.item_{n}", item, about
        else:
            for index, item in enumerate(node):
                yield from flatten(item, f"{prefix}.{index}")
    else:
        yield prefix, node, None


def date_variants(fact):
    y, m, d = DATE.match(fact.value).groups()
    variants = {
        ".mm_yyyy": (f"{m}/{y}", "formatted MM/YYYY"),
        ".month_year": (f"{MONTHS[int(m) - 1]} {y}", "as 'Month YYYY'"),
        ".year": (y, "year only"),
        ".mm": (m, "month number only, MM"),
    }
    if d:
        variants[".dd_mm_yyyy"] = (f"{d}/{m}/{y}", "formatted DD/MM/YYYY")
        variants[".mm_dd_yyyy"] = (f"{m}/{d}/{y}", "formatted MM/DD/YYYY (US order)")
    return [Fact(fact.key + suffix, value, f"{fact.about} — {how}") for suffix, (value, how) in variants.items()]


def money(amount):
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def format_date(value, order, sep):
    """An ISO profile date in the order a text field asks for: order like ['dd', 'mm', 'yyyy'] or
    ['month', 'yyyy'], sep '/', '-', '.' or ' '. None when the date lacks a part the field needs (the day)."""
    match = DATE.match(value or "")
    if not match:
        return None
    y, m, d = match.groups()
    parts = {"yyyy": y, "yy": y[2:], "mm": m, "dd": d, "month": MONTHS[int(m) - 1], "mmm": MONTHS[int(m) - 1][:3]}
    if any(parts.get(p) is None for p in order):
        return None
    return sep.join(parts[p] for p in order)


def undouble(text):
    """'Q? Q?' -> 'Q?': sites repeat a question in hidden text for screen readers."""
    words = str(text or "").replace("*", " ").split()
    half = len(words) // 2
    if half and len(words) % 2 == 0 and words[:half] == words[half:]:
        return " ".join(words[:half])
    return " ".join(words)


PLACEHOLDER_TAIL = re.compile(
    r"(\s*[*✱])?\s*(select\s*(one|an option)?|choose\s*(one|an option)?|please select|pick one|--|\d+ items? selected)"
    r"\s*\.*\s*$",
    re.I,
)


def same_question(a, b):
    """Is this the same question, asked again (another form, another job)? Whole text, or the part before the
    context ('Notice period' of 'Notice period — Select...'), ignoring case, punctuation and '*'."""

    def forms(q):
        q = undouble(q)
        # A dropdown's placeholder read with its label ("Current Salary Select...") is not part of the question.
        q = PLACEHOLDER_TAIL.sub("", str(q))
        whole = re.sub(r"[^0-9a-z]", "", str(q).lower())
        head = re.sub(r"[^0-9a-z]", "", str(q).split(" — ")[0].lower())
        return {x for x in (whole, head) if len(x) >= 8}

    return bool(forms(a) & forms(b))


def answers(saved, asked):
    """Does a saved answer to `saved` answer `asked`? The same question, or a years-of-experience question about
    the same skill in other words ('...with React?' answers '...experience in React.js')."""
    if same_question(saved, asked):
        return True
    from .skills import asked_skill

    skill = asked_skill(asked)
    return bool(skill) and asked_skill(saved) == skill


def native_date(value, input_type):
    """Convert an ISO profile date for <input type=date|month>. Returns (value, note) or None."""
    match = DATE.match(value or "")
    if not match:
        return None
    y, m, d = match.groups()
    if input_type == "month":
        return f"{y}-{m}", None
    if input_type == "date":
        return (f"{y}-{m}-{d}", None) if d else (f"{y}-{m}-01", "day not in profile; used the 1st")
    if input_type == "week":
        from datetime import date

        year, week, _ = date(int(y), int(m), int(d or 1)).isocalendar()
        return f"{year}-W{week:02d}", None if d else "day not in profile; used the week of the 1st"
    return None  # time / datetime-local: a time of day is never in a profile


class Profile:
    def __init__(self, data, base_dir=Path("."), answers=(), learned_path=None, questions_path=None):
        self.learned_path = Path(learned_path) if learned_path else None
        # QUESTIONS.md: what the agent couldn't answer, for you to answer in the file.
        self.questions_path = (
            Path(questions_path)
            if questions_path
            else (self.learned_path.with_name("QUESTIONS.md") if self.learned_path else None)
        )
        self.documents, self.missing_documents = {}, {}
        for name, entry in (data.get("documents") or {}).items():
            entry = entry if isinstance(entry, dict) else {"path": entry}
            path = Path(os.path.expanduser(entry["path"]))
            path = path if path.is_absolute() else (base_dir / path).resolve()
            target = self.documents if path.is_file() else self.missing_documents
            target[name] = Fact(name, str(path), entry.get("about") or name.replace("_", " "))
        facts = {}
        for key, value, about in flatten({k: v for k, v in data.items() if k != "documents"}):
            if value is None or as_text(value) == "":
                continue
            facts[key] = Fact(key, as_text(value), about or describe(key), "item" if ITEM.search(key) else "text")
        self.facts = self.derive(facts, str(data.get("compensation", {}).get("currency", "INR")).upper())
        self.saved = {}  # question -> your answer, from answers.json and learned.json
        for entry in answers:
            key = f"answer.{entry['id']}"
            self.facts[key] = Fact(key, entry["answer"].strip(), f"Your saved answer to: {entry['question']}")
            self.saved[entry["question"]] = entry["answer"].strip()
        if len(self.facts) > source_limit():
            raise ValueError(f"{len(self.facts)} facts + answers; the limit is {source_limit()}. Trim unused fields.")
        self.by_id = {safe_id(k): f for k, f in self.facts.items()}

    @staticmethod
    def derive(facts, currency):
        first, last = facts.get("personal.first_name"), facts.get("personal.last_name")
        if first and last and "personal.full_name" not in facts:
            facts["personal.full_name"] = Fact(
                "personal.full_name", f"{first.value} {last.value}", describe("personal.full_name")
            )
        fixed, variable = (
            facts.get("compensation.current.fixed_annual"),
            facts.get("compensation.current.variable_annual"),
        )
        if fixed and "compensation.current.total_annual" not in facts:
            total = float(fixed.value) + (float(variable.value) if variable else 0)
            facts["compensation.current.total_annual"] = Fact(
                "compensation.current.total_annual", money(total), describe("compensation.current.total_annual")
            )
        phone = facts.get("personal.phone")
        split = re.fullmatch(r"(\+\d{1,3})[\s-]+([\d\s-]{6,})", phone.value) if phone else None
        if split is None and phone:
            split = re.fullmatch(r"(\+91)(\d{10})", re.sub(r"[\s-]", "", phone.value))
        if split:
            for key, value in (("personal.phone_country_code", split[1]), ("personal.phone_local", split[2])):
                if key not in facts:
                    facts[key] = Fact(key, re.sub(r"[\s-]", "", value), describe(key))
        for kind in ("total", "relevant"):
            years = facts.get(f"work.{kind}_experience_years")
            try:
                amount = float(years.value) if years else None
            except ValueError:
                amount = None
            if amount is not None:
                months = round(amount * 12)
                for suffix, value in (("months", months), ("whole_years", months // 12), ("extra_months", months % 12)):
                    key = f"work.{kind}_experience_{suffix}"
                    if key not in facts:
                        facts[key] = Fact(key, str(value), describe(key))
        notice = facts.get("work.notice_period_days")
        if notice and "work.can_join_immediately" not in facts and re.fullmatch(r"\d+", notice.value):
            key = "work.can_join_immediately"
            facts[key] = Fact(key, "Yes" if int(notice.value) == 0 else "No", describe(key))
        backlogs = facts.get("academics.active_backlogs")
        if backlogs and "academics.has_active_backlogs" not in facts and backlogs.value.isdigit():
            key = "academics.has_active_backlogs"
            facts[key] = Fact(key, "Yes" if int(backlogs.value) else "No", describe(key))
        derived = {}
        for key, fact in facts.items():
            if re.fullmatch(r"compensation\.\w+\.\w+_annual", key):
                amount = float(fact.value.replace(",", ""))
                fact.value = money(amount)
                fact.about += f" — full annual amount in {currency}"
                if currency == "INR":
                    derived[key + "_lpa"] = Fact(
                        key + "_lpa",
                        money(amount / 100000),
                        describe(key) + " — in LAKHS per annum (LPA), e.g. 18 = ₹18,00,000",
                    )
                derived[key + "_monthly"] = Fact(
                    key + "_monthly",
                    money(round(amount / 12)),
                    describe(key) + f" — PER MONTH in {currency} (annual ÷ 12)",
                )
            elif DATE.match(fact.value):
                fact.kind = "date"
                fact.about += " — ISO date"
                derived.update({f.key: f for f in date_variants(fact)})
        return {**facts, **derived}

    @classmethod
    def load(cls, profile_path, answers_path=None, learned_path=None, questions_path=None):
        from . import inbox

        profile_path = Path(profile_path)
        data = json.loads(profile_path.read_text(encoding="utf-8"))
        answers = []
        for path in (answers_path, learned_path):
            if path and Path(path).is_file():
                answers += json.loads(Path(path).read_text(encoding="utf-8")).get("answers", [])
        questions_path = questions_path or inbox.path_for(profile_path.parent)
        answers += [
            {"id": f"md_{n}", "question": entry["question"], "answer": entry["answer"]}
            for n, entry in enumerate(inbox.read(questions_path), 1)
            if entry["answer"]
        ]
        return cls(data, profile_path.parent, answers, learned_path, questions_path)

    def sources(self, dates_only=False, exclude=()):
        """Choice id -> Fact. Dates-only fields may use ISO date facts only; `exclude` drops items already tried."""
        return {i: f for i, f in self.by_id.items() if (not dates_only or f.kind == "date") and i not in exclude}

    def candidate_state(self):
        """What the model sees to choose dropdown/radio options, plus your saved answers ('saved: <question>')."""
        state = {
            f.key: f.value[:200] for f in self.facts.values() if not f.key.startswith("answer.") and f.kind != "item"
        }
        state.update({f"saved: {q}": a[:200] for q, a in self.saved.items()})
        state.update({f"document.{name}": Path(doc.value).name for name, doc in self.documents.items()})
        return state

    def saved_answer(self, question):
        """Your saved answer to this question if you answered it before (here or on another form), else None."""
        return next((a for q, a in self.saved.items() if answers(q, question)), None)

    def add_pending(self, entries, done=()):
        """Questions an unattended run couldn't answer, added (once each) to QUESTIONS.md. Returns how many are open.
        `done`: questions this run filled from your profile or a draft; still-open entries for them are dropped, so
        the file lists only what really needs you."""
        from . import inbox

        path = self.questions_path
        if not path:
            return 0
        known = inbox.read(path)
        before = len(known)
        known = [q for q in known if q.get("answer") or not any(same_question(q["question"], d) for d in done)]
        for entry in entries:
            if self.saved_answer(entry["question"]):
                continue
            same = next((q for q in known if same_question(q["question"], entry["question"])), None)
            if same:
                same["urls"] = list(dict.fromkeys(same.get("urls", []) + entry.get("urls", [])))[-5:]
                same["options"] = same.get("options") or entry.get("options", [])
            else:
                known.append(
                    {
                        "question": undouble(entry["question"]),
                        "options": entry.get("options", []),
                        "urls": entry.get("urls", []),
                        "answer": "",
                    }
                )
        if entries or len(known) != before:
            inbox.write(path, known)
        return sum(1 for q in known if not q.get("answer"))

    def remember(self, question, answer):
        """Save an answer you typed so the next form fills it without asking."""
        if not self.learned_path:
            return
        path = self.learned_path
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"answers": []}
        entry = {"id": f"learned_{len(data['answers']) + 1}", "question": question, "answer": answer}
        data["answers"].append(entry)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        key = f"answer.{entry['id']}"
        self.facts[key] = Fact(key, answer, f"Your saved answer to: {question}")
        self.by_id[safe_id(key)] = self.facts[key]
        self.saved[question] = answer
