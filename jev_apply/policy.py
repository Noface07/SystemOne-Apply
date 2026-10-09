"""Guards that code enforces regardless of what any model decides."""

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

# The guards live in code so they can never silently go missing. data/policy.json only overrides them.
CONFIRM = [
    "gender",
    "sex",
    "pronoun",
    "race",
    "ethnic",
    "caste",
    "category",
    "religion",
    "disab",
    "veteran",
    "criminal",
    "convict",
    "arrest",
    "sponsor",
    "visa",
    "work authori",
    "authorized to work",
    "authorised to work",
    "date of birth",
    "birth",
    "age",
    "marital",
    "salary",
    "ctc",
    "compensation",
    "pay",
    "package",
    "background check",
    "i agree",
    "i certify",
    "i confirm",
    "i acknowledge",
    "declare",
    "consent",
    "terms",
]
NEVER_FILL = [
    "password",
    "otp",
    "one-time",
    "aadhaar",
    "aadhar",
    "pan card",
    "pan number",
    "permanent account number",
    "passport number",
    "social security",
    "ssn",
    "bank",
    "account number",
    "ifsc",
    "credit card",
    "debit card",
    "cvv",
    "uan",
]
# "I'm interested" opens a form as Apply does (Zoho Recruit), so it is held back the same way once a form is open.
SUBMIT = [
    "submit", "send application", "apply", "finish", "complete", "confirm and", "send", "i'm interested",
    "i’m interested", "i am interested",
]  # fmt: skip
# auto_consent: a required checkbox that only consents to a privacy notice or the site's terms may be ticked...
CONSENT = [
    "privacy policy", "privacy notice", "privacy statement", "data privacy", "data protection", "personal data",
    "terms and conditions", "terms & conditions", "terms of use", "terms of service",
]  # fmt: skip
# ...unless it also declares something about you: those go to QUESTIONS.md, and your answer there is reused.
DECLARATION = [
    "criminal", "convict", "arrest", "offence", "offense", "background", "true", "accurate", "correct", "certify",
    "falsif", "misrepresent", "drug", "relative", "conflict", "non-compete", "non-disclosure", "bond", "arbitrat",
    "sponsor", "visa", "authori", "disab", "veteran", "gender", "caste", "religion", "salary", "notice period",
]  # fmt: skip


def compile_patterns(words):
    # Match at the start of a word: "disab" catches "disability", "pan card" does not catch "company".
    return re.compile("|".join(r"(?<![a-z0-9])" + re.escape(w.lower()) for w in words) or r"(?!x)x", re.I)


NAVIGATION = re.compile(r"(next|continue|review|back|previous|proceed|save (and|&) continue)( step)?")


@dataclass
class Policy:
    min_operation_probability: float = 0.6
    min_target_probability: float = 0.6
    min_value_margin: float = 0.3
    drafts: str = "confirm"  # confirm: draft, then you edit/accept | ask: never draft, always ask you
    max_actions: int = 200
    # Naukri has no separate submit: its Apply (and the last answer of its questionnaire) sends the application.
    # assist: stop at Apply; after you click it, answers are typed and you confirm each Save.
    # auto: click Apply and answer the questionnaire from your profile; unsure questions are left, unapplied.
    # company_site: only follow "Apply on company site"; never Naukri's own Apply.
    naukri_apply: str = "assist"
    # Submitting: off unless you turn it on. When on, an unattended run clicks the final Submit only for a complete
    # application (nothing required empty or invalid, nothing left for you, no open question, no unread draft
    # unless submit_drafts) and never twice for one job (data/applied.json). A watched run asks you first.
    auto_submit: bool = False
    submit_drafts: bool = False
    # Unattended runs that reach a sign-in page alert you, show the tab and wait this long for you to sign in (or
    # create the account) before stopping the job. 0 stops at once. The agent never types into a login page.
    login_wait_s: int = 900
    # Unattended runs about to stop on required questions nothing on file answers put them in QUESTIONS.md at once
    # and wait this long for you to answer them (in the app's Questions page or the file). All answered: the run
    # carries on with the same form, using your answers. Otherwise the job stops as before and is offered for a
    # re-run once you answer. 0 stops at once.
    answer_wait_s: int = 300
    # At a "Continue with Google" sign-in: click it, choose the Google account whose address is your profile's e-mail
    # and confirm Google's "share your name, email and picture" page. Clicks only: nothing is ever typed into Google
    # (an e-mail, password or code request waits for you), and wider access (Drive, Gmail...) is left for you.
    google_sign_in: bool = False
    # After auto_submit clicks Submit: how long to watch for the site's own confirmation before recording the job
    # as "submitted (no confirmation seen)". Nothing is clicked while waiting.
    confirm_wait_s: int = 15
    # Spread applications across companies: at most `per_company` in any `company_gap_days` window (from
    # applied.json and within one batch). Jobs over the limit are deferred, listed, not dropped. 0 turns it off.
    per_company: int = 1
    company_gap_days: int = 14
    # Tick required privacy / terms consent boxes in unattended runs (off unless you turn it on). A box that also
    # matches declaration_patterns is never ticked this way: it becomes a Yes/No question in QUESTIONS.md.
    auto_consent: bool = False
    consent_patterns: list = field(default_factory=lambda: list(CONSENT))
    declaration_patterns: list = field(default_factory=lambda: list(DECLARATION))
    learned_answers: str = "learned.json"
    confirm_patterns: list = field(default_factory=lambda: list(CONFIRM))
    never_fill_patterns: list = field(default_factory=lambda: list(NEVER_FILL))
    submit_patterns: list = field(default_factory=lambda: list(SUBMIT))

    def __post_init__(self):
        if not self.submit_patterns:
            raise ValueError("policy.submit_patterns cannot be empty: it is what stops the agent before submitting")
        if self.naukri_apply not in {"assist", "auto", "company_site"}:
            raise ValueError("policy.naukri_apply must be 'assist', 'auto' or 'company_site'")
        if self.drafts not in {"confirm", "ask"}:
            raise ValueError("policy.drafts must be 'confirm' or 'ask'; drafts are never typed unseen")
        self._confirm = compile_patterns(self.confirm_patterns)
        self._never = compile_patterns(self.never_fill_patterns)
        self._submit = compile_patterns(self.submit_patterns)
        self._consent = compile_patterns(self.consent_patterns)
        self._declaration = compile_patterns(self.declaration_patterns)

    @classmethod
    def load(cls, path=None):
        values = json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).is_file() else {}
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in values.items() if k in names})

    @staticmethod
    def text(action):
        return " ".join(str(action.get(k) or "") for k in ("label", "context", "help", "placeholder"))

    def sensitive(self, action):
        # A button that only moves between steps (Next, Review...) carries no answer: its own label decides, not
        # the step around it ("Work authorization" heads LinkedIn's step; its Review button declares nothing).
        label = str(action.get("label") or "")
        if action.get("role") in {"button", "link"} and NAVIGATION.fullmatch(label.strip().lower()):
            return self._confirm.search(label)
        return self._confirm.search(self.text(action))

    def plain_consent(self, action):
        """A checkbox that only agrees to a privacy notice or terms, with nothing declared about you."""
        text = self.text(action)
        return (
            action.get("role") == "checkbox" and bool(self._consent.search(text)) and not self._declaration.search(text)
        )

    def never_fill(self, action):
        return self._never.search(self.text(action))

    def submit_like(self, action):
        # Buttons and links only, judged by their own label: "Apply" in a paragraph is not a submit button.
        return action.get("role") in {"button", "link"} and self._submit.search(action.get("label", ""))
