#!/usr/bin/env python3
"""Inline Telegram bot for searching Bot API docs, errors, features, Mini Apps, and FAQ."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
)

from docsbot.builder import build_index, default_index_path, load_index, save_index
from docsbot.compose import compose_preview, detect_compose, format_compose_html, make_resolver
from docsbot.formatters import (
    all_fields_query,
    format_message,
    help_article_description,
    help_article_title,
    help_message,
    result_description,
    result_title,
    sanitize_telegram_html,
)
from docsbot.helpguide import help_guide_markup, help_guide_text
from docsbot.menus import (
    about_text,
    bot_description,
    bot_short_description,
    category_overview_text,
    how_to_use_text,
    search_results_text,
    start_home_text,
    unknown_payload_text,
)
from docsbot.permalinks import (
    deep_link,
    encode_search_payload,
    parse_start_payload,
)
from docsbot import rich as richmsg
from docsbot.retrieve import (
    SearchEngine,
    build_engine,
    default_embeddings_path,
    default_model_cache,
    parse_offset,
)

log = logging.getLogger("docsbot.bot")

HELP_ID = "help:start"
PAGE_SIZE = 50
CACHE_TIME = 2
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

MENU_KINDS = {
    "methods": "method",
    "types": "type",
    "errors": "error",
    "features": "feature",
    "webapps": "webapp",
    "faq": "faq",
}
MENU_LABELS = {
    "methods": "Methods",
    "types": "Types",
    "errors": "Errors",
    "features": "Features",
    "webapps": "Mini Apps",
    "faq": "FAQ",
}


def get_index(context: ContextTypes.DEFAULT_TYPE) -> dict:
    idx = context.application.bot_data.get("index")
    if idx is None:
        raise RuntimeError("Docs index is not loaded")
    return idx


def get_engine(context: ContextTypes.DEFAULT_TYPE) -> SearchEngine:
    engine = context.application.bot_data.get("engine")
    if engine is None:
        raise RuntimeError("Search engine is not loaded")
    return engine


def _index_meta(context: ContextTypes.DEFAULT_TYPE) -> tuple[int, str | None, str | None, dict]:
    idx = get_index(context)
    count = idx.get("entry_count") or len(idx.get("entries") or [])
    version = ((idx.get("sources") or {}).get("api") or {}).get("version")
    return count, version, idx.get("built_at"), idx.get("sources") or {}


def _doc_markup(
    entry: dict,
    engine: SearchEngine,
    *,
    query: str = "",
    bot_username: str | None = None,
    back: str | None = "home",
) -> InlineKeyboardMarkup:
    url = entry.get("url") or ""
    payload = engine.permalink_for(entry)
    rows: list[list[InlineKeyboardButton]] = []
    top: list[InlineKeyboardButton] = []
    if url:
        top.append(InlineKeyboardButton("Open docs", url=url))
    if payload and bot_username:
        top.append(InlineKeyboardButton("Permalink", url=deep_link(bot_username, payload)))
    if top:
        rows.append(top)
    extra: list[InlineKeyboardButton] = []
    if entry.get("kind") in {"field", "fieldset"}:
        parent_payload = entry.get("parent_payload")
        parent_name = entry.get("parent_name") or "Parent"
        if len(parent_name) > 24:
            parent_name = parent_name[:23] + "…"
        if parent_payload and bot_username:
            extra.append(InlineKeyboardButton(parent_name, url=deep_link(bot_username, parent_payload)))
        if entry.get("kind") == "field" and entry.get("chain_query"):
            extra.append(
                InlineKeyboardButton("Fields", switch_inline_query_current_chat=entry["chain_query"])
            )
    query_all = all_fields_query(entry)
    if entry.get("kind") in {"method", "type"} and query_all and entry.get("fields"):
        extra.append(InlineKeyboardButton("All fields", switch_inline_query_current_chat=query_all))
    if extra:
        rows.append(extra[:2])
    nav: list[InlineKeyboardButton] = []
    if back:
        nav.append(InlineKeyboardButton("Back", callback_data=f"menu:{back}"))
    nav.append(InlineKeyboardButton("Home", callback_data="menu:home"))
    rows.append(nav[:2])
    return InlineKeyboardMarkup(rows)


def _home_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Search", switch_inline_query_current_chat=""),
                InlineKeyboardButton("Other chat", switch_inline_query=""),
            ],
            [
                InlineKeyboardButton("Methods", callback_data="menu:methods"),
                InlineKeyboardButton("Types", callback_data="menu:types"),
            ],
            [
                InlineKeyboardButton("Errors", callback_data="menu:errors"),
                InlineKeyboardButton("Features", callback_data="menu:features"),
            ],
            [
                InlineKeyboardButton("Mini Apps", callback_data="menu:webapps"),
                InlineKeyboardButton("FAQ", callback_data="menu:faq"),
            ],
            [
                InlineKeyboardButton("Help", callback_data="menu:help"),
                InlineKeyboardButton("About", callback_data="menu:about"),
            ],
        ]
    )


def _category_markup(kind_key: str, entries: list[dict], engine: SearchEngine) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for entry in entries[:12]:
        payload = engine.permalink_for(entry)
        if not payload:
            continue
        label = entry.get("name") or entry.get("title") or payload
        if len(label) > 30:
            label = label[:29] + "…"
        row.append(InlineKeyboardButton(label, callback_data=f"d:{payload}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    prefix = {"method": "m: ", "type": "t: ", "error": "e: ", "feature": "f: ", "webapp": "w: ", "faq": "faq: "}.get(
        MENU_KINDS.get(kind_key, ""), ""
    )
    rows.append(
        [
            InlineKeyboardButton("Search", switch_inline_query_current_chat=prefix),
            InlineKeyboardButton("Home", callback_data="menu:home"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _article(
    entry: dict,
    engine: SearchEngine,
    *,
    query: str = "",
    bot_username: str | None = None,
) -> InlineQueryResultArticle:
    payload = engine.permalink_for(entry)
    text = sanitize_telegram_html(
        format_message(entry, bot_username=bot_username, permalink=payload),
        max_len=4096,
    )
    url = entry.get("url") or ""
    buttons: list[list[InlineKeyboardButton]] = []
    top: list[InlineKeyboardButton] = []
    if url:
        top.append(InlineKeyboardButton("Open docs", url=url))
    if payload and bot_username:
        top.append(InlineKeyboardButton("Permalink", url=deep_link(bot_username, payload)))
    if top:
        buttons.append(top)
    extra: list[InlineKeyboardButton] = []
    if entry.get("kind") in {"field", "fieldset"}:
        parent_payload = entry.get("parent_payload")
        parent_name = entry.get("parent_name") or "Parent"
        if len(parent_name) > 24:
            parent_name = parent_name[:23] + "…"
        if parent_payload and bot_username:
            extra.append(InlineKeyboardButton(parent_name, url=deep_link(bot_username, parent_payload)))
        if entry.get("kind") == "field" and entry.get("chain_query"):
            extra.append(InlineKeyboardButton("Fields", switch_inline_query_current_chat=entry["chain_query"]))
    query_all = all_fields_query(entry)
    if entry.get("kind") in {"method", "type"} and query_all and entry.get("fields"):
        extra.append(InlineKeyboardButton("All fields", switch_inline_query_current_chat=query_all))
    if extra:
        buttons.append(extra[:2])
    # Unique inline result id: permalink is short and unique; fall back to entry id.
    result_id = (payload or entry.get("id") or "item")[:64]
    return InlineQueryResultArticle(
        id=result_id,
        title=result_title(entry),
        description=result_description(entry),
        input_message_content=InputTextMessageContent(
            message_text=text,
            parse_mode=ParseMode.HTML,
            link_preview_options=NO_PREVIEW,
        ),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _help_article(bot_username: str | None) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=HELP_ID,
        title=help_article_title(),
        description=help_article_description(),
        input_message_content=InputTextMessageContent(
            message_text=help_message(bot_username),
            parse_mode=ParseMode.HTML,
            link_preview_options=NO_PREVIEW,
        ),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Search", switch_inline_query_current_chat="")]]
        ),
    )


async def _send_or_edit(
    *,
    message: Message | None = None,
    query: CallbackQuery | None = None,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    text = sanitize_telegram_html(text, max_len=4096)
    if query is not None:
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )
        except BadRequest as exc:
            # "Message is not modified" is fine; anything else re-raise-as-log.
            if "not modified" not in str(exc).lower():
                raise
        return
    if message is not None:
        await message.reply_html(
            text,
            reply_markup=markup,
            link_preview_options=NO_PREVIEW,
        )


def _home_bundle(context: ContextTypes.DEFAULT_TYPE, username: str | None) -> tuple[str, InlineKeyboardMarkup]:
    count, version, _built, _sources = _index_meta(context)
    return start_home_text(username, count=count, version=version), _home_markup()



async def _send_rich_or_html(
    bot,
    *,
    chat_id: int | str,
    rich_message: dict,
    html_text: str,
    markup: InlineKeyboardMarkup | None = None,
    reply_to_message_id: int | None = None,
) -> str:
    """Send a Bot API 10.x rich message, falling back to HTML on any API error."""
    try:
        kwargs: dict = {"chat_id": chat_id, "rich_message": rich_message}
        if reply_to_message_id:
            kwargs["reply_parameters"] = {"message_id": reply_to_message_id}
        await bot.do_api_request("sendRichMessage", api_kwargs=kwargs)
        return "rich"
    except Exception as exc:
        log.exception("sendRichMessage failed; falling back to HTML: %s", exc)
        await bot.send_message(
            chat_id=chat_id,
            text=sanitize_telegram_html(html_text, max_len=4096),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=NO_PREVIEW,
            reply_to_message_id=reply_to_message_id,
        )
        return "html"


async def _edit_rich_or_html(
    query: CallbackQuery,
    *,
    rich_message: dict,
    html_text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> str:
    """Edit via editMessageText(rich_message=...), falling back to HTML text."""
    bot = query.get_bot()
    kwargs: dict = {"rich_message": rich_message}
    if query.inline_message_id:
        kwargs["inline_message_id"] = query.inline_message_id
    elif query.message is not None:
        kwargs["chat_id"] = query.message.chat.id
        kwargs["message_id"] = query.message.message_id
    else:
        log.warning("editMessageText: no message target on callback query")
        return "skipped"
    try:
        await bot.do_api_request("editMessageText", api_kwargs=kwargs)
        return "rich"
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return "unchanged"
        log.exception("editMessageText(rich_message) failed; falling back to HTML: %s", exc)
    except Exception as exc:
        log.exception("editMessageText(rich_message) failed; falling back to HTML: %s", exc)
    try:
        await query.edit_message_text(
            sanitize_telegram_html(html_text, max_len=4096),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=NO_PREVIEW,
        )
        return "html"
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return "unchanged"
        raise


async def _present(
    *,
    message: Message | None = None,
    query: CallbackQuery | None = None,
    rich_message: dict,
    html_text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    if query is not None:
        await _edit_rich_or_html(query, rich_message=rich_message, html_text=html_text, markup=markup)
        return
    if message is not None:
        await _send_rich_or_html(
            message.get_bot(),
            chat_id=message.chat.id,
            rich_message=rich_message,
            html_text=html_text,
            markup=markup,
            reply_to_message_id=message.message_id,
        )


async def _present_html(
    *,
    message: Message | None = None,
    query: CallbackQuery | None = None,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Command and menu pages: normal sendMessage / editMessageText with HTML."""
    text = sanitize_telegram_html(text, max_len=4096)
    if query is not None:
        try:
            await query.edit_message_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.warning("edit_message_text HTML failed (%s); sending a new message", exc)
            chat = query.message.chat if query.message else None
            if chat is not None:
                await chat.send_message(
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    link_preview_options=NO_PREVIEW,
                )
        return
    if message is not None:
        await message.reply_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=NO_PREVIEW,
        )


