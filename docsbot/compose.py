"""Compose a formatted message with $tokens replaced by doc deep links."""

from __future__ import annotations

import html as htmlmod
import re
from typing import Any, Callable

from .permalinks import deep_link
from .textutil import escape

DEFAULT_BOT = "autoblogcarpperbot"

# $sendMessage, $Message.chat, $sendMessage.text, $Update.*
TOKEN_RE = re.compile(
    r"\$([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_*,]+(?:\.[A-Za-z0-9_*,]+)*)?)"
)
# Markdown-ish markers applied after tokens are stashed.
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*")
_ITALIC = re.compile(r"(?<!\w)_(.+?)_(?!\w)")


def parse_query_for_compose(query: str) -> tuple[bool, str]:
    """Return (is_compose, body).

    Compose triggers:
    - a leading f: / F: when the remainder contains a $token
      (otherwise f: stays the features prefix);
    - otherwise, a $token plus any other non-empty text.
    """
    q = (query or "").strip()
    if not q:
        return False, q
    lowered = q.lower()
    if lowered.startswith("f:"):
        body = q[2:].lstrip()
        if TOKEN_RE.search(body):
            return True, body
        return False, q
    if not TOKEN_RE.search(q):
        return False, q
    remainder = TOKEN_RE.sub("", q).strip()
    if remainder:
        return True, q
    return False, q


def detect_compose(query: str) -> str | None:
    ok, body = parse_query_for_compose(query)
    return body if ok else None


def resolve_token(
    token: str,
    resolver: Callable[[str], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Resolve $token text (without the dollar) to a catalog or field entry."""
    name = (token or "").strip()
    if not name:
        return None
    return resolver(name)


def format_compose_html(
    body: str,
    *,
    resolver: Callable[[str], dict[str, Any] | None],
    bot_username: str | None,
) -> tuple[str, list[str], list[str]]:
    """Return (html, resolved_names, unresolved_tokens)."""
    mention = (bot_username or DEFAULT_BOT).lstrip("@")
    resolved: list[str] = []
    unresolved: list[str] = []
    links: list[str] = []

    def stash_token(m: re.Match[str]) -> str:
        token = m.group(1)
        entry = resolve_token(token, resolver)
        if entry is None:
            unresolved.append(m.group(0))
            links.append(escape(m.group(0)))
        else:
            label = entry.get("name") or token
            payload = entry.get("field_payload") or entry.get("permalink") or ""
            href = ""
            if payload:
                href = deep_link(mention, payload)
            elif entry.get("url"):
                href = entry["url"]
            if href:
                links.append(f'<a href="{htmlmod.escape(href, quote=True)}">{escape(label)}</a>')
                resolved.append(label)
            else:
                unresolved.append(m.group(0))
                links.append(escape(m.group(0)))
        return f"\x01{len(links) - 1}\x01"

    stashed = TOKEN_RE.sub(stash_token, body or "")
    formatted = _format_markdown(stashed)
    out_html = re.sub(
        r"\x01(\d+)\x01",
        lambda m: links[int(m.group(1))] if int(m.group(1)) < len(links) else m.group(0),
        formatted,
    ).strip()
    return out_html, resolved, unresolved


MIN_NAME_SCORE = 2082  # typo floor (2000 + 82)


def make_resolver(engine: Any) -> Callable[[str], dict[str, Any] | None]:
    """Lexical resolver: exact/prefix/typo names, plus field and fieldset paths."""
    from .dotpath import looks_like_dot_query, score_name

    catalog = [
        e
        for e in engine.entries
        if (e.get("kind") or "") in {"method", "type", "error", "feature", "webapp", "faq", "fact"}
    ]

    def resolve(token: str) -> dict[str, Any] | None:
        tok = (token or "").strip()
        if not tok:
            return None
        if looks_like_dot_query(tok):
            hits, _total, _t = engine.search(tok, limit=5, offset=0)
            if hits and hits[0][1].get("kind") in {"field", "fieldset"}:
                entry = dict(hits[0][1])
                entry["permalink"] = engine.permalink_for(hits[0][1])
                return entry
            return None
        best_s, best = 0.0, None
        for entry in catalog:
            s = score_name(entry.get("name") or "", tok, typo_partial=False)
            if s > best_s:
                best_s, best = s, entry
        if best is None or best_s < MIN_NAME_SCORE:
            return None
        out = dict(best)
        out["permalink"] = engine.permalink_for(best)
        return out

    return resolve


def _format_markdown(text: str) -> str:
    if not text:
        return ""
    raw = text
    stash: list[str] = []

    def put(html: str) -> str:
        stash.append(html)
        return f"\x00{len(stash) - 1}\x00"

    def repl_code(m: re.Match[str]) -> str:
        return put(f"<code>{escape(m.group(1))}</code>")

    def repl_bold(m: re.Match[str]) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return put(f"<b>{escape(inner)}</b>")

    def repl_italic(m: re.Match[str]) -> str:
        return put(f"<i>{escape(m.group(1))}</i>")

    work = _CODE.sub(repl_code, raw)
    work = _BOLD.sub(repl_bold, work)
    work = _ITALIC.sub(repl_italic, work)
    out: list[str] = []
    i = 0
    while i < len(work):
        if work[i] == "\x00":
            end = work.find("\x00", i + 1)
            if end == -1:
                out.append(escape(work[i:]))
                break
            try:
                out.append(stash[int(work[i + 1 : end])])
            except (ValueError, IndexError):
                out.append(escape(work[i : end + 1]))
            i = end + 1
            continue
        # Walk to next stash or end, escape the slice.
        nxt = work.find("\x00", i)
        chunk = work[i:] if nxt == -1 else work[i:nxt]
        out.append(escape(chunk))
        if nxt == -1:
            break
        i = nxt
    return "".join(out)


def compose_preview(body: str, resolved: list[str]) -> tuple[str, str]:
    """Inline result title and description."""
    plain = TOKEN_RE.sub(lambda m: m.group(1), body)
    plain = re.sub(r"\s+", " ", plain).strip()
    title = plain if len(plain) <= 64 else plain[:63].rsplit(" ", 1)[0] + "…"
    if not title:
        title = "Linked message"
    if resolved:
        desc = "Sends this text with links to " + ", ".join(resolved[:4])
        if len(resolved) > 4:
            desc += ", …"
    else:
        desc = "Sends this text. Some $tokens could not be resolved."
    if len(desc) > 100:
        desc = desc[:99].rsplit(" ", 1)[0] + "…"
    return title, desc
