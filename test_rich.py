#!/usr/bin/env python3
"""Offline validation of Bot API 10.x rich-message payloads against the schema.

No Telegram API calls are made. Tables must not appear in any payload.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from docsbot import rich
from docsbot.builder import default_index_path, load_index
from docsbot.formatters import format_message, result_description, result_title
from docsbot.dotpath import DotPathIndex
from docsbot.permalinks import build_permalink_map, encode_field_payload, encode_search_payload
from docsbot.typelinks import configure_type_links
from docsbot.rich_schema import (
    assert_valid,
    collect_block_types,
    schema_version,
    validate,
)

BOT = "autoblogcarpperbot"
FORBIDDEN = {"table"}
EXPECTED_SOMEWHERE = {
    "heading",
    "paragraph",
    "details",
    "list",
    "pre",
    "buttons",
}


def _walk_blocks(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_blocks(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_blocks(item)


def check(name: str, obj: dict, schema: str, failures: list[str]) -> set[str]:
    types = collect_block_types(obj)
    banned = types & FORBIDDEN
    if banned:
        failures.append(f"{name}: used forbidden block types {banned}")
    errors = validate(obj, schema)
    if errors:
        failures.append(f"{name}: {schema} invalid: {errors[:4]}")
    for block in _walk_blocks(obj):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "buttons":
            n = len(block.get("buttons") or [])
            if n > 2:
                failures.append(f"{name}: button row has {n} buttons (max 2)")
        if block.get("type") == "heading":
            text = block.get("text")
            if isinstance(text, str) and text:
                first = text[0]
                if ord(first) > 127 and not first.isalpha():
                    failures.append(f"{name}: heading starts with a symbol/emoji: {text!r}")
    return types


def count_details(obj) -> int:
    n = 0
    if isinstance(obj, dict):
        if obj.get("type") == "details":
            n += 1
        for v in obj.values():
            n += count_details(v)
    elif isinstance(obj, list):
        for item in obj:
            n += count_details(item)
    return n


def main() -> int:
    failures: list[str] = []
    print(f"Bot API schema version: {schema_version()}")
    if schema_version() < "10":
        failures.append(f"unexpected schema version {schema_version()}")

    idx = load_index(default_index_path())
    entries = idx["entries"]
    id_to_payload, _ = build_permalink_map(entries)
    configure_type_links(entries, id_to_payload)
    by_kind: dict[str, list] = {}
    by_name: dict[str, dict] = {}
    for e in entries:
        by_kind.setdefault(e.get("kind") or "", []).append(e)
        if e.get("name"):
            by_name.setdefault(e["name"], e)

    seen: set[str] = set()

    # Menus / helpers
    count = idx.get("entry_count") or len(entries)
    version = ((idx.get("sources") or {}).get("api") or {}).get("version")
    payloads = {
        "home": rich.home_rich_message(BOT, count=count, version=version),
        "howto": rich.howto_rich_message(BOT),
        "about": rich.about_rich_message(
            BOT,
            count=count,
            version=version,
            built_at=idx.get("built_at"),
            sources=idx.get("sources"),
        ),
        "unknown": rich.unknown_rich_message("nope"),
        "help_inline": rich.help_inline_rich_message(BOT),
    }
    methods = by_kind.get("method") or []
    cat_entries = methods[:10]
    payloads["category"] = rich.category_rich_message(
        "method",
        cat_entries,
        total=len(methods),
        permalinks=[id_to_payload.get(e["id"], "") for e in cat_entries],
    )
    sm = by_name["sendMessage"]
    hits = [(1.0, sm)]
    if "sendPhoto" in by_name:
        hits.append((0.9, by_name["sendPhoto"]))
    payloads["search"] = rich.search_results_rich_message(
        "send photo",
        hits,
        total=12,
        bot_username=BOT,
        permalinks=[id_to_payload.get(e["id"], "") for _s, e in hits],
        share_payload=encode_search_payload("send photo"),
    )

    for name, msg in payloads.items():
        seen |= check(name, msg, "InputRichMessage", failures)
        check(f"{name}/send", {"chat_id": 1, "rich_message": msg}, "SendRichMessageParams", failures)

    # Entry pages: every catalog entry, plus inline + edit wrappers for a sample.
    fielded = 0
    for i, entry in enumerate(entries):
        permalink = id_to_payload.get(entry["id"])
        msg = rich.entry_rich_message(
            entry, bot_username=BOT, permalink=permalink, back="home"
        )
        types = check(f"entry:{entry.get('kind')}:{entry.get('name') or entry.get('id')}", msg, "InputRichMessage", failures)
        seen |= types
        if entry.get("fields"):
            fielded += 1
            if "table" in types:
                failures.append(f"{entry.get('name')}: table block used")
            kind = entry.get("kind") or ""
            if kind in {"method", "type"}:
                if "table" in types:
                    failures.append(f"{entry.get('name')}: table block used")
                if "pre" in types:
                    failures.append(f"{entry.get('name')}: compact view should not include the example")
                blob = json.dumps(msg)
                if f"{entry.get('name')}.*" not in blob:
                    failures.append(f"{entry.get('name')}: compact view missing all-fields hint")
                # Short required/first-fields list should be collapsible.
                if entry.get("fields") and "details" not in types:
                    failures.append(f"{entry.get('name')}: compact field list is not collapsible")
        if i < 30 or entry.get("name") in {"sendMessage", "Message", "sendPhoto"}:
            inline_msg = rich.entry_rich_message(
                entry, bot_username=BOT, permalink=permalink, include_nav=False
            )
            article = rich.inline_article_payload(
                result_id=(permalink or entry["id"])[:64],
                title=result_title(entry),
                description=result_description(entry),
                rich=inline_msg,
            )
            check(f"inline:{entry.get('name')}", article, "InlineQueryResultArticle", failures)
            check(
                f"content:{entry.get('name')}",
                {"rich_message": inline_msg},
                "InputRichMessageContent",
                failures,
            )
            # editMessageText: chat_id + message_id + rich_message
            edit = {"chat_id": 1, "message_id": 2, "rich_message": msg}
            errs = validate(edit, "EditMessageTextParams")
            if errs:
                failures.append(f"edit {entry.get('name')}: {errs[:4]}")

    # Field / parameter path pages
    dp = DotPathIndex(entries, payloads=id_to_payload)
    field_queries = [
        "sendMessage.chat_id",
        "sendMessage.",
        "Message.from",
        "Message.chat.id",
        "Message.chat.",
        "InlineKeyboardButton.web_app",
        "setMyCommands.commands.command",
        "sendmesage.chat",
        "sendMessage.text,parse_mode",
        "Update.*",
        "user.id,first_name,username",
    ]
    for q in field_queries:
        result = dp.search(q, limit=16, offset=0)
        if not result or not result[0]:
            failures.append(f"dotpath {q!r} returned no hits")
            continue
        for _s, entry in result[0][:8]:
            pl = entry.get("field_payload") or encode_field_payload(entry.get("path") or [])
            msg = rich.entry_rich_message(entry, bot_username=BOT, permalink=pl, include_nav=False)
            seen |= check(f"field:{entry.get('name')}", msg, "InputRichMessage", failures)
            if entry.get("kind") == "field" and "details" not in collect_block_types(msg):
                failures.append(f"{entry.get('name')}: single field view is not collapsible")
            check(
                f"field-send:{entry.get('name')}",
                {"chat_id": 1, "rich_message": msg},
                "SendRichMessageParams",
                failures,
            )
            article = rich.inline_article_payload(
                result_id=(pl or entry["name"])[:64],
                title=result_title(entry),
                description=result_description(entry),
                rich=msg,
            )
            check(f"field-inline:{entry.get('name')}", article, "InlineQueryResultArticle", failures)

    # sendMessage layout expectations
    sm_msg = rich.entry_rich_message(
        sm, bot_username=BOT, permalink=id_to_payload[sm["id"]], back="methods"
    )
    sm_types = collect_block_types(sm_msg)
    for needed in ("heading", "paragraph", "details", "buttons"):
        if needed not in sm_types:
            failures.append(f"sendMessage missing {needed} block")
    if "table" in sm_types:
        failures.append("sendMessage used a table")
    if "footer" in sm_types:
        failures.append("sendMessage still has an Official docs/Permalink footer")
    if "pre" in sm_types:
        failures.append("sendMessage compact view should not include the example")
    if "details" not in sm_types:
        failures.append("sendMessage compact required fields are not collapsible")
    nd = count_details(sm_msg)
    if nd < 3:
        failures.append(f"sendMessage compact view should have an outer group plus a details block per required field (got {nd})")
    if nd > 5:
        failures.append(f"sendMessage compact view should not make a details block for every parameter (got {nd})")
    blob = json.dumps(sm_msg)
    if "Official docs" in blob:
        failures.append("sendMessage rich footer still includes Official docs")
    if "chat_id" not in blob or "sendMessage.*" not in blob:
        failures.append("sendMessage compact view missing required names or all-fields hint")
    if "sendMessage(text" in blob:
        failures.append("sendMessage rich view still includes a method signature")
    if "Parameters (16, 2 required)" not in blob:
        failures.append("sendMessage compact heading should be Parameters (16, 2 required)")
    if "Optional:" not in blob or "p_sendMessage__parse_mode" not in blob:
        failures.append("sendMessage compact view should list optional names as deep links")
    if "Required (2" in blob:
        failures.append("sendMessage compact heading should not be Required (2, 2 required)")
    if "t_Message" not in blob:
        failures.append("sendMessage return type is not deep-linked")
    # HTML fallback still exists in parallel
    html = format_message(sm, bot_username=BOT, permalink=id_to_payload[sm["id"]])
    if "sendMessage" not in html or "sendMessage.*" not in html or len(html) > 4096:
        failures.append("HTML fallback for sendMessage looks wrong")
    if "sendMessage(text" in html or "start=t_Message" not in html:
        failures.append("HTML fallback still has a signature or is missing the Message deep link")

    # All-fields and multi-field views use collapsible details/lists, never tables.
    for q in ("sendMessage.text,parse_mode", "Update.*", "getMyName.*"):
        result = dp.search(q, limit=3, offset=0)
        if not result or not result[0]:
            failures.append(f"dotpath {q!r} returned no hits")
            continue
        entry = result[0][0][1]
        msg = rich.entry_rich_message(entry, bot_username=BOT, permalink=entry.get("field_payload"), include_nav=False)
        types = check(f"fieldset:{entry.get('name')}", msg, "InputRichMessage", failures)
        if "details" not in types:
            failures.append(f"{q}: fieldset missing details")
        if "table" in types:
            failures.append(f"{q}: fieldset used a table")
        nfields = len(entry.get("selected_fields") or [])
        ndetails = count_details(msg)
        if nfields and ndetails < nfields:
            failures.append(f"{q}: expected a details block per field, got {ndetails} for {nfields} fields")
        payload = json.dumps(msg)
        if q == "getMyName.*" and (ndetails != 1 or "Parameters (1)" in payload):
            failures.append(f"getMyName.* should show the field details directly, not an outer Parameters group (details={ndetails})")
        if "Official docs" in payload or '"type": "footer"' in payload:
            failures.append(f"{q}: still has an Official docs/Permalink footer")
        seen |= types

    missing_expected = EXPECTED_SOMEWHERE - seen
    # expandable_blockquote is only used for long bodies; ok if rare but we want it if any long FAQ
    if "expandable_blockquote" in missing_expected:
        # not a hard fail if no long bodies exist, but note it
        print("  note: no expandable_blockquote in this catalog pass")
        missing_expected.remove("expandable_blockquote")
    if missing_expected:
        failures.append(f"expected block types never used: {missing_expected}")

    print(f"  validated {len(entries)} entries, {fielded} with fields/params")
    print(f"  block types used: {sorted(seen)}")
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures[:30]:
            print(" -", f)
        if len(failures) > 30:
            print(f" - … and {len(failures) - 30} more")
        return 1
    print("All rich-message schema checks passed (no tables, no live sends).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
