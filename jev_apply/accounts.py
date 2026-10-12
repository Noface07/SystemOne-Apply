"""Portal accounts: signing in, or creating the account first, on job portals that want one (Workday, iCIMS, Oracle,
SuccessFactors...), when policy.portal_accounts is on.

The e-mail is your profile's; the password is JEV_PORTAL_PASSWORD from .env (one password for every portal, as you
chose; never written to a report, a log or the repo). Nothing else is ever typed on these pages, and a portal that
refuses the sign-in once is not retried, so no account gets locked. Which portals have an account is kept in
data/accounts.json (site -> e-mail, created, last sign-in), so a portal where one was created is signed in to,
not signed up again.

Page reading is plain code: e-mail and password boxes by their type and label, the button by its words."""

import os
import re
from urllib.parse import urlparse

from .files import write_json

EMAIL = re.compile(r"e-?mail|user ?name|login id|user ?id", re.I)
CONFIRM = re.compile(r"verify|confirm|re-?enter|repeat|again", re.I)
SIGN_IN = re.compile(r"^\W*(sign ?in|log ?in|login|continue|next)\W*$", re.I)
SIGN_UP = re.compile(r"^\W*(create (an )?account|sign ?up|register|create)\W*$", re.I)
TO_SIGN_UP = re.compile(r"create (an )?account|sign ?up|register|new user|don.?t have an account", re.I)
AGREE = re.compile(r"\bagree\b|terms|privacy|consent|i (have )?read|acknowledge", re.I)
REFUSED = re.compile(
    r"(incorrect|invalid|wrong|doesn.?t match|not recogni[sz]ed|unable to sign|could ?n.?t sign)"
    r".{0,40}(password|e-?mail|user|credentials|sign|account)|no account|account (does not|doesn.?t) exist|"
    r"(password|e-?mail|credentials).{0,30}(incorrect|invalid|wrong)",
    re.I,
)
# The portal's answer to Create Account when the e-mail has one. Not "Already have an account? Sign In": that
# link sits on every Workday sign-up form.
TAKEN = re.compile(
    r"(account|e-?mail|user)[^.?!]{0,40}already (exists|registered|in use|taken|been registered)|"
    r"already (an|a) account (with|for|associated)|account with this e-?mail (address )?(already )?exists",
    re.I,
)
# The words of a sign-up form, seen even when its second password box is scrolled off or covered.
SIGN_UP_FORM = re.compile(
    r"verify (new )?password|confirm (your )?(new )?password|re-?enter (your )?password|already have an account", re.I
)
# A page saying the account waits for its e-mail to be verified ("Verify New Password" on the sign-up form doesn't).
VERIFY = re.compile(
    r"verify your (e-?mail|account)|confirm your (e-?mail|account)|check your (e-?mail|inbox)|"
    r"(sent|emailed) (you )?(a|an) (verification|confirmation|activation)|activation (link|e-?mail)|"
    r"verification (link|e-?mail) (has been |was )?sent",
    re.I,
)


def password():
    return (os.environ.get("JEV_PORTAL_PASSWORD") or "").strip()


def site(url):
    """The portal an account belongs to: a Workday tenant (acme.wd3.myworkdayjobs.com) is its own site."""
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def book_path(profile):
    folder = profile.questions_path.parent if profile.questions_path else None
    return folder / "accounts.json" if folder else None


def book(profile):
    import json

    path = book_path(profile)
    return json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else {}


def note(profile, url, **changes):
    path = book_path(profile)
    if not path:
        return
    accounts = book(profile)
    entry = accounts.setdefault(site(url), {"email": changes.pop("email", "")})
    entry.update({k: v for k, v in changes.items() if v is not None})
    write_json(path, accounts)


def leave_box(browser):
    """Focus out of the box just typed in: Workday's "Password Requirements" panel shows while the password box
    has focus, right over the Verify New Password box, and the sign-up form would then look like a sign-in."""
    try:
        browser.evaluate("document.activeElement?.blur?.(), true")
    except Exception:  # noqa: BLE001 - a page navigating away has nothing focused to leave
        pass


