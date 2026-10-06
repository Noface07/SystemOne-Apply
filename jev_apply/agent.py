"""The loop: observe -> the model picks operation + target -> resolve the value -> guarded execution -> read back.

Invariants (inherited from jev-ultrafast, plus two for applications):
  - Targets are observed nodes; values are profile facts, saved answers, your typing, or drafts you approved.
  - A browser mutation is never retried. Execution is logged before the next observation.
  - A value resolved for a field survives a stale-page retry only for the identical field.
  - The agent never clicks a final submit button; it stops and hands the form to you.
  - Sensitive questions, never-fill fields, low confidence and close calls always come to you.
"""

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

from . import applied, model, planner, rules
from .browser import ADVANCING, ExecutionUncertain, StalePage
from .profile import native_date, safe_id, same_question
from .questions import GOAL, SPECIAL

FILLS = {"fill", "select", "setdate", "upload"}
# What sites say once an application went through.
# A challenge a person must answer before the form sends: hCaptcha's frame or its response field.
CAPTCHA_CHECK = (
    '!!document.querySelector(\'iframe[src*="hcaptcha.com"],textarea[name="h-captcha-response"],'
    "[data-hcaptcha-widget-id],.h-captcha')"
)
SUBMITTED = re.compile(
    r"thank you for (applying|your application|your interest)|application (was |has been )?(sent|submitted|received)|"
    r"successfully (applied|submitted)|we'?ve received your application|your application is (in|on its way)|"
    r"thanks? (you )?for (applying|submitting)|we have received your application|application (is )?complete",
    re.I,
)
# Buttons that finish an application whatever page they are on. Never clicked unattended.
FINAL = re.compile(r"submit|send|finish|complete|confirm|done|review and|final", re.I)


class Stop(Exception):
    """You chose to stop."""


def d_of(action):
    """The decision record for an action the agent itself decided on (a policy you set), not a model."""
    return {"operation": "CLICK", "operation_probability": 1.0, "target_probability": 1.0, "latency_ms": 0}


def norm(value):
    return re.sub(r"[^0-9a-z]", "", str(value or "").lower())


def form_state(page):
    """What a form shows, without its text and scroll position: a ticking clock or a carousel is not a change."""
    keys = ("kind", "role", "label", "value", "current_value", "checked", "pressed", "chosen", "expanded", "invalid")
    content = [page.get("url"), [[a.get(k) for k in keys] for a in page.get("actions", [])]]
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


