"""The engagement tracker: import is idempotent, a later scrape adds only what is
new, snapshots record growth, and prospects are sorted by headline rules.

Offline, in-memory SQLite, synthetic people only (the repo is public; real
exports live in the gitignored tracker/data/).
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from tracker import fetch, store
from tracker.classify import is_icp, load_config, segment
from tracker.dashboard import build_data, render

CFG = Path(__file__).resolve().parents[1] / "tracker" / "config.json"
POST = "7500000000000000001"
AUTHOR = {"id": "AUTH", "name": "Pat Author", "info": "Co-Founder at Zavo",
          "linkedinUrl": "https://www.linkedin.com/in/pat"}


def actor(pid, name, position, **extra):
    return {"id": pid, "name": name, "position": position,
            "linkedinUrl": f"https://www.linkedin.com/in/{pid.lower()}", **extra}


def export(likes=3, extra_reactions=()):
    post = {"type": "post", "id": POST, "linkedinUrl": f"https://www.linkedin.com/posts/pat_x-activity-{POST}-ab",
            "content": "First line is the title\n\nBody.", "author": AUTHOR,
            "postedAt": {"date": "2026-09-01T10:00:00.000Z"},
            "engagement": {"likes": likes, "comments": 2, "shares": 0,
                           "reactions": [{"type": "LIKE", "count": likes}]}}
    reactions = [
        {"type": "reaction", "id": "r1", "reactionType": "LIKE", "postId": POST,
         "actor": actor("CPO1", "Casey Buyer", "Chief Procurement Officer at Acme")},
        {"type": "reaction", "id": "r2", "reactionType": "PRAISE", "postId": POST,
         "actor": actor("TEAM1", "Tam Mate", "Engineer at Zavo")},
        {"type": "reaction", "id": "r3", "reactionType": "LIKE", "postId": POST,
         "actor": actor("CONS1", "Con Sultant", "Procurement Director at Deloitte")},
        *extra_reactions,
    ]
    comment = {"type": "comment", "id": "c1", "postId": POST, "commentary": "Would love to hear more about this",
               "createdAt": "2026-09-01T12:00:00.000Z", "engagement": {"likes": 1},
               "actor": actor("CPO1", "Casey Buyer", "Chief Procurement Officer at Acme", author=False),
               "replies": [{"id": "c2", "commentary": "Let's talk", "createdAt": "2026-09-01T13:00:00.000Z",
                            "engagement": {"likes": 0}, "actor": {**AUTHOR, "position": AUTHOR["info"]}}]}
    # The actor repeats records once per query; the duplicate must not double count.
    return [post, copy.deepcopy(post), *reactions, reactions[0], comment]


class Import(unittest.TestCase):
    def setUp(self):
        self.db = store.connect(":memory:")

    def test_first_import_counts_unique_records(self):
        s = store.import_records(self.db, export())
        self.assertEqual((s["posts"], s["new_reactions"], s["new_comments"]), (1, 3, 2))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM people").fetchone()[0], 4)  # 3 reactors + author

    def test_later_scrape_adds_only_new_and_keeps_history(self):
        store.import_records(self.db, export(likes=3), captured_at="2026-09-02T00:00:00Z")
        later = export(likes=5, extra_reactions=[
            {"type": "reaction", "id": "r4", "reactionType": "LIKE", "postId": POST,
             "actor": actor("NEW1", "Nia New", "VP Procurement at Beta")}])
        s = store.import_records(self.db, later, captured_at="2026-09-09T00:00:00Z")
        self.assertEqual((s["new_people"], s["new_reactions"], s["new_comments"]), (1, 1, 0))
        hist = self.db.execute("SELECT reactions FROM post_snapshots ORDER BY import_id").fetchall()
        self.assertEqual([r[0] for r in hist], [3, 5])

    def test_same_file_twice_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.json"
            f.write_text(json.dumps(export()))
            store.import_file(self.db, f)
            with self.assertRaises(FileExistsError):
                store.import_file(self.db, f)

    def test_author_reply_is_flagged_and_not_an_engager(self):
        store.import_records(self.db, export())
        self.assertEqual(self.db.execute("SELECT is_author FROM comments WHERE id='c2'").fetchone()[0], 1)
        data = build_data(self.db, load_config(CFG))
        self.assertNotIn("AUTH", {p["id"] for p in data["people"]})


class Classify(unittest.TestCase):
    cfg = load_config(CFG)

    def test_segments(self):
        self.assertEqual(segment("Engineer at Zavo", self.cfg), "team")
        self.assertEqual(segment("Procurement Director at Deloitte", self.cfg), "consult")
        self.assertEqual(segment("Head of Procurement at Acme", self.cfg), "proc")

    def test_icp_needs_function_and_seniority_and_no_consultancy(self):
        self.assertTrue(is_icp("Chief Procurement Officer at Acme", self.cfg))
        self.assertFalse(is_icp("Procurement Analyst at Acme", self.cfg))
        self.assertFalse(is_icp("Procurement Director at Deloitte", self.cfg))

    def test_past_employer_does_not_disqualify(self):
        self.assertTrue(is_icp("Director - IT Sourcing at Bank | Ex - GEP | MBA", self.cfg))


class Dashboard(unittest.TestCase):
    def test_prospect_signals_and_render(self):
        db = store.connect(":memory:")
        store.import_records(db, export())
        store.set_status(db, "CPO1", "contacted", "DM sent")
        data = build_data(db, load_config(CFG))
        casey = next(p for p in data["people"] if p["id"] == "CPO1")
        self.assertTrue(casey["icp"] and casey["intent"])
        self.assertEqual(casey["status"], "contacted")
        self.assertEqual(data["posts"][0]["title"], "First line is the title")
        with tempfile.TemporaryDirectory() as d:
            html = render(data, Path(d) / "p.html").read_text()
        self.assertNotIn("__DATA__", html)
        self.assertIn("Casey Buyer", html)


class Fetch(unittest.TestCase):
    """The Apify run flow against a fake API: start, poll, page the dataset."""

    def test_input_turns_on_reactions_and_comments(self):
        payload = fetch.build_input(["https://www.linkedin.com/in/x/"], max_posts=5, posted_limit="month")
        self.assertEqual(payload["targetUrls"], ["https://www.linkedin.com/in/x/"])
        self.assertTrue(payload["scrapeReactions"] and payload["scrapeComments"])
        self.assertEqual((payload["maxPosts"], payload["postedLimit"]), (5, "month"))
        with self.assertRaises(ValueError):
            fetch.build_input(["u"], posted_limit="fortnight")
        with self.assertRaises(ValueError):
            fetch.build_input([])

    def test_run_polls_until_done_then_pages_the_dataset(self):
        calls, status = [], iter(["RUNNING", "SUCCEEDED"])
        rows = [{"type": "reaction", "id": str(i)} for i in range(1500)]

        def fake(method, url, token, body=None, timeout=60):
            calls.append((method, url.split("?")[0], token))
            if url.endswith("/runs"):
                return {"data": {"id": "run1", "defaultDatasetId": "ds1", "status": "READY"}}
            if "/actor-runs/" in url:
                return {"data": {"status": next(status)}}
            offset = int(url.split("offset=")[1].split("&")[0])
            return rows[offset:offset + 1000]

        items = fetch.run_actor({"targetUrls": ["u"]}, "tok", request=fake, sleep=lambda s: None, log=lambda m: None)
        self.assertEqual(len(items), 1500)
        self.assertEqual(sum(1 for c in calls if "/actor-runs/" in c[1]), 2)
        self.assertTrue(all(c[2] == "tok" for c in calls))
        self.assertTrue(calls[0][1].endswith(f"/acts/{fetch.ACTOR}/runs"))

    def test_failed_run_raises(self):
        def fake(method, url, token, body=None, timeout=60):
            if url.endswith("/runs"):
                return {"data": {"id": "r", "defaultDatasetId": "d", "status": "RUNNING"}}
            return {"data": {"status": "FAILED"}}
        with self.assertRaises(fetch.FetchError):
            fetch.run_actor({}, "t", request=fake, sleep=lambda s: None, log=lambda m: None)

    def test_missing_token_is_a_clear_error(self):
        with mock.patch.dict("os.environ", {"APIFY_TOKEN": ""}), mock.patch("lib._env.load_env"):
            with self.assertRaises(fetch.FetchError):
                fetch.get_token()


if __name__ == "__main__":
    unittest.main()
