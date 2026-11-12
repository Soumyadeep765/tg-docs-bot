"""Build Bot API 10.x InputRichMessage payloads (blocks) for private-chat docs.

Layouts use headings, paragraphs, lists, details / expandable blockquotes,
preformatted examples, dividers, footers, and button rows. Tables are not used.
"""

from __future__ import annotations

import html
import re
from typing import Any

from .formatters import (
    KIND_LABELS,
    all_fields_query,
    compact_field_names,
    _param_group_label,
    MAX_OPTIONAL_NAMES,
    example_snippet,
    _kind_signature,
)
from .typelinks import iter_type_tokens, type_href
from .permalinks import deep_link, encode_search_payload, field_deep_link
from .textutil import first_sentence

MAX_FIELDS = 32
FIELD_CHUNK = 16
MAX_RELATED = 40
MAX_DESC = 1600
MAX_FIELD_DESC = 240
MAX_LIST_ITEMS = 16

# Markdown-ish tokens used in Bot API descriptions.
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_HTML_BR = re.compile(r"<br\s*/?>", re.I)
_HTML_P = re.compile(r"</p\s*>", re.I)
_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+\n")


# ---------------------------------------------------------------------------
# Low-level InputRichText / InputRichBlock constructors
# ---------------------------------------------------------------------------

def bold(text: Any) -> dict[str, Any]:
    return {"type": "bold", "text": text}


def italic(text: Any) -> dict[str, Any]:
    return {"type": "italic", "text": text}


def code(text: str) -> dict[str, Any]:
    return {"type": "code", "text": text}


def url(text: Any, href: str) -> dict[str, Any]:
    return {"type": "url", "text": text, "url": href}


def rt_join(*parts: Any) -> Any:
    """Flatten InputRichText parts; a single plain string stays a string."""
    out: list[Any] = []
    for part in parts:
        if part is None or part == "":
            continue
        if isinstance(part, list):
            out.extend(part)
        else:
            out.append(part)
    if not out:
        return ""
    if len(out) == 1:
        return out[0]
    return out


def heading(text: Any, size: int = 2) -> dict[str, Any]:
    return {"type": "heading", "text": text, "size": size}


def paragraph(text: Any) -> dict[str, Any]:
    return {"type": "paragraph", "text": text}


def divider() -> dict[str, Any]:
    return {"type": "divider"}


def preformatted(text: str, language: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "pre", "text": text}
    if language:
        block["language"] = language
    return block


def footer(text: Any) -> dict[str, Any]:
    return {"type": "footer", "text": text}


def details(summary: Any, blocks: list[dict[str, Any]], *, is_open: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "details", "summary": summary, "blocks": blocks}
    if is_open:
        block["is_open"] = True
    return block


def expandable_blockquote(text: Any, credit: Any | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "expandable_blockquote", "text": text}
    if credit is not None:
        block["credit"] = credit
    return block


_BLOCK_TYPES = {
    "heading",
    "paragraph",
    "pre",
    "footer",
    "divider",
    "anchor",
    "list",
    "blockquote",
    "expandable_blockquote",
    "details",
    "buttons",
    "thinking",
    "pullquote",
}


