"""Run the agent unattended on real, public application pages with the example (fake) profile, to find what
still breaks on live sites. Nothing leaves the browser: every request that could submit or save anything
(POST/PUT/PATCH/DELETE, uploads, GraphQL mutations) is blocked, only read-only lookups (searches, location
suggestions, GraphQL queries) go through, and the agent never clicks a final submit anyway.

uv run --extra playwright --extra laya python scripts/live_check.py URL [URL ...] [--headed] [--out DIR]
    --chrome   in a new background tab of your everyday Chrome (your logins: LinkedIn, Indeed, Naukri). The tab's
               requests are paused and only reads go through, the same rule as above; the tab is closed at the end.
    --recon P  don't run the agent: load each page and print its links matching the regex P (to find job URLs).
    --data D   use your own profile in D (e.g. data/automation) instead of the example one. Still nothing is sent.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jev_apply.agent import Agent  # noqa: E402
from jev_apply.browser import Browser  # noqa: E402
from jev_apply.policy import Policy  # noqa: E402
from jev_apply.profile import Profile  # noqa: E402
from jev_apply.transport import PlaywrightTransport  # noqa: E402
from jev_apply.ui import UnattendedUI  # noqa: E402

READ_ONLY = re.compile(r"graphql|search|typeahead|autocomplete|suggest|lookup|location|geocod|places", re.I)
WRITES = re.compile(r"\bmutation\b|multipart/form-data|submit|apply", re.I)
# LinkedIn's web app renders by POSTing to /flagship-web/... (pages, navigations such as opening the Easy Apply
# form, components, app config): reads. Server requests that name an action (apply, submit, save, follow,
# message...) stay blocked: that is where an application would be sent.
LINKEDIN_READ = re.compile(r"linkedin\.com/flagship-web/", re.I)
LINKEDIN_ACTION = re.compile(
    r"apply|submit|save|follow|send|message|connect|invite|react|comment|share|delete|update|create|upload|report|"
    r"block|dismiss|hide|mute|post|like|endorse|withdraw",
    re.I,
)


def reads_only(method, url, body, kind, resource_type, log):
    """May this request go out? Reads yes; anything that could submit or save, no."""
    if method in {"GET", "HEAD", "OPTIONS"}:
        return True
    rendering = re.search(r"flagship-web/rsc-action/actions/(navigation|component|app-config)\b", url)
    if LINKEDIN_READ.search(url) and (rendering or not LINKEDIN_ACTION.search(url)):
        log.append(("allowed", method, url[:120]))  # LinkedIn's app fetches its pages and components by POST
        return True
    if resource_type.lower() == "document" and re.search(r"/job(\?|$)|in_iframe=1", url):
        log.append(("allowed", method, url[:120]))  # iCIMS loads the job page into its frame with a form POST
        return True
    try:  # GraphQL: a query reads, a mutation writes (Ashby saves every field as you type: blocked)
        queries = [q.get("query", "") for q in (lambda b: b if isinstance(b, list) else [b])(json.loads(body))]
        graphql = all(isinstance(q, str) and q.lstrip().startswith(("query", "{")) for q in queries)
    except (ValueError, AttributeError):
        graphql = False
    if graphql or (READ_ONLY.search(url) and not WRITES.search(body[:4000] + " " + kind)):
        log.append(("allowed", method, url[:120]))
        return True
    log.append(("BLOCKED", method, url[:260]))
    return False


def guard(log):
    def route(route, request):
        try:
            body = request.post_data or ""
        except Exception:  # binary body (a file): never sent
            body = "multipart/form-data"
        kind = request.headers.get("content-type", "")
        if reads_only(request.method, request.url, body, kind, request.resource_type, log):
            return route.continue_()
        return route.abort()

    return route


class ChromeGuard:
    """Your Chrome: every request of the test tab (and any tab it opens) is paused, and only reads continue."""

    def __init__(self, transport, log):
        from browser_harness.helpers import cdp, drain_events

        self.cdp, self.drain, self.log, self.sessions, self.stopped = cdp, drain_events, log, set(), False
        self.transport = transport
        self.drain()  # events from before this run belong to nobody here
        self.watch(transport.session)
        adopt = transport.adopt_popup

        def adopt_and_watch():
            moved = adopt()
            if moved:
                self.watch(transport.session)
            return moved

        transport.adopt_popup = adopt_and_watch
        threading.Thread(target=self.loop, daemon=True).start()

    def watch(self, session):
        self.cdp("Fetch.enable", session_id=session, patterns=[{"urlPattern": "*", "requestStage": "Request"}])
        self.sessions.add(session)

    def loop(self):
        while not self.stopped:
            try:
                events = self.drain()
            except Exception:
                time.sleep(0.05)
                continue
            for event in events:
                if event.get("method") != "Fetch.requestPaused" or event.get("session_id") not in self.sessions:
                    if event.get("session_id") in self.sessions and self.transport is not None:
                        self.transport.events_seen = getattr(self.transport, "events_seen", []) + [event]
                    continue
                params, session = event["params"], event["session_id"]
                request = params.get("request", {})
                headers = {k.lower(): v for k, v in request.get("headers", {}).items()}
                body = request.get("postData") or ("multipart/form-data" if request.get("hasPostData") else "")
                ok = reads_only(
                    request.get("method", "GET"),
                    request.get("url", ""),
                    body,
                    headers.get("content-type", ""),
                    params.get("resourceType", ""),
                    self.log,
                )
                try:
                    if ok:
                        self.cdp("Fetch.continueRequest", session_id=session, requestId=params["requestId"])
                    else:
                        self.cdp(
                            "Fetch.failRequest",
                            session_id=session,
                            requestId=params["requestId"],
                            errorReason="BlockedByClient",
                        )
                except Exception:
                    pass  # the page navigated away: the request is gone anyway
            time.sleep(0.02)

    def close(self):
        self.stopped = True


def fake_profile(folder):
    data = json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8"))
    resume, cover = folder / "Aarav_Sharma_Resume.pdf", folder / "Aarav_Sharma_Cover_Letter.pdf"
    for path in (resume, cover):
        path.write_bytes(b"%PDF-1.4\n% jev-apply live check: not a real document\n")
    data["documents"] = {
        "resume": {"path": str(resume), "about": "Résumé / CV (PDF)"},
        "cover_letter": {"path": str(cover), "about": "General cover letter (PDF)"},
    }
    return Profile(data, folder, learned_path=folder / "learned.json")


def real_profile(data, folder):
    """Your own profile, documents and saved answers. Questions a run would save for later go to a temporary
    copy, so a test run changes nothing in your data folder."""
    import shutil

    data = Path(data)
    if (data / "learned.json").is_file():
        shutil.copy(data / "learned.json", folder / "learned.json")
    return Profile.load(data / "profile.json", data / "answers.json", folder / "learned.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--max-actions", type=int, default=80)
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "jev-live"))
    parser.add_argument("--chrome", action="store_true")
    parser.add_argument("--recon", default=None)
    parser.add_argument("--data", default=None, help="your data folder (e.g. data/automation) instead of the example")
    args = parser.parse_args()
    os.environ.setdefault("DECISION_BACKEND", "laya")
    os.environ.pop("TEXT_MODEL_API_KEY", None)  # no drafting: job pages are not sent anywhere
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp())
    profile = real_profile(args.data, folder) if args.data else fake_profile(folder)
    policy = Policy(max_actions=args.max_actions)
    for number, url in enumerate(args.urls, 1):
        log, chrome_guard = [], None
        if args.chrome:
            from jev_apply.transport import ChromeTransport

            transport = ChromeTransport()
            chrome_guard = ChromeGuard(transport, log)
            transport.call(
                "Emulation.setDeviceMetricsOverride", width=1280, height=900, deviceScaleFactor=1, mobile=False
            )
        else:
            transport = PlaywrightTransport(profile_dir=None, headless=not args.headed)
            transport._context.route("**/*", guard(log))
            transport.page.set_viewport_size({"width": 1280, "height": 900})
        if args.recon:
            try:
                browser = Browser(url, transport)
                time.sleep(4)
                for _ in range(3):
                    browser.evaluate("window.scrollBy(0, innerHeight)")
                    time.sleep(1.5)
                links = browser.evaluate("[...new Set([...document.querySelectorAll('a[href]')].map(a=>a.href))]")
                print(f"\n=== {url}  title={browser.evaluate('document.title')!r}")
                for link in [x for x in links or [] if re.search(args.recon, x)][:40]:
                    print("   ", link)
            finally:
                if chrome_guard:
                    chrome_guard.close()
                transport.close()
            continue
        ui = UnattendedUI(f"[{number}] ")
        started = time.monotonic()
        try:
            browser = Browser(url, transport)
            report = Agent(browser, profile, policy, ui, run_dir=out / "runs").run()
        except Exception as error:
            report = {"status": f"crashed: {error!r}", "fills": [], "history": [], "left_for_you": []}
        shot = out / f"{number}.png"
        try:
            if args.chrome:
                import base64

                data = transport.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)["data"]
                shot.write_bytes(base64.b64decode(data))
            else:
                transport.page.screenshot(path=str(shot), full_page=True)
        except Exception:
            shot = None
        print(f"\n=== [{number}] {url}\n    status={report['status']}  {time.monotonic() - started:.0f}s  shot={shot}")
        if ui.stopped:
            print(f"    stopped: {ui.stopped}")
        for f in report["fills"]:
            kept = "" if f["confirmed"] is not False else "!! not kept"
            print(f"    fill  {f['label'][:60]!r:64} = {str(f['value'])[:30]!r} {kept}")
        for item in ui.needs + [f"{x['what']} ({x['why']})" for x in report.get("left_for_you", [])]:
            print(f"    left  {item[:120]}")
        for h in report.get("history", [])[-25:]:
            print(f"    step  {h.get('kind'):7} {str(h.get('action'))[:60]!r} ctx={str(h.get('context'))[:50]!r}")
        for a in report.get("last_page", [])[:60]:
            print(f"    page  {json.dumps(a, ensure_ascii=False)[:170]}")
        blocked = [x for x in log if x[0] == "BLOCKED"]
        print(f"    network: {len(blocked)} blocked, {len(log) - len(blocked)} read-only allowed")
        for x in blocked[:8] + [x for x in blocked[8:] if "flagship-web" in x[2] or "voyager" in x[2]][:40]:
            print(f"      {x}")
        if chrome_guard:
            chrome_guard.close()
        transport.close()


if __name__ == "__main__":
    main()
