"""Fetch fresh scrapes from Apify and import them.

Runs the actor "LinkedIn Profile Posts Scraper (No Cookies)"
(harvestapi/linkedin-profile-posts) with reactions and comments on, saves the
raw dataset to tracker/data/exports/, then imports it like any other export.

A profile scrape with reactions can outlast Apify's 5-minute synchronous
endpoint, so this starts a run, polls it, and then pages through the dataset.

Auth: APIFY_TOKEN from the environment or the repo's .env, sent as an
Authorization header (never in the URL, where it would leak into logs).
Standard library only.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ACTOR = "harvestapi~linkedin-profile-posts"
API = "https://api.apify.com/v2"
EXPORTS = Path(__file__).resolve().parent / "data" / "exports"
POSTED_LIMITS = ("1h", "24h", "week", "month", "3months", "6months", "year")
DONE = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


class FetchError(RuntimeError):
    pass


def get_token() -> str:
    try:
        from lib._env import load_env   # reads the repo .env when python-dotenv is installed
        load_env()
    except Exception:
        pass
    token = os.getenv("APIFY_TOKEN", "").strip()
    if not token:
        raise FetchError("APIFY_TOKEN is not set. Add it to the repo's .env file or export it in your shell.")
    return token


def build_input(profiles: list[str], *, max_posts: int = 20, posted_limit: Optional[str] = "3months",
                max_reactions: int = 0, max_comments: int = 0) -> dict[str, Any]:
    """The actor input. 0 for reactions or comments means all of them."""
    if not profiles:
        raise ValueError("at least one profile URL is needed")
    if posted_limit and posted_limit not in POSTED_LIMITS:
        raise ValueError(f"posted_limit must be one of {POSTED_LIMITS}")
    payload: dict[str, Any] = {
        "targetUrls": list(profiles),
        "maxPosts": max(0, int(max_posts)),
        "scrapeReactions": True,
        "maxReactions": max(0, int(max_reactions)),
        "scrapeComments": True,
        "maxComments": max(0, int(max_comments)),
    }
    if posted_limit:
        payload["postedLimit"] = posted_limit
    return payload


def _request(method: str, url: str, token: str, body: Optional[dict] = None, timeout: float = 60) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "User-Agent": "linkedin-skills-tracker/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise FetchError(f"Apify answered HTTP {e.code} for {method} {url.split('?')[0]}: {detail}") from None


def run_actor(payload: dict, token: str, *, poll_every: float = 10, max_wait: float = 3600,
              request: Callable = _request, sleep: Callable = time.sleep,
              log: Callable[[str], None] = print) -> list[dict]:
    run = request("POST", f"{API}/acts/{ACTOR}/runs", token, payload)["data"]
    run_id, dataset = run["id"], run["defaultDatasetId"]
    log(f"started Apify run {run_id}")
    waited = 0.0
    status = run.get("status")
    while status not in DONE:
        if waited >= max_wait:
            raise FetchError(f"run {run_id} still {status} after {int(max_wait)}s; it keeps running on Apify,"
                             f" import its dataset later from the Apify console")
        sleep(poll_every)
        waited += poll_every
        status = request("GET", f"{API}/actor-runs/{run_id}", token)["data"]["status"]
    if status != "SUCCEEDED":
        raise FetchError(f"run {run_id} ended {status}; see it in the Apify console")
    items: list[dict] = []
    offset, page = 0, 1000
    while True:
        chunk = request("GET", f"{API}/datasets/{dataset}/items?format=json&clean=true"
                               f"&offset={offset}&limit={page}", token)
        items.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    log(f"run {run_id} finished: {len(items)} records")
    return items


def save_export(items: list[dict], out_dir: Path = EXPORTS) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"apify-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path
