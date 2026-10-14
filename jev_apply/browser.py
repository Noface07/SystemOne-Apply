"""Observe and execute on one tab. Every target is an observed node; model output never becomes a selector."""

import hashlib
import json
import re
import sys
import time
from pathlib import Path

from . import helper

READ_STATE = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"
ADVANCING = re.compile(r"\b(next|continue|save|proceed|start|apply|review)\b", re.I)
SETTLE_S = 1.5  # after Next/Continue: wait for the page to change before asking what to do next
LOAD_S = 15
OBSERVE_S = 8


class StalePage(ValueError):
    """A decision no longer refers to the observed page. Nothing was executed."""


class ExecutionUncertain(RuntimeError):
    """A mutation may have partly happened. Never retried; the human takes a look."""


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "secrets", "scroll") if k in state}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


# Resolve the observed node (or its visible label proxy) and check it can really be acted on right now.
RESOLVE = """(action => {
  const c=window.__jevApply, e=c?.nodes.get(action.node), shown=action.proxy ? c?.nodes.get(action.proxy) : e;
  if (!e?.isConnected || !shown?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
      !shown.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
  if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
  if (action.offscreen) shown.scrollIntoView({block:'nearest'});  // an option scrolled out of its open list
  const place = () => { const r=c.rectOf ? c.rectOf(shown) : shown.getBoundingClientRect();
    return {r, x:r.x+r.width/2, y:r.y+r.height/2}; };
  const usable = ({r,x,y}) => r.width && r.height && x>=0 && y>=0 && x<innerWidth && y<innerHeight && c.hittable(shown,x,y);
  let p=place();
  // Under a sticky footer or header (Workday's Save bar), or just out of view: brought to the middle once. A
  // dialog over the page still covers it there, and nothing is clicked.
  if (!usable(p)) { shown.scrollIntoView({block:'center', inline:'nearest'}); p=place(); }
  if (!usable(p)) return null;
  return {x:p.x, y:p.y};
})"""

# Set a native <select> or date/time input the way React/Vue expect: native setter, then events. The element may
# live in a same-origin frame, so its own window's prototypes are used.
SET_VALUE = """(action => {
  const e=window.__jevApply?.nodes.get(action.node), view=e?.ownerDocument?.defaultView || window;
  const proto=e?.tagName==='SELECT' ? view.HTMLSelectElement.prototype : view.HTMLInputElement.prototype;
  if (action.kind==='select' && (e?.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
      !o.disabled && !o.closest('optgroup[disabled]')))) return null;
  if (action.kind==='setdate' && (e?.tagName!=='INPUT' ||
      !['date','month','week','time','datetime-local'].includes(e.type))) return null;
  e.focus();
  if (e.tagName==='SELECT' && e.multiple) {  // add one option; setting .value would drop the others
    [...e.options].find(o=>o.value===action.set).selected=true;
    e.dispatchEvent(new view.Event('input',{bubbles:true}));
    e.dispatchEvent(new view.Event('change',{bubbles:true}));
    e.blur();
    return [...e.selectedOptions].some(o=>o.value===action.set) ? action.set : null;
  }
  Object.getOwnPropertyDescriptor(proto,'value').set.call(e, action.set);
  e.dispatchEvent(new view.Event('input',{bubbles:true}));
  e.dispatchEvent(new view.Event('change',{bubbles:true}));
  e.blur();
  return e.value;
})"""


# A same-origin frame whose content is cut off and can't be scrolled (iCIMS sizes its frame by script, and the
# script may not have run): give it the height of its content, as that script would, so every field is reachable.
EXPAND_FRAMES = """(() => {
  for (const f of document.querySelectorAll('iframe,frame')) {
    let d=null; try { d=f.contentDocument; } catch {}
    if (!d?.body || f.clientHeight<50 || f.clientWidth<200) continue;
    const need=d.documentElement.scrollHeight, style=getComputedStyle(d.documentElement), body=getComputedStyle(d.body);
    const stuck=f.getAttribute('scrolling')==='no' || style.overflowY==='hidden' || body.overflowY==='hidden';
    if (stuck && need>f.clientHeight+40 && need<30000) f.style.height=need+'px';
  }
})()"""


