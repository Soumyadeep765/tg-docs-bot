"""Format index entries as Telegram HTML message contents and short descriptions."""

from __future__ import annotations

import html
import json
import re
from typing import Any

from .textutil import escape, first_sentence, md_to_telegram_html
from .permalinks import field_deep_link
from .typelinks import type_html

MAX_MESSAGE = 4096
SAFE_LIMIT = 4000

KIND_LABELS = {
    "method": "method",
    "type": "type",
    "error": "error",
    "feature": "feature",
    "webapp": "Mini App / Web App",
    "faq": "FAQ",
    "fact": "bot platform",
    "field": "parameter",
    "fieldset": "fields",
}

KIND_EMOJI = {
    "method": "📘",
    "type": "🧩",
    "error": "⚠️",
    "feature": "✨",
    "webapp": "🌐",
    "faq": "❓",
    "fact": "💡",
    "field": "🏷️",
}


def result_title(entry: dict[str, Any], limit: int = 96) -> str:
    title = entry.get("title") or entry.get("name") or "Untitled"
    if len(title) <= limit:
        return title
    cut = title[: limit - 1]
    if " " in cut[4:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ·,;:-") + "…"


def result_description(entry: dict[str, Any], limit: int = 100) -> str:
    """One-line inline subtitle: kind or type, then a short summary."""
    if entry.get("kind") == "field":
        typ = entry.get("field_type") or ""
        req = "required" if entry.get("required") else "optional"
        short = first_sentence(entry.get("description") or "", limit=limit)
        prefix = ", ".join(p for p in (typ, req) if p)
        text = f"{prefix} — {short}" if short else prefix
    elif entry.get("kind") == "fieldset":
        text = entry.get("short") or _kind_signature(entry)
    else:
        kind = KIND_LABELS.get(entry.get("kind") or "", entry.get("kind") or "")
        short = entry.get("short") or first_sentence(entry.get("description") or "", limit=limit)
        text = f"{kind} — {short}" if short else kind
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)
    return (cut[0] if cut else text[: limit - 1]).rstrip(" ·,;:—-") + "…"


def _field_summary_html(field: dict[str, Any], bot_username: str | None = None) -> str:
    name = escape(field.get("name") or field.get("field_name") or "")
    typ = type_html(
        field.get("type") or field.get("field_type") or "unknown",
        bot_username=bot_username,
        italic=True,
    )
    req = "required" if field.get("required") else "optional"
    return f"<code>{name}</code> · {typ} · {req}"


def _field_desc_html(field: dict[str, Any], *, max_len: int = 280) -> str:
    desc = md_to_telegram_html(field.get("description") or "").strip()
    if desc and len(desc) > max_len:
        desc = sanitize_telegram_html(desc, max_len=max_len)
    return desc


def _field_line(field: dict[str, Any], bot_username: str | None = None) -> str:
    summary = _field_summary_html(field, bot_username)
    desc = _field_desc_html(field, max_len=280)
    return f"{summary} — {desc}" if desc else summary


def _field_blockquote(field: dict[str, Any], bot_username: str | None = None, *, max_desc: int = 280) -> str:
    summary = _field_summary_html(field, bot_username)
    desc = _field_desc_html(field, max_len=max_desc)
    inner = f"{summary}\n{desc}" if desc else summary
    return f"<blockquote expandable>{inner}</blockquote>"


