"""Drive the local 3-step fixture in a real browser: snapshot + executor only. No model calls, no network.

uv run --extra playwright python scripts/local_check.py [--chromium /path/to/chrome] [--headed]
"""

import argparse
import functools
import http.server
import tempfile
import threading
from pathlib import Path

from jev_apply.browser import Browser, StalePage
from jev_apply.policy import Policy
from jev_apply.transport import PlaywrightTransport

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
results = []


def check(name, condition, detail=""):
    results.append((bool(condition), name))
    print(("  ok    " if condition else "  FAIL  ") + name + ("" if condition else f"  -> {detail}"))


def find(page, label=None, kind=None, context=None):
    for a in page["actions"]:
        if (
            (label is None or a["label"] == label)
            and (kind is None or a["kind"] == kind)
            and (context is None or context in a.get("context", ""))
        ):
            return a
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium", default=None)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    resume = Path(tempfile.mkdtemp()) / "Aarav_Sharma_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% test\n")
    policy = Policy.load()

    browser = Browser(
        f"http://localhost:{port}/careers.html",
        PlaywrightTransport(profile_dir=None, headless=not args.headed, executable_path=args.chromium),
    )
    browser.call("Emulation.setDeviceMetricsOverride", width=1200, height=800, deviceScaleFactor=1, mobile=False)
    try:
        print("careers page (cross-origin embedded form)")
        page = browser.observe()
        frame = find(page, kind="frame")
        check("embedded form offered as OPEN_FORM", frame and "Job application form" in frame["label"], page["actions"])
        browser.act(frame, page)
        page = browser.observe()
        check("opened the form in the tab", page["url"].endswith("/apply/step1.html"), page["url"])

        print("step 1: text, autocomplete, uploads, covering banner")
        check("Next hidden while the cookie banner covers it", find(page, "Next") is None)
        resume_field = find(page, kind="upload", context="Resume/CV")
        check(
            "hidden résumé input offered with its question",
            resume_field is not None,
            [a for a in page["actions"] if a["kind"] == "upload"],
        )
        check("optional cover-letter upload offered separately", find(page, kind="upload", context="Cover letter"))
        browser.act(find(page, "Only necessary cookies"), page)
        page = browser.observe()
        check("Next offered once the banner is gone", find(page, "Next"))
        for label, text in [
            ("First name *", "Aarav"),
            ("Last name *", "Sharma"),
            ("Email *", "aarav@example.com"),
            ("Mobile number *", "+91 98765 43210"),
        ]:
            field = find(page, label, "fill")
            browser.act(field, page, text=text)
            page = browser.observe()
            check(f"typed and read back: {label}", find(page, label, "fill")["value"] == text)
        city = find(page, "Current location *", "fill")
        check("autocomplete marked as combobox", city and city["role"] == "combobox")
        browser.act(city, page, text="Be")
        page = browser.observe()
        option = find(page, "Bengaluru")
        check("suggestions observed after typing", option and option["role"] == "option", page["actions"])
        browser.act(option, page)
        page = browser.observe()
        check("suggestion chosen", find(page, "Current location *", "fill")["value"] == "Bengaluru")
        resume_field = find(page, kind="upload", context="Resume/CV")
        browser.act(resume_field, page, files=[str(resume)])
        page = browser.observe()
        check(
            "résumé attached (read back from the input)",
            resume.name in find(page, kind="upload", context="Resume/CV")["value"],
        )
        try:
            browser.act(find(page, kind="upload", context="Resume/CV"), page, files=["/etc/passwd-does-not-exist"])
            check("missing files are refused", False)
        except ValueError:
            check("missing files are refused", True)

        stale = find(page, "Next")
        browser.evaluate("document.getElementById('fn').value='Someone else'")
        try:
            browser.act(stale, page)
            check("stale page rejected before clicking", False)
        except StalePage:
            check("stale page rejected before clicking", True)
        browser.evaluate("document.getElementById('fn').value='Aarav'")
        page = browser.observe()
        browser.act(find(page, "Next"), page)
        page = browser.observe()
        check("multi-page: Next reached step 2", page["url"].endswith("/apply/step2.html"), page["url"])

        print("step 2: scroll panel, CTC fields, select, dates, hidden radios, pressed buttons")
        down = find(page, kind="scroll")
        check("inner panel scroll offered", down and "inside the form panel" in down["label"], down)
        check("clipped fields not offered before scrolling", find(page, kind="setdate") is None)
        expected = find(page, "Expected CTC *", "fill")
        check("help text captured (Annual, in INR)", expected and expected.get("help") == "Annual, in INR", expected)
        for _ in range(4):
            if find(page, "Yes", context="visa") and find(page, "Earliest start date"):
                break
            browser.act(find(page, kind="scroll", label=down["label"]), page)
            page = browser.observe()
        start = find(page, "Earliest start date", "setdate")
        check("date input offered as SET_DATE", start is not None, [a["label"] for a in page["actions"]])
        if start:
            browser.act(start, page, value="2026-11-02")
            page = browser.observe()
            check("date set and read back", find(page, "Earliest start date", "setdate")["value"] == "2026-11-02")
        grad = find(page, "Graduation (month and year)", "setdate")
        if grad:
            browser.act(grad, page, value="2022-06")
            page = browser.observe()
            check("month input set", find(page, "Graduation (month and year)", "setdate")["value"] == "2022-06")
        else:
            check("month input offered", False, [a["label"] for a in page["actions"]])
        notice = find(page, "Notice period * → 30 days", "select")
        if notice is None:
            browser.act(find(page, kind="scroll", label="Scroll up inside the form panel"), page)
            page = browser.observe()
            notice = find(page, "Notice period * → 30 days", "select")
        check("native select option offered", notice is not None)
        if notice:
            browser.act(notice, page)
            page = browser.observe()
            check(
                "select read back",
                next(a for a in page["actions"] if a["label"].startswith("Notice period"))["current_value"]
                == "30 days",
            )
        for _ in range(4):
            if find(page, "Yes", context="visa"):
                break
            browser.act(find(page, kind="scroll", label=down["label"]), page)
            page = browser.observe()
        relocate = find(page, "Yes", context="relocate")
        check("styled radio offered via its label, with its question", relocate and relocate.get("proxy"), relocate)
        if relocate:
            browser.act(relocate, page)
            page = browser.observe()
            check("radio checked", find(page, "Yes", context="relocate")["checked"] == "true")
        visa_no = find(page, "No", context="visa sponsorship")
        check("Yes/No buttons carry aria-pressed + question", visa_no and visa_no.get("pressed") == "false", visa_no)
        if visa_no:
            browser.act(visa_no, page)
            page = browser.observe()
            check("pressed state read back", find(page, "No", context="visa sponsorship")["pressed"] == "true")
        browser.act(find(page, "Save and continue"), page)
        page = browser.observe()
        check("multi-page: reached step 3", page["url"].endswith("/apply/step3.html"), page["url"])

        print("step 3: tag input, native multi-select, textarea, sensitive select, submit guard")
        skills = find(page, "Key skills (add up to 8)", "fill")
        check("tag input offered and flagged multiple", skills and skills.get("multiple"), skills)
        for skill, typed in [("Python", "Python"), ("Go", "Go")]:
            browser.act(find(page, "Key skills (add up to 8)", "fill"), page, text=typed)
            page = browser.observe()
            options = [a["label"] for a in page["actions"] if a.get("role") == "option"]
            check(f"suggestions for {typed!r}: {options}", skill in options, options)
            browser.act(find(page, skill, "click"), page)
            page = browser.observe()
            field = find(page, "Key skills (add up to 8)", "fill")
            check(
                f"{skill} added as a chip, visible in the field's context, input cleared",
                skill in field.get("context", "") and field["value"] == "",
                field,
            )
        browser.act(find(page, "Key skills (add up to 8)", "fill"), page, text="Kafka")
        page = browser.observe()
        check(
            "an item the site doesn't offer produces no suggestion",
            not [a for a in page["actions"] if a.get("role") == "option"],
        )
        prefix = "Preferred locations (select all that apply) → "
        for city in ("Pune", "Hyderabad"):
            option = find(page, prefix + city, "select")
            check(f"multi-select option {city} offered and flagged multiple", option and option.get("multiple"), option)
            browser.act(option, page)
            page = browser.observe()
        current = next(a for a in page["actions"] if a["label"].startswith(prefix))["current_value"]
        check(f"multi-select keeps both choices ({current})", current == "Hyderabad, Pune", current)
        why = find(page, "Why do you want to join Acme? *", "fill")
        check("textarea offered", why and why.get("input_type") == "textarea")
        gender = find(page, "Gender (voluntary self-identification) → Prefer not to say", "select")
        check("EEO question flagged sensitive by policy", gender and policy.sensitive(gender))
        submit = find(page, "Submit application")
        check("final button recognised as submit-like", submit and policy.submit_like(submit))
        confirm = find(page, "I confirm the information above is accurate")
        check("declaration checkbox flagged sensitive", confirm and policy.sensitive(confirm))
        check("never submitted", "submitted" not in page["url"])

        print("large window: the form panel is not under the middle of the screen")
        browser.call("Emulation.setDeviceMetricsOverride", width=1920, height=1080, deviceScaleFactor=1, mobile=False)
        browser.navigate(f"http://localhost:{port}/apply/step2.html")
        page = browser.observe()
        check("panel scroll still offered", find(page, "Scroll down inside the form panel") is not None)
        title = find(page, "Current designation *", "fill")
        check("fields know the heading they sit under", title and title.get("section", "").startswith("Experience and"))

        print("other layouts: Lever radios, tables, div choices, masks, shadow DOM, frames, new tabs")
        browser.call("Emulation.setDeviceMetricsOverride", width=1200, height=1400, deviceScaleFactor=1, mobile=False)
        browser.navigate(f"http://localhost:{port}/apply/layouts.html")
        page = browser.observe()
        relocate, visa = find(page, "Yes", context="relocate"), find(page, "Yes", context="visa sponsorship")
        check(
            "Lever-style radios keep their own question",
            relocate and visa and relocate["context"] != visa["context"] and relocate["context"].startswith("Are you"),
            [relocate, visa],
        )
        check("an asterisk in the question makes it required", relocate and relocate.get("required"), relocate)
        check("a table row's first cell is the radios' question", find(page, "No", context="valid passport"))
        night = find(page, "No", context="night shifts")
        check("div Yes/No choices offered with their question", night and night.get("chosen") == "false", night)
        if night:
            browser.act(night, page)
            page = browser.observe()
            check("div choice shows as chosen", find(page, "No", context="night shifts")["chosen"] == "true")
        where = find(page, "Which location are you applying for? * → Pune", "select")
        check("a dropdown hidden over a styled box is offered", where, [a["label"] for a in page["actions"]][:40])
        if where:
            browser.act(where, page)
            page = browser.observe()
            chosen = next(a for a in page["actions"] if a["label"].startswith("Which location"))["current_value"]
            check("hidden dropdown set and read back", chosen == "Pune", chosen)
        dob = find(page, "Date of birth", "fill")
        check("date mask detected", dob and dob.get("masked"), dob)
        if dob:
            browser.act(dob, page, text="02/11/1998")
            page = browser.observe()
            check("masked field typed key by key", find(page, "Date of birth", "fill")["value"] == "02/11/1998")
        email = find(page, "Email", "fill")
        check(
            "invalid field and its message read",
            email and email.get("invalid") and email.get("error") == "Enter a valid email address",
            email,
        )
        slot = find(page, "Interview slot", "setdate")
        check("datetime-local offered as SET_DATE", slot and slot.get("input_type") == "datetime-local", slot)
        if slot:
            browser.act(slot, page, value="2026-11-02T10:30")
            page = browser.observe()
            check("datetime-local set", find(page, "Interview slot", "setdate")["value"] == "2026-11-02T10:30")
        for label, text in [("Cover note", "Hello"), ("Portfolio URL", "https://a.example"), ("Referral code", "R1")]:
            found = find(page, label, "fill")
            check(
                f"{label} offered (rich text / shadow DOM / same-origin frame)",
                found,
                [a["label"] for a in page["actions"]],
            )
            if found:
                browser.act(found, page, text=text)
                page = browser.observe()
                check(f"{label} typed and read back", find(page, label, "fill")["value"] == text)
        check("a same-origin frame is read in place, not opened", find(page, kind="frame") is None)
        button = find(page, "Upload resume")
        if button:
            browser.act({**button, "kind": "upload", "chooser": True}, page, files=[str(resume)])
            page = browser.observe()
            check("a file dialog opened by a button gets the résumé", resume.name in page["text"], page["text"][-300:])
        else:
            check("upload button offered", False, [a["label"] for a in page["actions"]])
        month = find(page, "Month", "fill")
        check("a 0x0 date part is offered through the box drawn for it", month and month.get("proxy"), month)
        if month:
            browser.act(month, page, text="08")
            page = browser.observe()
            check("0x0 date part typed and read back", find(page, "Month", "fill")["value"] == "08")
        tab = find(page, "Continue in a new tab")
        browser.act(tab, page)
        page = browser.observe()
        check("a link that opens a new tab is followed there", page["url"].endswith("/apply/step1.html"), page["url"])
    finally:
        browser.close()
        server.shutdown()
    failed = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