class Agent:
    def __init__(self, browser, profile, policy, ui, *, note="", run_dir="runs", answers=None, company=""):
        self.browser, self.profile, self.policy, self.ui = browser, profile, policy, ui
        self.answers, self.company = dict(answers or {}), company
        self.goal = GOAL + (f"\nCandidate's note: {note.strip()}" if note and note.strip() else "")
        self.run_dir = Path(run_dir)
        self.history, self.fills, self.decisions, self.handovers, self.asked = [], [], [], [], []
        self.skipped, self.vetoed = set(), set()
        self.items = {}  # (doc, node) -> item facts already typed into that tag field: never offered there again
        # Nobody at the keyboard: decide safely, list the rest. Read from the class so stub UIs don't fake it.
        self.unattended = getattr(type(ui), "unattended", False) is True
        self.left = []  # what was left for you in unattended mode
        self.no_review = set()  # documents where stopping for review was refused (required fields still empty)
        self.via_rule = False  # the value being typed now came from rules.py, not a model
        # (doc, question) -> nodes of radio/button groups left for you in unattended mode. The nodes keep a later
        # step's question with the same words (one document for a whole Workday application) from being skipped.
        self.skipped_questions = {}
        self.optional_skipped = []  # unattended: optional fields no fact answers, left empty
        self.inbox = []  # unattended: required questions left for you, saved for `jev-apply answer`
        self.pending = None
        self.status = "ready"
        self.started = time.perf_counter()
        self.page = browser.observe()
        self.start_url = self.page["url"]

    # ---- loop -------------------------------------------------------------------------------------------------

    def run(self):
        done = applied.submitted(self.applied_path(), self.start_url) if self.policy.auto_submit else None
        if done:
            self.ui.event(f"Already submitted on {done['at']}: skipped.")
            self.status = "already applied"
            return self.finish()
        # Answers this job's own form definition gives (Greenhouse / Lever / Ashby): each is an option your profile
        # already answers, in the form's exact words. Used for this run only, never saved.
        added = {q: a for q, a in self.answers.items() if not self.profile.saved_answer(q)}
        self.profile.saved.update(added)
        try:
            while self.status == "ready":
                self.tick()
        except Stop:
            self.status = "stopped"
        finally:
            for question in added:
                self.profile.saved.pop(question, None)
        return self.finish()

    def tick(self):
        try:
            self.step()
        except StalePage:
            self.reobserve()
        except ExecutionUncertain as error:
            self.handover(str(error))
        except (RuntimeError, ValueError) as error:  # provider/validation failure: nothing was executed
            self.handover(f"{error} You can fix it in the browser or just continue to retry the decision.")

    def step(self):
        acted = sum(h.get("kind") != "wait" for h in self.history)  # waiting for slow pages isn't an action
        if acted >= self.policy.max_actions or len(self.decisions) >= self.policy.max_actions * 3:
            self.status = "budget"
            return
        if not self.browser.fresh(self.page):
            self.page = self.browser.observe()
        offered = {**self.page, "actions": [a for a in self.page["actions"] if not self.excluded(a)]}
        exclude = ("REVIEW",) if self.page.get("doc") in self.no_review else ()
        d = model.choose(offered, self.goal, self.history, self.profile.candidate_state(), exclude)
        self.decisions.append(
            {k: d[k] for k in ("operation", "target", "operation_probability", "target_probability", "latency_ms")}
            | {"url": self.page["url"]}
        )
        operation, action = d["operation"], d["action"]
        if operation == "REVIEW":
            if not self.browser.fresh(self.page):
                raise StalePage("Page changed since the decision")
            empty = self.empty_required()
            if empty and (
                self.unattended
                or not self.ui.approve(
                    "Stop here for your review", f"the model thinks it's done, but these are empty: {', '.join(empty)}"
                )
            ):
                self.no_review.add(self.page.get("doc"))  # keep filling this page
                return
            self.status = "review"
            return
        if operation == "APPLIED":
            self.status = "applied"  # Naukri, auto-apply on: the site says the application went through
            applied.record(
                self.applied_path(),
                [self.start_url, self.page["url"]],
                "applied",
                self.page.get("title", ""),
                company=self.company,
            )
            return
        if operation == "BLOCKED":
            if planner.sign_in_wall(self.page) and self.unattended and self.wait_for_login():
                return  # signed in: carry on from the form
            self.handover(
                d.get("reason")
                or "The agent needs you here: a login, CAPTCHA/OTP, a question your profile can't answer, or the "
                "site reporting missing or invalid fields (see the red messages on the page)."
            )
            return
        low = (
            d["operation_probability"] < self.policy.min_operation_probability
            or (d["target_probability"] is not None and d["target_probability"] < self.policy.min_target_probability)
            # A model's pick only just ahead of the next option is a close call, however sure it sounds.
            or (d.get("target_margin") is not None and d["target_margin"] < self.policy.min_value_margin)
        )
        repeats = (  # tag fields take an item per entry and "Add another" a block per job: those repeat by design
            action
            and action.get("kind") not in {"wait", "scroll", "key"}
            and not action.get("multiple")
            and not planner.ADD.search(action.get("label", ""))
        )
        if repeats and self.repeated(action) >= 3:
            # Done three times on this page and still proposed: the page doesn't take it. Drop it and go on.
            self.vetoed.add((*self.here(action), action["label"]))
            self.ui.event(f"'{action['label'][:60]}' was done three times without effect; leaving it for you.")
            self.left.append(
                {"what": f"'{action['label'][:80]}'", "why": "the page didn't take it", "url": self.page["url"]}
            )
            return
        handler = {
            "click": self.do_click,
            "fill": self.do_fill,
            "select": self.do_select,
            "setdate": self.do_date,
            "upload": self.do_upload,
        }.get(action["kind"], self.do_control)
        handler(action, d, low)

    def repeated(self, action):
        """How often this very action (same control, question and page) was already carried out."""
        context = (action.get("context") or "")[:160]
        return sum(
            1
            for h in self.history
            if h.get("kind") == action["kind"]
            and h.get("action") == action["label"]
            and h.get("context") == context
            and h.get("url") == self.page["url"]
        )

    def empty_required(self):
        """Required questions on this page still without an answer (text, dates, files, dropdowns, radio and
        Yes/No groups, checkboxes) that weren't deliberately left for you."""
        shown = [m[:40] for m in planner.unanswered([a for a in self.page["actions"] if not self.excluded(a)])]
        # ...and those above or below the screen, which the page reports on its own.
        return shown + [m[:40] for m in self.page.get("required_empty", []) if m[:40] not in shown]

    def consents(self, action):
        """auto_consent: an unticked box that only agrees to a privacy notice or the site's terms."""
        return self.policy.auto_consent and action.get("checked") == "false" and self.policy.plain_consent(action)

    def declaration(self, action):
        """A single checkbox (not one of a group of options): 'I confirm I have no criminal record'."""
        return (
            action.get("kind") == "click"
            and action.get("role") == "checkbox"
            and (
                not action.get("context")  # no question around it: it stands alone
                or len([o for o in self.group_of(action) if o.get("role") == "checkbox"]) < 2
            )
        )

    def matches_profile(self, action):
        """A choice (dropdown option, radio, Yes/No button) whose text is exactly what your profile says for
        that question, per the fixed rules: e.g. 'No' for 'Do you require visa sponsorship?'. A single checkbox
        matches when you answered Yes to it in QUESTIONS.md ('Tick this box? ...')."""
        if self.declaration(action):
            saved = self.profile.saved_answer(planner.declaration_question(action))
            return action.get("checked") == "false" and norm(saved or "") in {"yes", "tick", "agree", "true"}
        if action["kind"] == "select":
            question, option = action["label"].split(" → ", 1)[0], action["label"].split(" → ", 1)[-1]
        elif action["kind"] == "click" and action.get("context") and action.get("role") != "link":
            question, option = action["context"], action["label"]
        else:
            return False
        if planner.eeo_decline(question, option, self.profile.candidate_state()):
            return True
        group = self.group_of(action)
        facts = planner.Facts(self.profile.candidate_state())
        if any(o.get("id") == action.get("id") for o in planner.range_answer(question, group, facts)):
            return True  # "16-20 LPA" for your 17 LPA: computed from your profile, not guessed
        saved = self.profile.saved_answer(planner.question_of(question, group))
        if saved and norm(saved) == norm(option):
            return True  # the answer you saved for this question
        key = rules.key_for({"label": question, "context": question}, self.profile, set())
        for k in key if isinstance(key, tuple) else (key,):
            fact = self.profile.by_id.get(safe_id(k)) if k and not k.isupper() else None
            if fact and norm(fact.value) == norm(option):
                return True
        return False

    def group_of(self, action):
        """The other options of the question an option belongs to (same dropdown, or same question context)."""
        if action["kind"] == "select":
            return [a for a in self.page["actions"] if a["kind"] == "select" and a.get("node") == action.get("node")]
        return [a for a in self.page["actions"] if a["kind"] == "click" and a.get("context") == action.get("context")]

    def looks_final(self, action):
        """Unattended, nobody to ask: is this submit-like button the final submit? Anything that says submit,
        send or finish always is (a review page has no fields but its Submit is final). Only an 'Apply'-style
        button on a page without fields (a job posting) is treated as opening the form."""
        if FINAL.search(action.get("label", "")):
            return True
        return any(a["kind"] in FILLS and not a.get("nav") for a in self.page["actions"])

    def here(self, action):
        # Node ids restart in every new document, so skips and vetoes belong to one document only. A single-page
        # app may reuse a node for another step's field: the question keeps them apart.
        return (self.page.get("doc"), action.get("node"), action.get("label", "").split(" → ")[0])

    def excluded(self, action):
        if (*self.here(action), action["label"]) in self.vetoed:
            return True
        nodes = self.skipped_questions.get((self.page.get("doc"), action.get("context")))
        if action["kind"] == "click" and nodes and action.get("node") in nodes:
            states = (action.get("pressed"), action.get("chosen"))
            return action.get("role") in {"radio", "option", "checkbox"} or "true" in states or "false" in states
        if action["kind"] == "click" and action["label"].startswith("Open "):
            # The click that opens a text field's suggestions goes with the field: skipped together.
            return (self.page.get("doc"), action.get("node"), action["label"][5:]) in self.skipped
        return action["kind"] in {"fill", "setdate", "upload", "select"} and self.here(action) in self.skipped

    # ---- operations -----------------------------------------------------------------------------------------

    def naukri_apply(self, action):
        """Naukri's own Apply button (it sends the application at once), handled per policy.naukri_apply.
        Returns True when handled here (clicked or stopped)."""
        if "naukri.com" not in self.page["url"] or not planner.NAUKRI_APPLY.search(action.get("label", "")):
            return False
        mode = self.policy.naukri_apply
        if self.policy.auto_submit and mode == "assist":
            mode = "auto"  # submitting is on: Naukri's own Apply is part of it (unless naukri_apply is company_site)
        if mode == "auto":
            self.execute(action, d_of(action), source="applied on Naukri (naukri_apply: auto)")
            return True
        if re.search(r"company site", action.get("label", ""), re.I):
            return False  # opens the employer's own form (Naukri marks the job 'Applied' when this is followed)
        why = (
            "Naukri's own Apply sends your application at once. naukri_apply is 'company_site': only 'Apply on "
            "company site' is followed."
            if mode == "company_site"
            else "Naukri's Apply sends your application at once, so it's yours to click (naukri_apply: assist). "
            "Click it, then continue: the agent answers the questionnaire and you confirm each Save."
        )
        self.left.append({"what": f"Click '{action['label']}' on Naukri", "why": why, "url": self.page["url"]})
        if self.unattended:
            self.status = "review"
        else:
            self.handover(why)
        return True

    def do_click(self, action, d, low):
        if self.naukri_apply(action):
            return
        if self.policy.submit_like(action):
            if self.looks_final(action) if self.unattended else self.ui.is_final_submit(action["label"]):
                if self.policy.auto_submit and self.submit(action, d):
                    return
                self.log(action, d, source="stopped before submit")
                for question in self.empty_required():
                    self.left.append(
                        {"what": f"Answer '{question}'", "why": "required, still empty", "url": self.page["url"]}
                    )
                self.status = "review"
                return
        else:
            self.via_rule = self.matches_profile(action) or self.consents(action)
            if not self.confirm(action, d, low, f"Click '{action['label']}'"):
                return
        self.execute(action, d)

    def unanswered_entries(self):
        """The questions still open on the page where the run ended (required ones, and Yes/No questions the agent
        declined to guess), with their options, for QUESTIONS.md."""
        actions = [a for a in self.page["actions"] if not self.excluded(a) and not a.get("nav")]
        entries, seen = [], set()
        for a in actions:
            if a.get("required") and a.get("checked") == "false" and self.declaration(a) and not self.consents(a):
                question = planner.declaration_question(a)
                if question not in seen:
                    seen.add(question)  # answered Yes, it is ticked on every later form; No leaves it
                    entries.append({"question": question, "options": ["Yes", "No"], "urls": [self.page["url"]]})
                continue
            if planner.is_choice(a) and a.get("context") and a["context"] not in seen:
                seen.add(a["context"])
                group = [o for o in actions if planner.is_choice(o) and o.get("context") == a["context"]]
                options = list(dict.fromkeys(planner.option_text(o) for o in group))
                if planner.resume_cards(group):
                    continue  # a list of saved résumés, not a question
                binary = all(re.fullmatch(r"yes|no|true|false|agree|disagree", norm(o)) for o in options)
                if (a.get("required") or binary) and not any(planner.is_on(o) for o in group):
                    question = planner.question_of(a["context"], group)
                    entries.append({"question": question, "options": options, "urls": [self.page["url"]]})
            elif a["kind"] == "select" and a.get("required") and a.get("node") not in seen:
                seen.add(a.get("node"))
                if planner.PLACEHOLDER.search(a.get("current_value") or ""):
                    group = [o for o in actions if o["kind"] == "select" and o.get("node") == a.get("node")]
                    options = [planner.option_text(o) for o in group][:30]
                    entries.append(
                        {"question": a["label"].split(" → ")[0], "options": options, "urls": [self.page["url"]]}
                    )
            elif a["kind"] == "fill" and a.get("required") and not a.get("value") and not a.get("multiple"):
                entries.append({"question": self.question(a), "options": [], "urls": [self.page["url"]]})
        # Above or below the screen when the run ended: the page lists its unanswered required questions itself.
        for q in self.page.get("open_questions", []):
            if q.get("question") and not any(same_question(q["question"], e["question"]) for e in entries):
                entries.append({"question": q["question"], "options": q.get("options", []), "urls": [self.page["url"]]})
        return entries

    def applied_path(self):
        folder = self.profile.questions_path.parent if self.profile.questions_path else None
        return folder / "applied.json" if folder else None

    def not_ready(self):
        """Why this application must not be submitted yet (empty list: it's complete)."""
        why = [f"'{q}' is required and still empty" for q in self.empty_required()]
        why += [f"'{f}' is marked invalid" for f in planner.invalid_fields(self.page["actions"])]
        why += [f"{x['what']} ({x['why']})" for x in self.left]
        why += [f"open question: {q['question'][:60]}" for q in self.inbox]
        why += [f"'{f['label'][:40]}' didn't keep its value" for f in self.fills if f["confirmed"] is False]
        if not self.policy.submit_drafts:
            why += [f"draft to read: {f['label'][:40]}" for f in self.fills if f["source"] == "draft"]
        if self.handovers:
            why.append("the run needed you along the way")
        return why

    def submit(self, action, d):
        """auto_submit: click the final Submit when the application is complete; True when it was clicked."""
        why = self.not_ready()
        if why:
            self.left.append({"what": "Not submitted", "why": "; ".join(why)[:300], "url": self.page["url"]})
            return False
        if not self.unattended and not self.ui.approve(f"Submit this application now ('{action['label']}')?", ""):
            return False
        if self.needs_a_person():
            # Lever (and any hCaptcha form): Submit opens a challenge only a person can answer. Leave the filled
            # form for you, say so, and let the batch move on.
            self.browser.show()
            self.ui.event(
                f"\a!!! SUBMIT YOURSELF: '{action['label']}' needs a captcha only you can solve. The form is filled "
                f"in the open tab: {self.page['url'][:120]}"
            )
            self.left.append({"what": f"Click '{action['label']}' and solve the captcha", "why": "captcha",
                              "url": self.page["url"]})  # fmt: skip
            self.status = "ready to submit (captcha)"
            return True
        before = self.page
        self.execute(action, d, source="submitted (auto_submit)")
        refused = planner.invalid_fields(self.page["actions"]) or (
            planner.ERRORS.search(self.page.get("text", "")) and self.page["fingerprint"] != before["fingerprint"]
        )
        if refused:
            self.left.append({"what": "Submit was refused by the site", "why": "see the page", "url": self.page["url"]})
            self.status = "stopped"
            return True
        confirmed = self.wait_for_confirmation()
        self.status = "submitted" if confirmed else "submitted (no confirmation seen)"
        applied.record(
            self.applied_path(),
            [self.start_url, before["url"]],
            self.status,
            before.get("title", ""),
            company=self.company,
        )
        return True

    def needs_a_person(self):
        """Does this form's Submit run a challenge only a person can answer? Lever always does (hCaptcha); any
        page carrying an hCaptcha does too. Invisible reCAPTCHA passes by itself and doesn't count."""
        if re.search(r"^https?://jobs(\.eu)?\.lever\.co/", self.page.get("url", "")):
            return True
        check = getattr(self.browser, "evaluate", None)
        try:
            return bool(check) and check(CAPTCHA_CHECK) is True
        except (StalePage, RuntimeError):
            return False

    def wait_for_confirmation(self):
        """After Submit: the site's own confirmation ("Your application was sent", "Thank you for applying"),
        which often appears a moment later (LinkedIn's pop-up, a redirect to a thank-you page). Nothing is
        clicked while waiting."""
        deadline = time.monotonic() + self.policy.confirm_wait_s
        while True:
            text = self.page.get("text", "")
            if SUBMITTED.search(text) or planner.APPLIED.search(text) or planner.APPLIED_AGO.search(text):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(1)
            try:
                self.page = self.browser.observe()
            except StalePage:
                continue  # the site is navigating to its confirmation page

    def do_select(self, action, d, low):
        option = action["label"].split(" → ", 1)[-1]
        self.via_rule = self.matches_profile(action)
        if self.confirm(action, d, low, f"Choose '{option}' for '{self.question(action)}'"):
            self.execute(action, d, source="choice", shown=option, expected=option, multiple=action.get("multiple"))

    def do_fill(self, action, d, low):
        if self.policy.never_fill(action):
            self.skipped.add(self.here(action))
            self.handover(f"'{self.question(action)}' is on your never-fill list. Fill it yourself if it's needed.")
            return
        resolved = self.resolve_text(action)
        if resolved is None:
            return
        text, source = resolved
        checked = source in {"you", "draft"}  # you already saw this exact value
        if checked or self.confirm(action, d, low, f"Type {text!r} into '{self.question(action)}'"):
            self.execute(
                action, d, text=text, source=source, shown=text, expected=text, multiple=action.get("multiple")
            )

    def do_date(self, action, d, low):
        if self.policy.never_fill(action):
            self.skipped.add(self.here(action))
            self.handover(f"'{self.question(action)}' is on your never-fill list.")
            return
        kind = action.get("input_type")
        if kind in {"time", "datetime-local"}:
            # A time of day is never in a profile: you give it (as HH:MM, or YYYY-MM-DDTHH:MM).
            shape = "HH:MM" if kind == "time" else "YYYY-MM-DDTHH:MM"
            text, _ = self.ui.ask(self.question(action), shape)
            self.asked.append({"question": self.question(action), "answered": bool(text), "saved": False})
            pattern = r"\d{2}:\d{2}" if kind == "time" else r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"
            if not text or not re.fullmatch(pattern, text.strip()):
                self.skip(action, d)
                return
            self.execute(action, d, value=text.strip(), source="you", shown=text.strip(), expected=text.strip())
            return
        resolved = self.resolve_text(action, dates_only=True)
        if resolved is None:
            return
        converted = native_date(resolved[0], action.get("input_type", "date"))
        if converted is None:
            self.skipped.add(self.here(action))
            self.handover(f"Couldn't turn {resolved[0]!r} into a date for '{self.question(action)}'. Please set it.")
            return
        value, note = converted
        if note:
            self.ui.event(f"'{self.question(action)}': {note}")
        if resolved[1] == "you" or self.confirm(action, d, low, f"Set '{self.question(action)}' to {value}"):
            self.execute(action, d, value=value, source=resolved[1], shown=value, expected=value)

    def do_upload(self, action, d, low):
        self.via_rule = False
        documents = self.profile.documents
        if not documents:
            self.skipped.add(self.here(action))
            self.handover("This form wants a file, and your profile lists no existing documents. Attach it yourself.")
            return
        result = model.document_choice(action, self.page, documents)
        choice = result["choice"]
        if result["margin"] < self.policy.min_value_margin or choice == "ASK_USER":
            options = [
                (name, f"{doc.about} ({Path(doc.value).name})", result["probabilities"].get(name))
                for name, doc in documents.items()
            ]
            choice = self.ui.pick(f"Which document goes in '{self.question(action)}'?", options) or "SKIP_FIELD"
        if choice == "SKIP_FIELD":
            self.skip(action, d)
            return
        path = documents[choice].value
        if self.confirm(action, d, low, f"Attach {Path(path).name} to '{self.question(action)}'"):
            self.execute(action, d, files=[path], source="profile", shown=Path(path).name, expected=Path(path).name)

    def do_control(self, action, d, low):
        self.via_rule = False
        if action["kind"] == "frame" and not self.confirm(action, d, low, "Open the embedded application form"):
            return
        self.execute(action, d)

    # ---- values ---------------------------------------------------------------------------------------------

    def question(self, action):
        label = action["label"].split(" → ")[0]
        context = action.get("context", "")
        if action.get("multiple"):
            return label  # a tag field's context is its added items, not its question
        return f"{label} — {context[:140]}" if context and label not in context else (context[:160] or label)

    def resolve_text(self, action, dates_only=False):
        """Returns (text, source) or None if the field stays empty. Source: profile | saved | you | draft."""
        key = (*self.here(action), action["label"], action.get("context"), dates_only)
        if self.pending and self.pending[0] == key:
            return self.pending[1]
        saved = self.profile.saved_answer(self.question(action)) if not dates_only else None
        if saved and not action.get("multiple"):
            self.via_rule = True  # your own words for this very question: typed as saved
            return self.remember_resolution(action, key, (saved, "saved"))
        tried = self.items.get(self.here(action), set())
        result = model.value_source(action, self.page, self.profile, self.history, dates_only, tried)
        choice = result["choice"]
        if not result.get("rule") and choice not in SPECIAL and not rules.related(self.profile.by_id[choice], action):
            choice = "ASK_USER"  # a model's pick sharing no word with the question is a guess: never typed
        self.via_rule = bool(result.get("rule")) and choice not in SPECIAL
        if result["margin"] < self.policy.min_value_margin and choice not in {"ASK_USER"}:
            options = [(i, self.describe(i), p) for i, p in result["ranked"][:3] if i != "ASK_USER"]
            choice = self.ui.pick(f"Close call for '{self.question(action)}'. Which is right?", options) or "ASK_USER"
        if choice == "SKIP_FIELD":
            self.skip(action, None)
            return None
        if choice == "DRAFT_ANSWER" and self.policy.drafts == "confirm":
            try:
                draft, meta = model.draft_text(action, self.page, self.profile)
            except (RuntimeError, ValueError) as error:  # the drafting model failing must not stop the form
                draft, meta = None, {"reason": str(error)}
            if draft:
                text = self.ui.edit_draft(self.question(action), draft)
                resolved = (text, "draft" if text == draft else "you") if text else None
                return self.remember_resolution(action, key, resolved)
            self.ui.event(f"No draft ({meta.get('reason', 'model returned nothing')}); asking you instead.")
            choice = "ASK_USER"
        if choice in {"ASK_USER", "DRAFT_ANSWER"}:
            hint = "Dates as YYYY-MM-DD or YYYY-MM" if dates_only else action.get("placeholder", "")
            return self.remember_resolution(action, key, self.ask_you(action, hint))
        fact = self.profile.by_id[choice]
        if fact.kind == "item":
            self.items.setdefault(self.here(action), set()).add(choice)
        value = fact.value if dates_only else rules.fit(fact, action, self.profile)
        if value is None:
            # The fact can't go in as the field wants it (a date shape, a whole number, a length limit): yours.
            limits = [
                action.get("placeholder") or "",
                f"max {action['maxlength']} chars" if action.get("maxlength") else "",
            ]
            return self.remember_resolution(action, key, self.ask_you(action, " · ".join(x for x in limits if x)))
        return self.remember_resolution(action, key, (value, "saved" if fact.key.startswith("answer.") else "profile"))

    def ask_you(self, action, hint=""):
        """Your answer for a field no fact fills: (text, 'you') or None. Unattended, an optional field is simply
        left empty (listed apart in the report), so the list of things that need you holds what matters."""
        question = self.question(action)
        if self.unattended and not action.get("required"):
            self.optional_skipped.append(question[:100])
            return None
        if self.unattended:
            self.inbox.append({"question": question, "kind": "text", "hint": hint, "urls": [self.page["url"]]})
        text, remember = self.ui.ask(question, hint)
        self.asked.append({"question": question, "answered": bool(text), "saved": remember})
        if text and remember:
            self.profile.remember(question, text)
        return (text, "you") if text else None

    def remember_resolution(self, action, key, resolved):
        if resolved is None:
            self.skip(action, None)
            return None
        self.pending = (key, resolved)  # survives a stale retry of this same field only
        return resolved

    def describe(self, choice):
        if choice in SPECIAL:
            return SPECIAL[choice]
        fact = self.profile.by_id[choice]
        return f"{fact.value[:60]!r}  ← {fact.about[:90]}"

    # ---- guards, execution, verification ---------------------------------------------------------------------

    def confirm(self, action, d, low, what):
        reasons = []
        if (
            "naukri.com" in self.page["url"]
            and action.get("chat")
            and planner.CHAT_SEND.search(action.get("label", ""))
            and self.policy.naukri_apply != "auto"
        ):
            reasons.append("sends this answer to Naukri; the last answer completes your application")
        if action.get("replaces"):
            what += f" (replacing {action['replaces']!r} the site had there)"
            reasons.append("changes a value already on the form")
        if low:
            p = d["target_probability"] if d["target_probability"] is not None else d["operation_probability"]
            reasons.append(f"the model is only {p:.0%} sure")
        if self.policy.sensitive(action):
            reasons.append("sensitive question")
        if self.ui.step:
            reasons.append("step mode")
        if self.unattended:
            # Nobody to ask: go ahead only when the model is sure, and for sensitive fields only with a value a
            # fixed rule took from your profile. A value already on the form is replaced only by such a value.
            sure = not low and (not self.policy.sensitive(action) or (self.via_rule and action["kind"] != "upload"))
            sure = sure and (not action.get("replaces") or self.via_rule)
            if sure:
                return True
            self.left.append(
                {"what": what, "why": ", ".join(r for r in reasons if r != "step mode"), "url": self.page["url"]}
            )
            if action["kind"] in {"select", "click"} and (action["kind"] == "select" or action.get("context")):
                group = self.group_of(action)
                question = (
                    action["label"].split(" → ", 1)[0]
                    if action["kind"] == "select"
                    else planner.question_of(action["context"], group)
                )
                options = list(dict.fromkeys(planner.option_text(o) for o in group))[:30]
                self.inbox.append(
                    {"question": question, "kind": "choice", "options": options, "urls": [self.page["url"]]}
                )
            elif action["kind"] == "fill":
                self.inbox.append({"question": self.question(action), "kind": "text", "urls": [self.page["url"]]})
            # Leave the whole question, not just this one option, so no other option gets tried in its place.
            if action["kind"] == "select":
                self.skipped.add(self.here(action))
            elif action["kind"] == "click" and action.get("context"):
                nodes = {a.get("node") for a in self.page["actions"] if a.get("context") == action["context"]}
                self.skipped_questions.setdefault((self.page.get("doc"), action["context"]), set()).update(nodes)
        elif not reasons or self.ui.approve(what, ", ".join(reasons)):
            return True
        self.vetoed.add((*self.here(action), action["label"]))
        self.ui.event("Skipped. The agent won't propose that again on this page; do it yourself if needed.")
        return False

    def skip(self, action, d):
        self.skipped.add(self.here(action))
        self.history.append(
            {
                "step": len(self.history) + 1,
                "action": action["label"],
                "kind": "skip",
                "context": (action.get("context") or "")[:160],
                "source": "left empty",
                "page_changed": None,
                "url": self.page["url"],
            }
        )

    def log(self, action, d, source=None):
        entry = {
            "step": len(self.history) + 1,
            "action": action["label"],
            "context": (action.get("context") or "")[:160],
            "kind": action["kind"],
            "operation": d["operation"],
            "section": action.get("section"),
            "chat": bool(action.get("chat")),  # a chat questionnaire's answer: its Save comes next
            "source": source,
            "operation_probability": d["operation_probability"],
            "target_probability": d["target_probability"],
            "latency_ms": d["latency_ms"],
            "url": self.page["url"],
            "page_changed": None,
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000),
        }
        self.history.append(entry)
        return entry

    def execute(
        self, action, d, *, text=None, value=None, files=None, source=None, shown=None, expected=None, multiple=False
    ):
        self.browser.act(action, self.page, text=text, value=value, files=files)
        self.pending = None
        entry = self.log(action, d, source)  # logged before observing, so a navigation can't erase it
        if text is not None:
            entry["typed"] = text  # which suggestion answers it: the one that is this text
        fill = None
        if action["kind"] in FILLS:
            fill = {
                "label": self.question(action),
                "value": shown,
                "source": source,
                "kind": action["kind"],
                "node": action["node"],
                "doc": self.page.get("doc"),
                "multiple": bool(multiple),
                "chooser": bool(action.get("chooser")),
                "expected": expected,
                "url": self.page["url"],
                "confirmed": None,
            }
            self.fills.append(fill)
        previous = self.page
        self.page = self.browser.observe()
        self.stay_on_job()
        if action["kind"] == "wait":  # still drawing the form? Page text and animations don't count
            entry["page_changed"] = form_state(self.page) != form_state(previous)
        else:
            entry["page_changed"] = self.page["fingerprint"] != previous["fingerprint"]
        if action["kind"] == "click" and entry["page_changed"] and ADVANCING.search(action.get("label", "")):
            self.no_review.clear()  # a new step: its required fields are checked afresh
        if fill:
            self.verify(fill)
        self.track_items()
        recent = self.history[-3:]
        if len(recent) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in recent):
            self.handover("Three actions in a row changed nothing on the page.")

    def stay_on_job(self):
        """A job board page that moved to another job (a card in "More jobs" clicked): stop at once, so nothing is
        filled, or sent, for a job you didn't choose."""
        start, now = applied.board_job(self.start_url), applied.board_job(self.page["url"])
        if start and now and now.split(":", 1)[0] == start.split(":", 1)[0] and now != start:
            self.status = "stopped"
            self.left.append(
                {"what": "Stopped", "why": f"the page moved to another job ({now})", "url": self.page["url"]}
            )
            raise Stop()

    def verify(self, fill):
        """Independent read-back: did the page keep the value we set?"""
        if self.page.get("doc") != fill["doc"]:
            return  # a new document: its node ids are unrelated
        if fill.get("chooser") or fill["kind"] == "upload":
            # The file's name shown on the page: Workday and others empty the input once the file is in their list.
            stem = norm(Path(str(fill["expected"])).stem)
            shown = norm(self.page.get("text", "") + " ".join(a["label"] for a in self.page["actions"]))
            fill["confirmed"] = bool(stem) and stem in shown
            return
        same = [a for a in self.page["actions"] if a.get("node") == fill["node"]]
        if not same:
            return  # moved to another page/step; the value can't be re-read here
        current = same[0].get("current_value" if fill["kind"] == "select" else "value", "")
        expected = fill["expected"]
        contains = fill["kind"] == "upload" or fill["multiple"]  # one of several files / selected options
        ok = norm(expected) in norm(current) if contains else norm(current) == norm(expected)
        fill["confirmed"] = ok
        if not ok:
            self.ui.event(f"'{fill['label'][:50]}' now shows {current!r}, expected {expected!r}")

    def track_items(self):
        """Typing into a tag field only counts once the item shows up as added (a chip) in the field's context."""
        fields = {a.get("node"): a for a in self.page["actions"] if a["kind"] == "fill"}
        for f in self.fills:
            if f["kind"] == "fill" and f["multiple"] and f["doc"] == self.page.get("doc") and f["node"] in fields:
                f["added"] = norm(f["value"]) in norm(fields[f["node"]].get("context", ""))

    def reobserve(self):
        while True:
            try:
                self.page = self.browser.observe()
                return
            except StalePage:
                self.handover("The page did not finish loading.", observe=False)

    def wait_for_login(self):
        """Unattended, at a sign-in page: alert the candidate, show the tab and wait for them to sign in or create
        the account (once per company portal; the browser keeps the session). True once the page is past it."""
        wait = self.policy.login_wait_s
        if wait <= 0:
            return False
        self.browser.show()
        self.ui.event(
            f"\a!!! LOGIN NEEDED: sign in (or create your account) in the open tab: {self.page['url'][:120]}  "
            f"Waiting up to {wait // 60} min; the agent never types into a login page."
        )
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(5)
            try:
                self.page = self.browser.observe()
            except StalePage:
                continue  # the portal is navigating after your sign-in
            if not planner.sign_in_wall(self.page):
                self.ui.event("Signed in: continuing.")
                self.history.append(
                    {
                        "step": len(self.history) + 1,
                        "action": "The candidate signed in",
                        "kind": "handover",
                        "context": "login",
                        "source": None,
                        "page_changed": True,
                        "url": self.page["url"],
                    }
                )
                return True
        return False

    def handover(self, reason, observe=True):
        self.browser.show()
        self.handovers.append({"reason": reason, "url": self.page["url"]})
        if not self.ui.handover(reason):
            raise Stop()
        self.history.append(
            {
                "step": len(self.history) + 1,
                "action": "The candidate took over and returned",
                "kind": "handover",
                "context": reason[:160],
                "source": None,
                "page_changed": None,
                "url": self.page["url"],
            }
        )
        if observe:
            self.reobserve()

    def finish(self):
        self.browser.show()
        folder = self.run_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
        folder.mkdir(parents=True, exist_ok=True)
        report = {
            "status": self.status,
            "start_url": self.start_url,
            "final_url": self.page["url"],
            "elapsed_s": round(time.perf_counter() - self.started, 1),
            "fills": [
                {k: v for k, v in f.items() if k not in {"node", "doc", "multiple", "chooser"}} for f in self.fills
            ],
            "unconfirmed": sum(f["confirmed"] is False for f in self.fills),
            "not_added": [f["value"] for f in self.fills if f.get("added") is False],
            "asked": self.asked,
            "left_for_you": self.left,
            "optional_skipped": self.optional_skipped,
            "handovers": self.handovers,
            "decisions": len(self.decisions),
            "history": self.history,
            # The last page as the agent saw it (labels, not values), for fixing sites it can't handle yet.
            "last_page": [
                {
                    k: a[k]
                    for k in (
                        "kind",
                        "role",
                        "label",
                        "context",
                        "placeholder",
                        "input_type",
                        "required",
                        "expanded",
                        "checked",
                        "pressed",
                        "chosen",
                        "multiple",
                        "invalid",
                        "error",
                    )
                    if a.get(k) not in (None, "")
                }
                for a in self.page["actions"]
            ],
            "path": str(folder / "report.json"),
        }
        if self.unattended and self.status != "submitted":
            self.inbox += self.unanswered_entries()
        done = [
            q
            for h in self.history
            if h.get("kind") == "fill" and h.get("source") in {"profile", "saved", "draft"}
            for q in (h.get("action"), h.get("context"))
            if q
        ]
        report["pending_total"] = self.profile.add_pending(self.inbox, done)
        Path(report["path"]).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        self.ui.review(report)
        return report