def _fields_expandable_html(
    fields: list[dict[str, Any]],
    bot_username: str | None = None,
    *,
    heading: str | None = None,
    budget: int | None = None,
) -> str:
    """Per-field expandable quotes when they fit; otherwise grouped quotes."""
    if not fields:
        return ""
    limit = budget if budget is not None else SAFE_LIMIT
    # A single field is shown directly, without an outer "Parameters (1)" heading.
    if len(fields) == 1:
        heading = None
    heading_html = f"<b>{escape(heading)}</b>\n" if heading else ""

    def pack(quotes: list[str]) -> str:
        body = "\n".join(quotes)
        return heading_html + body if heading_html else body

    for desc_len in (280, 160, 100):
        quotes = [_field_blockquote(f, bot_username, max_desc=desc_len) for f in fields]
        text = pack(quotes)
        if len(text) <= limit:
            return text

    chunk = 8
    groups: list[str] = []
    n = len(fields)
    for i in range(0, n, chunk):
        part = fields[i : i + chunk]
        label = f"{heading or 'Fields'} {i + 1}–{i + len(part)} of {n}" if n > chunk else (heading or "Fields")
        inner = "\n".join(_field_line(f, bot_username) for f in part)
        groups.append(f"<blockquote expandable><b>{escape(label)}</b>\n{inner}</blockquote>")
        trial = pack(groups)
        if len(trial) > limit:
            groups.pop()
            break
    return pack(groups) if groups else pack([_field_line(f, bot_username) for f in fields[:8]])


def _placeholder(field: dict[str, Any]) -> Any:
    typ = (field.get("type") or "").lower()
    name = field.get("name") or "value"
    if name in {"chat_id", "from_chat_id", "user_id", "sender_chat_id"}:
        return 123456
    if name in {"text", "caption", "question", "title", "description", "first_name"}:
        return "Hello"
    if name in {"message_id", "offset", "limit", "timeout", "duration"}:
        return 1
    if name == "parse_mode":
        return "HTML"
    if name == "currency":
        return "XTR"
    if "bool" in typ:
        return True
    if "int" in typ:
        return 0
    if "array" in typ:
        return []
    if typ == "string":
        return "…"
    return {}


def example_snippet(entry: dict[str, Any]) -> tuple[str, str] | None:
    """Return (language, code) for a compact example, or None."""
    kind = entry.get("kind") or ""
    name = entry.get("name") or ""
    fields = entry.get("fields") or []
    required = [f for f in fields if f.get("required")] or fields[:4]
    if kind == "method" and name:
        obj = {field["name"]: _placeholder(field) for field in required[:8]}
        code = f"// POST /{name}\n" + json.dumps(obj, indent=2, ensure_ascii=False)
        return "json", code
    if kind == "type" and name and required:
        obj = {field["name"]: _placeholder(field) for field in required[:8]}
        return "json", json.dumps(obj, indent=2, ensure_ascii=False)
    if kind == "error":
        body: dict[str, Any] = {"ok": False}
        if entry.get("http_code") is not None:
            body["error_code"] = entry["http_code"]
        body["description"] = entry.get("error_message") or entry.get("title") or "Bad Request"
        params = entry.get("error_parameters") or []
        if params:
            body["parameters"] = {p: (12 if p == "retry_after" else 123) for p in params}
        return "json", json.dumps(body, indent=2, ensure_ascii=False)
    return None


def method_signature(entry: dict[str, Any]) -> str | None:
    """Compact method signature: name(required[, optional, …])."""
    if (entry.get("kind") or "") != "method":
        return None
    name = entry.get("name") or ""
    if not name:
        return None
    fields = entry.get("fields") or []
    req = [f.get("name") for f in fields if f.get("required") and f.get("name")]
    opt = [f.get("name") for f in fields if not f.get("required") and f.get("name")]
    if opt:
        opt_bit = f"[{opt[0]}]" if len(opt) == 1 else f"[{opt[0]}, …]"
        inner = ", ".join(req + [opt_bit]) if req else opt_bit
    else:
        inner = ", ".join(req)
    return f"{name}({inner})"


def _kind_signature(entry: dict[str, Any]) -> str:
    kind = entry.get("kind") or ""
    label = KIND_LABELS.get(kind, kind)
    bits = [label]
    if kind == "method" and entry.get("returns"):
        bits.append(f"returns {entry['returns']}")
    if kind == "error" and entry.get("http_code") is not None:
        bits.append(f"HTTP {entry['http_code']}")
    if kind == "fieldset":
        parent = entry.get("container_name") or entry.get("parent_name") or ""
        n = len(entry.get("selected_fields") or [])
        role = entry.get("role") or "fields"
        if entry.get("mode") == "all":
            return f"all {role} of {parent} · {n}"
        return f"{n} {role} of {parent}"
    return " · ".join(str(b) for b in bits if b)


