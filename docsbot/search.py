"""Fuzzy, prefix-filterable lexical scoring over the docs index."""

from __future__ import annotations

import re
from typing import Any, Iterable

from rapidfuzz import fuzz

# Longer prefixes first so "faq:" is not eaten by a shorter key.
PREFIXES: list[tuple[str, str]] = [
    ("faq:", "faq"),
    ("fact:", "fact"),
    ("m:", "method"),
    ("t:", "type"),
    ("e:", "error"),
    ("f:", "feature"),
    ("w:", "webapp"),
]

KIND_ALIASES = {
    "method": "method",
    "methods": "method",
    "type": "type",
    "types": "type",
    "error": "error",
    "errors": "error",
    "feature": "feature",
    "features": "feature",
    "webapp": "webapp",
    "webapps": "webapp",
    "miniapp": "webapp",
    "miniapps": "webapp",
    "faq": "faq",
    "fact": "fact",
    "facts": "fact",
}

# Scores at or above these are "good enough" to show (full queries).
MIN_SCORE = 500
# Always show exact / prefix name hits first.
NAME_EXACT = 10_000
NAME_PREFIX = 9_000
NAME_CONTAINS = 8_000
# 1–2 character auto-suggest uses this floor on lexical prefix hits.
SHORT_PREFIX = 1_000


def parse_query(query: str) -> tuple[str | None, str]:
    q = (query or "").strip()
    lowered = q.lower()
    for prefix, kind in PREFIXES:
        if lowered.startswith(prefix):
            return kind, q[len(prefix) :].strip()
    return None, q


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _words(s: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w]


def score_entry(entry: dict[str, Any], query: str, *, short_query: bool | None = None) -> float:
    if not query:
        return 0.0
    q = _norm(query)
    compact_q = q.replace(" ", "")
    if short_query is None:
        short_query = len(compact_q) <= 2

    name = _norm(entry.get("name") or "")
    title = _norm(entry.get("title") or "")
    compact_name = name.replace(" ", "")
    compact_title = title.replace(" ", "")

    if name == q or title == q:
        return float(NAME_EXACT)
    if compact_name == compact_q or compact_title == compact_q:
        return float(NAME_EXACT - 10)
    if title.endswith(" " + q) or title.startswith(q + " ") or f" {q} " in f" {title} ":
        if len(q) >= 4:
            return float(NAME_EXACT - 50)

    # Prefix / partial on the identifier. Allow 1–2 character prefixes so the
    # inline list can refine on every keystroke (s → se → send → sendM…).
    if compact_q and (compact_name.startswith(compact_q) or compact_title.startswith(compact_q)):
        # Prefer shorter / closer names, but 1-char hits stay below 2-char, etc.
        tightness = min(len(compact_q), 20) * 15
        return float(NAME_PREFIX - min(len(compact_name) or len(name), 200) + tightness)
    if name.startswith(q) or title.startswith(q):
        return float(NAME_PREFIX - min(len(name), 200) + min(len(q), 50))

    if compact_q and len(compact_q) >= 2:
        # Prefix of any word in the name ("edit" vs "editMessageText" already
        # covered; "kybrd" won't hit here, but "keyb" vs "inline keyboard" will).
        for word in _words(name) + _words(title) + _words(entry.get("title") or ""):
            compact_w = word.replace(" ", "")
            if compact_w.startswith(compact_q) and len(compact_q) >= 2:
                return float(NAME_PREFIX - 80 - min(len(compact_w), 40))
            if len(compact_q) >= 4 and compact_q in compact_w:
                return float(NAME_CONTAINS - 40)

    if q in name or q in title:
        pos = name.find(q) if q in name else title.find(q)
        return float(NAME_CONTAINS - pos)

    if compact_q and compact_q in compact_name and len(compact_q) >= 3:
        return float(NAME_CONTAINS - 20)

    # Field / param name prefix (e.g. "caption" → sendPhoto has a caption field).
    if len(compact_q) >= 3:
        for field in entry.get("fields") or []:
            fn = _norm(field.get("name") or "").replace(" ", "")
            fd = _norm(field.get("description") or "")
            if not fn:
                continue
            if fn == compact_q:
                return 2200.0
            if fn.startswith(compact_q):
                return 1800.0
            if compact_q in fn and len(compact_q) >= 4:
                return 1400.0
            if len(compact_q) >= 5 and compact_q in fd:
                return 900.0

    if short_query:
        # 1–2 characters: do not run loose fuzzy matching (too many false hits).
        return 0.0

    # Typo-tolerant name matching ("sendMes", "inlin kybrd").
    name_score = fuzz.WRatio(q, entry.get("name") or "")
    title_score = fuzz.WRatio(q, entry.get("title") or "")
    # Compare typo tokens against spaced identifiers.
    spaced = _norm((entry.get("name") or "") + " " + (entry.get("title") or ""))
    typo = max(
        fuzz.token_sort_ratio(q, spaced),
        fuzz.partial_ratio(q, spaced),
        fuzz.token_set_ratio(q, spaced),
    )
    search_text = entry.get("search_text") or ""
    blob = search_text[:4000]
    blob_score = fuzz.token_set_ratio(q, blob)
    partial_name = fuzz.partial_ratio(q, name or title)

    # Lower typo thresholds slightly so "inlin kybrd" can still land on the name.
    if name_score < 70:
        name_score = 0
    if title_score < 70:
        title_score = 0
    if typo < 70:
        typo = 0
    if blob_score < 60:
        blob_score = 0
    if partial_name < 80:
        partial_name = 0

    score = max(name_score * 12, title_score * 12, typo * 11, blob_score * 8, partial_name * 10)

    tokens = [t for t in q.split() if t]
    blob_l = blob.lower()
    name_l = f"{name} {title}"
    if tokens:
        hits = sum(1 for t in tokens if t in blob_l)
        if hits == len(tokens):
            score += 250
            if all(t in name_l for t in tokens):
                score += 400
        elif hits >= max(1, len(tokens) - 1) and len(tokens) >= 3:
            score += 120

    if name_score >= 85 and len(name) <= 40:
        score += 80

    # A button/type that has a url / login_url field is the usual "button that opens a website".
    if any(w in q for w in ("website", "webpage", "web site", "url", "link")) or (
        "open" in q and "button" in q
    ):
        for field in entry.get("fields") or []:
            fn = (field.get("name") or "").lower()
            if fn in {"url", "login_url"}:
                score = max(score, 2100.0)
                break

    if entry.get("kind") == "error":
        for method in entry.get("related_methods") or []:
            ml = _norm(method).replace(" ", "")
            if not ml:
                continue
            if ml == compact_q or (len(compact_q) >= 5 and ml.startswith(compact_q)):
                score += 800
                break
    return float(score)


