"""Two ways to reach a browser tab over CDP. The executor only needs call(), show() and close()."""

import os
from pathlib import Path


class ChromeTransport:
    """Your running Chrome through Browser Harness: a new tab in your everyday profile and its logins."""

    def __init__(self):
        from browser_harness.admin import ensure_daemon
        from browser_harness.helpers import cdp

        ensure_daemon()
        self._cdp = cdp
        # In front by default: a background tab gets no animation frames, and JavaScript portals such as Workday
        # never finish drawing their form there. JEV_BACKGROUND_TABS=1 keeps the agent's tabs behind yours.
        background = (os.environ.get("JEV_BACKGROUND_TABS") or "0").strip() not in {"", "0", "false", "no"}
        self.target = cdp("Target.createTarget", url="about:blank", background=background)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.opened = [self.target]  # this tab, then any tab it opened that the run moved to
        self.tall()

    def tall(self):
        """A small window clips the footer (Next, Submit) of a tall modal form such as LinkedIn Easy Apply, and a
        clipped button can't be clicked. Lay this tab out at least JEV_VIEWPORT_HEIGHT px tall (your window's width
        is kept; only the agent's own tabs are affected)."""
        height = int(os.environ.get("JEV_VIEWPORT_HEIGHT") or 1000)
        try:
            if (
                height
                and self.call("Runtime.evaluate", expression="innerHeight", returnByValue=True)["result"]["value"]
                < height
            ):
                self.call(
                    "Emulation.setDeviceMetricsOverride", width=0, height=height, deviceScaleFactor=0, mobile=False
                )
        except Exception:  # an older Chrome or a closed tab: the page keeps its own size
            pass

    def adopt_popup(self):
        """A page this tab opened (Apply with target=_blank, window.open) becomes the tab to work in."""
        infos = self._cdp("Target.getTargets").get("targetInfos", [])
        popup = next(
            (
                t
                for t in reversed(infos)
                if t.get("type") == "page" and t.get("openerId") == self.target and t["targetId"] not in self.opened
            ),
            None,
        )
        if popup is None:
            return False
        self.target = popup["targetId"]
        self.opened.append(self.target)
        self.session = self._cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self._cdp("Target.activateTarget", targetId=self.target)
        self.tall()
        return True

    def call(self, method, **params):
        return self._cdp(method, session_id=self.session, **params)

    def events(self):
        """This tab's CDP events since the last call (a file chooser opening, for one)."""
        from browser_harness.helpers import drain_events

        self.events_seen = getattr(self, "events_seen", [])
        self.events_seen += [e for e in drain_events() if e.get("session_id") == self.session]
        out, self.events_seen = self.events_seen, []
        return out

    def show(self):
        self._cdp("Target.activateTarget", targetId=self.target)

    def close(self):
        if self.target:
            for target in self.opened:
                try:
                    self._cdp("Target.closeTarget", targetId=target)
                except Exception:  # already closed by you
                    pass
            self.target = None


# Pages this tool created in a Playwright context (id(context) -> ids of pages): a new page that isn't one of
# them was opened by the site.
_OWNED = {}


class _Popups:
    """Playwright: a page the site opened from this tab becomes the tab to work in."""

    def _listen(self):
        self._events = getattr(self, "_events", [])
        self._session.on(
            "Page.fileChooserOpened",
            lambda params: self._events.append({"method": "Page.fileChooserOpened", "params": params}),
        )

    def events(self):
        try:
            self.page.wait_for_timeout(50)  # Playwright's sync API hands over CDP events only while it runs
        except Exception:
            pass
        out, self._events = getattr(self, "_events", []), []
        return out

    def _own(self, page):
        _OWNED.setdefault(id(self._context), set()).add(id(page))

    def adopt_popup(self):
        owned = _OWNED.setdefault(id(self._context), set())
        new = [p for p in self._context.pages if id(p) not in owned and not p.is_closed()]
        if not new:
            return False
        mine = [p for p in new if _opener(p) is self.page]
        # Links with rel=noopener hide the opener: with a single tab of ours, a new page can only be from it.
        tabs = [p for p in self._context.pages if id(p) in owned and not p.is_closed()]
        popup = (mine or (new if len(tabs) <= 1 else []) or [None])[-1]
        if popup is None:
            return False
        self._own(popup)
        popup.wait_for_load_state("domcontentloaded")
        self.page = popup
        self._session = self._context.new_cdp_session(popup)
        self._listen()
        popup.bring_to_front()
        return True


def _opener(page):
    try:
        return page.opener()
    except Exception:
        return None


class PlaywrightTransport(_Popups):
    """A separate Chromium profile that only this tool uses. Log in to job portals there once; it persists."""

    def __init__(self, profile_dir="~/.jev-apply/browser", headless=False, executable_path=None):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        options = dict(headless=headless, executable_path=executable_path or os.environ.get("JEV_CHROMIUM") or None)
        if profile_dir:
            directory = Path(os.path.expanduser(profile_dir))
            directory.mkdir(parents=True, exist_ok=True)
            self._context = self._pw.chromium.launch_persistent_context(str(directory), no_viewport=True, **options)
        else:
            self._browser = self._pw.chromium.launch(**options)
            self._context = self._browser.new_context(no_viewport=True)
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        for page in self._context.pages:  # restored or blank pages at start-up are not popups
            self._own(page)
        self._session = self._context.new_cdp_session(self.page)
        self._listen()

    def call(self, method, **params):
        try:
            return self._session.send(method, params)
        except Exception as error:  # Same contract as Browser Harness: protocol failures are RuntimeError.
            raise RuntimeError(str(error)) from None

    def show(self):
        self.page.bring_to_front()

    def new_tab(self):
        """Another tab in the same browser and profile (same logins), for filling several jobs side by side."""
        return PlaywrightTab(self._context)

    def close(self):
        if self._pw:
            try:
                self._context.close()
            except Exception:  # you already closed the browser window yourself: nothing left to close
                pass
            self._pw.stop()
            self._pw = None


class PlaywrightTab(_Popups):
    """One more tab of a PlaywrightTransport's browser. Closing it leaves the browser running."""

    def __init__(self, context):
        self._context = context
        self.page = context.new_page()
        self._own(self.page)
        self._session = context.new_cdp_session(self.page)
        self._listen()

    def call(self, method, **params):
        try:
            return self._session.send(method, params)
        except Exception as error:
            raise RuntimeError(str(error)) from None

    def show(self):
        self.page.bring_to_front()

    def close(self):
        if self.page:
            try:
                self.page.close()
            except Exception:
                pass
            self.page = None