def kind_signature_html(entry: dict[str, Any], bot_username: str | None = None) -> str:
    """Italic kind line, with return types deep-linked when they exist in the index."""
    kind = entry.get("kind") or ""
    if kind == "fieldset":
        return f"<i>{escape(_kind_signature(entry))}</i>"
    label = KIND_LABELS.get(kind, kind)
    bits: list[str] = [escape(label)]
    if kind == "method" and entry.get("returns"):
        bits.append("returns " + type_html(entry["returns"], bot_username=bot_username, italic=False))
    if kind == "error" and entry.get("http_code") is not None:
        bits.append(escape(f"HTTP {entry['http_code']}"))
    return "<i>" + " · ".join(b for b in bits if b) + "</i>"


def format_field_message(
    entry: dict[str, Any],
    *,
    bot_username: str | None = None,
    permalink: str | None = None,
) -> str:
    """HTML fallback for a single parameter / field path."""
    title = escape(entry.get("title") or entry.get("name") or "")
    role = entry.get("role") or "field"
    typ = entry.get("field_type") or "unknown"
    req = "required" if entry.get("required") else "optional"
    container = entry.get("container_name") or entry.get("parent_name") or ""
    parent = entry.get("parent_name") or ""
    header = f"<b>{title}</b>\n<i>{escape(role)}"
    if container:
        header += f" of <code>{escape(container)}</code>"
    header += f" · {escape(req)}</i>"

    field_quote = _field_blockquote(
        {
            "name": entry.get("field_name") or (entry.get("name") or title).rsplit(".", 1)[-1],
            "type": typ,
            "required": entry.get("required"),
            "description": entry.get("description") or "",
        },
        bot_username,
    )
    parts = [header, field_quote]
    text = "\n\n".join(p for p in parts if p)
    return sanitize_telegram_html(text, max_len=MAX_MESSAGE)


def all_fields_query(entry: dict[str, Any]) -> str:
    name = (entry.get("name") or "").strip()
    return f"{name}.*" if name else ""