# Enter is only ever pressed in a search prompt ("type and press Enter to find a value") that is not inside a
# <form>, because Enter in a form can submit it. Checked in the page itself, right before the key press.
# Portals whose search boxes need Enter to show their values, where Enter in such a box only runs the search
# (Workday's prompts, iCIMS, Oracle, SuccessFactors): Enter is allowed there even inside a <form>, in a search box
# only. Everywhere else, Enter inside a form could submit it and is never pressed.
PROMPT_ENTER_SITES = re.compile(
    r"myworkdayjobs\.com|\.workday\.com|icims\.com|oraclecloud\.com|successfactors\.|sapsf\.", re.I
)
# Empty a text input as a person deleting its text would (React/Workday see the change), and leave focus in it.
CLEAR_INPUT = """(node => {
  const e=window.__jevApply?.nodes.get(node); if (!e) return false;
  const view=e.ownerDocument?.defaultView || window;
  const proto=e.tagName==='TEXTAREA' ? view.HTMLTextAreaElement.prototype : view.HTMLInputElement.prototype;
  e.focus();
  if (e.value) {
    Object.getOwnPropertyDescriptor(proto,'value').set.call(e,'');
    e.dispatchEvent(new view.Event('input',{bubbles:true}));
  }
  e.focus();
  return e.value==='';
})"""
# A box that adds one item per entry ("Type to Add Skills", "Add skills"): a tag field.
ADDS_ITEMS = re.compile(r"\btype to add\b|\badd (more |your |a )?(skills?|tags?|keywords?|items?)\b", re.I)
SAFE_ENTER = """(action => {
  const e=window.__jevApply?.nodes.get(action.node);
  if (!e?.isConnected || e.tagName!=='INPUT' || ['submit','button','image','reset'].includes(e.type)) return false;
  if ((e.form || e.closest('form')) && !action.in_form_ok) return false;
  const described=document.getElementById(e.getAttribute('aria-describedby')||'')?.innerText;
  const hint=[e.placeholder,e.getAttribute('aria-label'),described].join(' ');
  const prompt=['combobox','searchbox'].includes(e.getAttribute('role')||'') || e.type==='search';
  if (!prompt && !/search|type to|press enter|hit enter/i.test(hint)) return false;
  e.focus();
  return document.activeElement===e;
})"""


