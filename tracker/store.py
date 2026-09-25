"""The engagement database: one SQLite file that remembers every import.

Each Apify export is a snapshot. Importing it adds what is new (people, posts,
reactions, comments) and records the post counts at that moment, so running the
same scrape again next week shows growth and new faces instead of overwriting.

Input is the dataset of the Apify actor "LinkedIn Profile Posts Scraper (No
Cookies)" with reactions and comments turned on: a flat list of records whose
`type` is "post", "reaction" or "comment". The same record can appear more than
once (the actor emits it for each query that reached it); ids de-duplicate it.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "engagement.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file TEXT, sha256 TEXT UNIQUE, imported_at TEXT, records INTEGER,
  new_people INTEGER, new_reactions INTEGER, new_comments INTEGER
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, url TEXT, author_id TEXT, author_name TEXT, author_headline TEXT,
  text TEXT, posted_at TEXT, is_repost INTEGER, repost_author TEXT, repost_text TEXT,
  first_import INTEGER, last_import INTEGER
);
CREATE TABLE IF NOT EXISTS post_snapshots (
  post_id TEXT, import_id INTEGER, captured_at TEXT,
  reactions INTEGER, comments INTEGER, shares INTEGER, mix TEXT,
  PRIMARY KEY (post_id, import_id)
);
CREATE TABLE IF NOT EXISTS people (
  id TEXT PRIMARY KEY, name TEXT, headline TEXT, url TEXT,
  first_seen TEXT, last_seen TEXT, first_import INTEGER, last_import INTEGER
);
CREATE TABLE IF NOT EXISTS headline_history (
  person_id TEXT, headline TEXT, seen_at TEXT, import_id INTEGER,
  PRIMARY KEY (person_id, headline)
);
CREATE TABLE IF NOT EXISTS reactions (
  post_id TEXT, person_id TEXT, type TEXT, first_import INTEGER, last_import INTEGER,
  PRIMARY KEY (post_id, person_id)
);
CREATE TABLE IF NOT EXISTS comments (
  id TEXT PRIMARY KEY, post_id TEXT, parent_id TEXT, person_id TEXT, is_author INTEGER,
  text TEXT, created_at TEXT, likes INTEGER, link TEXT, first_import INTEGER, last_import INTEGER
);
CREATE TABLE IF NOT EXISTS person_status (
  person_id TEXT PRIMARY KEY, status TEXT, note TEXT, updated_at TEXT
);
"""


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _activity_id(value: Any) -> Optional[str]:
    """The numeric activity id, from a bare id, a URN or a post URL."""
    if value is None:
        return None
    m = re.search(r"activity[:\-](\d{15,})", str(value))
    if m:
        return m.group(1)
    s = str(value)
    return s if s.isdigit() else None


def _person_key(actor: dict) -> Optional[str]:
    return actor.get("id") or actor.get("profileId") or actor.get("linkedinUrl")