def _howto_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Search", switch_inline_query_current_chat=""),
                InlineKeyboardButton("Home", callback_data="menu:home"),
            ]
        ]
    )


def _about_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Search", switch_inline_query_current_chat=""),
                InlineKeyboardButton("Home", callback_data="menu:home"),
            ]
        ]
    )


def _rich_inline_article(
    entry: dict,
    engine: SearchEngine,
    *,
    query: str = "",
    bot_username: str | None = None,
) -> dict:
    payload = engine.permalink_for(entry)
    body = richmsg.entry_rich_message(
        entry,
        bot_username=bot_username,
        permalink=payload,
        query=query,
        include_nav=False,
    )
    result_id = (payload or entry.get("id") or "item")[:64]
    return richmsg.inline_article_payload(
        result_id=result_id,
        title=result_title(entry),
        description=result_description(entry),
        rich=body,
    )


def _rich_help_article(bot_username: str | None) -> dict:
    return richmsg.inline_article_payload(
        result_id=HELP_ID,
        title=help_article_title(),
        description=help_article_description(),
        rich=richmsg.help_inline_rich_message(bot_username),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    username = context.bot.username
    payload = " ".join(context.args).strip() if context.args else ""
    if payload:
        await _handle_payload(update.message, context, payload, username)
        return
    text, markup = _home_bundle(context, username)
    await _present_html(message=update.message, text=text, markup=markup)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    username = context.bot.username
    await _present_html(
        message=update.message,
        text=help_guide_text(1, username),
        markup=help_guide_markup(1, username),
    )


async def _handle_payload(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    payload: str,
    username: str | None,
) -> None:
    engine = get_engine(context)
    kind, data = parse_start_payload(payload, engine.payload_to_entry)
    if kind == "field":
        entry = engine.entry_for_payload(data) if isinstance(data, str) else data
        if not entry:
            await _present_html(
                message=message,
                text=unknown_payload_text(payload),
                markup=_home_markup(),
            )
            return
        kind = "entry"
        data = entry
    if kind == "entry":
        entry = data
        permalink = engine.permalink_for(entry)
        text = format_message(entry, bot_username=username, permalink=permalink)
        await _present(
            message=message,
            rich_message=richmsg.entry_rich_message(
                entry,
                bot_username=username,
                permalink=permalink,
                back="home",
            ),
            html_text=text,
            markup=_doc_markup(entry, engine, bot_username=username, back="home"),
        )
        return
    if kind == "search":
        query = data or ""
        hits, total, _ = engine.search(query, limit=8, offset=0)
        text = search_results_text(query, hits)
        if total > 8:
            text += f"\n\n<i>{total:,} matches in total.</i>"
        rows: list[list[InlineKeyboardButton]] = []
        permalinks: list[str] = []
        hit_btns: list[InlineKeyboardButton] = []
        for _score, entry in hits:
            pl = engine.permalink_for(entry)
            permalinks.append(pl or "")
            if not pl:
                continue
            label = entry.get("name") or entry.get("title") or pl
            if len(label) > 28:
                label = label[:27] + "…"
            hit_btns.append(InlineKeyboardButton(label, callback_data=f"d:{pl}"))
        for i in range(0, len(hit_btns), 2):
            rows.append(hit_btns[i : i + 2])
        share = encode_search_payload(query)
        rows.append(
            [
                InlineKeyboardButton("Search", switch_inline_query_current_chat=query),
                InlineKeyboardButton("Share", url=deep_link(username, share)),
            ]
        )
        rows.append([InlineKeyboardButton("Home", callback_data="menu:home")])
        await _present_html(
            message=message,
            text=text,
            markup=InlineKeyboardMarkup(rows),
        )
        return
    if kind == "home":
        text, markup = _home_bundle(context, username)
        await _present_html(message=message, text=text, markup=markup)
        return
    await _present_html(
        message=message,
        text=unknown_payload_text(payload),
        markup=_home_markup(),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if cq is None:
        return
    data = (cq.data or "").strip()
    username = context.bot.username
    engine = get_engine(context)
    await cq.answer()

    if data.startswith("d:"):
        payload = data[2:]
        entry = engine.entry_for_payload(payload)
        if not entry:
            await cq.answer("That page is no longer in the catalog.", show_alert=True)
            return
        permalink = engine.permalink_for(entry)
        text = format_message(entry, bot_username=username, permalink=permalink)
        kind_key = next((k for k, v in MENU_KINDS.items() if v == entry.get("kind")), "home")
        await _present(
            query=cq,
            rich_message=richmsg.entry_rich_message(
                entry,
                bot_username=username,
                permalink=permalink,
                back=kind_key,
            ),
            html_text=text,
            markup=_doc_markup(entry, engine, bot_username=username, back=kind_key),
        )
        return

    if data.startswith("guide:"):
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 1
        await _present_html(
            query=cq,
            text=help_guide_text(page, username),
            markup=help_guide_markup(page, username),
        )
        return

    if not data.startswith("menu:"):
        await cq.answer()
        return
    action = data.split(":", 1)[1]

    if action == "home":
        text, markup = _home_bundle(context, username)
        await _present_html(query=cq, text=text, markup=markup)
        return
    if action == "help":
        await _present_html(
            query=cq,
            text=help_guide_text(1, username),
            markup=help_guide_markup(1, username),
        )
        return
    if action == "about":
        count, version, built_at, sources = _index_meta(context)
        await _present_html(
            query=cq,
            text=about_text(
                username, count=count, version=version, built_at=built_at, sources=sources
            ),
            markup=_about_markup(),
        )
        return
    if action in MENU_KINDS:
        kind = MENU_KINDS[action]
        popular, total = engine.suggest_empty(kind, limit=10, offset=0)
        entries = [e for _s, e in popular]
        await _present_html(
            query=cq,
            text=category_overview_text(kind, entries, total=total),
            markup=_category_markup(action, entries, engine),
        )
        return


async def _answer_compose(iq, context, body: str, engine: SearchEngine, username: str | None) -> None:
    html, resolved, _unresolved = format_compose_html(
        body, resolver=make_resolver(engine), bot_username=username
    )
    html = sanitize_telegram_html(html, max_len=4096)
    title, desc = compose_preview(body, resolved)
    rich = richmsg.html_rich_message(html)
    rich_results = [
        richmsg.inline_article_payload(
            result_id="compose:1",
            title=title,
            description=desc,
            rich=rich,
        )
    ]
    html_article = InlineQueryResultArticle(
        id="compose:1",
        title=title,
        description=desc,
        input_message_content=InputTextMessageContent(
            message_text=html,
            parse_mode=ParseMode.HTML,
            link_preview_options=NO_PREVIEW,
        ),
    )
    try:
        await context.bot.do_api_request(
            "answerInlineQuery",
            api_kwargs={
                "inline_query_id": iq.id,
                "results": rich_results,
                "cache_time": CACHE_TIME,
                "next_offset": "",
                "is_personal": False,
            },
        )
    except BadRequest as exc:
        if "query is too old" in str(exc).lower() or "query id is invalid" in str(exc).lower():
            log.info("Dropped stale compose query")
            return
        log.exception("compose answerInlineQuery rich failed; falling back to HTML: %s", exc)
        await iq.answer(
            results=[html_article],
            cache_time=CACHE_TIME,
            next_offset="",
            is_personal=False,
        )


async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    iq = update.inline_query
    if iq is None:
        return
    offset = parse_offset(iq.offset)
    query = iq.query or ""
    username = context.bot.username
    t0 = time.perf_counter()
    engine = get_engine(context)

    compose_body = detect_compose(query)
    if compose_body is not None:
        await _answer_compose(iq, context, compose_body, engine, username)
        return

    # First page of a brand-new (empty) query: include a help card, then 49 entries.
    # Subsequent pages use a 50-entry window and no help card. Engine offset always
    # counts catalog entries, not the help card.
    include_help = (not query.strip()) and offset == 0
    page_limit = PAGE_SIZE - 1 if include_help else PAGE_SIZE
    hits, total, timings = engine.search(query, limit=page_limit, offset=offset)

    html_results: list[InlineQueryResultArticle] = []
    rich_results: list[dict] = []
    if include_help:
        html_results.append(_help_article(username))
        rich_results.append(_rich_help_article(username))
    for _score, entry in hits:
        html_results.append(_article(entry, engine, query=query, bot_username=username))
        rich_results.append(_rich_inline_article(entry, engine, query=query, bot_username=username))

    delivered = len(hits)
    next_offset = ""
    if offset + delivered < total:
        next_offset = str(offset + delivered)

    used = "rich"
    try:
        await context.bot.do_api_request(
            "answerInlineQuery",
            api_kwargs={
                "inline_query_id": iq.id,
                "results": rich_results,
                "cache_time": CACHE_TIME,
                "next_offset": next_offset,
                "is_personal": False,
            },
        )
    except BadRequest as exc:
        # Stale inline queries happen if the user types faster than we answer.
        if "query is too old" in str(exc).lower() or "query id is invalid" in str(exc).lower():
            log.info("Dropped stale inline query q=%r offset=%s", query, offset)
            return
        log.exception("answerInlineQuery with InputRichMessageContent failed; falling back to HTML: %s", exc)
        used = "html"
        await iq.answer(
            results=html_results,
            cache_time=CACHE_TIME,
            next_offset=next_offset,
            is_personal=False,
        )
    except Exception as exc:
        log.exception("answerInlineQuery with InputRichMessageContent failed; falling back to HTML: %s", exc)
        used = "html"
        try:
            await iq.answer(
                results=html_results,
                cache_time=CACHE_TIME,
                next_offset=next_offset,
                is_personal=False,
            )
        except BadRequest as exc2:
            if "query is too old" in str(exc2).lower() or "query id is invalid" in str(exc2).lower():
                log.info("Dropped stale inline query q=%r offset=%s", query, offset)
                return
            raise
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log.info(
        "inline q=%r offset=%s hits=%s/%s next=%r mode=%s answer=%.1fms search=%.1fms %s",
        query,
        offset,
        len(rich_results),
        total,
        next_offset,
        used,
        elapsed_ms,
        timings.get("total_ms", 0.0),
        {k: round(v, 1) for k, v in timings.items() if k != "total_ms"},
    )


async def refresh_index(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    path: Path = app.bot_data["index_path"]
    log.info("Refreshing docs index…")
    try:
        index = await asyncio.to_thread(build_index)
        await asyncio.to_thread(save_index, index, path)
        engine = await asyncio.to_thread(build_engine, index)
        app.bot_data["index"] = index
        app.bot_data["engine"] = engine
        log.info("Index refreshed: %s entries", index.get("entry_count"))
        if index.get("warnings"):
            for w in index["warnings"]:
                log.warning("Index warning: %s", w)
    except Exception:
        log.exception("Periodic index refresh failed; keeping the existing index")


async def _install_profile(app: Application) -> None:
    """Set commands and the public bot description. Best-effort; never block startup."""
    username = app.bot.username
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Open the docs menu"),
                BotCommand("help", "Open the Search Guide"),
            ]
        )
        log.info("setMyCommands ok")
    except Exception:
        log.exception("setMyCommands failed")
    try:
        await app.bot.set_my_short_description(bot_short_description())
        log.info("setMyShortDescription ok")
    except Exception:
        log.exception("setMyShortDescription failed")
    try:
        await app.bot.set_my_description(bot_description(username))
        log.info("setMyDescription ok")
    except Exception:
        log.exception("setMyDescription failed")


async def post_init(app: Application) -> None:
    path: Path = app.bot_data["index_path"]
    if path.exists():
        try:
            app.bot_data["index"] = load_index(path)
            log.info(
                "Loaded index from %s (%s entries, built %s)",
                path,
                app.bot_data["index"].get("entry_count"),
                app.bot_data["index"].get("built_at"),
            )
        except Exception:
            log.exception("Existing index at %s could not be read; rebuilding", path)
            app.bot_data["index"] = None
    if not app.bot_data.get("index"):
        log.info("Building docs index at startup…")
        index = await asyncio.to_thread(build_index)
        save_index(index, path)
        app.bot_data["index"] = index
        log.info("Built index with %s entries", index.get("entry_count"))

    log.info("Preparing search engine (BM25 + char n-grams + embeddings)…")
    app.bot_data["engine"] = await asyncio.to_thread(
        build_engine,
        app.bot_data["index"],
        embeddings_path=default_embeddings_path(),
        model_cache=default_model_cache(),
    )

    await _install_profile(app)

    hours = float(os.environ.get("REFRESH_HOURS") or app.bot_data.get("refresh_hours") or 24)
    app.bot_data["refresh_hours"] = hours
    if hours > 0 and app.job_queue is not None:
        app.job_queue.run_repeating(
            refresh_index,
            interval=hours * 3600,
            first=hours * 3600,
            name="refresh-docs-index",
        )
        log.info("Scheduled index refresh every %s hours", hours)
    elif hours > 0:

        async def loop() -> None:
            try:
                while True:
                    await asyncio.sleep(hours * 3600)

                    class _Ctx:
                        application = app

                    await refresh_index(_Ctx())  # type: ignore[arg-type]
            except asyncio.CancelledError:
                return

        app.bot_data["refresh_task"] = asyncio.create_task(loop())
        log.info("Scheduled index refresh task every %s hours (no JobQueue)", hours)


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("refresh_task")
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, BadRequest) and (
        "query is too old" in str(err).lower() or "query id is invalid" in str(err).lower()
    ):
        log.info("Ignored stale inline/callback query: %s", err)
        return
    if isinstance(err, TimedOut):
        log.warning("Telegram request timed out: %s", err)
        return
    log.exception("Update failed: %s", err)