def lexical_search(
    index: dict[str, Any] | list[dict[str, Any]],
    query: str,
    *,
    limit: int = 50,
    offset: int = 0,
    min_score: float = MIN_SCORE,
) -> tuple[list[tuple[float, dict[str, Any]]], int]:
    """Lexical-only search (used as a fallback when no SearchEngine is loaded)."""
    entries: Iterable[dict[str, Any]]
    if isinstance(index, dict):
        entries = index.get("entries") or []
    else:
        entries = index

    kind, q = parse_query(query)
    if kind:
        pool = [e for e in entries if e.get("kind") == kind]
    else:
        pool = list(entries)

    if not q:
        def kind_only_key(e: dict[str, Any]) -> tuple:
            k = e.get("kind") or ""
            if k == "error":
                return (0, int(e.get("http_code") or 0), e.get("name") or "")
            if k in {"method", "type"}:
                return (1, e.get("name") or "")
            return (2, e.get("title") or "")

        ranked = [(0.0, e) for e in sorted(pool, key=kind_only_key)]
        total = len(ranked)
        return ranked[offset : offset + limit], total

    compact_q = _norm(q).replace(" ", "")
    short = len(compact_q) <= 2
    floor = 100.0 if short else min_score
    scored: list[tuple[float, dict[str, Any]]] = []
    for e in pool:
        s = score_entry(e, q, short_query=short)
        if s >= floor:
            scored.append((s, e))

    kind_rank = {
        "method": 0,
        "type": 1,
        "error": 2,
        "feature": 3,
        "webapp": 3,
        "faq": 4,
        "fact": 5,
    }

    def sort_key(item: tuple[float, dict[str, Any]]) -> tuple:
        s, e = item
        return (-s, kind_rank.get(e.get("kind") or "", 9), len(e.get("name") or ""), e.get("name") or "")

    scored.sort(key=sort_key)
    looks_like_name = " " not in q.strip() and len(compact_q) <= 40
    if looks_like_name and scored and scored[0][0] >= NAME_CONTAINS:
        cut = max(floor, 850)
        scored = [item for item in scored if item[0] >= cut]
    total = len(scored)
    return scored[offset : offset + limit], total


# Back-compat alias; the bot and tests prefer docsbot.retrieve.search once an engine exists.
search = lexical_search