def load_records(path: Path | str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items") or data.get("data") or []
    if not isinstance(data, list):
        raise ValueError("expected a JSON list of Apify records")
    return data


def import_file(db: sqlite3.Connection, path: Path | str, *, allow_repeat: bool = False) -> dict:
    """Import one Apify export. Refuses a file already imported (same bytes)
    unless `allow_repeat`, because a repeat would add a fake snapshot."""
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if not allow_repeat and db.execute("SELECT 1 FROM imports WHERE sha256=?", (digest,)).fetchone():
        raise FileExistsError(f"{path} was already imported")
    records = load_records(path)
    return import_records(db, records, file=str(path), sha256=None if allow_repeat else digest)


def import_records(db: sqlite3.Connection, records: Iterable[dict], *, file: str = "",
                   sha256: Optional[str] = None, captured_at: Optional[str] = None) -> dict:
    records = list(records)
    at = captured_at or _now()
    cur = db.execute(
        "INSERT INTO imports(file, sha256, imported_at, records) VALUES (?,?,?,?)",
        (file, sha256, at, len(records)),
    )
    imp = cur.lastrowid
    stats = {"import_id": imp, "records": len(records), "posts": 0,
             "new_people": 0, "new_reactions": 0, "new_comments": 0}
    seen_people: set[str] = set()

    def person(actor: dict) -> Optional[str]:
        pid = _person_key(actor or {})
        if not pid:
            return None
        headline = actor.get("position") or actor.get("info") or ""
        row = db.execute("SELECT headline FROM people WHERE id=?", (pid,)).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO people(id,name,headline,url,first_seen,last_seen,first_import,last_import)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (pid, actor.get("name"), headline, actor.get("linkedinUrl"), at, at, imp, imp),
            )
            stats["new_people"] += 1
        else:
            # Keep the newest non-empty headline; an empty one is a scrape gap, not a change.
            db.execute(
                "UPDATE people SET name=COALESCE(?,name), headline=CASE WHEN ?<>'' THEN ? ELSE headline END,"
                " url=COALESCE(url,?), last_seen=?, last_import=? WHERE id=?",
                (actor.get("name"), headline, headline, actor.get("linkedinUrl"), at, imp, pid),
            )
        if headline:
            db.execute("INSERT OR IGNORE INTO headline_history VALUES (?,?,?,?)", (pid, headline, at, imp))
        seen_people.add(pid)
        return pid

    posts_done: set[str] = set()
    for r in records:
        if r.get("type") != "post":
            continue
        pid = _activity_id(r.get("id")) or _activity_id(r.get("linkedinUrl"))
        if not pid or pid in posts_done:
            continue
        posts_done.add(pid)
        author = r.get("author") or {}
        repost = r.get("repost") or None
        posted = (r.get("postedAt") or {}).get("date")
        exists = db.execute("SELECT 1 FROM posts WHERE id=?", (pid,)).fetchone()
        vals = (r.get("linkedinUrl"), author.get("id"), author.get("name"), author.get("info"),
                r.get("content") or "", posted, 1 if repost else 0,
                ((repost or {}).get("author") or {}).get("name"), (repost or {}).get("content"))
        if exists:
            db.execute("UPDATE posts SET url=?,author_id=?,author_name=?,author_headline=?,text=?,posted_at=?,"
                       "is_repost=?,repost_author=?,repost_text=?,last_import=? WHERE id=?", (*vals, imp, pid))
        else:
            db.execute("INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (pid, *vals, imp, imp))
        eng = r.get("engagement") or {}
        mix = {x.get("type"): x.get("count", 0) for x in eng.get("reactions") or [] if x.get("type")}
        db.execute("INSERT OR REPLACE INTO post_snapshots VALUES (?,?,?,?,?,?,?)",
                   (pid, imp, at, eng.get("likes") or 0, eng.get("comments") or 0,
                    eng.get("shares") or 0, json.dumps(mix)))
        stats["posts"] += 1

    def post_of(r: dict) -> Optional[str]:
        return (_activity_id(r.get("postId")) or _activity_id((r.get("query") or {}).get("post"))
                or _activity_id(r.get("linkedinUrl")))

    for r in records:
        if r.get("type") != "reaction":
            continue
        post_id, who = post_of(r), person(r.get("actor") or {})
        if not post_id or not who:
            continue
        row = db.execute("SELECT 1 FROM reactions WHERE post_id=? AND person_id=?", (post_id, who)).fetchone()
        if row:
            db.execute("UPDATE reactions SET type=?, last_import=? WHERE post_id=? AND person_id=?",
                       (r.get("reactionType"), imp, post_id, who))
        else:
            db.execute("INSERT INTO reactions VALUES (?,?,?,?,?)", (post_id, who, r.get("reactionType"), imp, imp))
            stats["new_reactions"] += 1

    def comment(c: dict, post_id: str, parent: Optional[str]) -> None:
        cid = str(c.get("id") or "")
        actor = c.get("actor") or {}
        who = person(actor)
        if not cid or not who:
            return
        vals = (post_id, parent, who, 1 if actor.get("author") else 0, c.get("commentary") or "",
                c.get("createdAt"), (c.get("engagement") or {}).get("likes") or 0, c.get("linkedinUrl"))
        if db.execute("SELECT 1 FROM comments WHERE id=?", (cid,)).fetchone():
            db.execute("UPDATE comments SET post_id=?,parent_id=?,person_id=?,is_author=?,text=?,created_at=?,"
                       "likes=?,link=?,last_import=? WHERE id=?", (*vals, imp, cid))
        else:
            db.execute("INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?,?)", (cid, *vals, imp, imp))
            stats["new_comments"] += 1
        for reply in c.get("replies") or []:
            comment(reply, post_id, cid)

    for r in records:
        if r.get("type") == "comment":
            post_id = post_of(r)
            if post_id:
                comment(r, post_id, None)

    # A post author appears in replies as an actor; flag them by author id as well,
    # since the actor's own `author` flag is missing on nested replies.
    db.execute("UPDATE comments SET is_author=1 WHERE person_id IN (SELECT author_id FROM posts"
               " WHERE posts.id=comments.post_id)")
    db.execute("UPDATE imports SET new_people=?, new_reactions=?, new_comments=? WHERE id=?",
               (stats["new_people"], stats["new_reactions"], stats["new_comments"], imp))
    db.commit()
    return stats


def set_status(db: sqlite3.Connection, person_id: str, status: str, note: str = "") -> None:
    db.execute("INSERT OR REPLACE INTO person_status VALUES (?,?,?,?)", (person_id, status, note, _now()))
    db.commit()


def find_people(db: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    like = f"%{query.lower()}%"
    return db.execute("SELECT * FROM people WHERE lower(name) LIKE ? OR id=? OR url=?",
                      (like, query, query)).fetchall()
