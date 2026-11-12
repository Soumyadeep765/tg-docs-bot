"""Short /start menu copy and category overviews (HTML)."""

from __future__ import annotations

from typing import Any, Iterable

from .formatters import KIND_LABELS
from .permalinks import deep_link
from .textutil import escape, first_sentence


def start_home_text(bot_username: str | None = None, *, count: int = 0, version: str | None = None) -> str:
    mention = f"@{bot_username.lstrip('@')}" if bot_username else "@your_bot"
    intro = (
        "Inline search for the official Telegram Bot API docs: "
        "methods, types, errors, features, Mini Apps, and the FAQ."
    )
    if version:
        intro += f" This catalog is Bot API {escape(str(version))}."
    examples = [
        f"{mention} sendMessage",
        f"{mention} sendMessage.text",
        f"{mention} sendMessage.text,parse_mode",
        f"{mention} Update.*",
        f"{mention} e: FLOOD",
        f"{mention} f: Hey use $sendMessage",
    ]
    example_lines = "\n".join(f"• <code>{escape(q)}</code>" for q in examples)
    return (
        "<b>Bot API reference</b>\n\n"
        f"{intro}\n\n"
        "<b>How to use</b>\n"
        "Type the bot username in any chat, then a query:\n"
        f"{example_lines}\n\n"
        "Send <code>/help</code> for the full Search Guide."
    )


def how_to_use_text(bot_username: str | None = None) -> str:
    mention = f"@{bot_username}" if bot_username else "@your_bot"
    link = deep_link(bot_username, "m_sendMessage") if bot_username else "https://t.me/your_bot?start=m_sendMessage"
    return (
        f"<b>How to use</b>\n\n"
        f"Type <code>{escape(mention)} sendMessage</code> in any chat. "
        f"Results update as you type; scroll for more pages.\n\n"
        f"Filter with <code>m:</code> <code>t:</code> <code>e:</code> "
        f"<code>f:</code> <code>w:</code> <code>faq:</code>. "
        f"Open a field with <code>sendMessage.chat_id</code> or <code>Message.chat.id</code>; "
        f"a trailing <code>.</code> lists every field.\n\n"
        f"Each result includes a Permalink, for example "
        f"<a href=\"{escape(link)}\">{escape(link)}</a>."
    )


def about_text(
    bot_username: str | None = None,
    *,
    count: int = 0,
    version: str | None = None,
    built_at: str | None = None,
    sources: dict[str, Any] | None = None,
) -> str:
    mention = f"@{bot_username}" if bot_username else "@your_bot"
    ver = f" · Bot API {escape(version)}" if version else ""
    lines = [
        f"<b>About {escape(mention)}</b>",
        "",
        "Inline reference for the Bot API: methods, types, errors, features, Mini Apps, and the FAQ.",
        "",
        f"{count:,} entries{ver}.",
    ]
    extra: list[str] = []
    if built_at:
        extra.append(f"Catalog built <code>{escape(built_at)}</code>. Refreshes about once a day.")
    if sources:
        labels = {
            "api": "Bot API schema",
            "errors": "API errors",
            "fact": "Intro / facts",
            "feature": "Features",
            "webapp": "Mini Apps",
            "faq": "FAQ",
        }
        source_bits: list[str] = []
        for key, meta in sources.items():
            url = (meta or {}).get("url") if isinstance(meta, dict) else None
            label = labels.get(key, key)
            if url and str(url).startswith("http"):
                source_bits.append(f"<a href=\"{escape(str(url))}\">{escape(label)}</a>")
            else:
                source_bits.append(escape(label))
        if source_bits:
            extra.append("Sources: " + " · ".join(source_bits))
    if extra:
        lines.append("")
        lines.extend(extra)
    return "\n".join(lines)


def category_overview_text(kind: str, entries: Iterable[dict[str, Any]], *, total: int | None = None) -> str:
    label = {
        "method": "Methods",
        "type": "Types",
        "error": "Errors",
        "feature": "Features",
        "webapp": "Mini Apps",
        "faq": "FAQ",
    }.get(kind, kind)
    intros = {
        "method": "HTTP methods on the Bot API.",
        "type": "Objects returned or accepted by methods.",
        "error": "HTTP errors returned by the Bot API.",
        "feature": "Platform features: keyboards, payments, Mini Apps, and more.",
        "webapp": "Telegram Mini Apps: launch, init data, theme, and JS APIs.",
        "faq": "Answers from the official Bot FAQ.",
    }
    lines = [f"<b>{escape(label)}</b>", "", intros.get(kind, "")]
    if total is not None:
        lines.append(f"{total:,} in this category.")
    lines.append("")
    for entry in entries:
        name = escape(entry.get("title") or entry.get("name") or "")
        short = escape(first_sentence(entry.get("short") or entry.get("description") or "", limit=80))
        lines.append(f"<code>{name}</code>" + (f" — {short}" if short else ""))
    return "\n".join(lines)


def search_results_text(query: str, hits: list[tuple[float, dict[str, Any]]]) -> str:
    lines = [f"<b>Results for</b> <code>{escape(query)}</code>", ""]
    if not hits:
        lines.append("No matches. Try a shorter query, or search inline.")
        return "\n".join(lines)
    for _score, entry in hits:
        kind = KIND_LABELS.get(entry.get("kind") or "", entry.get("kind") or "")
        name = escape(entry.get("title") or entry.get("name") or "")
        short = escape(first_sentence(entry.get("short") or "", limit=80))
        lines.append(f"<code>{name}</code> · <i>{escape(kind)}</i>")
        if short:
            lines.append(short)
    return "\n".join(lines)


def unknown_payload_text(payload: str) -> str:
    return (
        "<b>Not found</b>\n\n"
        "This link may be outdated, or the catalog was rebuilt."
    )


def bot_short_description() -> str:
    return "Search the Telegram Bot API: methods, types, errors, features, and Mini Apps."


def bot_description(bot_username: str | None = None) -> str:
    mention = f"@{bot_username}" if bot_username else "@your_bot"
    return (
        "Inline Bot API reference: methods, types, errors, features, Mini Apps, and the FAQ. "
        f"Type {mention} sendMessage in any chat. "
        "Filter with m: t: e: f: w: faq:. "
        "Open a field with sendMessage.chat_id. "
        "Share a page with its Permalink."
    )
