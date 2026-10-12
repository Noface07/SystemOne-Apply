"""Real-page replay tests (see jev_apply/replay.py).

    uv run python scripts/replay.py promote runs/<report folder>   # keep a good run's pages as goldens
    uv run python scripts/replay.py check [--bless] [--site maersk]  # read every kept page again, decide again

check opens each frozen page in headless Chromium (no site, no network), reads it with today's snapshot.js and runs
the planner on that reading. Exit 1 when a page reads differently or the decision changed; --bless writes the new
reading and decision as expected (after you have looked at the differences and they are the better ones)."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jev_apply import replay  # noqa: E402
from jev_apply.browser import Browser  # noqa: E402
from jev_apply.transport import PlaywrightTransport  # noqa: E402

DEST = ROOT / "data" / "replay"


def promote(args):
    folder = Path(args.run)
    report = folder / "report.json"
    pages = Path(json.loads(report.read_text(encoding="utf-8")).get("pages") or "") if report.is_file() else folder
    if not pages.is_dir():
        sys.exit(f"No kept pages for {folder} (its run kept none, or JEV_CAPTURE was 0).")
    kept = replay.promote(pages, args.dest)
    print(f"Kept {len(kept)} pages under {args.dest}")
    return 0


def check(args):
    files = [f for f in replay.goldens(args.dest) if not args.site or args.site in f.parent.name]
    if not files:
        print(f"No kept pages under {args.dest}: promote a run first.")
        return 0
    browser = Browser("about:blank", PlaywrightTransport(profile_dir=None, headless=True))
    failed = 0
    try:
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            html = path.with_suffix(".html")
            browser.navigate(html.as_uri())
            width, height = (browser.evaluate("document.documentElement.dataset.jevViewport") or "1280,900").split(",")
            browser.call("Emulation.setDeviceMetricsOverride", width=int(float(width)), height=int(float(height)),
                         deviceScaleFactor=1, mobile=False)  # fmt: skip
            browser.evaluate(replay.RESTORE)
            page = browser.observe()
            now = replay.reading(page["actions"])
            lines = replay.diff(data["expected"]["reading"], now)
            decided = replay.decide(data, page["actions"])
            if not args.reading_only and not replay.same_decision(decided, data["expected"]["decision"]):
                lines.append(f"  decision: {data['expected']['decision']} -> {decided}")
            name = f"{path.parent.name}/{path.stem}"
            if lines:
                failed += 1
                print(f"DIFF  {name}\n" + "\n".join(lines[:25]))
                if args.bless:
                    data["expected"] = {"reading": now, "decision": decided}
                    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                    print("  blessed")
            else:
                print(f"ok    {name}")
    finally:
        browser.transport.close()
    print(f"{len(files) - failed}/{len(files)} pages read and decided as kept")
    return 1 if failed and not args.bless else 0


def fields(args):
    """Field names (Workday's formField-*) on kept pages that rules.FIELD_IDS doesn't list yet, with their labels:
    the evidence for adding one (a name and the label it always comes with)."""
    from collections import defaultdict

    from jev_apply import rules

    seen = defaultdict(set)
    for path in replay.goldens(args.dest):
        for a in json.loads(path.read_text(encoding="utf-8"))["actions"]:
            if a.get("field_id") and a["field_id"] not in rules.FIELD_IDS:
                seen[a["field_id"]].add((a["label"].split(" → ")[0][:60], path.parent.name))
    for name, where in sorted(seen.items()):
        labels = sorted({label for label, _ in where})
        print(f"{name:<32} {len({site for _, site in where})} site(s)  labels: {' | '.join(labels)[:120]}")
    if not seen:
        print("Every field name on the kept pages is known.")
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("promote")
    p.add_argument("run", help="a run's report folder (runs/<time>) or its pages folder")
    p.add_argument("--dest", type=Path, default=DEST)
    c = sub.add_parser("check")
    c.add_argument("--bless", action="store_true")
    c.add_argument("--site", default="")
    c.add_argument("--reading-only", action="store_true", help="compare only how pages read")
    c.add_argument("--dest", type=Path, default=DEST)
    f = sub.add_parser("fields")
    f.add_argument("--dest", type=Path, default=DEST)
    args = parser.parse_args()
    return {"promote": promote, "check": check, "fields": fields}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
