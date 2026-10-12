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
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import applied, model, planner, replay, rules
from .browser import ADVANCING, ExecutionUncertain, StalePage
from .files import write_text
from .profile import native_date, not_a_question, one_time_code, safe_id, same_question
from .questions import GOAL, SPECIAL

FILLS = {"fill", "select", "setdate", "upload"}
ANSWER_ROUNDS = 3  # a form that keeps asking new questions waits for you at most this often in one run
# A job located in India, by its address or posting (Workday: ".../job/Bangalore/...").
INDIAN_JOB = re.compile(
    r"\bindia\b|bangalore|bengaluru|hyderabad|\bpune\b|chennai|mumbai|new[- ]?delhi|\bdelhi\b|gurgaon|gurugram|"
    r"noida|kolkata|ahmedabad|jaipur|udaipur|kochi|coimbatore|chandigarh|indore|vadodara|nagpur|thiruvananthapuram",
    re.I,
)
# What sites say once an application went through.
# A challenge a person must answer before the form sends: hCaptcha's frame or its response field.
# What a portal's page says once an e-mailed verification link did its work, or why it didn't.
ACTIVATED = re.compile(r"(account|e-?mail)\W+(has been |is |was )?(verified|activated|confirmed)|verification "
                       r"(was )?successful|successfully (verified|activated)|you can now sign in", re.I)  # fmt: skip
INVALID_LINK = re.compile(r"invalid (token|link)|link (has )?expired|token (has )?expired|no longer valid", re.I)
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


# Google's confirmation buttons, and what Google would share beyond your name, e-mail, language and picture.
GOOGLE_GO = re.compile(r"\s*(continue|allow|confirm|next)\s*", re.I)
GOOGLE_WIDE = re.compile(
    r"\b(google drive|gmail|contacts|calendar|photos|youtube|see, edit|edit, create|delete|manage your|send email)\b",
    re.I,
)


class Stop(Exception):
    """You chose to stop."""


class Skipped(Exception):
    """You pressed "Skip job" in the bubble: this job ends here, the batch goes on."""


RESCUE_STEPS = 5  # steps one rescue may take on a stuck page

