"""Stable Telegram start-payload permalinks for index entries and searches.

Deep links look like https://t.me/<bot>?start=<payload>. Telegram allows at most
64 characters in the set [A-Za-z0-9_-].
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any, Iterable, Literal

PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_LEN = 64
KIND_PREFIX = {
    "method": "m",
    "type": "t",
    "error": "e",
    "feature": "f",
    "webapp": "w",
    "faq": "q",
    "fact": "b",
}
# Reverse map, plus a couple of aliases.
PREFIX_KIND = {v: k for k, v in KIND_PREFIX.items()}


def deep_link(bot_username: str | None, payload: str) -> str:
    user = (bot_username or "your_bot").lstrip("@")
    return f"https://t.me/{user}?start={payload}"


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", text or "")
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _hash_payload(kind_prefix: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).digest()[:8]
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{kind_prefix}h_{token}"[:MAX_LEN]


def _valid(payload: str) -> bool:
    return bool(PAYLOAD_RE.fullmatch(payload))


def encode_field_payload(path: list[str]) -> str:
    """Permalink for a field path such as sendMessage.chat_id -> p_sendMessage__chat_id.

    Falls back to a short ph_<hash> token when the readable slug is too long.
    """
    parts = [_slug(p) for p in path if p]
    if len(parts) >= 2 and all(parts):
        readable = "p_" + "__".join(parts)
        if _valid(readable) and len(readable) <= MAX_LEN:
            return readable
    key = ".".join(path)
    return _hash_payload("p", key)


def field_deep_link(parent_name: str, field_name: str, bot_username: str | None) -> str | None:
    """Readable p_Parent__field deep link, or None if a hash fallback would be needed."""
    if not parent_name or not field_name or not bot_username:
        return None
    payload = encode_field_payload([parent_name, field_name])
    if payload.startswith("p_") and not payload.startswith("ph_"):
        return deep_link(bot_username, payload)
    return None


def encode_all_fields_payload(parent_name: str) -> str:
    """Permalink for an all-fields view: pa_sendMessage."""
    slug = _slug(parent_name)
    readable = f"pa_{slug}" if slug else ""
    if readable and _valid(readable):
        return readable
    return _hash_payload("pa", parent_name or "")


def encode_multi_fields_payload(parent_name: str, field_names: list[str]) -> str:
    """Permalink for a multi-field view: pm_sendMessage__text__parse_mode."""
    parts = [_slug(parent_name)] + [_slug(f) for f in field_names if f]
    readable = "pm_" + "__".join(p for p in parts if p)
    if _valid(readable) and len(readable) <= MAX_LEN:
        return readable
    return _hash_payload("pm", ".".join([parent_name] + list(field_names)))


def encode_search_payload(query: str) -> str:
    """Encode a search query as s_<base64url>, truncated to 64 chars if needed."""
    raw = (query or "").encode("utf-8")
    # Leave room for the "s_" prefix; drop padding.
    while True:
        token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        payload = "s_" + token
        if len(payload) <= MAX_LEN:
            return payload
        # Shorten the query until it fits (rare for normal inline queries).
        if not raw:
            return "s_"
        raw = raw[:-1]


def decode_search_payload(payload: str) -> str | None:
    if not payload.startswith("s_"):
        return None
    token = payload[2:]
    if not token:
        return ""
    pad = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except Exception:
        return None


def build_permalink_map(entries: Iterable[dict[str, Any]]) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Return (entry.id -> payload, payload -> entry).

    Prefers a readable slug such as m_sendMessage when it is unique and valid;
    otherwise a short stable hash (mh_<token>).
    """
    items = [e for e in entries if e.get("id")]
    # Two-pass: propose readable slugs, then hash collisions / overflows.
    proposed: list[tuple[dict[str, Any], str | None]] = []
    counts: dict[str, int] = {}
    for entry in items:
        kind = entry.get("kind") or ""
        prefix = KIND_PREFIX.get(kind, "x")
        name = entry.get("name") or entry.get("title") or ""
        slug = _slug(name)
        readable = f"{prefix}_{slug}" if slug else ""
        if readable and _valid(readable) and len(readable) <= MAX_LEN:
            counts[readable] = counts.get(readable, 0) + 1
            proposed.append((entry, readable))
        else:
            proposed.append((entry, None))

    id_to_payload: dict[str, str] = {}
    payload_to_entry: dict[str, dict[str, Any]] = {}
    used: set[str] = set()
    for entry, readable in proposed:
        payload = None
        if readable and counts.get(readable, 0) == 1 and readable not in used:
            payload = readable
        if payload is None:
            payload = _hash_payload(KIND_PREFIX.get(entry.get("kind") or "", "x"), entry["id"])
            # Extremely unlikely hash collision: salt with the name.
            if payload in used:
                payload = _hash_payload("z", entry["id"] + ":" + (entry.get("name") or ""))
        used.add(payload)
        id_to_payload[entry["id"]] = payload
        payload_to_entry[payload] = entry
    return id_to_payload, payload_to_entry


def parse_start_payload(
    payload: str,
    payload_to_entry: dict[str, dict[str, Any]],
) -> tuple[Literal["home", "entry", "search", "field", "unknown"], Any]:
    """Classify a /start payload."""
    p = (payload or "").strip()
    if not p:
        return "home", None
    # Telegram sometimes delivers the payload with a bot mention leftover; keep [A-Za-z0-9_-] only.
    if not _valid(p):
        p = _slug(p)
        if not p:
            return "unknown", payload
    if p.startswith("s_"):
        query = decode_search_payload(p)
        if query is None:
            return "unknown", payload
        return "search", query
    # Field / parameter paths: p_sendMessage__chat_id, pa_sendMessage,
    # pm_sendMessage__text__parse_mode, or ph_/pah_/pmh_ hashes.
    # Resolution of the path is left to the caller (SearchEngine.entry_for_payload).
    if p.startswith(("p_", "pa_", "pm_", "ph_", "pah_", "pmh_")):
        return "field", p
    entry = payload_to_entry.get(p)
    if entry is not None:
        return "entry", entry
    return "unknown", payload
