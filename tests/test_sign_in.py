"""Sign-in walls: "Continue with Google"-style pages count as sign-ins, and signing in with Google is clicks only."""

from dataclasses import replace

from jev_apply import agent as loop
from jev_apply import planner
from jev_apply.policy import Policy
from jev_apply.profile import Profile
from jev_apply.ui import UnattendedUI

from .test_agent import PROFILE, FakeBrowser

EMAIL = "aarav.sharma@mail.test"


def page(url, actions, text="", login=False):
    acts = [{"id": f"e{n}", "node": n, **a} for n, a in enumerate(actions, 1)]
    acts.append({"id": "wait", "kind": "wait", "label": "Wait for the page to update"})
    return {"url": url, "title": "", "text": text, "actions": acts, "login": login, "doc": url, "fingerprint": url}


def button(label):
    return {"kind": "click", "role": "button", "label": label}


SITE = "https://jobs.example.test/apply/123"
SSO_PAGE = page(SITE, [button("Continue with Google"), button("Sign in with Microsoft")], "Sign in to apply")
CHOOSER = page(
    "https://accounts.google.com/o/oauth2/v2/auth/oauthchooseaccount",
    [button(f"Aarav Sharma {EMAIL}"), button("Someone Else other@mail.test"), button("Use another account")],
    "Choose an account to continue to Example Jobs",
)
CONSENT = page(
    "https://accounts.google.com/signin/oauth/id",
    [button("Cancel"), button("Continue")],
    "Sign in to Example Jobs. By continuing, Google will share your name, email address, language preference, and "
    "profile picture with Example Jobs.",
)
FORM = page(SITE, [{"kind": "fill", "role": "textbox", "label": "Current CTC (in LPA)", "value": ""}], "Apply")


def build(tmp_path, pages, **policy):
    browser = FakeBrowser(pages)
    data = {**PROFILE, "personal": {**PROFILE["personal"], "email": EMAIL}}
    profile = Profile(data, tmp_path, learned_path=tmp_path / "learned.json")
    return loop.Agent(browser, profile, replace(Policy.load(), **policy), UnattendedUI(), run_dir=tmp_path / "runs")


def test_sso_only_pages_and_identity_provider_pages_are_sign_in_walls():
    assert planner.sign_in_wall(SSO_PAGE) and planner.sign_in_wall(CHOOSER)
    assert not planner.sign_in_wall(FORM)
    # "Continue with Google" beside a real application form is an option, not a wall; "Apply with LinkedIn" neither.
    mixed = page(SITE, [button("Continue with Google"), *FORM["actions"][:1]])
    assert not planner.sign_in_wall(mixed)
    assert not planner.sign_in_wall(page(SITE, [button("Apply with LinkedIn")]))
    # An account form (e-mail + password boxes aside) with a Google button is still a sign-in.
    account = page(SITE, [{"kind": "fill", "label": "Email address"}, button("Sign up with Google")])
    assert planner.sign_in_wall(account)


def test_google_sign_in_clicks_your_account_and_basic_sharing_only(tmp_path, monkeypatch):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)  # Google's redirects are instant here
    agent = build(tmp_path, [SSO_PAGE, CHOOSER, CONSENT, FORM], google_sign_in=True)
    assert agent.sign_in_with_google()
    clicked = [action for action, _ in agent.browser.acts]
    assert clicked == ["e1", "e1", "e2"]  # Continue with Google, your account, Continue
    assert all("text" not in kw or kw["text"] is None for _, kw in agent.browser.acts)  # nothing typed
    assert [h["action"] for h in agent.history][-1].startswith("Continue (Google shares")


def test_google_sign_in_leaves_typing_and_wider_access_to_you(tmp_path, monkeypatch):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    password = page(
        "https://accounts.google.com/v3/signin/challenge/pwd", [{"kind": "fill", "label": "Enter your password"}]
    )
    agent = build(tmp_path, [SSO_PAGE, password], google_sign_in=True)
    assert not agent.sign_in_with_google()
    assert [a for a, _ in agent.browser.acts] == ["e1"]  # only the Google button: nothing typed

    wide = {
        **CONSENT,
        "text": "Example Jobs wants to access your Google Account: See, edit, create and delete your Google Drive files",  # noqa: E501
    }
    agent = build(tmp_path, [SSO_PAGE, CHOOSER, wide], google_sign_in=True)
    assert not agent.sign_in_with_google()
    assert len(agent.browser.acts) == 2  # Google button and your account, never "Continue" on wider access

    other = {**CHOOSER, "actions": [a for a in CHOOSER["actions"] if EMAIL not in a.get("label", "")]}
    agent = build(tmp_path, [SSO_PAGE, other], google_sign_in=True)
    assert not agent.sign_in_with_google() and len(agent.browser.acts) == 1  # not your account: never another one


def test_google_sign_in_is_off_unless_you_turn_it_on():
    assert Policy().google_sign_in is False


def test_a_closed_popup_returns_the_run_to_its_tab():
    from jev_apply.transport import ChromeTransport

    calls = []
    tab = object.__new__(ChromeTransport)
    tab.target, tab.opened = "popup", ["job", "popup"]

    def cdp(method, **params):
        calls.append(method)
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "job", "type": "page"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "s2"}
        return {}

    tab._cdp = cdp
    assert tab.back_to_open_tab() and tab.target == "job" and tab.session == "s2" and tab.opened == ["job"]
    assert not tab.back_to_open_tab()  # still open: nothing to do