DOING = {"TYPE_TEXT": "Typing into", "CLICK": "Clicking", "SELECT": "Choosing", "SET_DATE": "Setting",
         "UPLOAD": "Uploading", "OPEN_FORM": "Opening", "PRESS_ENTER": "Searching in"}  # fmt: skip


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
        self.paused = False  # you pressed Pause in the bubble
        self.mail_since = time.time() - 120  # e-mails a portal sends from here on (or just before) are this run's
        self.answer_rounds = 0  # times this run waited for your answers (ANSWER_ROUNDS at most)
        self.teacher_rounds = 0  # times this run asked the teacher (ANSWER_ROUNDS at most)
        self.reloads = 0  # page reloads after a site error ("Something went wrong"), 2 at most
        self.rescues = 0  # times Claude was asked for the next step on a stuck page (3 at most)
        # (question, option) picked as your answer in the list's own words ("BS" for B.Tech), and questions already
        # looked up this run (once each): see closest_pick
        self.mapped, self.looked_up = set(), set()
        self.rescue_log, self.closest_log = [], []  # what each did, for the run's report and metrics
        self.recorder = replay.Recorder(self.run_dir)
        self.pending = None
        self.claude = []  # Claude calls this run made (drafts): time, tokens, cost
        self.receipt = None  # the site's page right after Submit: text and a screenshot
        self.status = "ready"
        self.started = time.perf_counter()
        self.page = browser.observe()
        self.start_url = self.page["url"]
        # A job in your own country (job_in_your_country): "eligible to work in the country of this position" is then
        # answered from your work authorization there. Anywhere else it is asked.
        home = self.job_in_your_country()
        if home:
            self.profile.note_job_country(home)
        # "Have you applied to <company> before?": from your record of applications on this company's site.
        self.profile.note_fact("application.applied_here_before", "Yes" if self.applied_here() else "No",
                               "Applied to this company before (from your application record)")  # fmt: skip
        # The date a form is filled ("Date:" beside a signature, Blue Yonder's attestation): today, for this run only
        self.profile.note_fact("application.today", datetime.now().date().isoformat(), "Today's date")
        self.profile.facts["application.today"].kind = "date"

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
            while True:
                try:
                    try:
                        while self.status == "ready":
                            self.tick()
                    except Stop:
                        self.status = "stopped"
                    # About to stop on questions only you can answer: wait for your answers, then carry on here.
                    stuck = self.unattended and self.status in {"stopped", "review"}
                    # Questions only you could answer: the teacher first (from your profile), then you.
                    if not (stuck and (self.consult_teacher() or self.wait_for_answers())):
                        break
                except Skipped:
                    self.status = "deferred"
                    self.ui.event("deferred: skipped by you")
                    self.show_helper("Skipped: on to the next job.")
                    break
        finally:
            for question in added:
                self.profile.saved.pop(question, None)
        return self.finish()

    def tick(self):
        self.helper_check()  # Pause holds the run right here; Skip ends the job
        try:
            self.step()
            self.stale = (None, 0)
        except StalePage:
            # The page changed under the action, or it was covered: decided again. The same action refused three
            # times running is dropped (a Workday chip's ×, out of view, was retried 450 times without a trace).
            action = getattr(self, "last_action", None)
            key = (self.page.get("doc"), action.get("label") if action else None)
            streak = self.stale[1] + 1 if getattr(self, "stale", (None, 0))[0] == key else 1
            self.stale = (key, streak)
            if action and streak >= 3 and action.get("kind") not in {"wait", "scroll", "key"}:
                self.vetoed.add((*self.here(action), action["label"]))
                self.ui.event(f"'{action['label'][:60]}' couldn't be done (the page kept changing); leaving it.")
                self.left.append(
                    {"what": f"'{action['label'][:80]}'", "why": "the page wouldn't take it", "url": self.page["url"]}
                )
                self.stale = (None, 0)
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
        planned = d
        d = self.closest_pick(d, offered) or d
        # The page, what was read on it and decided, kept for replay tests (replay.py; once per layout)
        self.recorder.record(self.browser, self.page, offered, self.history, self.profile.candidate_state(), exclude,
                             planned, d)  # fmt: skip
        self.decisions.append(
            {k: d[k] for k in ("operation", "target", "operation_probability", "target_probability", "latency_ms")}
            | {"url": self.page["url"]}
        )
        operation, action = d["operation"], d["action"]
        self.last_action = action
        if action and operation in DOING:
            self.show_helper(f"{DOING[operation]} '{action['label'][:70]}'")
        elif operation in {"WAIT", "SCROLL_DOWN", "SCROLL_UP"}:
            words = {"WAIT": "Waiting for the page", "SCROLL_UP": "Looking further up the form"}
            self.show_helper(words.get(operation, "Looking further down the form"))
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
        if operation == "BLOCKED" and d.get("reason") == planner.ALREADY_APPLIED:
            # Done before (by you, or an earlier run): not a failure. Recorded, so no later batch tries it again.
            self.status = "already applied"
            applied.record(self.applied_path(), [self.start_url, self.page["url"]], "already applied",
                           self.page.get("title", ""), company=self.company)  # fmt: skip
            self.ui.event("already applied on the site: nothing to do")
            return
        if operation == "BLOCKED" and d.get("reason") == planner.SITE_ERROR_REASON and self.reloads < 2:
            # "Something went wrong" that waiting didn't clear: load the page again (twice at most), as you would.
            self.reloads += 1
            self.ui.event("the site showed an error: loading the page again")
            self.browser.navigate(self.page["url"])
            self.history.append({"step": len(self.history) + 1, "action": "Reload after the site's error",
                                 "kind": "wait", "context": "", "url": self.page["url"], "page_changed": True})  # fmt: skip
            self.page = self.browser.observe()
            return
        if operation == "BLOCKED":
            if planner.sign_in_wall(self.page):
                if self.policy.google_sign_in and self.sign_in_with_google():
                    return  # signed in with Google: carry on from the form
                if self.policy.portal_accounts and self.sign_in_with_password():
                    return  # signed in (or signed up) with your portal account: carry on from the form
                if self.unattended and self.wait_for_login():
                    return  # signed in: carry on from the form
            self.handover(
                self.exhausted_reason(d.get("reason"))
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
        if action and (*self.here(action), action["label"]) in self.vetoed:
            # Given up on already, and proposed again (a step the planner makes up, not one the page offers): a
            # loop. Hand over rather than spin.
            self.handover(f"'{action['label'][:80]}' keeps coming back after the page wouldn't take it.")
            return
        repeats = (  # tag fields take an item per entry and "Add another" a block per job: those repeat by design
            action
            # Enter is offered only right after something new was typed (each skill of a tag box): never a repeat.
            and action.get("kind") not in {"wait", "scroll", "key", "enter"}
            and not action.get("multiple")
            and not planner.ADD.search(action.get("label", ""))
        )
        if repeats and self.repeated(action) >= 3:
            # Done three times on this page and still proposed: the page doesn't take it. Claude, with the page and
            # the last steps, may see what is missing (a dropdown left empty); otherwise drop it and go on.
            stuck = f"'{action['label'][:80]}' was done three times and the page didn't move on."
            if self.unattended and self.rescue(stuck, avoid=[action["label"]]):
                return
            self.vetoed.add((*self.here(action), action["label"]))
            self.ui.event(f"'{action['label'][:60]}' was done three times without effect; leaving it for you.")
            if getattr(self, "rescue_why", ""):
                self.ui.event(f"Claude: {self.rescue_why[:160]}")
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
        history = self.history
        if planner.ADVANCE.search(action.get("label", "")):
            # Save / Next pressed again after something was filled is a new attempt: only presses since the last
            # answer count (Workday's Save pressed early, before the page was filled, must not ban it for good).
            answered = [i for i, h in enumerate(history) if h.get("kind") in {"fill", "select", "setdate", "upload"}
                        or (h.get("kind") == "click" and not planner.ADVANCE.search(str(h.get("action") or "")))]
            history = history[answered[-1] + 1:] if answered else history
        return sum(
            1
            for h in history
            if h.get("kind") == action["kind"]
            and h.get("action") == action["label"]
            and h.get("context") == context
            and h.get("url") == self.page["url"]
            and planner.same_block(h, action)  # "Month" of Work Experience 2 is not the Month of Work Experience 1
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
            # "I currently work here" follows from your job's dates (planner): never a declaration to ask you about
            and not planner.CURRENT_JOB.search(action.get("label") or "")
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
        elif self.list_option(action) and self.opened_question():
            question, option = self.opened_question(), action["label"]  # the dropdown just opened, whatever is around
        elif action["kind"] == "click" and action.get("context") and action.get("role") != "link":
            question, option = action["context"], action["label"]
        else:
            return False
        if (norm(question), norm(planner.option_text(action) or option)) in self.mapped:
            return True  # your answer in the list's own words, picked for this question (closest_pick)
        if planner.eeo_decline(question, option, self.profile.candidate_state()):
            return True
        group = self.group_of(action)
        facts = planner.Facts(self.profile.candidate_state())
        if any(o.get("id") == action.get("id") for o in planner.range_answer(question, group, facts)):
            return True  # "16-20 LPA" for your 17 LPA: computed from your profile, not guessed
        saved = self.profile.saved_answer(planner.question_of(question, group))
        if saved and norm(saved) == norm(option):
            return True  # the answer you saved for this question
        opened = self.opener() if self.list_option(action) else {}
        key = rules.key_for({"label": question, "context": question, "field_id": (opened or {}).get("field_id")},
                            self.profile, set())  # fmt: skip
        # Workday names the country after the answer ("Single (India)", "Citizen (India)"): yours, or not this one
        countries = {norm(f.value) for k, f in self.profile.facts.items() if k.endswith("country") and f.value}
        place = re.fullmatch(r"(.+?)\s*\(([^()]+)\)\s*", planner.option_text(action) or option)
        if place and norm(place.group(2)) in countries:
            option = place.group(1)
        elif place and countries and re.search(r"countr|citizen|nationality|marital|birth", question, re.I):
            return False  # another country's ("Citizen (Belgium)")
        for k in key if isinstance(key, tuple) else (key,):
            fact = self.profile.by_id.get(safe_id(k)) if k and not k.isupper() else None
            if fact and norm(fact.value) == norm(option):
                return True
            if fact and norm(fact.value) in {"yes", "no", "true", "false"}:
                # "Yes, I do" / "No, I don't": the one option of the question that starts with your Yes or No
                word = "yes" if norm(fact.value) in {"yes", "true"} else "no"
                starts = [o for o in group or [action] if re.match(rf"\s*{word}\b", planner.option_text(o), re.I)]
                if re.match(rf"\s*{word}\b", option, re.I) and len(starts) <= 1:
                    return True
        return False

    def closest_pick(self, d, page):
        """A list is open and your answer isn't in it as written (no pick, or only a near guess): the option that is
        your answer in the list's own words ("BS" for your B.Tech, "Manipal University" or "Other" for Manipal
        University Jaipur), from the fact the rules give that question. None leaves the decision as it was."""
        from . import teacher

        options = [a for a in page["actions"] if self.list_option(a) and not planner.CHIP.search(a["label"])]
        sure = d.get("action") in options and (d.get("target_probability") or 0) >= 0.9
        question = self.opened_question()
        if not options or sure or not question:
            return None
        opener = self.opener() or {}
        if opener.get("kind") in {"fill", "enter"}:
            # Typed into: only a search or lookup box's own suggestions are its list, never a list that happens to be
            # open beside a plain text box (an unrelated "Other" for your first name)
            box = next((a for a in page["actions"] if a["kind"] == "fill" and a["label"] == question), None)
            if not box or not (planner.is_prompt(box) or planner.LOOKUP.search(box["label"])):
                return None
        field = {"label": question, "context": opener.get("context") or question, "section": opener.get("section"),
                 "field_id": opener.get("field_id")}  # fmt: skip
        key = rules.key_for(field, self.profile, set(), page_text=self.page.get("text", ""))
        fact = next((self.profile.by_id.get(safe_id(k)) for k in (key if isinstance(key, tuple) else (key,))
                     if k and not k.isupper() and self.profile.by_id.get(safe_id(k))), None)  # fmt: skip
        texts = list(dict.fromkeys(planner.option_text(o) for o in options))
        asked = (question, fact.value if fact else None, tuple(texts))  # a new search's results: looked up anew
        if fact is None or asked in self.looked_up:
            return None  # nothing on file answers it (yours to say), or already looked up this run
        if any(a["kind"] == "fill" and a.get("multiple") and a["label"] == question for a in page["actions"]):
            return None  # a box of several items (your skills): each is matched as it is typed
        picked = d.get("action") in options and planner.option_text(d["action"])
        if picked and norm(picked).startswith(norm(fact.value)):
            return None  # your answer and more ("Udaipur, Rajasthan, India" for Udaipur): that one
        self.looked_up.add(asked)
        exact = next((t for t in texts if norm(t) == norm(fact.value)), None)
        # Written exactly as your answer ("India", "Male"): that one, with no Claude call (ABB asked Claude for both)
        found = (exact, "same", "written as your answer") if exact else teacher.closest_option(
            question, fact.key, fact.value, fact.about, texts, self.profile,
            urlparse(self.page["url"]).hostname or "", self.run_dir)  # fmt: skip
        if not found:
            return None
        text, match, why = found
        option = next(o for o in options if planner.option_text(o) == text)
        if planner.TICKED.search(option["label"]):
            return None  # already ticked: a click would untick it
        self.mapped.add((norm(question), norm(text)))
        self.closest_log.append({"question": question[:120], "fact": fact.key, "value": fact.value[:80],
                                 "option": text[:80], "match": match, "why": why[:120]})  # fmt: skip
        self.ui.event(f"'{question[:50]}': {fact.value[:40]} is '{text[:40]}' here ({match}: {why[:60]})")
        return planner.decision("CLICK", option, 1.0)

    @staticmethod
    def list_option(action):
        """An entry of a list a dropdown or search box opened: an option, or a Workday result ("India not checked")."""
        return action["kind"] == "click" and (
            action.get("role") == "option" or bool(planner.TICKED_SUFFIX.search(action.get("label") or ""))
        )

    def opener(self):
        """The last step that opened something: a dropdown clicked, a search typed or entered."""
        return next((h for h in reversed(self.history) if h.get("kind") not in {"wait", "scroll", "skip"}), None)

    def opened_question(self):
        """The question of the list the last step opened, whose options carry no question of their own (Workday's sit
        in a pop-up at the page's end, under its footer's "Follow Us"): a "Select One" dropdown's, or the search box
        typed in ("Please specify your Citizenship Status" for "Citizen (India)"); else None."""
        opened = next((h for h in reversed(self.history) if h.get("kind") == "click"
                       and planner.SHOWS_NOTHING.search(str(h.get("action") or ""))), None)  # fmt: skip
        last = next((h for h in reversed(self.history) if h.get("kind") not in {"wait", "scroll"}), None)
        if last and last.get("kind") in {"fill", "enter"}:
            return re.sub(r"^Press Enter in '(.*)'$", r"\1", str(last.get("action") or "")) or None
        if not opened or last is not opened:
            return None
        # Its button names it ("Please specify your Country of Birth Select One"), sharper than any context
        named = planner.SHOWS_NOTHING.sub("", str(opened.get("action") or "")).strip()
        return named or opened.get("context") or None

    def group_of(self, action):
        """The other options of the question an option belongs to (same dropdown, or same question context)."""
        if action["kind"] == "select":
            return [a for a in self.page["actions"] if a["kind"] == "select" and a.get("node") == action.get("node")]
        if action.get("role") == "option":
            # The open list's options (Workday's carry no question; the one clicked is given its dropdown's)
            return [a for a in self.page["actions"] if a["kind"] == "click" and a.get("role") == "option"
                    and (a.get("context") or "") in {"", action.get("context") or ""}]  # fmt: skip
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
            if self.list_option(action) and self.opened_question():
                # An option of the dropdown just opened is judged with its question ("Yes, I do" to "Please confirm
                # if you have a valid PAN card"), not with the footer it happens to sit under.
                action = {**action, "context": self.opened_question()}
            # Opening a "Select One" dropdown answers nothing (the option clicked next is the answer, and that one
            # must come from your profile): a sensitive question's dropdown may be opened.
            opens = action.get("role") == "button" and bool(planner.SHOWS_NOTHING.search(action.get("label", "")))
            # A cookie banner's Accept (as you asked) or Decline only closes it and says nothing about you, though
            # its text speaks of "consent" (Maersk's banner covered Continue Application).
            label = action.get("label", "")
            cookies = bool(planner.ACCEPT_COOKIES.search(label) or planner.COOKIES.search(label)) and bool(re.search(
                r"cookie|tracking|consent manager|privacy preference",
                f"{action.get('label', '')} {action.get('context', '')}", re.I))  # fmt: skip
            self.via_rule = self.matches_profile(action) or self.consents(action) or opens or cookies
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

    def applied_here(self):
        """Did you send an application on this company's site before (applied.json: same host, submitted/applied)?"""
        from urllib.parse import urlparse

        host = (urlparse(self.start_url).hostname or "").lower()
        path = self.applied_path()
        if not host or not path or not path.is_file():
            return False
        return any((urlparse(e.get("url") or "").hostname or "").lower() == host
                   and re.search(r"submit|applied", str(e.get("status") or ""), re.I)
                   for e in applied.read(path).values())  # fmt: skip

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
        self.capture_receipt(confirmed)
        applied.record(
            self.applied_path(),
            [self.start_url, before["url"]],
            self.status,
            before.get("title", ""),
            company=self.company,
        )
        return True

    def capture_receipt(self, confirmed):
        """Keep what the site showed right after Submit: its text and a screenshot, saved with the run report.
        Nothing is clicked; a failure only means no receipt."""
        import base64

        shot = None
        try:
            shot = base64.b64decode(self.browser.call("Page.captureScreenshot", format="png")["data"])
        except Exception:  # an older browser, a closed tab: the text is still kept
            pass
        self.receipt = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "url": self.page.get("url"),
            "title": self.page.get("title"),
            "confirmed": bool(confirmed),
            "text": (self.page.get("text") or "")[:6000],
            "_png": shot,
        }

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
        if one_time_code(f"{self.question(action)} {action.get('context') or ''}"):
            # A code the site just sent you: yours to type in the tab, never asked in QUESTIONS.md or kept.
            if self.unattended and self.wait_for_code():
                return
            self.skipped.add(self.here(action))
            self.handover("The site sent you a verification code (e-mail or SMS): type it in the tab.")
            return
        if self.policy.never_fill(action):
            self.skipped.add(self.here(action))
            self.handover(f"'{self.question(action)}' is on your never-fill list. Fill it yourself if it's needed.")
            return
        if action.get("retype"):
            # Your school's name cut shorter (planner.shorter_name of what was typed from your profile): same fact.
            resolved = (action["retype"], "profile")
        else:
            resolved = self.resolve_text(action)
        if resolved is None:
            return
        text, source = resolved
        checked = source in {"you", "draft"}  # you already saw this exact value
        if checked or self.confirm(action, d, low, f"Type {text!r} into '{self.question(action)}'"):
            whole = self.whole_date(action, text)
            if whole:
                # Workday's MM / DD / YYYY boxes move on by themselves as each part is complete: the whole date typed
                # key by key from the first box, as a person does (typing each box apart spilled into the next).
                self.execute({**action, "masked": True}, d, text=whole, source=source, shown=whole, expected=text)
                return
            self.execute(
                action, d, text=text, source=source, shown=text, expected=text, multiple=action.get("multiple")
            )

    def whole_date(self, action, text):
        """The digits of a whole date when `action` is the first of a date's empty part boxes ("Month", "Day",
        "Year" under one question): "10262026" for Month 10, Day 26, Year 2026. None otherwise."""
        part = re.compile(r"\s*(month|day|year|mm|dd|yyyy)\s*\*?\s*", re.I)
        if not part.fullmatch(action.get("label") or "") or action.get("value"):
            return None
        boxes = [a for a in self.page["actions"] if a["kind"] == "fill" and part.fullmatch(a.get("label") or "")
                 and a.get("context") == action.get("context") and a.get("section") == action.get("section")]  # fmt: skip
        boxes.sort(key=lambda a: ((a.get("rect") or {}).get("x", 0), a.get("node") or 0))
        if len(boxes) < 2 or boxes[0].get("node") != action.get("node") or any(a.get("value") for a in boxes):
            return None
        digits = []
        for box in boxes:
            value = text if box is boxes[0] else None
            if value is None:
                choice = rules.match(box, self.profile, False, (), self.page.get("text", ""))
                fact = self.profile.by_id.get(choice) if choice else None
                value = rules.fit(fact, box, self.profile) if fact else None
            width = 4 if re.match(r"\s*(year|yyyy)", box["label"], re.I) else 2
            if not value or not str(value).isdigit():
                return None
            digits.append(str(value).zfill(width))
        return "".join(digits)

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
            if str(meta.get("model") or "").startswith("claude-code"):
                keep = ("latency_ms", "api_ms", "input_tokens", "output_tokens", "cache_read_tokens",
                        "cache_write_tokens", "cost_usd", "ok", "reason", "model")  # fmt: skip
                self.claude.append({"stage": "draft", "question": self.question(action)[:140],
                                    **{k: meta.get(k) for k in keep if meta.get(k) is not None}})  # fmt: skip
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
                # and its "Select One" dropdown is not opened again for nothing: the one sharing its question, or
                # (an option of the list just opened) the one that opened it, whose label names the question itself
                opened = self.opener() if self.list_option(action) else None
                for a in self.page["actions"]:
                    by_label = opened and opened.get("kind") == "click" and a["label"] == opened.get("action")
                    if planner.SHOWS_NOTHING.search(a.get("label", "")) and (a.get("context") == action["context"]
                                                                             or by_label):  # fmt: skip
                        self.vetoed.add((*self.here(a), a["label"]))
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
            "field_id": action.get("field_id"),  # the field's stable name (Workday), for the options it opens
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

    @staticmethod
    def plain_box(action):
        """A text box that only holds what is typed: no suggestion list, no tags, no search behind it."""
        words = f"{action.get('label') or ''} {action.get('context') or ''}"
        return (
            action.get("role") in {"textbox", "spinbutton"}
            and not action.get("multiple")
            and not planner.is_prompt(action)
            and not planner.LOOKUP.search(words)
            and action.get("input_type") not in {"textarea", None}
        )

    def execute(
        self, action, d, *, text=None, value=None, files=None, source=None, shown=None, expected=None, multiple=False
    ):
        self.browser.act(action, self.page, text=text, value=value, files=files)
        if action["kind"] == "fill" and self.plain_box(action):
            # Workday takes a typed value into its form only when focus leaves the box: pressed Save straight
            # after typing, it says "Phone Number is required". Search and lookup boxes keep focus (their list).
            try:
                self.browser.evaluate("document.activeElement?.blur?.(), true")
            except (StalePage, RuntimeError):
                pass
        # A person's pace (policy.pace_s): the site draws what changed before the next look (waits and scrolls aside).
        if action["kind"] not in {"wait", "scroll"}:
            advancing = action["kind"] == "click" and planner.ADVANCE.search(action.get("label", ""))
            time.sleep(self.policy.pace_after_next_s if advancing else self.policy.pace_s)
            if not advancing:
                # Something new answered (MKS: its terms box, ticked after Save had been pressed three times): Save /
                # Next given up on this page may be tried again.
                self.vetoed = {v for v in self.vetoed if not planner.ADVANCE.search(str(v[-1]))}
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
        elif action["kind"] in {"enter", "click"}:
            self.recheck_prompts(action)
        self.track_items()
        recent = self.history[-3:]
        # Stuck only when a Save / Next among them didn't move the page either: typing a value the field already
        # held changes nothing, and the run should go on to Save rather than stop.
        if (
            len(recent) == 3
            and all(h["page_changed"] is False and h["kind"] != "wait" for h in recent)
            and any(h["kind"] == "click" and planner.ADVANCE.search(str(h.get("action") or "")) for h in recent)
        ):
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
        if not str(current).strip() and same[0].get("role") == "spinbutton" and fill["kind"] == "fill":
            # Workday's Month box redraws as focus jumps to its Year: read it once more before calling it empty
            # (typing it again would send the digits into the Year box).
            time.sleep(0.6)
            try:
                self.page = self.browser.observe()
            except StalePage:
                return
            again = [a for a in self.page["actions"] if a.get("node") == fill["node"]]
            current = again[0].get("value", "") if again else current
        expected = fill["expected"]
        contains = fill["kind"] == "upload" or fill["multiple"]  # one of several files / selected options
        ok = norm(expected) in norm(current) if contains else norm(current) == norm(expected)
        if not ok and re.fullmatch(r"\d{1,4}", str(expected).strip()) and re.fullmatch(r"\d{1,4}", str(current).strip()):
            ok = int(expected) == int(current)  # Workday shows month "04" as "4"
        if not ok and fill["kind"] == "fill" and planner.is_prompt(same[0]):
            # A search prompt (Workday) empties its box and shows the pick beside it: "1 item selected, LinkedIn".
            if self.prompt_picked(fill, same[0]):
                ok = True
            elif not norm(current):
                fill["confirmed"] = None  # not chosen yet: judged again after its Enter or its list
                return
        fill["confirmed"] = ok
        if not ok:
            self.ui.event(f"'{fill['label'][:50]}' now shows {current!r}, expected {expected!r}")

    def prompt_picked(self, fill, field):
        """Does a search prompt show the typed value as chosen ("1 item selected, LinkedIn", or its chip)?"""
        shown = norm(" ".join([field.get("context") or ""] + [
            a["label"] for a in self.page["actions"]
            if a.get("role") == "option" and "press delete" in a["label"].lower()  # Workday's chosen-item chip
        ]))  # fmt: skip
        return bool(norm(fill["expected"])) and norm(fill["expected"]) in shown and (
            bool(planner.SELECTED.search(field.get("context") or "")) or "press delete" in shown
        )

    def recheck_prompts(self, action):
        """After Enter in (or a pick from) a search prompt: the fills still waiting there are read back now."""
        for fill in self.fills:
            if fill.get("confirmed") is not None or fill.get("doc") != self.page.get("doc"):
                continue
            field = next((a for a in self.page["actions"] if a.get("node") == fill["node"]), None)
            if field and (action.get("node") == fill["node"] or action.get("role") == "option"):
                fill["confirmed"] = self.prompt_picked(fill, field) or None

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

    def sign_in_with_google(self):
        """At a sign-in wall with "Continue with Google": click it, choose the Google account whose address is your
        profile's e-mail, and confirm Google's page sharing only your name, e-mail, language and picture. Clicks
        only, each once. Anything Google wants typed (an address, a password, a code) or wider access is left for
        you (False: the run then waits for you as at any sign-in). True once the site is past its sign-in."""
        fact = self.profile.facts.get("personal.email")
        email = (fact.value if fact else "").strip().lower()
        if not email:
            return False
        done = set()
        for _ in range(12):
            page = self.page
            url = page.get("url") or ""
            on_google = url.startswith("https://accounts.google.com/")
            if not on_google and not planner.sign_in_wall(page):
                self.ui.event("Signed in with Google: continuing.")
                return True
            if not on_google:
                target = next((a for a in planner.sso_buttons(page) if "google" in a["label"].lower()), None)
                what = "Continue with Google"
            elif page.get("login") or any(a.get("kind") == "fill" for a in page.get("actions") or []):
                self.ui.event("Google asks for something typed (an address, password or code): that's yours.")
                return False
            else:
                target = next((a for a in page["actions"] if a.get("kind") == "click"
                               and email in (a.get("label") or "").lower()), None)  # fmt: skip
                what = f"Choose {email}"
                if target is None:
                    target = next((a for a in page["actions"] if a.get("kind") == "click"
                                   and a.get("role") in {"button", "link"}
                                   and GOOGLE_GO.fullmatch(a.get("label") or "")), None)  # fmt: skip
                    what = "Continue (Google shares your name, e-mail and picture)"
                    if target is not None and GOOGLE_WIDE.search(page.get("text") or ""):
                        self.ui.event("Google asks to share more than your name and e-mail: that's yours to decide.")
                        return False
            if target is None or (url, target["label"]) in done:
                return False  # nothing safe to click, or the same click again: never retried
            done.add((url, target["label"]))
            try:
                self.browser.act(target, page)
            except (ExecutionUncertain, StalePage):
                return False
            self.history.append({"step": len(self.history) + 1, "action": what, "kind": "click",
                                 "context": "google sign-in", "source": "google sign-in", "url": url,
                                 "page_changed": None})  # fmt: skip
            for _ in range(10):  # Google's windows and redirects take a moment
                time.sleep(1)
                try:
                    self.page = self.browser.observe()
                    break
                except StalePage:
                    continue
        return False

    def back_to_job(self):
        """After a sign-in, a portal may land on your candidate home ("My Applications") instead of the job (Blue
        Yonder's Workday): with no application form showing and the job no longer in the address, back to the job
        link this run started from, whose Apply now goes on with the signed-in account."""
        slug = urlparse(self.start_url).path.rstrip("/").split("/")[-1]
        url = self.page.get("url") or ""
        if not slug or slug in url or planner.application_fields(self.page):
            return
        self.ui.event("signed in, but the portal showed its home page: back to the job")
        try:
            self.browser.navigate(self.start_url)
            self.page = self.browser.observe()
        except (StalePage, RuntimeError, ValueError):
            pass
        self.history.append({"step": len(self.history) + 1, "action": "Back to the job after signing in",
                             "kind": "wait", "context": "", "url": self.start_url, "page_changed": True})  # fmt: skip

    def sign_in_with_password(self):
        """At a portal's own sign-in: sign in with your profile's e-mail and JEV_PORTAL_PASSWORD, or create the
        account first (accounts.py reads the page; the agent types only those two and ticks the terms box of a
        sign-up form). True once past the sign-in; False hands over (or waits) as before. A refused sign-in, or
        the same step twice, ends it: never retried, so no account gets locked."""
        from . import accounts

        secret = accounts.password()
        fact = self.profile.facts.get("personal.email")
        email = (fact.value if fact else "").strip()
        if not secret or not email:
            return False
        start = self.page["url"]
        known = accounts.book(self.profile).get(accounts.site(start), {})
        done, signed_up, signing_up, waits, scrolls, typed = set(), False, False, 0, 0, set()
        verified = False  # the e-mailed verification link was opened in this sign-in
        for _ in range(30):
            page = self.page
            _, boxes, _, _ = accounts.fields(page)
            if not planner.sign_in_wall(page) and not boxes:
                now = datetime.now().isoformat(timespec="seconds")
                created = now if signed_up and not known.get("created") else known.get("created")
                accounts.note(self.profile, start, email=email, last_sign_in=now, created=created)
                self.ui.event("signed in with your portal account: continuing")
                self.back_to_job()
                return True
            text = page.get("text") or ""
            if signed_up and accounts.TAKEN.search(text) and not known.get("created"):
                # The portal says this e-mail has an account already: sign in to it from now on.
                known = {**known, "created": "existing"}
                accounts.note(self.profile, start, email=email, created="existing")
            # Workday may show its sign-in form again with a "verify your e-mail" note once the account is made.
            # ... or ask for it at its sign-in, later, for an account made before ("Verify your account before you
            # sign in"): the link from your e-mail, once.
            if accounts.VERIFY.search(text) and not verified and (not boxes or signed_up or known.get("created")):
                # The account exists now, whatever happens next: sign in, not up, next time.
                if signed_up:
                    accounts.note(
                        self.profile, start, email=email, created=datetime.now().isoformat(timespec="seconds")
                    )
                verified = True
                since = self.mail_since
                try:  # the e-mail may have come during an earlier run: from the account's creation on
                    since = min(since, datetime.fromisoformat(str(known.get("created"))).timestamp() - 300)
                except ValueError:
                    pass
                link = self.mail("link", since=since)
                used = hashlib.sha256(link.encode()).hexdigest()[:16] if link else None
                if link and used == known.get("link_used"):
                    link = None  # opened once already: a one-time link only says "invalid token" the second time
                if not link:
                    self.ui.event("the portal wants its e-mail verified: waiting for that")
                    return False
                self.ui.event("opening the verification link from your e-mail")
                accounts.note(self.profile, start, link_used=used)
                try:
                    self.browser.navigate(link)
                    # Workday's activation page confirms the token from its script after loading: leaving at once
                    # spends the one-time link without the account being verified (Blue Yonder: "invalid token")
                    said = ""
                    for _ in range(20):
                        time.sleep(1)
                        said = (self.browser.evaluate("document.body ? document.body.innerText : ''") or "")[:2000]
                        if ACTIVATED.search(said) or INVALID_LINK.search(said):
                            break
                    self.ui.event("e-mail verified" if ACTIVATED.search(said) else
                                  "the verification link was refused (used or expired)" if INVALID_LINK.search(said)
                                  else "verification page gave no answer in 20 s")  # fmt: skip
                    self.browser.navigate(start)  # back to the job: its sign-in now takes the verified account
                    self.page = self.browser.observe()
                except (StalePage, RuntimeError, ValueError):
                    return False
                self.history.append({"step": len(self.history) + 1, "action": "opened the e-mail verification link",
                                     "kind": "handover", "context": "portal account", "source": "gmail",
                                     "url": start, "page_changed": True})  # fmt: skip
                known = {**known, "created": known.get("created") or "verified"}
                done.clear()  # a new page after the link: its sign-in steps are new too
                signed_up = signing_up = False
                typed.clear()
                continue
            step = accounts.plan(
                page, bool(known.get("created")), tried_sign_up=signed_up, sign_up_form=signing_up and not signed_up,
                passwords_typed=len(typed), looked=scrolls > 0,
            )
            if step[0] == "stop":
                self.ui.event(f"portal sign-in: {step[1]}")
                return False
            kind, target = step[0], step[1]
            if kind == "wait":
                waits += 1
                if waits > 4:
                    self.ui.event(f"portal sign-in: {step[2]}")
                    return False
                accounts.leave_box(self.browser)
                time.sleep(1)
                try:
                    self.page = self.browser.observe()
                except StalePage:
                    pass
                continue
            if kind == "scroll":  # a pop-up form cut short by the window: down to the rest of it
                scrolls += 1
                if scrolls > 8:
                    self.ui.event("portal sign-in: the form's button never came into view")
                    return False
                try:
                    self.browser.act(target, page)
                    self.page = self.browser.observe()
                except (ExecutionUncertain, StalePage, ValueError):
                    pass
                continue
            key = (page["url"], kind, target["label"], target.get("node"))
            if key in done:
                return False  # the same step again: the page isn't taking it
            done.add(key)
            text = (email if step[2] == "email" else secret) if kind == "fill" else None
            try:
                self.browser.act(target, page, text=text) if text else self.browser.act(target, page)
            except (ExecutionUncertain, StalePage, ValueError):
                return False
            if kind == "fill":
                accounts.leave_box(self.browser)
                if step[2] == "password":
                    typed.add(target.get("node"))
            if kind == "switch":  # the other form: its boxes are new
                typed.clear()
            signing_up = (signing_up or len(boxes) >= 2) and kind != "switch"
            signed_up = signed_up or (kind == "click" and step[2] == "sign up")
            shown = {"fill": email if text == email else "********"}.get(kind)  # the password is never written down
            self.history.append({"step": len(self.history) + 1, "action": f"{kind}: {target['label'][:60]}",
                                 "kind": "fill" if kind == "fill" else "click", "typed": shown,
                                 "context": "portal account", "source": "portal account", "url": page["url"],
                                 "page_changed": None})  # fmt: skip
            # A sign-in (or Create Account) navigates, and Workday takes a few seconds: until the page changes.
            settle = 12 if kind == "click" else 1
            deadline = time.monotonic() + settle
            while True:
                time.sleep(0.8 if kind in {"fill", "tick"} else 1.5)
                try:
                    self.page = self.browser.observe()
                except StalePage:
                    if time.monotonic() < deadline + 6:
                        continue
                    return False
                moved = self.page.get("fingerprint") != page.get("fingerprint")
                if kind != "click" or moved or time.monotonic() > deadline:
                    break
        return False

    # ---- the bubble in the tab (helper.py) -------------------------------------------------------------------

    def show_helper(self, status, **state):
        """What the bubble says: what the run is doing, or what it waits for (and the questions, to answer there)."""
        put = getattr(self.browser, "helper_state", None)
        if put:
            put({"status": status, "paused": self.paused, **state})

    def helper_check(self):
        """Your presses in the bubble. Pause holds the run here until Resume (or Continue); Skip ends the job
        (Skipped); answers typed there are saved like answers in Questions. Returns whether you pressed Continue."""
        drain = getattr(self.browser, "helper_events", None)
        if not drain:
            return False
        pressed, told = False, False
        while True:
            for event in drain():
                kind = event.get("type")
                if kind == "pause":
                    self.paused = True
                elif kind in {"resume", "continue"}:
                    self.paused, pressed = False, pressed or kind == "continue"
                elif kind == "skip":
                    raise Skipped()
                elif kind == "answers":
                    self.save_answers(event.get("answers") or {})
                    pressed = True
            if not self.paused:
                if told:
                    self.ui.event("resumed by you")
                return pressed
            if not told:
                self.show_helper("Paused. Nothing happens until you press Resume.")
                self.ui.event("paused by you")
                told = True
            time.sleep(1)

    def save_answers(self, answers):
        """Answers you typed in the bubble, into QUESTIONS.md (every later form reuses them, as from the app)."""
        from . import inbox

        path = self.profile.questions_path
        if not path or not answers:
            return

        def change(entries):
            for question, answer in answers.items():
                entry = next((e for e in entries if same_question(e["question"], question)), None)
                if entry is None:
                    entry = {"question": question, "options": [], "urls": [self.page["url"]], "answer": ""}
                    entries.append(entry)
                entry["answer"] = " ".join(str(answer).split())

        inbox.update(path, change)

    def askable(self, question):
        """A question for QUESTIONS.md: not a code the site just sent (typed in the tab, never kept) and not a
        field on your never-fill list."""
        return not not_a_question(question) and not self.policy.never_fill({"label": question, "context": ""})

    def open_questions(self):
        """The required questions this page still needs and nothing on file answers, once each."""
        out, seen = [], set()
        for entry in self.inbox + self.unanswered_entries():
            question = entry["question"]
            if question in seen or self.profile.saved_answer(question) or not self.askable(question):
                continue
            seen.add(question)
            out.append(entry)
        return out

    def continue_token(self):
        """A fresh token for a wait: the app's Continue button drops runs/continue/<token> to end it early."""
        return uuid.uuid4().hex[:8]

    def continued(self, token):
        """You pressed Continue in the app for this wait (and the signal is used up)."""
        flag = self.run_dir / "continue" / token
        if not flag.exists():
            return False
        flag.unlink(missing_ok=True)
        return True

    def resume_page(self, what, context):
        """Carry on with this form after you stepped in: offer again what was set aside, and log it."""
        self.skipped.clear()
        self.vetoed.clear()
        self.skipped_questions.clear()
        self.no_review.clear()
        self.left = [x for x in self.left if x.get("url") != self.page["url"]]
        if getattr(self.ui, "stopped", None):
            self.ui.stopped = None
        self.history.append(
            {
                "step": len(self.history) + 1,
                "action": what,
                "kind": "handover",
                "context": context[:160],
                "source": "you",
                "page_changed": None,
                "url": self.page["url"],
            }
        )
        self.status = "ready"

    def mail(self, want, timeout=150, since=None):
        """The newest code ("code") or verification link ("link") this portal e-mailed since the wait began (or
        `since`), from your Gmail (gmail.py, read-only), or None when Gmail isn't connected or nothing came."""
        from . import gmail

        folder = self.profile.questions_path.parent if self.profile.questions_path else None
        if not self.policy.email_codes or not folder or not gmail.configured(folder):
            return None
        self.show_helper(f"Reading the {'code' if want == 'code' else 'verification link'} from your e-mail…")
        start = self.mail_since if since is None else since
        return gmail.wait_for(folder, start, self.page["url"], want, timeout=timeout)

    def type_mailed_code(self):
        """A code box (or one box per digit): the code from your e-mail typed in. True once it's in."""
        boxes = [a for a in self.page["actions"] if a["kind"] == "fill" and one_time_code(self.question(a))]
        empty = [a for a in boxes if not a.get("value")]
        if not empty:
            return False
        code = self.mail("code")
        if not code:
            return False
        parts = list(code) if len(empty) == len(code) and len(empty) > 1 else [code]
        for box, part in zip(empty, parts):
            try:
                self.browser.act(box, self.page, text=part)
            except (ExecutionUncertain, StalePage, ValueError):
                return False
        self.history.append({"step": len(self.history) + 1, "action": "typed the e-mailed verification code",
                             "kind": "fill", "typed": "******", "context": "verification code", "source": "gmail",
                             "url": self.page["url"], "page_changed": None})  # fmt: skip
        try:
            self.page = self.browser.observe()
        except StalePage:
            pass
        self.resume_page("Typed the code from your e-mail", "verification code")
        self.ui.event("code typed from your e-mail: carrying on")
        return True

    def wait_for_code(self):
        """Unattended, at a box for a code the site just sent you (e-mail or SMS verification): alert you, show the
        tab and wait (login_wait_s) for you to type it there. Codes are new every time, so they never go to
        QUESTIONS.md. True once the code boxes are filled or gone, or you press Continue in the app."""
        wait = self.policy.login_wait_s
        if wait <= 0:
            return False
        self.browser.show()
        token = self.continue_token()
        self.ui.event(
            f"\a!!! waiting for you to enter the verification code in the open tab (up to {max(1, wait // 60)} min) "
            f"[continue {token}]: {self.page['url'][:100]}"
        )
        if self.type_mailed_code():
            return True
        deadline = time.monotonic() + wait
        self.show_helper(
            "Type the verification code the site sent you (e-mail or SMS), then it carries on.", waiting=True
        )
        while time.monotonic() < deadline:
            time.sleep(3)
            done = self.continued(token) or self.helper_check()
            try:
                self.page = self.browser.observe()
            except StalePage:
                continue  # the site is checking the code
            boxes = [a for a in self.page["actions"] if a["kind"] == "fill" and one_time_code(self.question(a))]
            if done or not boxes or all(a.get("value") for a in boxes):
                self.resume_page("The candidate entered the verification code", "verification code")
                self.ui.event("code entered: carrying on")
                return True
        self.ui.event("no code entered in time: the job stops here")
        return False

    def answers_given(self, entries):
        """{question: your answer} for those of `entries` answered in QUESTIONS.md so far."""
        from . import inbox

        found = {}
        for entry in inbox.read(self.profile.questions_path):
            if not entry["answer"]:
                continue
            for wanted in entries:
                if wanted["question"] not in found and same_question(entry["question"], wanted["question"]):
                    found[wanted["question"]] = entry["answer"]
        return found

    def consult_teacher(self):
        """The teacher (teacher.py) answers what it can of this page's open questions from your profile. Any answer:
        it is used like a saved answer, and the run carries on with this form (True). The rest go to you."""
        from . import teacher

        if not self.policy.teacher or self.teacher_rounds >= ANSWER_ROUNDS or not teacher.available():
            return False
        try:
            self.page = self.browser.observe()
        except StalePage:
            pass
        entries = self.open_questions()
        if not entries:
            return False
        self.teacher_rounds += 1
        self.show_helper(f"Working out {len(entries)} question(s) from your profile…")
        given = teacher.ask(entries, self.profile, self.page["url"], self.run_dir)
        if not given:
            return False
        self.profile.saved.update(given)
        self.asked += [{"question": q, "answered": True, "saved": True, "source": "teacher"} for q in given]
        self.inbox = [e for e in self.inbox if e["question"] not in given]
        self.ui.event(f"teacher answered {len(given)} of {len(entries)} from your profile: carrying on")
        self.resume_page(f"The teacher answered {len(given)} question(s) from the profile", " | ".join(given)[:160])
        return True

    def wait_for_answers(self):
        """Unattended, about to stop on questions only you can answer: put them in QUESTIONS.md now, then wait
        (policy.answer_wait_s) for you to answer them in the app or the file. All answered: they become saved
        answers, what was set aside on this page is offered again, and the run carries on with this same form
        (True). Otherwise False: the job stops as before, and is offered for a re-run once you answer."""
        wait = self.policy.answer_wait_s
        if wait <= 0 or not self.profile.questions_path or self.answer_rounds >= ANSWER_ROUNDS:
            return False
        try:
            self.page = self.browser.observe()
        except StalePage:
            pass
        entries = self.open_questions()
        if not entries:
            return False
        self.answer_rounds += 1
        self.profile.add_pending(entries)
        asked = " | ".join(e["question"][:80] for e in entries)
        token = self.continue_token()
        self.ui.event(
            f"waiting for your answers (up to {max(1, wait // 60)} min, in Questions) [continue {token}]: {asked}"
        )
        deadline = time.monotonic() + wait
        self.show_helper(f"Waiting for your answer{'s' if len(entries) > 1 else ''}: answer here or in the app.",
                         waiting=True, questions=[{"question": e["question"], "options": e.get("options") or []}
                                                  for e in entries])  # fmt: skip
        while time.monotonic() < deadline:
            time.sleep(3)
            pressed_here = self.helper_check()  # answers typed in the bubble are saved before they're read below
            given = self.answers_given(entries)
            # All answered, or you pressed Continue (you filled them on the page yourself, or want it to try again).
            pressed = self.continued(token) or (pressed_here and len(given) < len(entries))
            if len(given) < len(entries) and not pressed:
                continue
            self.profile.saved.update(given)  # your answers, typed by you: used as saved answers from here on
            self.asked += [{"question": q, "answered": True, "saved": True} for q in given]
            self.inbox = [e for e in self.inbox if e["question"] not in given]
            # What was set aside on this page was set aside for want of these answers: offer it all again.
            what = f"The candidate answered {len(given)} question(s)" + (" and pressed Continue" if pressed else "")
            try:
                self.page = self.browser.observe()  # you may have typed on the page itself
            except StalePage:
                pass
            self.resume_page(what, asked)
            self.ui.event("answered: carrying on with this form" if not pressed else "continuing: you said go ahead")
            return True
        self.ui.event("no answer in time: the job stops here, and can be re-run once you answer")
        return False

    def wait_for_login(self):
        """Unattended, at a sign-in page: alert the candidate, show the tab and wait for them to sign in or create
        the account (once per company portal; the browser keeps the session). True once the page is past it."""
        wait = self.policy.login_wait_s
        if wait <= 0:
            return False
        self.browser.show()
        self.ui.event(
            f"\a!!! LOGIN NEEDED: sign in (or create your account) in the open tab: {self.page['url'][:120]}  "
            f"Waiting up to {wait // 60} min (with 'Sign in / sign up on portals' on, it signs in itself where it can)."
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
        # Not the Save / Next already given up on here: what it is waiting for
        if self.unattended and self.rescue(reason, avoid=[v[-1] for v in self.vetoed]):
            return  # Claude found the next step on this page: the run goes on
        if getattr(self, "rescue_why", ""):
            reason = f"{reason} Claude: {self.rescue_why}"
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

    def job_in_your_country(self):
        """Your country when the job's address or page names it, your city or a place you'd work in (for India
        also its main cities): "eligible to work in the country of this position" then has your answer. Else None
        (a job elsewhere: that question is yours)."""
        country = getattr(self.profile.by_id.get(safe_id("personal.country")), "value", "") or ""
        if not country:
            return None
        places = {country}
        for key, fact in self.profile.facts.items():
            if key == "personal.city" or key.startswith("preferences.preferred_locations"):
                places.update(p.strip() for p in re.split(r"[,;/]", fact.value) if p.strip())
        where = f"{self.start_url} {self.page.get('title', '')} {self.page.get('text', '')[:3000]}"
        named = "|".join(rf"\b{re.escape(p)}\b" for p in places if len(p) >= 3)
        if (named and re.search(named, where, re.I)) or (norm(country) == "india" and INDIAN_JOB.search(where)):
            return country
        return None

    def exhausted_reason(self, reason):
        """The planner's "no application form here" when the page does have fields, every one of them already tried
        or left for you (Maersk, after its Save was given up on): say that, what was left and the site's errors."""
        from . import teacher

        if not reason or not reason.startswith("Nothing here leads to an application form"):
            return reason
        fields = [a for a in self.page["actions"] if a["kind"] in {"fill", "select", "setdate", "upload"}
                  or (a["kind"] == "click" and a.get("role") in {"option", "checkbox", "radio"})]  # fmt: skip
        here = [x["what"] for x in self.left if x.get("url") == self.page["url"]]
        if not fields and not here:
            return reason
        errors = teacher.page_errors(self.page)[:3]
        return (
            "Everything the agent could do on this page was tried or left for you"
            + (": " + "; ".join(dict.fromkeys(here[-4:])) if here else "")
            + "." + (" The site says: " + " / ".join(errors) if errors else "")
        )

    def rescue(self, reason, avoid=()):
        """About to stop, nobody watching: Claude (teacher.rescue) gets the page moving again, up to RESCUE_STEPS steps,
        seeing after each what happened. Code checks every step (teacher.rescue, rescue_refusal): no submit,
        text only a fact's value, a sensitive question only with your profile's answer. Three rescues at most in a
        run, never a control in `avoid` (the one that just failed). True once a step was carried out (the run goes
        on); False with Claude's reason in self.rescue_why (said in the handover)."""
        from . import teacher

        self.rescue_why = ""
        if self.rescues >= 3 or not self.policy.teacher or not teacher.available():
            return False
        self.rescues += 1
        self.show_helper("Stuck here: asking Claude to get the page moving…")
        tried = []
        for _ in range(RESCUE_STEPS):
            try:
                self.page = self.browser.observe()
            except StalePage:
                break
            reply = teacher.rescue(self.page, self.history, self.profile, reason, self.policy, self.run_dir, avoid,
                                   tried)  # fmt: skip
            if reply.get("done"):
                self.ui.event(f"Claude: fixed, carrying on ({reply.get('why', '')[:80]})")
                if not tried:
                    self.rescue_why = f"saw nothing left to fix here ({reply.get('why', '')[:120]})"
                break
            action = reply.get("action")
            refusal = self.rescue_refusal(action, reply) if action else None
            if not action or refusal:
                tried.append({"did": action["label"][:70] if action else None,
                              "result": f"refused: {refusal}" if refusal else reply.get("why", "")[:120]})  # fmt: skip
                if action or reply.get("refused"):
                    continue  # refused: Claude may find another way
                self.rescue_why = reply.get("why", "")
                break
            text = reply.get("text")
            self.ui.event(f"Claude's next step: {action['label'][:60]} ({reply.get('why', '')[:80]})")
            operation = {"fill": "TYPE_TEXT", "select": "SELECT", "key": "KEY", "scroll": "SCROLL_DOWN"}
            d = {"operation": operation.get(action["kind"], "CLICK"), "target": action.get("id"),
                 "operation_probability": 1.0, "target_probability": 1.0, "latency_ms": 0}  # fmt: skip
            before = self.page
            try:
                self.execute(action, d, text=text, source="teacher", shown=text, expected=text)
            except (StalePage, ExecutionUncertain, ValueError) as error:
                tried.append({"did": action["label"][:70], "result": f"failed: {error}"[:120]})
                continue
            moved = self.page.get("fingerprint") != before.get("fingerprint")
            tried.append({"did": f"{action['kind']} {action['label'][:60]}", "typed": (text or "")[:40],
                          "result": "page changed" if moved else "nothing changed",
                          "errors_after": teacher.page_errors(self.page)[:5]})  # fmt: skip
        else:
            self.rescue_why = self.rescue_why or "five steps didn't get the page moving"
        done = any(t["result"] in {"page changed", "nothing changed"} for t in tried)
        self.rescue_log.append({"reason": reason[:160], "steps": [t.get("did") for t in tried], "helped": done,
                                "why": "" if done else self.rescue_why[:200], "url": self.page.get("url")})  # fmt: skip
        if done:
            self.vetoed = {v for v in self.vetoed if not planner.ADVANCE.search(str(v[-1]))}  # Save may be tried again
        elif not self.rescue_why:
            self.rescue_why = "; ".join(str(t["result"]) for t in tried)[:200]
        if not done:
            self.rescue_log[-1]["why"] = self.rescue_why[:200]
        return done

    def rescue_refusal(self, action, reply):
        """Why a step Claude proposed can't be taken here, or None. A sensitive question (your citizenship, a
        consent) only with the answer your profile gives: the option your facts settle, the list's own wording of
        the fact Claude names when that fact is the one the rules give this question, or a plain consent box.
        Opening its dropdown answers nothing, so that may always be done."""
        question = self.opened_question() if self.list_option(action) else None
        judged = {**action, "context": question} if question else action
        if not self.policy.sensitive(judged):
            return None
        if action.get("role") == "button" and planner.SHOWS_NOTHING.search(action.get("label") or ""):
            return None  # opening a dropdown: the option clicked next is the answer
        if self.matches_profile(judged) or self.consents(action):
            return None
        cited = str(reply.get("fact") or "")
        asked = question or action["label"]
        field = ({"label": question, "context": question, "field_id": (self.opener() or {}).get("field_id")}
                 if question else {k: action.get(k) for k in ("label", "context", "section", "field_id")})  # fmt: skip
        key = rules.key_for(field, self.profile, set(), page_text=self.page.get("text", ""))
        if cited and cited in (key if isinstance(key, tuple) else (key,)) and cited in self.profile.facts:
            # The fact the rules give this question, in the list's own words ("India" for nationality "Indian")
            self.mapped.add((norm(asked), norm(planner.option_text(action))))
            return None
        return "a sensitive question that nothing on file answers this way"

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
            # The latest decisions, with their targets: a loop that does nothing visible shows up here.
            "decisions_tail": [
                {**{k: v for k, v in x.items() if k != "url"},
                 "label": next((a["label"][:60] for a in self.page.get("actions") or [] if a.get("id") == x.get("target")), None)}
                for x in self.decisions[-30:]
            ],  # fmt: skip
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
            "pages": str(self.recorder.folder) if self.recorder.count else None,  # kept for replay tests
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
        self.inbox = [e for e in self.inbox if self.askable(e["question"])]  # codes are typed in the tab, not kept
        report["pending_total"] = self.profile.add_pending(self.inbox, done)
        # The questions this job stopped on: once all are answered, the app offers to run it again.
        report["waiting_on"] = list(
            dict.fromkeys(e["question"] for e in self.inbox if not self.profile.saved_answer(e["question"]))
        )
        report["claude"] = self.claude
        from . import metrics

        report.update(metrics.tags(self.start_url, self.status, self.handovers, self.left))
        report["rescues"], report["closest"] = self.rescue_log, self.closest_log
        if self.receipt:
            receipt = {k: v for k, v in self.receipt.items() if not k.startswith("_")}
            head = f"{receipt['at']}  {receipt['url']}\n{receipt['title']}\n\n"
            (folder / "receipt.txt").write_text(head + receipt["text"] + "\n", encoding="utf-8")
            if self.receipt.get("_png"):
                (folder / "receipt.png").write_bytes(self.receipt["_png"])
                receipt["screenshot"] = "receipt.png"
            report["receipt"] = {k: v for k, v in receipt.items() if k != "text"} | {"text": receipt["text"][:1500]}
        write_text(Path(report["path"]), json.dumps(report, indent=2, ensure_ascii=False))
        self.ui.review(report)
        return report
