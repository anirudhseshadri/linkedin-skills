"""Build the Post Pulse dashboard from the engagement database.

The page is one static HTML file with the data embedded, so it opens offline
and can be shared as a file. It is rebuilt from the database after every
import; nothing in it is edited by hand.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tracker.classify import has_intent, is_icp, load_config, segment

TEMPLATE = Path(__file__).resolve().parent / "template.html"


def _title(text: str, is_repost: bool, repost_author: Optional[str]) -> str:
    if is_repost:
        return f"Reshare: {repost_author}" if repost_author else "Reshare"
    first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "Untitled post")
    return first if len(first) <= 70 else first[:68].rstrip() + "…"


def build_data(db: sqlite3.Connection, cfg: Optional[dict] = None, author: Optional[str] = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    where, args = ("", ())
    if author:
        where, args = ("WHERE lower(author_name) LIKE ? OR author_id=?", (f"%{author.lower()}%", author))
    post_rows = db.execute(f"SELECT * FROM posts {where}", args).fetchall()
    post_ids = [r["id"] for r in post_rows]
    marks = ",".join("?" * len(post_ids)) or "''"

    imports = [dict(r) for r in db.execute("SELECT id, file, imported_at, records, new_people, new_reactions,"
                                           " new_comments FROM imports ORDER BY id")]
    latest = imports[-1]["id"] if imports else None

    snaps = defaultdict(list)
    for s in db.execute(f"SELECT * FROM post_snapshots WHERE post_id IN ({marks}) ORDER BY import_id", post_ids):
        snaps[s["post_id"]].append(s)

    posts = []
    for r in post_rows:
        hist = snaps.get(r["id"], [])
        last = hist[-1] if hist else None
        posts.append({
            "id": r["id"], "title": _title(r["text"], bool(r["is_repost"]), r["repost_author"]),
            "date": (r["posted_at"] or "")[:10], "url": r["url"], "text": r["text"] or r["repost_text"] or "",
            "repost": bool(r["is_repost"]), "author": r["author_name"],
            "reactions": last["reactions"] if last else 0, "comments": last["comments"] if last else 0,
            "shares": last["shares"] if last else 0, "mix": json.loads(last["mix"]) if last else {},
            "history": [{"at": h["captured_at"], "i": h["import_id"], "reactions": h["reactions"],
                         "comments": h["comments"], "shares": h["shares"]} for h in hist],
        })

    rx = [{"pid": r["post_id"], "t": r["type"], "a": r["person_id"]}
          for r in db.execute(f"SELECT * FROM reactions WHERE post_id IN ({marks})", post_ids)]

    ppl_rows = {r["id"]: r for r in db.execute("SELECT * FROM people")}
    status = {r["person_id"]: dict(r) for r in db.execute("SELECT * FROM person_status")}
    authors = {r["author_id"] for r in post_rows if r["author_id"]}

    cm_rows = db.execute(f"SELECT * FROM comments WHERE post_id IN ({marks}) ORDER BY created_at", post_ids).fetchall()
    replies = defaultdict(list)
    for c in cm_rows:
        if c["parent_id"]:
            replies[c["parent_id"]].append(c)

    def who(pid: str) -> dict:
        p = ppl_rows.get(pid)
        return {"n": p["name"] if p else "Unknown", "p": (p["headline"] if p else "") or "",
                "u": p["url"] if p else ""}

    comments = []
    for c in cm_rows:
        if c["parent_id"]:
            continue
        rs = replies.get(c["id"], [])
        comments.append({
            **who(c["person_id"]), "aid": c["person_id"], "pid": c["post_id"], "t": c["text"],
            "at": (c["created_at"] or "")[:16], "likes": c["likes"], "replies": len(rs), "link": c["link"],
            "me": bool(c["is_author"]), "s": segment(who(c["person_id"])["p"], cfg),
            "r": [{**who(x["person_id"]), "t": x["text"], "at": (x["created_at"] or "")[:16],
                   "likes": x["likes"], "me": bool(x["is_author"])} for x in rs],
        })

    engaged = defaultdict(set)
    rx_count, cm_count, intent = Counter(), Counter(), defaultdict(bool)
    for r in rx:
        engaged[r["a"]].add(r["pid"])
        rx_count[r["a"]] += 1
    for c in cm_rows:
        if c["is_author"] or c["person_id"] in authors:
            continue
        engaged[c["person_id"]].add(c["post_id"])
        cm_count[c["person_id"]] += 1
        if has_intent(c["text"], cfg):
            intent[c["person_id"]] = True

    multi_import = len(imports) > 1
    people = []
    for pid, pset in engaged.items():
        if pid in authors:
            continue
        p = ppl_rows.get(pid)
        if not p:
            continue
        headline = p["headline"] or ""
        icp = is_icp(headline, cfg)
        new = multi_import and p["first_import"] == latest
        score = (4 if intent[pid] else 0) + (3 if cm_count[pid] else 0) + 2 * (len(pset) - 1) + (1 if new else 0)
        st = status.get(pid) or {}
        people.append({
            "id": pid, "n": p["name"], "p": headline, "u": p["url"], "posts": sorted(pset),
            "rx": rx_count[pid], "cm": cm_count[pid], "s": segment(headline, cfg), "icp": icp,
            "intent": intent[pid], "new": new, "first": (p["first_seen"] or "")[:10], "score": score,
            "status": st.get("status", ""), "note": st.get("note", ""),
        })

    owner = cfg.get("owner_label") or (Counter(r["author_name"] for r in post_rows).most_common(1) or [("", 0)])[0][0]
    return {
        "posts": posts, "people": people, "comments": comments, "rx": rx,
        "meta": {
            "owner": owner, "teamLabel": cfg.get("team_label", "Team"), "icpLabel": (cfg.get("icp") or {}).get("label", "ICP"),
            "imports": imports, "latest": latest,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
    }


def render(data: dict, out: Path | str) -> Path:
    html = TEMPLATE.read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html.replace("__DATA__", blob, 1), encoding="utf-8")
    return out