def _is_block(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("type") in _BLOCK_TYPES


def list_block(items: list[Any]) -> dict[str, Any]:
    packed: list[dict[str, Any]] = []
    for item in items:
        # Already a list item object.
        if isinstance(item, dict) and "blocks" in item and item.get("type") not in _BLOCK_TYPES:
            packed.append(item)
        elif _is_block(item):
            packed.append({"blocks": [item]})
        elif isinstance(item, list) and item and all(_is_block(x) for x in item):
            packed.append({"blocks": item})
        else:
            # Strings, InputRichText objects, or arrays of rich text become one paragraph.
            packed.append({"blocks": [paragraph(item)]})
    return {"type": "list", "items": packed}


def buttons_row(buttons: list[dict[str, Any]], *, align: str | None = None) -> dict[str, Any]:
    if not 1 <= len(buttons) <= 8:
        raise ValueError("button rows must have 1-8 buttons")
    block: dict[str, Any] = {"type": "buttons", "buttons": buttons}
    if align:
        block["align"] = align
    return block


def url_button(text: str, href: str, *, style: str | None = None) -> dict[str, Any]:
    btn: dict[str, Any] = {"text": text, "url": href}
    if style:
        btn["style"] = style
    return btn


def callback_button(text: str, data: str, *, style: str | None = None) -> dict[str, Any]:
    btn: dict[str, Any] = {"text": text, "callback_data": data}
    if style:
        btn["style"] = style
    return btn


def switch_inline_button(
    text: str,
    query: str = "",
    *,
    current_chat: bool = True,
    style: str | None = None,
) -> dict[str, Any]:
    key = "switch_inline_query_current_chat" if current_chat else "switch_inline_query"
    btn: dict[str, Any] = {"text": text, key: query}
    if style:
        btn["style"] = style
    return btn


def rich_message(blocks: list[dict[str, Any]], *, skip_entity_detection: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {"blocks": [b for b in blocks if b]}
    if skip_entity_detection:
        payload["skip_entity_detection"] = True
    return payload


# ---------------------------------------------------------------------------
# Text conversion
# ---------------------------------------------------------------------------

def html_to_plain(text: str) -> str:
    if not text:
        return ""
    text = _HTML_BR.sub("\n", text)
    text = _HTML_P.sub("\n\n", text)
    text = _HTML_TAG.sub("", text)
    text = html.unescape(text)
    text = _WS.sub("\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return (cut or text[: limit - 1]).rstrip(".,;:") + "…"


def md_to_rich(text: str, *, limit: int | None = None) -> Any:
    """Turn markdown-ish / stripped-HTML description text into InputRichText."""
    text = html_to_plain(text or "")
    if limit is not None:
        text = _truncate(text, limit)
    if not text:
        return ""

    # Tokenize links and code first so later emphasis does not split them.
    placeholders: list[Any] = []

    def stash(value: Any) -> str:
        placeholders.append(value)
        return f"\x00{len(placeholders) - 1}\x00"

    def repl_link(m: re.Match[str]) -> str:
        return stash(url(m.group(1), m.group(2)))

    def repl_code(m: re.Match[str]) -> str:
        return stash(code(m.group(1)))

    work = _LINK.sub(repl_link, text)
    work = _CODE.sub(repl_code, work)
    work = _BOLD.sub(lambda m: stash(bold(m.group(1))), work)
    work = _ITALIC.sub(lambda m: stash(italic(m.group(1))), work)

    if "\x00" not in work:
        return work

    parts: list[Any] = []
    buf: list[str] = []
    i = 0
    while i < len(work):
        if work[i] == "\x00":
            end = work.find("\x00", i + 1)
            if end == -1:
                buf.append(work[i:])
                break
            if buf:
                parts.append("".join(buf))
                buf = []
            try:
                parts.append(placeholders[int(work[i + 1 : end])])
            except (ValueError, IndexError):
                parts.append(work[i : end + 1])
            i = end + 1
            continue
        buf.append(work[i])
        i += 1
    if buf:
        parts.append("".join(buf))
    return rt_join(*parts)


def _paragraphs_from_body(text: str, *, limit: int = MAX_DESC) -> list[dict[str, Any]]:
    plain = html_to_plain(text)
    if not plain:
        return []
    chunks = [c.strip() for c in re.split(r"\n{2,}", plain) if c.strip()]
    if not chunks:
        return []
    blocks: list[dict[str, Any]] = []
    used = 0
    for chunk in chunks:
        if used >= limit:
            break
        remain = limit - used
        piece = chunk if len(chunk) <= remain else _truncate(chunk, remain)
        blocks.append(paragraph(md_to_rich(piece)))
        used += len(piece)
    leftover = sum(len(c) for c in chunks) - used
    if leftover > 40:
        rest = _truncate(" ".join(chunks)[used:], 700)
        if rest:
            blocks.append(expandable_blockquote(md_to_rich(rest), credit="More"))
    return blocks


# ---------------------------------------------------------------------------
# Entry + menu payloads
# ---------------------------------------------------------------------------

KIND_PREFIX = {
    "method": "m: ",
    "type": "t: ",
    "error": "e: ",
    "feature": "f: ",
    "webapp": "w: ",
    "faq": "faq: ",
}

CATEGORY_TITLES = {
    "method": "Methods",
    "type": "Types",
    "error": "Errors",
    "feature": "Features",
    "webapp": "Mini Apps",
    "faq": "FAQ",
}

CATEGORY_INTROS = {
    "method": "HTTP methods on the Bot API.",
    "type": "Objects returned or accepted by methods.",
    "error": "HTTP errors returned by the Bot API.",
    "feature": "Platform features: keyboards, payments, Mini Apps, and more.",
    "webapp": "Telegram Mini Apps: launch, init data, theme, and JS APIs.",
    "faq": "Answers from the official Bot FAQ.",
}


def type_rich(type_str: str, bot_username: str | None = None) -> Any:
    """Rich text for a type string, with catalog types as deep links."""
    parts: list[Any] = []
    for kind, val in iter_type_tokens(type_str):
        if kind == "text":
            parts.append(italic(val) if (val or "").strip() else val)
            continue
        name = val.get("name") or ""
        href = type_href(val, bot_username)
        if href:
            parts.append(url(name, href))
        else:
            parts.append(italic(name))
    return rt_join(*parts) if parts else italic(type_str or "unknown")


def kind_signature_rich(entry: dict[str, Any], bot_username: str | None = None) -> Any:
    kind = entry.get("kind") or ""
    if kind == "fieldset":
        return italic(_kind_signature(entry))
    parts: list[Any] = [italic(KIND_LABELS.get(kind, kind))]
    if kind == "method" and entry.get("returns"):
        parts.extend([" · ", italic("returns "), type_rich(entry["returns"], bot_username)])
    if kind == "error" and entry.get("http_code") is not None:
        parts.extend([" · ", italic(f"HTTP {entry['http_code']}")])
    return rt_join(*parts)


def _subtitle(entry: dict[str, Any], bot_username: str | None = None) -> Any:
    return kind_signature_rich(entry, bot_username)


def _field_item(field: dict[str, Any], bot_username: str | None = None) -> Any:
    """Plain-text field line, kept for any remaining list uses."""
    name = field.get("name") or field.get("field_name") or ""
    typ = field.get("type") or field.get("field_type") or "unknown"
    req = "required" if field.get("required") else "optional"
    desc = md_to_rich(field.get("description") or "", limit=MAX_FIELD_DESC)
    bits: list[Any] = [code(name), " · ", type_rich(typ, bot_username), f" · {req}"]
    if desc:
        bits.extend([" — ", desc])
    return rt_join(*bits)


def _field_summary(field: dict[str, Any], bot_username: str | None = None) -> Any:
    name = field.get("name") or field.get("field_name") or ""
    typ = field.get("type") or field.get("field_type") or "unknown"
    req = "required" if field.get("required") else "optional"
    return rt_join(code(name), " · ", type_rich(typ, bot_username), f" · {req}")


def _field_details(field: dict[str, Any], bot_username: str | None = None) -> dict[str, Any]:
    """One collapsible details block for a single field or parameter."""
    inner = _paragraphs_from_body(field.get("description") or "", limit=MAX_FIELD_DESC)
    if not inner:
        inner = [paragraph(italic("No description."))]
    return details(_field_summary(field, bot_username), inner)


def _grouped_field_details(
    fields: list[dict[str, Any]],
    bot_username: str | None = None,
    *,
    heading_label: str = "Fields",
    req_n: int | None = None,
) -> list[dict[str, Any]]:
    """Field details, nested in one or more outer collapsed groups when needed."""
    if not fields:
        return []
    n = len(fields)
    if n == 1:
        return [_field_details(fields[0], bot_username)]
    if req_n is None:
        req_n = sum(1 for f in fields if f.get("required"))
    chunk = FIELD_CHUNK
    groups: list[dict[str, Any]] = []
    for i in range(0, n, chunk):
        part = fields[i : i + chunk]
        inner = [_field_details(f, bot_username) for f in part]
        if n <= chunk:
            meta = f"{n}" + (f", {req_n} required" if req_n else "")
            label = f"{heading_label} ({meta})"
        else:
            label = f"{heading_label} {i + 1}–{i + len(part)} of {n}"
        groups.append(details(label, inner))
    return groups


def _fields_block(entry: dict[str, Any], bot_username: str | None = None) -> list[dict[str, Any]]:
    fields = entry.get("fields") or []
    if not fields:
        return []
    kind = entry.get("kind") or ""
    heading_label = "Parameters" if kind == "method" else "Fields"
    return _grouped_field_details(fields, bot_username, heading_label=heading_label)


def _fieldset_blocks(entry: dict[str, Any], bot_username: str | None = None) -> list[dict[str, Any]]:
    fields = entry.get("selected_fields") or []
    if not fields:
        return []
    role = entry.get("role") or "fields"
    heading_label = "Parameters" if role == "parameters" else "Fields"
    return _grouped_field_details(fields, bot_username, heading_label=heading_label)


def _field_name_rich(parent_name: str, field: dict[str, Any], bot_username: str | None = None) -> Any:
    name = field.get("name") or ""
    href = field_deep_link(parent_name, name, bot_username)
    if href:
        return url(name, href)
    return code(name)


def _optional_names_paragraph(
    parent_name: str,
    fields: list[dict[str, Any]],
    bot_username: str | None,
    *,
    prefix: str = "",
) -> dict[str, Any] | None:
    named = [f for f in fields if f.get("name")]
    if not named:
        return None
    shown = named[:MAX_OPTIONAL_NAMES]
    bits: list[Any] = [prefix] if prefix else []
    for i, field in enumerate(shown):
        if i:
            bits.append(", ")
        bits.append(_field_name_rich(parent_name, field, bot_username))
    if len(named) > len(shown):
        bits.append(", …")
    return paragraph(rt_join(*bits))


def _compact_fields_blocks(entry: dict[str, Any], bot_username: str | None = None) -> list[dict[str, Any]]:
    fields = [f for f in (entry.get("fields") or []) if f.get("name")]
    name = entry.get("name") or ""
    kind = entry.get("kind") or ""
    blocks: list[dict[str, Any]] = []
    if fields:
        required = [f for f in fields if f.get("required")]
        optional = [f for f in fields if not f.get("required")]
        label = _param_group_label(kind, len(fields), len(required))
        inner: list[dict[str, Any]] = [_field_details(f, bot_username) for f in required]
        if optional:
            prefix = "Optional: " if required else ""
            opt = _optional_names_paragraph(name, optional, bot_username, prefix=prefix)
            if opt:
                inner.append(opt)
        if inner:
            blocks.append(details(label, inner))
    if name:
        blocks.append(
            paragraph(rt_join("Type ", code(f"{name}.*"), " to see every field."))
        )
    return blocks


def _error_extra_blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    params = entry.get("error_parameters") or []
    if params:
        blocks.append(
            details(
                "Response parameters",
                [list_block([code(str(p)) for p in params])],
            )
        )
    mt = entry.get("mtproto_codes") or []
    if mt:
        blocks.append(
            details(
                "MTProto codes",
                [list_block([code(str(c)) for c in mt])],
            )
        )
    methods = entry.get("related_methods") or []
    if methods:
        if len(methods) >= 100:
            blocks.append(
                details(
                    f"Related methods ({len(methods)})",
                    [paragraph("Applies to most Bot API methods.")],
                )
            )
        else:
            shown = methods[:MAX_RELATED]
            inner: list[dict[str, Any]] = [list_block([code(m) for m in shown])]
            if len(methods) > MAX_RELATED:
                inner.append(paragraph(f"… and {len(methods) - MAX_RELATED} more."))
            blocks.append(details(f"Related methods ({len(methods)})", inner))
    return blocks


def _example_block(entry: dict[str, Any]) -> dict[str, Any] | None:
    example = example_snippet(entry)
    if not example:
        return None
    lang, src = example
    return details(
        "Example",
        [preformatted(src, language=lang)],
    )


def _footer_links(
    *,
    docs_url: str | None,
    permalink: str | None,
    bot_username: str | None,
) -> dict[str, Any] | None:
    bits: list[Any] = []
    if docs_url:
        bits.append(url("Official docs", docs_url))
    if permalink and bot_username:
        if bits:
            bits.append(" · ")
        bits.append(url("Permalink", deep_link(bot_username, permalink)))
    elif permalink:
        if bits:
            bits.append(" · ")
        bits.append(rt_join("start=", code(permalink)))
    if not bits:
        return None
    return footer(rt_join(*bits))


def _chunk(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def entry_action_buttons(
    entry: dict[str, Any],
    *,
    bot_username: str | None = None,
    permalink: str | None = None,
    query: str = "",
    back: str | None = None,
    include_nav: bool = True,
) -> list[dict[str, Any]]:
    """At most two plain-label buttons per row."""
    rows: list[dict[str, Any]] = []
    top: list[dict[str, Any]] = []
    href = entry.get("url") or ""
    if href:
        top.append(url_button("Open docs", href, style="primary"))
    if permalink and bot_username:
        top.append(url_button("Permalink", deep_link(bot_username, permalink), style="link"))
    if top:
        rows.append(buttons_row(top[:2]))

    extra: list[dict[str, Any]] = []
    parent_payload = entry.get("parent_payload")
    parent_name = entry.get("parent_name") or "Parent"
    if len(parent_name) > 24:
        parent_name = parent_name[:23] + "…"
    if entry.get("kind") in {"field", "fieldset"} and parent_payload and bot_username:
        extra.append(url_button(parent_name, deep_link(bot_username, parent_payload)))
    if entry.get("kind") == "field" and entry.get("chain_query"):
        extra.append(switch_inline_button("Fields", entry["chain_query"], current_chat=True))
    query_all = all_fields_query(entry)
    if entry.get("kind") in {"method", "type"} and query_all and entry.get("fields"):
        extra.append(switch_inline_button("All fields", query_all, current_chat=True))
    if extra:
        rows.append(buttons_row(extra[:2]))

    if include_nav:
        nav: list[dict[str, Any]] = []
        if back:
            nav.append(callback_button("Back", f"menu:{back}"))
        nav.append(callback_button("Home", "menu:home"))
        rows.append(buttons_row(nav[:2]))
    return rows


def field_rich_message(
    entry: dict[str, Any],
    *,
    bot_username: str | None = None,
    permalink: str | None = None,
    query: str = "",
    back: str | None = None,
    include_nav: bool = True,
) -> dict[str, Any]:
    title = entry.get("title") or entry.get("name") or "Field"
    role = entry.get("role") or "field"
    typ = entry.get("field_type") or "unknown"
    req = "required" if entry.get("required") else "optional"
    container = entry.get("container_name") or entry.get("parent_name") or ""
    sig_parts: list[Any] = [italic(role)]
    if container:
        sig_parts.extend([" of ", code(container)])
    sig_parts.extend([" · ", italic(req)])
    blocks: list[dict[str, Any]] = [
        heading(title, size=2),
        paragraph(rt_join(*sig_parts)),
        _field_details(
            {
                "name": entry.get("field_name") or (title.rsplit(".", 1)[-1] if title else ""),
                "type": typ,
                "required": entry.get("required"),
                "description": entry.get("description") or "",
            },
            bot_username,
        ),
    ]

    blocks.extend(
        entry_action_buttons(
            entry,
            bot_username=bot_username,
            permalink=permalink,
            query=query or (entry.get("name") or ""),
            back=back,
            include_nav=include_nav,
        )
    )
    return rich_message(blocks)


def fieldset_rich_message(
    entry: dict[str, Any],
    *,
    bot_username: str | None = None,
    permalink: str | None = None,
    query: str = "",
    back: str | None = None,
    include_nav: bool = True,
) -> dict[str, Any]:
    title = entry.get("title") or entry.get("name") or "Fields"
    blocks: list[dict[str, Any]] = [
        heading(title, size=2),
        paragraph(_subtitle(entry, bot_username)),
    ]
    blocks.extend(_fieldset_blocks(entry, bot_username))
    blocks.extend(
        entry_action_buttons(
            entry,
            bot_username=bot_username,
            permalink=permalink,
            query=query or (entry.get("name") or ""),
            back=back,
            include_nav=include_nav,
        )
    )
    return rich_message(blocks)


def html_rich_message(html: str) -> dict[str, Any]:
    """InputRichMessage using the html branch (for composed text)."""
    return {"html": html or ""}


def entry_rich_message(
    entry: dict[str, Any],
    *,
    bot_username: str | None = None,
    permalink: str | None = None,
    query: str = "",
    back: str | None = None,
    include_nav: bool = True,
) -> dict[str, Any]:
    if entry.get("kind") == "field":
        return field_rich_message(
            entry,
            bot_username=bot_username,
            permalink=permalink,
            query=query,
            back=back,
            include_nav=include_nav,
        )
    if entry.get("kind") == "fieldset":
        return fieldset_rich_message(
            entry,
            bot_username=bot_username,
            permalink=permalink,
            query=query,
            back=back,
            include_nav=include_nav,
        )
    title = entry.get("title") or entry.get("name") or "Untitled"
    kind = entry.get("kind") or ""
    blocks: list[dict[str, Any]] = [
        heading(title, size=2),
        paragraph(_subtitle(entry, bot_username)),
    ]
    body_src = entry.get("html") or entry.get("description") or entry.get("short") or ""
    body_blocks = _paragraphs_from_body(body_src)
    if body_blocks:
        blocks.extend(body_blocks)

    subtypes = entry.get("subtypes") or []
    if subtypes:
        blocks.append(
            details(
                "Subtypes",
                [paragraph(rt_join(*[x for s in subtypes for x in (code(s), ", ")][:-1]))],
            )
        )

    if kind in {"method", "type"}:
        blocks.extend(_compact_fields_blocks(entry, bot_username))
    else:
        blocks.extend(_fields_block(entry, bot_username))
    if kind == "error":
        extras = _error_extra_blocks(entry)
        if extras:
            blocks.extend(extras)

    if kind not in {"method", "type"}:
        example = _example_block(entry)
        if example:
            blocks.append(example)

    blocks.extend(
        entry_action_buttons(
            entry,
            bot_username=bot_username,
            permalink=permalink,
            query=query,
            back=back,
            include_nav=include_nav,
        )
    )
    return rich_message(blocks)


def home_rich_message(
    bot_username: str | None = None,
    *,
    count: int = 0,
    version: str | None = None,
) -> dict[str, Any]:
    mention = f"@{bot_username}" if bot_username else "@your_bot"
    blocks: list[dict[str, Any]] = [
        heading("Bot API reference", size=2),
        paragraph(rt_join("Type ", code(f"{mention} sendMessage"), " in any chat.")),
        buttons_row(
            [
                switch_inline_button("Search", "", current_chat=True, style="primary"),
                switch_inline_button("Other chat", "", current_chat=False),
            ]
        ),
        buttons_row(
            [
                callback_button("Methods", "menu:methods"),
                callback_button("Types", "menu:types"),
            ]
        ),
        buttons_row(
            [
                callback_button("Errors", "menu:errors"),
                callback_button("Features", "menu:features"),
            ]
        ),
        buttons_row(
            [
                callback_button("Mini Apps", "menu:webapps"),
                callback_button("FAQ", "menu:faq"),
            ]
        ),
        buttons_row(
            [
                callback_button("Help", "menu:help"),
                callback_button("About", "menu:about"),
            ]
        ),
    ]
    return rich_message(blocks)


def howto_rich_message(bot_username: str | None = None) -> dict[str, Any]:
    mention = f"@{bot_username}" if bot_username else "@your_bot"
    sample = deep_link(bot_username, "m_sendMessage") if bot_username else "https://t.me/your_bot?start=m_sendMessage"
    blocks: list[dict[str, Any]] = [
        heading("How to use", size=2),
        paragraph(
            rt_join(
                "Type ",
                code(f"{mention} sendMessage"),
                " in any chat. Results update as you type; scroll for more pages.",
            )
        ),
        paragraph(
            rt_join(
                "Filter with ",
                code("m:"),
                " ",
                code("t:"),
                " ",
                code("e:"),
                " ",
                code("f:"),
                " ",
                code("w:"),
                " ",
                code("faq:"),
                ". Open a field with ",
                code("sendMessage.chat_id"),
                " or ",
                code("Message.chat.id"),
                "; a trailing ",
                code("."),
                " lists every field.",
            )
        ),
        paragraph(rt_join("Each result includes a Permalink, for example ", url(sample, sample), ".")),
        buttons_row(
            [
                switch_inline_button("Search", "", current_chat=True, style="primary"),
                callback_button("Home", "menu:home"),
            ]
        ),
    ]
    return rich_message(blocks)


def about_rich_message(
    bot_username: str | None = None,
    *,
    count: int = 0,
    version: str | None = None,
    built_at: str | None = None,
    sources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mention = f"@{bot_username}" if bot_username else "@your_bot"
    ver = f" · Bot API {version}" if version else ""
    blocks: list[dict[str, Any]] = [
        heading(f"About {mention}", size=2),
        paragraph(
            "Inline reference for the Bot API: methods, types, errors, features, Mini Apps, and the FAQ."
        ),
        paragraph(rt_join(bold(f"{count:,}"), f" entries{ver}.")),
    ]
    source_blocks: list[dict[str, Any]] = []
    if built_at:
        source_blocks.append(paragraph(rt_join("Catalog built ", code(built_at), ". Refreshes about once a day.")))
    if sources:
        labels = {
            "api": "Bot API schema",
            "errors": "API errors",
            "fact": "Intro / facts",
            "feature": "Features",
            "webapp": "Mini Apps",
            "faq": "FAQ",
        }
        items: list[Any] = []
        for key, meta in sources.items():
            label = labels.get(key, key)
            href = (meta or {}).get("url") if isinstance(meta, dict) else None
            if href and str(href).startswith("http"):
                items.append(url(label, str(href)))
            else:
                items.append(label)
        if items:
            source_blocks.append(list_block(items))
    if source_blocks:
        blocks.append(details("Sources", source_blocks))
    blocks.append(
        buttons_row(
            [
                switch_inline_button("Search", "", current_chat=True, style="primary"),
                callback_button("Home", "menu:home"),
            ]
        )
    )
    return rich_message(blocks)


def category_rich_message(
    kind: str,
    entries: list[dict[str, Any]],
    *,
    total: int | None = None,
    permalinks: list[str] | None = None,
) -> dict[str, Any]:
    title = CATEGORY_TITLES.get(kind, kind)
    intro = CATEGORY_INTROS.get(kind, "")
    blocks: list[dict[str, Any]] = [
        heading(title, size=2),
        paragraph(intro),
    ]
    if total is not None:
        blocks.append(paragraph(f"{total:,} in this category."))

    items: list[Any] = []
    for entry in entries:
        name = entry.get("title") or entry.get("name") or ""
        short = first_sentence(entry.get("short") or entry.get("description") or "", limit=80)
        items.append(rt_join(code(name), f" — {short}") if short else code(name))
    if items:
        blocks.append(list_block(items))

    nav_buttons: list[dict[str, Any]] = []
    for entry, payload in zip(entries, permalinks or []):
        if not payload:
            continue
        label = entry.get("name") or entry.get("title") or payload
        if len(label) > 30:
            label = label[:29] + "…"
        nav_buttons.append(callback_button(label, f"d:{payload}"))
    for row in _chunk(nav_buttons, 2):
        blocks.append(buttons_row(row))

    prefix = KIND_PREFIX.get(kind, "")
    blocks.append(
        buttons_row(
            [
                switch_inline_button("Search", prefix, current_chat=True, style="primary"),
                callback_button("Home", "menu:home"),
            ]
        )
    )
    return rich_message(blocks)


def search_results_rich_message(
    query: str,
    hits: list[tuple[float, dict[str, Any]]],
    *,
    total: int | None = None,
    bot_username: str | None = None,
    permalinks: list[str] | None = None,
    share_payload: str | None = None,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        heading("Results", size=2),
        paragraph(rt_join("Query ", code(query))),
    ]
    if not hits:
        blocks.append(paragraph("No matches. Try a shorter query, or search inline."))
    else:
        items: list[Any] = []
        for _score, entry in hits:
            kind = KIND_LABELS.get(entry.get("kind") or "", entry.get("kind") or "")
            name = entry.get("title") or entry.get("name") or ""
            short = first_sentence(entry.get("short") or "", limit=80)
            line = rt_join(code(name), " · ", italic(kind))
            if short:
                items.append([paragraph(line), paragraph(short)])
            else:
                items.append(line)
        blocks.append(list_block(items))
        if total is not None and total > len(hits):
            blocks.append(paragraph(italic(f"{total:,} matches in total.")))

    result_btns: list[dict[str, Any]] = []
    for (_score, entry), payload in zip(hits, permalinks or []):
        if not payload:
            continue
        label = entry.get("name") or entry.get("title") or payload
        if len(label) > 28:
            label = label[:27] + "…"
        result_btns.append(callback_button(label, f"d:{payload}"))
    for row in _chunk(result_btns, 2):
        blocks.append(buttons_row(row))

    share_row: list[dict[str, Any]] = [
        switch_inline_button("Search", query, current_chat=True, style="primary")
    ]
    target_payload = share_payload
    if target_payload is None and query:
        try:
            target_payload = encode_search_payload(query)
        except Exception:
            target_payload = None
    if target_payload and bot_username:
        share_row.append(url_button("Share", deep_link(bot_username, target_payload), style="link"))
    else:
        share_row.append(callback_button("Home", "menu:home"))
        blocks.append(buttons_row(share_row))
        return rich_message(blocks)
    blocks.append(buttons_row(share_row))
    blocks.append(buttons_row([callback_button("Home", "menu:home")]))
    return rich_message(blocks)


def unknown_rich_message(_payload: str | None = None) -> dict[str, Any]:
    return rich_message(
        [
            heading("Not found", size=2),
            paragraph("This link may be outdated, or the catalog was rebuilt."),
            buttons_row(
                [
                    switch_inline_button("Search", "", current_chat=True, style="primary"),
                    callback_button("Home", "menu:home"),
                ]
            ),
        ]
    )


def help_inline_rich_message(bot_username: str | None = None) -> dict[str, Any]:
    mention = f"@{bot_username}" if bot_username else "@your_bot"
    return rich_message(
        [
            heading("Bot API reference", size=2),
            paragraph(
                rt_join(
                    "Type ",
                    code(f"{mention} sendMessage"),
                    " in any chat. Results update as you type; scroll for more pages.",
                )
            ),
            paragraph(
                rt_join(
                    "Filter with ",
                    code("m:"),
                    " ",
                    code("t:"),
                    " ",
                    code("e:"),
                    " ",
                    code("f:"),
                    " ",
                    code("w:"),
                    " ",
                    code("faq:"),
                    ". Open a field with ",
                    code("sendMessage.chat_id"),
                    " or ",
                    code("Message.chat.id"),
                    ".",
                )
            ),
            buttons_row([switch_inline_button("Search", "", current_chat=True, style="primary")]),
        ]
    )


def inline_article_payload(
    *,
    result_id: str,
    title: str,
    description: str,
    rich: dict[str, Any],
) -> dict[str, Any]:
    """Raw InlineQueryResultArticle using InputRichMessageContent."""
    return {
        "type": "article",
        "id": result_id,
        "title": title,
        "description": description,
        "input_message_content": {"rich_message": rich},
    }