def main() -> None:
    # Load the bot's .env even if cwd is not the project, and prefer it over a
    # pre-set (possibly unrelated) BOT_TOKEN in the environment.
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
    parser = argparse.ArgumentParser(description="Telegram Bot API docs inline search bot")
    parser.add_argument("--token", default=os.environ.get("BOT_TOKEN"), help="Bot token (or set BOT_TOKEN)")
    parser.add_argument(
        "--index",
        default=os.environ.get("INDEX_PATH"),
        help="Path to the JSON index (default: data/index.json)",
    )
    parser.add_argument(
        "--refresh-hours",
        type=float,
        default=float(os.environ.get("REFRESH_HOURS") or 24),
        help="How often to rebuild the index (0 disables). Default: 24",
    )
    args = parser.parse_args()
    if not args.token:
        raise SystemExit(
            "BOT_TOKEN is not set. Put it in the environment or a .env file (see .env.example)."
        )

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx INFO logs include the bot token in the request URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    path = Path(args.index) if args.index else default_index_path()
    app = (
        Application.builder()
        .token(args.token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["index_path"] = path
    app.bot_data["refresh_hours"] = args.refresh_hours
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_error_handler(on_error)
    log.info("Starting polling…")
    app.run_polling(allowed_updates=["message", "inline_query", "callback_query"])


if __name__ == "__main__":
    main()