def compact_field_names(fields: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """A short required list, or the first few field names."""
    required = [f for f in fields if f.get("required") and f.get("name")]
    if required:
        return "Required", required
    named = [f for f in fields if f.get("name")]
    return "Fields", named[:6]


def format_fieldset_message(
    entry: dict[str, Any],
    *,
    bot_username: str | None = None,
    permalink: str | None = None,
) -> str:
    title = escape(entry.get("title") or entry.get("name") or "")
    header = f"<b>{title}</b>\n{kind_signature_html(entry, bot_username)}"
    fields = entry.get("selected_fields") or []
    role = entry.get("role") or "fields"
    heading = "Parameters" if role == "parameters" else "Fields"
    body = _fields_expandable_html(fields, bot_username, heading=heading)
    def join(bs: list[str]) -> str:
        return "\n\n".join(b for b in bs if b and b.strip())

    budget = SAFE_LIMIT - len(header) - 2
    body = _fields_expandable_html(fields, bot_username, heading=heading, budget=max(200, budget))
    return sanitize_telegram_html(join([header, body]), max_len=MAX_MESSAGE)


def _param_group_label(kind: str, total: int, required: int) -> str:
    heading = "Parameters" if kind == "method" else "Fields"
    if required:
        return f"{heading} ({total}, {required} required)"
    return f"{heading} ({total}, none required)"


def _field_name_html(parent_name: str, field: dict[str, Any], bot_username: str | None) -> str:
    name = field.get("name") or ""
    href = field_deep_link(parent_name, name, bot_username)
    if href:
        return f'<a href="{html.escape(href, quote=True)}">{escape(name)}</a>'
    return f"<code>{escape(name)}</code>"


MAX_OPTIONAL_NAMES = 16


def _optional_names_html(
    parent_name: str,
    fields: list[dict[str, Any]],
    bot_username: str | None,
    *,
    prefix: str = "",
    limit: int = MAX_OPTIONAL_NAMES,
) -> str:
    named = [f for f in fields if f.get("name")]
    if not named:
        return ""
    shown = named[:limit]
    names = ", ".join(_field_name_html(parent_name, f, bot_username) for f in shown)
    if len(named) > len(shown):
        names += ", …"
    return f"{escape(prefix)}{names}" if names else ""


def _compact_fields_html(entry: dict[str, Any], bot_username: str | None = None) -> str:
    fields = [f for f in (entry.get("fields") or []) if f.get("name")]
    name = entry.get("name") or ""
    kind = entry.get("kind") or ""
    hint = f"Type <code>{escape(name)}.*</code> to see every field." if name else ""
    if not fields:
        return hint
    required = [f for f in fields if f.get("required")]
    optional = [f for f in fields if not f.get("required")]
    label = _param_group_label(kind, len(fields), len(required))
    parts: list[str] = []
    for field in required:
        parts.append(_field_blockquote(field, bot_username))
    if optional:
        prefix = "Optional: " if required else ""
        opt = _optional_names_html(name, optional, bot_username, prefix=prefix)
        if opt:
            parts.append(opt)
    inner = "\n".join(parts)
    block = f"<blockquote expandable><b>{escape(label)}</b>\n{inner}</blockquote>" if inner else ""
    if hint:
        block = (block + "\n\n" + hint) if block else hint
    return block


def format_message(
    entry: dict[str, Any],
    *,
    bot_username: str | None = None,
    permalink: str | None = None,
) -> str:
    if entry.get("kind") == "field":
        return format_field_message(entry, bot_username=bot_username, permalink=permalink)
    if entry.get("kind") == "fieldset":
        return format_fieldset_message(entry, bot_username=bot_username, permalink=permalink)
    kind = entry.get("kind") or ""
    title = escape(entry.get("title") or entry.get("name") or "")
    url = entry.get("url") or ""

    header = f"<b>{title}</b>\n{kind_signature_html(entry, bot_username)}"
    if kind == "error" and entry.get("error_message") and entry["error_message"] != entry.get("title"):
        header += f"\n<code>{escape(entry['error_message'])}</code>"

    if entry.get("html"):
        body = (entry["html"] or "").strip()
    else:
        body = md_to_telegram_html(entry.get("description") or "").strip()

    details_chunks: list[str] = []
    subtypes = entry.get("subtypes") or []
    if subtypes:
        details_chunks.append(
            "<b>Subtypes</b>\n" + ", ".join(f"<code>{escape(s)}</code>" for s in subtypes)
        )

    fields = entry.get("fields") or []
    if kind in {"method", "type"}:
        preview = _compact_fields_html(entry, bot_username)
        if preview:
            details_chunks.append(preview)
    elif fields:
        heading = "Parameters" if kind == "method" else "Fields"
        req_n = sum(1 for f in fields if f.get("required"))
        meta = f"{len(fields)}" + (f", {req_n} required" if req_n else "")
        details_chunks.append(
            f"<b>{heading}</b> ({meta})\n" + "\n".join(_field_line(f, bot_username) for f in fields)
        )

    if kind == "error":
        params = entry.get("error_parameters") or []
        if params:
            details_chunks.append(
                "<b>Response parameters</b>\n" + ", ".join(f"<code>{escape(p)}</code>" for p in params)
            )
        mt = entry.get("mtproto_codes") or []
        if mt:
            details_chunks.append(
                "<b>MTProto</b>\n" + ", ".join(f"<code>{escape(c)}</code>" for c in mt)
            )
        methods = entry.get("related_methods") or []
        if methods:
            if len(methods) >= 100:
                details_chunks.append(
                    f"<b>Related methods</b> ({len(methods)})\nApplies to most Bot API methods."
                )
            else:
                shown = methods[:25]
                suffix = f"\n… and {len(methods) - 25} more" if len(methods) > 25 else ""
                details_chunks.append(
                    f"<b>Related methods</b> ({len(methods)})\n"
                    + ", ".join(f"<code>{escape(m)}</code>" for m in shown)
                    + suffix
                )

    if kind not in {"method", "type"}:
        example = example_snippet(entry)
        if example:
            lang, code = example
            details_chunks.append(
                f'<b>Example</b>\n<pre><code class="language-{escape(lang)}">'
                f"{escape(code)}</code></pre>"
            )

    details_html = ""
    if details_chunks:
        joined = "\n\n".join(details_chunks)
        if kind in {"method", "type"}:
            details_html = joined
        else:
            details_html = "<blockquote expandable>" + joined + "</blockquote>"

    def join(bs: list[str]) -> str:
        return "\n\n".join(b for b in bs if b and b.strip())

    text = join([header, body, details_html])
    if len(text) <= SAFE_LIMIT:
        return sanitize_telegram_html(text, max_len=MAX_MESSAGE)

    budget = SAFE_LIMIT - len(header) - 2
    body_t, details_t = body, details_html
    if len(body_t) + len(details_t) + 2 > budget and body_t:
        allow = max(80, budget - len(details_t) - 2)
        body_t = _trim_html(body_t, allow)
    if len(body_t) + len(details_t) + 2 > budget and details_t:
        allow = max(120, budget - len(body_t) - 2)
        details_t = _trim_html(details_t, allow)

    text = join([header, body_t, details_t])
    return sanitize_telegram_html(text, max_len=MAX_MESSAGE)


def _cut_outside_tags(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_lt = cut.rfind("<")
    last_gt = cut.rfind(">")
    if last_lt > last_gt:
        cut = cut[:last_lt]
    return cut.rstrip()


def close_telegram_html(text: str) -> str:
    """Close any unclosed Telegram HTML tags so answerInlineQuery will accept it."""
    stack: list[str] = []
    for m in re.finditer(r"</?([a-zA-Z0-9-]+)(\s[^>]*)?>", text or ""):
        raw = m.group(0)
        name = m.group(1).lower()
        if raw.startswith("</"):
            if name in stack:
                while stack and stack[-1] != name:
                    stack.pop()
                if stack:
                    stack.pop()
        elif raw.endswith("/>"):
            continue
        else:
            stack.append(name)
    if not stack:
        return text
    return text + "".join(f"</{n}>" for n in reversed(stack))


def sanitize_telegram_html(text: str, max_len: int | None = None) -> str:
    """Truncate outside tags if needed, then close any dangling open tags."""
    text = text or ""
    if max_len is None or len(text) <= max_len:
        return close_telegram_html(text)
    reserve = 80
    budget = max(1, max_len - reserve)
    cut = _cut_outside_tags(text, budget)
    if not cut.endswith("…") and len(cut) < len(text):
        cut = cut.rstrip() + "…"
    closed = close_telegram_html(cut)
    if len(closed) > max_len:
        closed = close_telegram_html(_cut_outside_tags(text, max(1, max_len - 120)).rstrip() + "…")
        if len(closed) > max_len:
            plain = re.sub(r"<[^>]+>", "", text)
            if len(plain) > max_len - 1:
                plain = plain[: max_len - 1].rstrip() + "…"
            return html.escape(plain)
    return closed


def _trim_html(text: str, limit: int) -> str:
    return sanitize_telegram_html(text, max_len=limit)


def help_message(bot_username: str | None = None) -> str:
    mention = f"@{bot_username}" if bot_username else "@your_bot"
    return (
        "<b>Bot API reference</b>\n\n"
        f"Type <code>{escape(mention)} sendMessage</code> in any chat. "
        "Results update as you type; scroll for more pages.\n\n"
        "Filter with <code>m:</code> <code>t:</code> <code>e:</code> "
        "<code>f:</code> <code>w:</code> <code>faq:</code>. "
        "Open a field with <code>sendMessage.chat_id</code> or <code>Message.chat.id</code>."
    )


def help_article_title() -> str:
    return "Search the Bot API"


def help_article_description() -> str:
    return "Type a method, type, or field path. Prefixes: m: t: e: f: w: faq:"
