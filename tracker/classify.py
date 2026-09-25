"""Sort engagers by their LinkedIn headline: an audience group, and whether they
fit the ICP (ideal customer profile). Rules live in config.json, not here."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LOCAL_CONFIG = HERE / "data" / "config.json"
DEFAULT_CONFIG = HERE / "config.json"

# "Ex-GEP", "ex Deloitte", "Formerly Accenture" describe a past job. Matching
# them would file a current in-house buyer under the consultancy they left.
_PAST = re.compile(r"\b(ex|former|formerly|previously)[\s\-–:]+[\w&.]+", re.I)


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path) if path else (LOCAL_CONFIG if LOCAL_CONFIG.exists() else DEFAULT_CONFIG)
    return json.loads(p.read_text(encoding="utf-8"))


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def current_role(headline: str) -> str:
    return _PAST.sub(" ", headline or "")


def segment(headline: str, cfg: dict) -> str:
    h = headline or ""
    if _any(cfg.get("team_keywords", []), current_role(h)):
        return "team"
    now = current_role(h)
    for key in cfg.get("segment_order", []):
        if key in ("team", "other"):
            continue
        if _any(cfg["segments"].get(key, []), now):
            return key
    return "other"


def is_icp(headline: str, cfg: dict) -> bool:
    icp = cfg.get("icp") or {}
    now = current_role(headline)
    if not now.strip() or _any(cfg.get("team_keywords", []), now):
        return False
    return (_any(icp.get("function_keywords", []), now)
            and _any(icp.get("seniority_keywords", []), now)
            and not _any(icp.get("exclude_keywords", []), now))


def has_intent(text: str, cfg: dict) -> bool:
    return _any([re.escape(p) for p in cfg.get("intent_phrases", [])], text or "")
