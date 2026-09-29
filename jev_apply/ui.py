"""Every place the agent defers to you. Swap this class for a web UI later; the agent only calls these methods."""

import textwrap


def read_block(prompt):
    print(prompt + " (finish with an empty line)")
    lines = []
    while True:
        line = input()
        if not line:
            return "\n".join(lines).strip()
        lines.append(line)


class TerminalUI:
    def __init__(self, step=False):
        self.step = step

    def event(self, message):
        print(f"  · {message}")

    def approve(self, what, why=""):
        print(f"\n? {what}" + (f"\n  ({why})" if why else ""))
        return input("  Do it? [Y/n] ").strip().lower() in {"", "y", "yes"}

    def is_final_submit(self, label):
        print(f"\n! The next click would be '{label}'.")
        print("  The agent never submits applications. Is this the FINAL submit?")
        answer = input(
            "  [Y] yes, stop here and I'll review + submit myself / [n] no, it only opens or continues the form: "
        )
        return answer.strip().lower() not in {"n", "no"}

    def pick(self, question, options):
        """options: [(id, description, probability)]. Returns an id, or None to ask/handle it yourself."""
        print(f"\n? {question}")
        for number, (_, description, probability) in enumerate(options, 1):
            print(f"  {number}. {description}" + (f"  [{probability:.0%}]" if probability is not None else ""))
        print(f"  {len(options) + 1}. None of these — let me type the answer")
        raw = input("  Choice [1]: ").strip() or "1"
        try:
            index = int(raw) - 1
        except ValueError:
            return None
        return options[index][0] if 0 <= index < len(options) else None

    def ask(self, question, hint=""):
        """Returns (text or None to leave empty, remember)."""
        print(f"\n? The form asks: {question}")
        if hint:
            print(f"  ({hint})")
        text = input("  Your answer (empty = leave the field empty): ").strip()
        if not text:
            return None, False
        remember = input("  Save this answer for future forms? [y/N] ").strip().lower() in {"y", "yes"}
        return text, remember

    def edit_draft(self, question, draft):
        """Returns final text or None (leave empty)."""
        print(f"\n? Draft answer for: {question}\n")
        print(textwrap.indent(textwrap.fill(draft, 96), "    "))
        choice = input("\n  [Enter] use it / [e] write my own / [s] leave empty: ").strip().lower()
        if choice == "s":
            return None
        if choice == "e":
            return read_block("  Type your answer") or None
        return draft

    def handover(self, reason):
        print(f"\n>> Over to you: {reason}")
        print("   Do it in the browser tab, then come back here.")
        return input("   [Enter] continue / [q] stop: ").strip().lower() not in {"q", "quit"}

    def review(self, report):
        fills = report["fills"]
        print("\n" + "=" * 72)
        print(f"Stopped: {report['status']} after {report['elapsed_s']} s — {report['final_url']}")
        print(f"{len(fills)} fields filled. Please read these before you submit:")
        for f in fills:
            flag = {"draft": "DRAFT — read it", "you": "your answer", "profile": "", "saved": "saved answer"}.get(
                f["source"], ""
            )
            check = "" if f["confirmed"] is not False else "  !! value did not stick — check this field"
            if f.get("added") is False:
                check = "  !! not added — the site had no matching option"
            print(f"  - {f['label'][:48]:<48} = {str(f['value'])[:40]:<40} {flag}{check}")
        if report.get("not_added"):
            print(f"\nNot added (no matching option on the site): {', '.join(report['not_added'])}")
        if report["unconfirmed"]:
            print(f"\n{report['unconfirmed']} field(s) could not be confirmed on the page.")
        if str(report["status"]).startswith("submitted"):
            print(f"\n{report['status'].capitalize()} (auto_submit). Recorded in applied.json: never applied to twice.")
        else:
            print("\nNothing was submitted. Review every page in the browser, then submit it yourself.")
        print(f"Report: {report['path']}")


class UnattendedUI(TerminalUI):
    """Nobody at the keyboard. Never approves anything itself (the agent decides that, safely); drafts are
    typed and flagged for you to read; anything it would have asked is left empty and listed at the end."""

    unattended = True

    def __init__(self, label=""):
        super().__init__(step=False)
        self.label = label
        self.needs = []  # questions left for you
        self.stopped = None  # why this job stopped early, if it did

    def event(self, message):
        print(f"  · {self.label}{message}")

    def approve(self, what, why=""):
        self.needs.append(f"{what} ({why})" if why else what)
        return False

    def is_final_submit(self, label):
        return True

    def pick(self, question, options):
        return None  # a close call becomes a question for you

    def ask(self, question, hint=""):
        self.needs.append(question)
        return None, False

    def edit_draft(self, question, draft):
        return draft  # typed, and shown as DRAFT in the review so you read it before submitting

    def handover(self, reason):
        self.stopped = reason
        print(f"  · {self.label}needs you: {reason}")
        return False

    def review(self, report):
        drafts = sum(f["source"] == "draft" for f in report["fills"])
        print(
            f"  · {self.label}{report['status']}: {len(report['fills'])} fields filled, {drafts} draft(s) to read, "
            f"{len(self.needs) + len(report.get('left_for_you', []))} left for you"
        )
