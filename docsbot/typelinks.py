"""Deep-link Bot API type names inside return and field type strings."""

from __future__ import annotations

import html as htmlmod
import re
from typing import Any, Iterable

from .permalinks import deep_link
from .textutil import escape

# Identifiers that are not catalog types and must stay as plain text.
PRIMITIVES = frozenset({
    "string", "integer", "boolean", "float", "int", "long", "number",
    "true", "false", "null", "object", "value", "int64", "double",
})

IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# lowercased type name -> {name, payload, url}
_TYPE_LINKS: dict[str, dict[str, str]] = {}


def configure_type_links(
    entries: Iterable[dict[str, Any]],
    id_to_payload: dict[str, str] | None = None,
) -> None:
    """Build the lookup used to turn type names into start= payloads."""
    global _TYPE_LINKS
    payloads = id_to_payload or {}
    links: dict[str, dict[str, str]] = {}
    for entry in entries:
        if (entry.get("kind") or "") != "type":
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        links[name.lower()] = {
            "name": name,
            "payload": payloads.get(entry.get("id") or "") or "",
            "url": entry.get("url") or "",
        }
    _TYPE_LINKS = links


def lookup_type(name: str) -> dict[str, str] | None:
    if not name or name.lower() in PRIMITIVES:
        return None
    found = _TYPE_LINKS.get(name.lower())
    return dict(found) if found else None


def iter_type_tokens(type_str: str) -> list[tuple[str, Any]]:
    """Split a type string into ('text', str) and ('link', info) parts."""
    text = type_str or ""
    parts: list[tuple[str, Any]] = []
    last = 0
    for m in IDENT_RE.finditer(text):
        if m.start() > last:
            parts.append(("text", text[last : m.start()]))
        info = lookup_type(m.group(0))
        if info:
            parts.append(("link", info))
        else:
            parts.append(("text", m.group(0)))
        last = m.end()
    if last < len(text):
        parts.append(("text", text[last:]))
    if not parts:
        parts.append(("text", text or "unknown"))
    return parts


def type_href(info: dict[str, str], bot_username: str | None) -> str:
    payload = info.get("payload") or ""
    if payload and bot_username:
        return deep_link(bot_username, payload)
    return info.get("url") or ""


def type_html(
    type_str: str,
    *,
    bot_username: str | None = None,
    italic: bool = True,
) -> str:
    """HTML for a type string, with catalog types turned into deep links."""
    chunks: list[str] = []
    for kind, val in iter_type_tokens(type_str):
        if kind == "text":
            chunks.append(escape(val))
            continue
        label = escape(val.get("name") or "")
        href = type_href(val, bot_username)
        if href:
            chunks.append(f'<a href="{htmlmod.escape(href, quote=True)}">{label}</a>')
        else:
            chunks.append(f"<code>{label}</code>")
    inner = "".join(chunks) or escape(type_str or "unknown")
    return f"<i>{inner}</i>" if italic else inner
