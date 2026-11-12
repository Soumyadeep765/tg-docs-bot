"""Text helpers: identifiers, HTML escaping, and markdown-to-Telegram-HTML."""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin

# Telegram Bot API HTML supports a limited tag set.
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_WS = re.compile(r"[ \t]+\n")


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def split_ident(name: str) -> str:
    """Turn sendMessage / WebAppInitData / retry_after into spaced words."""
    s = _ACRONYM.sub(r"\1 \2", name)
    s = _CAMEL.sub(r"\1 \2", s)
    s = s.replace("_", " ").replace("-", " ").replace(":", " ").replace("%", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def slug_id(kind: str, *parts: str, limit: int = 64) -> str:
    """Build a Telegram inline result id (1–64 bytes), hashing if needed."""
    import hashlib

    raw = ":".join(p for p in (kind, *parts) if p)
    encoded = raw.encode("utf-8")
    if len(encoded) <= limit:
        return raw
    digest = hashlib.sha1(encoded).hexdigest()[:10]
    # Keep a readable prefix and a unique suffix.
    prefix = raw.encode("utf-8")[: limit - 11]
    # Avoid cutting mid-character.
    prefix_str = prefix.decode("utf-8", errors="ignore").rstrip(":")
    return f"{prefix_str}:{digest}"[:limit]


def first_sentence(text: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    # Prefer a short first sentence.
    for sep in (". ", ".\n", "? ", "! "):
        i = text.find(sep)
        if 20 <= i <= limit:
            return text[: i + 1]
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut + "…"


def md_to_telegram_html(text: str) -> str:
    """Convert Bot API markdown-ish descriptions to Telegram HTML."""
    if not text:
        return ""
    text = html.unescape(text)

    placeholders: list[str] = []

    def stash(html_fragment: str) -> str:
        placeholders.append(html_fragment)
        return f"\x00PH{len(placeholders) - 1}\x00"

    def repl_code(m: re.Match[str]) -> str:
        return stash(f"<code>{escape(m.group(1))}</code>")

    def repl_link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2).strip()
        return stash(f'<a href="{html.escape(url, quote=True)}">{escape(label)}</a>')

    text = _CODE.sub(repl_code, text)
    text = _LINK.sub(repl_link, text)

    def repl_bold(m: re.Match[str]) -> str:
        return stash(f"<b>{escape(m.group(1))}</b>")

    def repl_italic(m: re.Match[str]) -> str:
        return stash(f"<i>{escape(m.group(1))}</i>")

    text = _BOLD.sub(repl_bold, text)
    text = _ITALIC.sub(repl_italic, text)
    text = escape(text)

    def unstash(m: re.Match[str]) -> str:
        return placeholders[int(m.group(1))]

    # Unstash repeatedly (bold inside recovered text shouldn't occur, but links/code can nest).
    for _ in range(3):
        text = re.sub(r"\x00PH(\d+)\x00", unstash, text)
    return text


def html_fragment_to_telegram(
    fragment,  # bs4 element or list of elements
    base_url: str,
) -> str:
    """Convert a slice of core.telegram.org HTML to Telegram HTML."""
    from bs4 import Comment, NavigableString, Tag

    def convert(node) -> str:
        if node is None:
            return ""
        if isinstance(node, Comment):
            return ""
        if isinstance(node, NavigableString):
            return escape(str(node))
        if not isinstance(node, Tag):
            return ""
        name = node.name.lower()
        if name in {"script", "style", "video", "source", "svg"}:
            return ""
        if name == "i" and "anchor-icon" in (node.get("class") or []):
            return ""
        if name == "img":
            alt = node.get("alt") or ""
            return escape(alt)
        if name in {"br", "hr"}:
            return "\n"
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return f"\n<b>{inner(node)}</b>\n"
        if name in {"b", "strong"}:
            return f"<b>{inner(node)}</b>"
        if name in {"i", "em"}:
            return f"<i>{inner(node)}</i>"
        if name in {"u", "ins"}:
            return f"<u>{inner(node)}</u>"
        if name in {"s", "strike", "del"}:
            return f"<s>{inner(node)}</s>"
        if name == "code":
            return f"<code>{inner(node)}</code>"
        if name == "pre":
            return f"\n<pre>{inner(node)}</pre>\n"
        if name == "blockquote":
            return f"\n<blockquote>{inner(node)}</blockquote>\n"
        if name == "a":
            href = node.get("href") or ""
            if href.startswith("#") or href.startswith("/"):
                href = urljoin(base_url if base_url.endswith("/") else base_url + "/", href)
            label = inner(node).strip() or escape(href)
            if href:
                return f'<a href="{html.escape(href, quote=True)}">{label}</a>'
            return label
        if name in {"ul", "ol"}:
            items = []
            for li in node.find_all("li", recursive=False):
                items.append(f"• {convert(li).strip()}")
            return "\n" + "\n".join(items) + "\n"
        if name == "li":
            return inner(node)
        if name == "table":
            return "\n" + table_to_text(node) + "\n"
        if name in {"p", "div"}:
            return inner(node).strip() + "\n\n"
        if name == "mark":
            return inner(node)
        # token and other custom tags: keep children
        return inner(node)

    def inner(tag) -> str:
        return "".join(convert(c) for c in tag.children)

    def table_to_text(table) -> str:
        rows = []
        headers: list[str] = []
        thead = table.find("thead")
        if thead:
            headers = [
                " ".join(th.get_text(" ", strip=True).split())
                for th in thead.find_all(["th", "td"])
            ]
        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            # skip header row if it lived in tbody-less tables
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            if all(c.name == "th" for c in cells) and not headers:
                headers = [" ".join(c.get_text(" ", strip=True).split()) for c in cells]
                continue
            values = [convert(c).strip() for c in cells]
            values = [re.sub(r"\s+", " ", v) for v in values]
            if headers and len(headers) == len(values):
                # Field / Type / Description style tables become a compact list.
                lower = [h.lower() for h in headers]
                if "field" in lower or "name" in lower or "property" in lower:
                    name = values[lower.index("field") if "field" in lower else (lower.index("name") if "name" in lower else 0)]
                    typ = values[lower.index("type")] if "type" in lower else ""
                    desc = ""
                    if "description" in lower:
                        desc = values[lower.index("description")]
                    typ_bit = f" (<i>{typ}</i>)" if typ else ""
                    desc_bit = f" — {desc}" if desc else ""
                    rows.append(f"• {name}{typ_bit}{desc_bit}")
                    continue
                if "event" in lower or "method" in lower:
                    head = values[0]
                    rest = " — ".join(v for v in values[1:] if v)
                    rows.append(f"• <b>{head}</b>" + (f" — {rest}" if rest else ""))
                    continue
                joined = " | ".join(f"{h}: {v}" for h, v in zip(headers, values) if v)
                rows.append(f"• {joined}")
            else:
                rows.append("• " + " | ".join(v for v in values if v))
        return "\n".join(rows)

    if isinstance(fragment, (list, tuple)):
        out = "".join(convert(n) for n in fragment)
    else:
        out = convert(fragment)
    out = html.unescape(out) if False else out  # already escaped text nodes
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = _WS.sub("\n", out)
    return out.strip()


def plain_text_from_html_nodes(nodes) -> str:
    from bs4 import Comment, Tag

    chunks: list[str] = []
    for n in nodes:
        if isinstance(n, Comment):
            continue
        if isinstance(n, Tag):
            # Drop comments inside, then take visible text.
            for c in n.find_all(string=lambda s: isinstance(s, Comment)):
                c.extract()
            t = n.get_text(" ", strip=True)
            if t:
                chunks.append(t)
        else:
            t = str(n).strip()
            if t:
                chunks.append(t)
    return re.sub(r"\s+", " ", " ".join(chunks)).strip()
