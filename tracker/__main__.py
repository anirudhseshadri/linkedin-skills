"""Engagement tracker command line. Run from the repo root:

    python3 -m tracker fetch                                 # scrape via Apify, import, rebuild (needs APIFY_TOKEN)
    python3 -m tracker import export.json [more.json ...]   # add scrapes, rebuild the dashboard
    python3 -m tracker dashboard                             # rebuild tracker/data/post-pulse.html
    python3 -m tracker prospects [--csv out.csv]             # ICP prospects, strongest signal first
    python3 -m tracker mark "Jane Doe" contacted --note "DM sent 26 Sep"
    python3 -m tracker summary                               # what the database holds
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from tracker import fetch, store
from tracker.classify import load_config
from tracker.dashboard import build_data, render

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_OUT = DATA_DIR / "post-pulse.html"
STATUSES = ("open", "contacted", "replied", "meeting", "customer", "not-a-fit")


def _dashboard(db, args) -> Path:
    out = render(build_data(db, load_config(args.config), author=args.author), args.out)
    print(f"dashboard: {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tracker", description="Track LinkedIn engagers over time.")
    ap.add_argument("--db", default=str(store.DEFAULT_DB), help="SQLite file (default: tracker/data/engagement.db)")
    ap.add_argument("--config", default=None, help="rules file (default: tracker/data/config.json, else tracker/config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="run the Apify scraper now, then import the result")
    f.add_argument("profiles", nargs="*", help="LinkedIn profile URLs (default: \"profiles\" in the config)")
    f.add_argument("--max-posts", type=int, default=20, help="posts per profile (default 20)")
    f.add_argument("--since", default="3months", choices=fetch.POSTED_LIMITS, help="only posts newer than this")
    f.add_argument("--max-reactions", type=int, default=0, help="per post, 0 = all (default)")
    f.add_argument("--max-comments", type=int, default=0, help="per post, 0 = all (default)")
    f.add_argument("--yes", action="store_true", help="skip the spending prompt (for scheduled runs)")
    f.add_argument("--out", default=str(DEFAULT_OUT))
    f.add_argument("--author", default=None)

    p = sub.add_parser("import", help="add one or more Apify JSON exports")
    p.add_argument("files", nargs="+")
    p.add_argument("--again", action="store_true", help="import a file even if these exact bytes were imported before")
    p.add_argument("--no-dashboard", action="store_true")

    for name in ("import", "dashboard"):
        q = p if name == "import" else sub.add_parser("dashboard", help="rebuild the HTML dashboard")
        q.add_argument("--out", default=str(DEFAULT_OUT))
        q.add_argument("--author", default=None, help="only posts by this author (name or id)")

    p = sub.add_parser("prospects", help="list ICP prospects")
    p.add_argument("--csv", default=None)
    p.add_argument("--author", default=None)
    p.add_argument("--all", action="store_true", help="include people already marked contacted")

    p = sub.add_parser("mark", help="set a person's outreach status")
    p.add_argument("person", help="name (partial is fine), LinkedIn id or profile URL")
    p.add_argument("status", choices=STATUSES)
    p.add_argument("--note", default="")

    sub.add_parser("summary", help="counts in the database")

    args = ap.parse_args(argv)
    db = store.connect(args.db)

    if args.cmd == "fetch":
        cfg = load_config(args.config)
        profiles = args.profiles or cfg.get("profiles") or []
        if not profiles:
            print('no profiles: pass a URL, or add "profiles": ["https://www.linkedin.com/in/..."] '
                  "to tracker/data/config.json")
            return 1
        payload = fetch.build_input(profiles, max_posts=args.max_posts, posted_limit=args.since,
                                    max_reactions=args.max_reactions, max_comments=args.max_comments)
        print(f"Apify run {fetch.ACTOR} (paid per result on your Apify account):")
        print("  " + json.dumps(payload))
        if not args.yes:
            if not sys.stdin.isatty():
                print("not a terminal, so no one can confirm the spend: re-run with --yes")
                return 1
            if input("Run it? [y/N] ").strip().lower() not in ("y", "yes"):
                print("cancelled")
                return 1
        try:
            items = fetch.run_actor(payload, fetch.get_token())
        except fetch.FetchError as e:
            print(f"fetch failed: {e}")
            return 1
        path = fetch.save_export(items)
        s = store.import_file(db, path)
        print(f"saved {path}\nimported: {s['posts']} posts, {s['new_people']} new people, "
              f"{s['new_reactions']} new reactions, {s['new_comments']} new comments")
        _dashboard(db, args)
    elif args.cmd == "import":
        for f in args.files:
            try:
                s = store.import_file(db, f, allow_repeat=args.again)
            except FileExistsError as e:
                print(f"skipped: {e} (use --again to import it anyway)")
                continue
            print(f"imported {f}: {s['records']} records, {s['posts']} posts, {s['new_people']} new people, "
                  f"{s['new_reactions']} new reactions, {s['new_comments']} new comments")
        if not args.no_dashboard:
            _dashboard(db, args)
    elif args.cmd == "dashboard":
        _dashboard(db, args)
    elif args.cmd == "prospects":
        data = build_data(db, load_config(args.config), author=args.author)
        titles = {p["id"]: p["title"] for p in data["posts"]}
        rows = [p for p in data["people"] if p["icp"] and (args.all or p["status"] != "contacted")]
        rows.sort(key=lambda p: (-p["score"], -len(p["posts"]), p["n"]))
        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["name", "headline", "profile", "score", "posts_engaged", "reactions", "comments",
                            "asked_to_talk", "first_seen", "status", "note", "posts"])
                for p in rows:
                    w.writerow([p["n"], p["p"], p["u"], p["score"], len(p["posts"]), p["rx"], p["cm"],
                                "yes" if p["intent"] else "", p["first"], p["status"], p["note"],
                                " ; ".join(titles.get(x, x) for x in p["posts"])])
            print(f"{len(rows)} prospects written to {args.csv}")
        else:
            for p in rows:
                flags = ", ".join(f for f, on in (("asked to talk", p["intent"]), ("commented", p["cm"]),
                                                  (f"{len(p['posts'])} posts", len(p["posts"]) > 1),
                                                  ("new", p["new"])) if on)
                print(f"{p['score']:>2}  {p['n']}  |  {p['p'][:80]}" + (f"  [{flags}]" if flags else ""))
            print(f"\n{len(rows)} prospects")
    elif args.cmd == "mark":
        hits = store.find_people(db, args.person)
        if len(hits) != 1:
            print(f"{len(hits)} people match {args.person!r}; be more specific." if hits
                  else f"no one matches {args.person!r}")
            for h in hits[:10]:
                print(f"  {h['name']}  |  {h['url']}")
            return 1
        store.set_status(db, hits[0]["id"], args.status, args.note)
        print(f"{hits[0]['name']}: {args.status}")
    elif args.cmd == "summary":
        for table in ("imports", "posts", "people", "reactions", "comments", "person_status"):
            print(f"{table:14} {db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