def fields(page):
    """(e-mail box, password boxes, buttons, agree boxes, sign-up link) on a sign-in or sign-up page."""
    actions = [a for a in page.get("actions") or [] if not a.get("nav")]
    boxes = [a for a in actions if a.get("kind") == "fill"]
    # Password boxes come apart from the actions (browser.observe), so nothing else ever sees them.
    passwords = [a for a in page.get("secrets") or [] if a.get("kind") == "fill" and not a.get("nav")]
    email = next((a for a in boxes if a.get("input_type") == "email"), None) or next(
        (a for a in boxes if a not in passwords and EMAIL.search(f"{a['label']} {a.get('placeholder') or ''}")), None
    )
    clicks = [a for a in actions if a.get("kind") == "click" and a.get("role") in {"button", "link"}]
    agree = [a for a in actions if a.get("kind") == "click" and a.get("role") == "checkbox"
             and a.get("checked") == "false" and AGREE.search(f"{a['label']} {a.get('context') or ''}")]  # fmt: skip
    return email, passwords, clicks, agree


def plan(page, known, tried_sign_up=False, sign_up_form=False, passwords_typed=0, looked=False):
    """The next step on a sign-in / sign-up page: ("fill", box, what) | ("tick", box) | ("click", button, mode) |
    ("switch", link) to the other form | ("scroll", action) down the form | ("wait", None, why) | ("stop", reason).

    `known`: this portal has an account for the e-mail (created here before, or the portal said it exists).
    `tried_sign_up`: Create Account was pressed in this sign-in, so an "already exists" message is the portal's
    answer to it. `sign_up_form`: this form showed two password boxes before (one may be scrolled off or covered
    now). `passwords_typed`: password boxes typed in so far on this form. `looked`: the form was scrolled
    already, so a Create Account link below the fold would have shown.

    Two password boxes, or the words of one ("Verify New Password", "Already have an account?"), make a sign-up
    form; one box a sign-in. With no account on record the agent creates one first; a portal that answers
    "already exists" is then signed in to. A small window cuts a pop-up form short (Workday's Create Account):
    whatever is visible is filled, then the form is scrolled to the rest and to its button."""
    email, passwords, clicks, agree = fields(page)
    text = page.get("text") or ""
    taken = tried_sign_up and bool(TAKEN.search(text))
    if REFUSED.search(text) and not taken:
        return ("stop", "the portal refused the sign-in")
    known = known or taken
    down = next((a for a in page.get("actions") or [] if a.get("id") == "scroll_down"), None)
    to_sign_in = next(
        (c for c in clicks if SIGN_IN.search(c["label"]) or re.search(r"\b(sign ?in|log ?in)\b", c["label"], re.I)),
        None,
    )
    to_sign_up = next((c for c in clicks if TO_SIGN_UP.search(c["label"])), None)
    signing_up = len(passwords) >= 2 or (sign_up_form and bool(passwords)) or bool(SIGN_UP_FORM.search(text))
    if signing_up and known and to_sign_in:
        return ("switch", to_sign_in)  # the account exists: its sign-in form, not a second account
    if len(passwords) == 1 and not signing_up and not known and to_sign_up:
        return ("switch", to_sign_up)  # no account here yet: create it first
    if passwords and not signing_up and not known and not to_sign_up and down and not looked:
        return ("scroll", down)  # a sign-in form with no account on record: its Create Account link may be lower
    if email and not email.get("value"):
        return ("fill", email, "email")
    for box in passwords:
        if not box.get("value"):
            return ("fill", box, "password")
    if not passwords and not email and not signing_up:
        if to_sign_up and not known:
            return ("switch", to_sign_up)
        if to_sign_in:
            return ("switch", to_sign_in)  # only a Sign In button (Blue Yonder): it opens the form
        return ("scroll", down) if down else ("stop", "no e-mail or password box here")
    if signing_up:
        filled = sum(1 for box in passwords if box.get("value"))
        if max(passwords_typed, filled) < 2:  # the Verify New Password box is further down, or under a popup
            return ("scroll", down) if down else ("wait", None, "the second password box isn't reachable")
        for box in agree:
            return ("tick", box)
        button = next((c for c in clicks if c.get("role") == "button" and SIGN_UP.search(c["label"])), None)
        if button:
            return ("click", button, "sign up")
        return ("scroll", down) if down else ("stop", "no Create Account button")
    button = next((c for c in clicks if c.get("role") == "button" and SIGN_IN.search(c["label"])), None)
    if button:
        return ("click", button, "sign in")
    if down:
        return ("scroll", down)
    if not passwords and not known and to_sign_up:
        return ("switch", to_sign_up)
    return ("stop", "no Sign In button")