class Browser:
    def __init__(self, url, transport):
        self.transport = transport
        self.after_input = None
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.install_helper()
        self.navigate(url)

    # ---- the helper bubble (helper.py): yours to pause, continue or skip from the tab itself ----------------

    def install_helper(self):
        """Put the bubble in this tab, now and on every page it loads. A browser that refuses just has none."""
        try:
            self.call("Page.addScriptToEvaluateOnNewDocument", source=helper.BUBBLE)
            self.evaluate(helper.BUBBLE)
        except Exception:  # noqa: BLE001 - the bubble is a convenience: never a reason to fail the run
            pass

    def _helper(self, expression):
        try:
            return self.evaluate(expression)
        except Exception:  # noqa: BLE001 - a page between documents has no bubble for a moment
            return None

    def helper_events(self):
        """What you pressed in the bubble since the last call: [{type: pause|resume|continue|skip|answers}]."""
        raw = self._helper(helper.DRAIN)
        try:
            return json.loads(raw) if raw else []
        except ValueError:
            return []

    def helper_state(self, state):
        self._helper(helper.show_state(state))

    def call(self, method, **params):
        return self.transport.call(method, **params)

    def show(self):
        """Bring the tab to the front, as a courtesy: a tab you closed (or a portal replaced) never ends the run here;
        the next look at the page says what happened."""
        try:
            self.transport.show()
        except Exception:  # noqa: BLE001 - see above
            pass

    def close(self):
        self.transport.close()

    @property
    def frozen(self):
        """Three reads in a row went unanswered: the page's script is stuck, not loading."""
        return getattr(self, "unanswered", 0) >= 3

    def reopen(self, url):
        """The same address in a fresh tab, when the transport can (Chrome); False otherwise."""
        if not hasattr(self.transport, "reopen"):
            return False
        self.transport.reopen(url)
        self.unanswered, self.after_input = 0, None
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.install_helper()
        self.wait_loaded()
        return True

    def evaluate(self, expression, await_promise=False):
        try:
            response = self.call(
                "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=await_promise
            )
        except TimeoutError:
            # The page didn't answer in time (Stryker, while Google's sign-in window opened): busy, as a page that is
            # navigating is; the next look at it says how it is. Never sent again from here.
            self.unanswered = getattr(self, "unanswered", 0) + 1
            raise StalePage("The page didn't answer (busy)") from None
        self.unanswered = 0
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def navigate(self, url):
        try:
            self.call("Page.navigate", url=url)
        except TimeoutError:  # no answer yet; the navigation itself carries on, so wait for the page instead
            pass
        self.wait_loaded()

    def wait_loaded(self):
        deadline = time.monotonic() + LOAD_S
        time.sleep(0.05)
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return
            except (StalePage, RuntimeError):
                pass
            time.sleep(0.05)

    def settle(self, action, page):
        """Read-only wait after input; runs after the action was logged."""
        try:
            if action["kind"] == "click" and (ADVANCING.search(action["label"]) or action["role"] == "link"):
                # A step change: wait until the page differs (new document or re-rendered step), max SETTLE_S.
                deadline = time.monotonic() + SETTLE_S
                while time.monotonic() < deadline and self.evaluate(MARKER) == page["marker"]:
                    time.sleep(0.05)
                return
            self.evaluate(
                """(action => new Promise(resolve => {
                  const field=window.__jevApply?.nodes.get(action.node);
                  // A field that offers suggestions as you type: wait for them (they often come over the network),
                  // but stop as soon as they show, or once the page has been quiet for a while.
                  const hint=[field?.placeholder,field?.getAttribute('aria-label'),action.label].join(' ');
                  const autocomplete=action.kind==='fill' && !!field && (field.getAttribute('role')==='combobox' ||
                    field.hasAttribute('aria-autocomplete') || field.hasAttribute('list') ||
                    field.getAttribute('aria-haspopup')==='listbox' || field.getAttribute('autocomplete')==='off' ||
                    /search|type|start typing|location|city|country|school|college|university|skill/i.test(hint));
                  let frames=0, stopped=false, changed=performance.now();
                  const started=performance.now();
                  const observer=new MutationObserver(()=>{changed=performance.now()});
                  if (autocomplete) observer.observe(document.body,{childList:true,subtree:true,attributes:true});
                  const finish=()=>{stopped=true;observer.disconnect();resolve(true)};
                  setTimeout(finish,autocomplete ? 1200 : 60);
                  const shown=e=>{
                    const r=e.getBoundingClientRect();
                    return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                      e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                  };
                  const ready=()=>{
                    if (stopped) return;
                    const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                      .split(/\\s+/).filter(Boolean);
                    const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                    const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                    const now=performance.now();
                    const quiet=now-started>450 && now-changed>300;
                    if (++frames>=2 && (!autocomplete || options.some(shown) || quiet)) finish();
                    else requestAnimationFrame(ready);
                  };
                  requestAnimationFrame(ready);
                }))("""
                + json.dumps(action)
                + ")",
                await_promise=True,
            )
        except (StalePage, RuntimeError):
            pass  # navigating away is a normal outcome of a click

    def follow_popup(self):
        """An Apply link that opened a new tab or window: continue there (the transport switches its tab)."""
        back = getattr(self.transport, "back_to_open_tab", None)
        try:
            if back and back():
                self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        except Exception:  # the browser itself is gone: the next call reports it
            pass
        adopt = getattr(self.transport, "adopt_popup", None)
        if adopt and adopt():
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            self.install_helper()
            self.wait_loaded()

    def observe(self):
        if self.after_input:
            (action, page), self.after_input = self.after_input, None
            self.settle(action, page)
        self.follow_popup()
        try:
            self.evaluate(EXPAND_FRAMES)
        except (StalePage, RuntimeError):
            pass  # the page is navigating: nothing to resize yet
        deadline = time.monotonic() + OBSERVE_S
        self._helper(helper.HIDE)  # out of the way while the page is read (it never covers a field)
        try:
            while True:
                try:
                    info = self.evaluate(READ_STATE)
                    if info is None:
                        raise StalePage("Document is navigating")
                    # Password boxes go to the portal sign-in (accounts.py) only, never to the planner or a model.
                    actions = info.get("actions") or []
                    info["secrets"] = [a for a in actions if a.get("secret")]
                    info["actions"] = [a for a in actions if not a.get("secret")]
                    for a in info["actions"]:
                        # Workday's "Type to Add Skills" adds one item per search, like a tag field, though the
                        # page never says it holds several.
                        if a.get("kind") == "fill" and ADDS_ITEMS.search(f"{a.get('label')} {a.get('placeholder')}"):
                            a["multiple"] = True
                    info["fingerprint"] = fingerprint(info)
                    return info
                except (StalePage, RuntimeError):
                    if time.monotonic() > deadline:
                        raise StalePage("The page did not settle") from None
                    time.sleep(0.1)
        finally:
            self._helper(helper.SHOW)

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select", "setdate", "upload", "frame"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevApply; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            if action["kind"] in {"upload", "frame"}:  # hidden/embedded nodes: identity + page, not visibility
                return bool(current) and current[0] == page["page_key"]
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None, value=None, files=None):
        """Execute one observed action. `text` for fill, `value` for setdate, `files` for upload (code-owned).
        The bubble is hidden meanwhile, so no click can land on it."""
        self._helper(helper.HIDE)
        try:
            return self._act(action, page, text, value, files)
        finally:
            self._helper(helper.SHOW)

    def _act(self, action, page, text=None, value=None, files=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        kind = action["kind"]
        if kind == "wait":
            time.sleep(0.3)
            return
        if kind == "scroll":
            self.call(
                "Input.dispatchMouseEvent",
                type="mouseWheel",
                x=action["x"],
                y=action["y"],
                deltaX=0,
                deltaY=action["delta"],
            )
        elif kind == "enter":
            if action.get("in_form_ok") and not PROMPT_ENTER_SITES.search(self.evaluate("location.hostname") or ""):
                raise ValueError(
                    f"Enter inside a form is allowed only on known portals; nothing pressed in '{action['label']}'."
                )
            if not self.evaluate(SAFE_ENTER + "(" + json.dumps(action) + ")"):
                raise ValueError(f"Pressing Enter isn't safe in '{action['label']}'; nothing was pressed.")
            for event in ("keyDown", "keyUp"):
                self.call(
                    "Input.dispatchKeyEvent",
                    type=event,
                    key="Enter",
                    code="Enter",
                    windowsVirtualKeyCode=13,
                    **({"text": "\r"} if event == "keyDown" else {}),
                )
        elif kind == "key":
            for event in ("keyDown", "keyUp"):
                self.call("Input.dispatchKeyEvent", type=event, key="Escape", code="Escape", windowsVirtualKeyCode=27)
        elif kind == "frame":
            src = self.evaluate(f"window.__jevApply?.nodes.get({int(action['node'])})?.src ?? null")
            if not isinstance(src, str) or not re.match(r"^https?://", src):
                raise StalePage("Embedded form is gone")
            self.navigate(src)
            return
        elif kind == "upload":
            self.upload(action, files)
        elif kind in {"select", "setdate"}:
            wanted = action["value"] if kind == "select" else value
            result = self.call(
                "Runtime.evaluate",
                expression=SET_VALUE + "(" + json.dumps({**action, "set": wanted}) + ")",
                returnByValue=True,
            )
            if result.get("exceptionDetails") or result.get("result", {}).get("value") != wanted:
                # The change event may already have fired. Stop rather than guess.
                raise ExecutionUncertain(f"'{action['label']}' did not accept {wanted!r}; please check it.")
        else:
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            target = self.evaluate(RESOLVE + "(" + json.dumps(action) + ")")
            if target is None:
                raise StalePage("Target changed or is covered. Observe again.")
            for event in ("mousePressed", "mouseReleased"):
                self.call(
                    "Input.dispatchMouseEvent", type=event, x=target["x"], y=target["y"], button="left", clickCount=1
                )
            if kind == "fill" and action.get("proxy") is not None:
                # A box drawn over by another element (Workday's "MM / YYYY": the display sits on the input). The
                # click may leave focus where the site last put it (its Year box, after a Month): focus the input
                # itself, so the text goes where it was meant to.
                self.evaluate(
                    f"(() => {{ const e = window.__jevApply?.nodes.get({int(action['node'])}); "
                    "if (e && document.activeElement !== e) e.focus(); return true; })()"
                )
                time.sleep(0.25)  # Workday swaps its display for the input on focus: text typed meanwhile is lost
            if kind == "fill":
                modifier = 4 if sys.platform == "darwin" else 2
                self.call(
                    "Input.dispatchKeyEvent",
                    type="keyDown",
                    key="a",
                    code="KeyA",
                    modifiers=modifier,
                    commands=["selectAll"],
                )
                self.call("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", modifiers=modifier)
                if action.get("multiple"):
                    # A tag box (Workday's "Type to Add Skills") keeps the last search's text, and after a result is
                    # picked focus sits in its list, so Ctrl+A selects nothing: empty that very input the way the
                    # page's own code sees it (native setter + input event), focus it, then type one item.
                    self.evaluate(CLEAR_INPUT + "(" + json.dumps(int(action["node"])) + ")")
                if action.get("masked"):
                    # A typing mask ("DD/MM/YYYY") reads keystrokes, not pasted text: clear, then type key by key.
                    for event in ("keyDown", "keyUp"):
                        self.call(
                            "Input.dispatchKeyEvent",
                            type=event,
                            key="Backspace",
                            code="Backspace",
                            windowsVirtualKeyCode=8,
                        )
                    for char in text:
                        self.call("Input.dispatchKeyEvent", type="keyDown", key=char, text=char, unmodifiedText=char)
                        self.call("Input.dispatchKeyEvent", type="keyUp", key=char)
                else:
                    self.call("Input.insertText", text=text)
        self.after_input = (action, page)  # scrolls and key presses also need a couple of frames to render

    def upload_by_chooser(self, action, files):
        """A button that makes its file input only when clicked ("Upload resume" on LinkedIn): click it with the
        file dialog intercepted, then give that dialog your file. Nothing opens on your screen."""
        events = getattr(self.transport, "events", None)
        if events is None:
            raise ExecutionUncertain("This browser connection can't answer a file dialog; attach the file yourself.")
        target = self.evaluate(RESOLVE + "(" + json.dumps({**action, "kind": "click"}) + ")")
        if target is None:
            raise StalePage("Upload button changed or is covered. Observe again.")
        self.call("Page.enable")
        self.call("Page.setInterceptFileChooserDialog", enabled=True)
        try:
            events()  # older events belong to nobody here
            for event in ("mousePressed", "mouseReleased"):
                self.call(
                    "Input.dispatchMouseEvent", type=event, x=target["x"], y=target["y"], button="left", clickCount=1
                )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                opened = next((e for e in events() if e.get("method") == "Page.fileChooserOpened"), None)
                if opened:
                    self.call(
                        "DOM.setFileInputFiles",
                        files=[str(Path(f).resolve()) for f in files],
                        backendNodeId=opened["params"]["backendNodeId"],
                    )
                    return
                time.sleep(0.1)
            raise ExecutionUncertain(f"'{action['label']}' opened no file dialog; attach the file yourself.")
        finally:
            self.call("Page.setInterceptFileChooserDialog", enabled=False)

    def upload(self, action, files):
        if not files or not all(Path(f).is_file() for f in files):
            raise ValueError("Upload needs existing files from your profile's documents")
        if action.get("chooser"):
            return self.upload_by_chooser(action, files)
        result = self.call(
            "Runtime.evaluate",
            expression=f"(() => {{ const e=window.__jevApply?.nodes.get({int(action['node'])}); "
            "return e?.isConnected && e.tagName==='INPUT' && e.type==='file' && !e.disabled ? e : null; })()",
            returnByValue=False,
        )
        object_id = result.get("result", {}).get("objectId")
        if not object_id:
            raise StalePage("File field is gone")
        self.call("DOM.setFileInputFiles", files=[str(Path(f).resolve()) for f in files], objectId=object_id)
