#!/usr/bin/env python3
"""Offline tests for the index, lexical+semantic search, HTML, and latency."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from docsbot.builder import default_index_path, load_index
from docsbot.formatters import (
    close_telegram_html,
    example_snippet,
    format_message,
    result_description,
    result_title,
    sanitize_telegram_html,
)
from docsbot.compose import detect_compose, format_compose_html, make_resolver, parse_query_for_compose
from docsbot.helpguide import help_guide_text, help_page_count, help_page_titles, help_pages
from docsbot.menus import about_text, start_home_text
from docsbot.permalinks import encode_all_fields_payload, encode_multi_fields_payload
from docsbot.permalinks import (
    PAYLOAD_RE,
    decode_search_payload,
    encode_field_payload,
    encode_search_payload,
    parse_start_payload,
)
from docsbot.retrieve import build_engine, parse_offset
from docsbot.search import parse_query

SAMPLES = [
    "sendMessage",
    "InlineKeyboard",
    "Too Many Requests",
    "web app init data",
    "payments",
]

PREFIX_SAMPLES = [
    "m: send",
    "t: User",
    "e: FLOOD",
    "f: payments",
    "w: WebAppInitData",
    "faq: webhook",
]

NATURAL = [
    "how to edit a message",
    "send photo with caption",
    "user blocked bot",
    "get updates long polling",
    "button that opens website",
    "pay with stars",
    "sendMes",
    "inlin kybrd",
]

SHORT = ["s", "se", "ed", "in"]


def show_hits(query: str, engine, limit: int = 8) -> tuple[list, float]:
    t0 = time.perf_counter()
    hits, total, timings = engine.search(query, limit=limit, offset=0)
    ms = (time.perf_counter() - t0) * 1000
    print(f"\n=== {query!r}  ({len(hits)}/{total} shown, {ms:.1f}ms search={timings.get('total_ms', ms):.1f}) ===")
    rows = []
    for i, (score, e) in enumerate(hits, 1):
        msg = format_message(e)
        ok_len = len(msg) <= 4096
        print(
            f"  {i:2}. {score:7.1f}  [{e['kind']:7}]  {e['title']}"
            f"  msg={len(msg)}{' OK' if ok_len else ' TOO LONG'}"
        )
        print(f"      {result_title(e)} — {result_description(e)}")
        rows.append((score, e, msg))
    return rows, ms


def assert_true(cond: bool, msg: str, failures: list) -> None:
    if not cond:
        failures.append(msg)
        print(f"  FAIL: {msg}")
    else:
        print(f"  ok: {msg}")


def names_of(rows, n=8) -> list[str]:
    return [e["name"] for _, e, _ in rows[:n]]


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_index_path()
    if not path.exists():
        print(f"Index not found: {path}\nRun: python build_index.py", file=sys.stderr)
        return 2
    index = load_index(path)
    print(f"Loaded {path}  entries={index.get('entry_count')}  built_at={index.get('built_at')}")

    print("Building search engine…")
    t0 = time.perf_counter()
    engine = build_engine(index)
    print(f"Engine ready in {(time.perf_counter()-t0)*1000:.0f}ms (internal build {engine.build_ms:.0f}ms)")
    print("embeddings:", None if engine.embeddings is None else engine.embeddings.shape)

    failures: list[str] = []

    assert_true(parse_query("m: sendMessage") == ("method", "sendMessage"), "prefix m:", failures)
    assert_true(parse_query("faq: hooks") == ("faq", "hooks"), "prefix faq:", failures)
    assert_true(parse_query("sendMessage") == (None, "sendMessage"), "no prefix", failures)

    # HTML sanitizer: unclosed tags must be closed (this was breaking live inline results).
    raw = '<b>Title</b> hello <a href="https://example.com">link'
    closed = close_telegram_html(raw)
    assert_true(closed.endswith("</a>"), "closes dangling <a>", failures)
    raw2 = "hello <code>sendMessage"
    assert_true("</code>" in close_telegram_html(raw2), "closes dangling <code>", failures)
    long_unclosed = "<b>" + ("x" * 5000) + " <a href='http://x.com'>abc"
    sanitized = sanitize_telegram_html(long_unclosed, max_len=400)
    assert_true(len(sanitized) <= 400, f"sanitize respects max_len ({len(sanitized)})", failures)
    assert_true("<a " not in sanitized or "</a>" in sanitized, "truncated html has no dangling <a>", failures)

    results = {}
    latencies = {}
    for q in SAMPLES + PREFIX_SAMPLES + NATURAL + SHORT:
        rows, ms = show_hits(q, engine)
        results[q] = rows
        latencies[q] = ms

    empty_rows, empty_ms = show_hits("", engine, limit=20)
    results[""] = empty_rows
    latencies[""] = empty_ms

    # Original ranking
    hits = results["sendMessage"]
    assert_true(bool(hits), "sendMessage returned hits", failures)
    if hits:
        top = hits[0][1]
        assert_true(top["kind"] == "method" and top["name"] == "sendMessage", "sendMessage is the top hit", failures)

    names = names_of(results["InlineKeyboard"])
    assert_true(
        any("InlineKeyboard" in n for n in names[:5]),
        f"InlineKeyboard types appear in the top results: {names[:5]}",
        failures,
    )

    hits = results["Too Many Requests"]
    assert_true(bool(hits) and "Too Many Requests" in (hits[0][1].get("title") or ""), "429 is top for Too Many Requests", failures)

    hits = results["web app init data"]
    names = names_of(hits)
    assert_true(
        any("InitData" in n or "Initializing Mini Apps" in n for n in names[:6]),
        f"init data related entries: {names[:6]}",
        failures,
    )

    names = names_of(results["payments"])
    assert_true(any("payment" in n.lower() or "Payment" in n or n == "Payments" for n in names[:8]), f"payments: {names[:8]}", failures)

    hits = results["m: send"]
    assert_true(all(e["kind"] == "method" for _, e, _ in hits), "m: returns only methods", failures)
    hits = results["t: User"]
    assert_true(all(e["kind"] == "type" for _, e, _ in hits) and hits[0][1]["name"] == "User", "t: User exact first", failures)
    hits = results["e: FLOOD"]
    assert_true(bool(hits) and "Too Many Requests" in hits[0][1].get("title", ""), "e: FLOOD → 429", failures)
    hits = results["f: payments"]
    assert_true(all(e["kind"] == "feature" for _, e, _ in hits) and bool(hits), "f: payments finds features", failures)
    hits = results["w: WebAppInitData"]
    assert_true(bool(hits) and hits[0][1]["name"] == "WebAppInitData", "w: WebAppInitData first", failures)
    hits = results["faq: webhook"]
    assert_true(all(e["kind"] == "faq" for _, e, _ in hits) and bool(hits), "faq: webhook finds FAQ", failures)

    # Natural language / typo / partial
    def top_names(q, n=6):
        return [(e["kind"], e["name"]) for _, e, _ in results[q][:n]]

    edit = top_names("how to edit a message")
    assert_true(
        any("editMessage" in n for k, n in edit),
        f"'how to edit a message' should surface editMessage*: {edit}",
        failures,
    )
    photo = top_names("send photo with caption")
    assert_true(
        any(n == "sendPhoto" or "caption" in n.lower() or "Photo" in n for k, n in photo),
        f"'send photo with caption' should surface sendPhoto: {photo}",
        failures,
    )
    blocked = top_names("user blocked bot")
    assert_true(
        any(
            "blocked" in (e.get("title") or "").lower()
            or "blocked" in (e.get("error_message") or "").lower()
            or "blocked" in (e.get("name") or "").lower()
            for _, e, _ in results["user blocked bot"][:6]
        ),
        f"'user blocked bot' should surface the blocked-by-user error: {blocked}",
        failures,
    )
    polling = top_names("get updates long polling")
    assert_true(
        any(n == "getUpdates" or "polling" in n.lower() or "How do I get updates" in n for k, n in polling),
        f"'get updates long polling' should surface getUpdates/FAQ: {polling}",
        failures,
    )
    button = top_names("button that opens website")
    assert_true(
        any(
            "InlineKeyboard" in n or "url" in n.lower() or "keyboard" in n.lower() or "Web App" in n or "web" in n.lower()
            for k, n in button
        ),
        f"'button that opens website' should surface url/inline keyboard: {button}",
        failures,
    )
    stars = top_names("pay with stars")
    assert_true(
        any("star" in n.lower() or "payment" in n.lower() or n == "Payments" or "Invoice" in n for k, n in stars),
        f"'pay with stars' should surface payments/stars: {stars}",
        failures,
    )
    sendmes = top_names("sendMes")
    assert_true(
        any(n == "sendMessage" for k, n in sendmes[:3]),
        f"'sendMes' should prefix-match sendMessage: {sendmes}",
        failures,
    )
    typo = top_names("inlin kybrd")
    assert_true(
        any("InlineKeyboard" in n or n == "Inline Keyboards" for k, n in typo),
        f"'inlin kybrd' should typo-match InlineKeyboard*: {typo}",
        failures,
    )

    # Short auto-suggest prefixes
    for q, expect_prefix in (("s", "s"), ("se", "se"), ("ed", "ed")):
        rows = results[q]
        assert_true(bool(rows), f"{q!r} returns suggestions", failures)
        if rows:
            compact = [e["name"].replace(" ", "").lower() for _, e, _ in rows]
            assert_true(
                any(n.lower().startswith(expect_prefix) or expect_prefix in n.lower() for n in compact[:8]),
                f"{q!r} suggestions start with/contain {expect_prefix!r}: {compact[:8]}",
                failures,
            )

    # Empty query: popular methods/types
    empty_names = names_of(results[""], n=15)
    assert_true(
        "sendMessage" in empty_names and "getUpdates" in empty_names,
        f"empty query suggests popular methods: {empty_names}",
        failures,
    )

    # Message limits + closed tags for heavy entries
    for probe in ("Message", "t: InlineKeyboardMarkup", "e: Too Many Requests", "how to edit a message"):
        hits, total, _ = engine.search(probe, limit=3)
        for score, e in hits:
            msg = format_message(e)
            assert_true(len(msg) <= 4096, f"{probe} / {e['title']} length {len(msg)} <= 4096", failures)
            # crude dangling-open-tag check
            for tag in ("a", "code", "b", "i", "pre", "blockquote"):
                opens = msg.count(f"<{tag}") + msg.count(f"<{tag} ")
                # <a href counts as <a
                opens = msg.count(f"<{tag}>") + msg.count(f"<{tag} ")
                closes = msg.count(f"</{tag}>")
                # allow mismatch only if zero opens
                if opens > closes:
                    assert_true(False, f"{e['title']} unclosed <{tag}> opens={opens} closes={closes}", failures)

    page1, total, _ = engine.search("message", limit=50, offset=0)
    page2, total2, _ = engine.search("message", limit=50, offset=50)
    assert_true(total == total2 and total > 50, f"'message' has more than 50 hits ({total})", failures)
    ids1 = {e["id"] for _, e in page1}
    ids2 = {e["id"] for _, e in page2}
    assert_true(not ids1.intersection(ids2), "pagination pages do not overlap", failures)

    # Walk every page of a large result set and of the empty catalog.
    def walk(query, label):
        seen = set()
        offset = 0
        pages = 0
        last_total = None
        while True:
            hits, tot, _ = engine.search(query, limit=50, offset=offset)
            if last_total is None:
                last_total = tot
            assert_true(tot == last_total, f"{label} total stable at offset {offset}", failures)
            ids = [e["id"] for _, e in hits]
            overlap = seen.intersection(ids)
            assert_true(not overlap, f"{label} page offset {offset} overlaps earlier ids", failures)
            seen.update(ids)
            pages += 1
            if offset + len(hits) >= tot or not hits:
                break
            offset = offset + len(hits)
            if pages > 80:
                assert_true(False, f"{label} walked too many pages", failures)
                break
        assert_true(len(seen) == last_total, f"{label} walked {len(seen)} of {last_total} across {pages} pages", failures)
        return last_total, pages

    n_msg, p_msg = walk("message", "'message'")
    print(f"  pagination walk 'message': {n_msg} hits in {p_msg} pages")
    n_empty, p_empty = walk("", "empty catalog")
    print(f"  pagination walk empty: {n_empty} hits in {p_empty} pages")
    assert_true(n_empty >= 600, f"empty catalog should page through most/all entries ({n_empty})", failures)
    n_methods, p_methods = walk("m:", "m:")
    print(f"  pagination walk m:: {n_methods} hits in {p_methods} pages")
    assert_true(n_methods >= 100, f"m: should page all methods ({n_methods})", failures)

    # Offset parsing
    assert_true(parse_offset("") == 0 and parse_offset(None) == 0, "empty offset -> 0", failures)
    assert_true(parse_offset("50") == 50 and parse_offset("offset=100") == 100, "numeric / offset= offsets", failures)
    assert_true(parse_offset("o:25") == 25 and parse_offset("abc") == 0, "o:25 and junk offsets", failures)
    assert_true(parse_offset("-1") == 0, "negative offset rejected", failures)
    # Engine clamps a huge offset to an empty page with a stable total
    hits_big, tot_big, _ = engine.search("message", limit=50, offset=10_000)
    assert_true(tot_big == n_msg and hits_big == [], f"huge offset is empty, total stays {tot_big}", failures)

    # Permalinks
    payloads = list(engine.id_to_payload.values())
    assert_true(len(payloads) == len(set(payloads)) == len(engine.entries), "permalinks unique and complete", failures)
    assert_true(all(PAYLOAD_RE.fullmatch(p) for p in payloads), "all permalinks match Telegram charset/length", failures)
    sm = engine.by_kind_name.get(("method", "sendMessage"))
    sm_payload = engine.permalink_for(sm) if sm else None
    print(f"  sendMessage permalink: {sm_payload}")
    assert_true(sm_payload == "m_sendMessage", f"readable slug for sendMessage (got {sm_payload})", failures)
    kind, data = parse_start_payload(sm_payload, engine.payload_to_entry)
    assert_true(kind == "entry" and data["name"] == "sendMessage", "decode m_sendMessage", failures)
    # Long error names should still get a short valid payload
    long_err = next((e for e in engine.entries if e["kind"] == "error" and len(e.get("error_message") or "") > 40), None)
    if long_err:
        ep = engine.permalink_for(long_err)
        assert_true(ep and len(ep) <= 64 and PAYLOAD_RE.fullmatch(ep), f"long error permalink ok: {ep}", failures)
        assert_true(engine.entry_for_payload(ep) is long_err, "hash permalink round-trips", failures)

    sp = encode_search_payload("how to edit a message")
    assert_true(sp.startswith("s_") and PAYLOAD_RE.fullmatch(sp), f"search payload {sp}", failures)
    assert_true(decode_search_payload(sp) == "how to edit a message", "search payload round-trips", failures)
    kind, data = parse_start_payload(sp, engine.payload_to_entry)
    assert_true(kind == "search" and data == "how to edit a message", "parse search start payload", failures)
    # Long query still fits 64 chars
    long_q = "x" * 200
    sp_long = encode_search_payload(long_q)
    assert_true(len(sp_long) <= 64 and PAYLOAD_RE.fullmatch(sp_long), "long search payload truncated to 64", failures)
    decoded_long = decode_search_payload(sp_long)
    assert_true(decoded_long and long_q.startswith(decoded_long), "truncated search payload is a prefix", failures)
    assert_true(parse_start_payload("", engine.payload_to_entry)[0] == "home", "empty start payload is home", failures)
    assert_true(parse_start_payload("no_such_page_zzz", engine.payload_to_entry)[0] == "unknown", "unknown payload", failures)

    # Dot-path targeting (sendMessage.chat_id, Message.chat.id, typo parents)
    print("\n==== DOT-PATH ====")
    def path_names(query, limit=50, offset=0):
        hits, total, _ = engine.search(query, limit=limit, offset=offset)
        return [e["name"] for _s, e in hits], total, hits

    names, total, hits = path_names("sendMessage.chat_id")
    print(f"  sendMessage.chat_id -> {names} (total {total})")
    assert_true(names == ["sendMessage.chat_id"], f"exact field {names}", failures)
    assert_true(hits and hits[0][1]["required"] and "Integer or String" in (hits[0][1].get("field_type") or ""), "chat_id type/required", failures)

    names, total, _ = path_names("sendmessage.reply")
    print(f"  sendmessage.reply -> {names}")
    assert_true(
        names[:2] == ["sendMessage.reply_markup", "sendMessage.reply_parameters"] or set(names) >= {"sendMessage.reply_markup", "sendMessage.reply_parameters"},
        f"partial after dot {names}",
        failures,
    )

    names, total, _ = path_names("sendMessage.", limit=50)
    print(f"  sendMessage. -> {total} params, first {names[:4]}")
    assert_true(
        total == 17 and names[0] == "sendMessage.*" and "sendMessage.chat_id" in names and "sendMessage.text" in names,
        f"autosuggest all params plus all-fields pin ({total}) {names}",
        failures,
    )

    names, total, _ = path_names("Message.from")
    print(f"  Message.from -> {names}")
    assert_true(names == ["Message.from"], f"Message.from should be exact, got {names}", failures)

    names, total, _ = path_names("InlineKeyboardButton.web_app")
    assert_true(names == ["InlineKeyboardButton.web_app"], f"web_app field {names}", failures)

    names, total, _ = path_names("Message.chat.id")
    print(f"  Message.chat.id -> {names}")
    assert_true(names == ["Message.chat.id"], f"chained path {names}", failures)
    names, total, _ = path_names("Message.chat.")
    assert_true(
        total == 9 and names[0] == "Message.chat.*" and "Message.chat.id" in names,
        f"Message.chat. lists Chat fields plus all-fields pin ({total}) {names}",
        failures,
    )

    names, total, _ = path_names("setMyCommands.commands.command")
    assert_true(names == ["setMyCommands.commands.command"], f"Array of X unwrap {names}", failures)

    names, total, _ = path_names("sendmesage.chat")
    print(f"  sendmesage.chat -> {names}")
    assert_true(names and names[0] == "sendMessage.chat_id", f"fuzzy parent typo {names}", failures)

    names, total, _ = path_names("m: sendMessage.chat_id")
    assert_true(names == ["sendMessage.chat_id"], f"prefix + path {names}", failures)

    # Pagination over Message. (120 fields)
    seen = set()
    off = 0
    pages = 0
    while off < 200:
        page, tot, _ = engine.search("Message.", limit=50, offset=off)
        if not page:
            break
        ids = [e["name"] for _s, e in page]
        assert_true(not (set(ids) & seen), f"page overlap at {off}", failures)
        seen.update(ids)
        off += len(page)
        pages += 1
    print(f"  Message. pagination: {len(seen)}/{tot} in {pages} pages")
    assert_true(len(seen) == tot == 121, f"Message. should page all-fields plus 120 fields, got {len(seen)}/{tot}", failures)
    assert_true("Message.*" in seen, "Message. pins an all-fields result", failures)

    # Permalinks
    fp = encode_field_payload(["sendMessage", "chat_id"])
    assert_true(fp == "p_sendMessage__chat_id" and PAYLOAD_RE.fullmatch(fp), f"readable field permalink {fp}", failures)
    kind, data = parse_start_payload(fp, engine.payload_to_entry)
    assert_true(kind == "field" and data == fp, f"parse field payload {kind} {data}", failures)
    resolved = engine.entry_for_payload(fp)
    assert_true(resolved and resolved.get("name") == "sendMessage.chat_id", "p_sendMessage__chat_id round-trips", failures)
    assert_true(engine.permalink_for(resolved) == fp, "permalink_for field entry", failures)
    cp = encode_field_payload(["Message", "chat", "id"])
    assert_true(cp == "p_Message__chat__id", f"chained permalink {cp}", failures)
    assert_true(engine.entry_for_payload(cp) and engine.entry_for_payload(cp)["name"] == "Message.chat.id", "chained permalink resolves", failures)

    # HTML fallback + regular search still works
    field_entry = resolved
    fhtml = format_message(field_entry, bot_username="autoblogcarpperbot", permalink=fp)
    print("  field HTML:", fhtml[:200].replace("\n", " | "))
    assert_true("sendMessage.chat_id" in fhtml and "Official docs" not in fhtml and "Permalink" not in fhtml, "field HTML has heading and no text footer", failures)
    assert_true(len(fhtml) <= 4096, "field HTML fits", failures)
    chat_hits, _, _ = engine.search("Message.chat", limit=1, offset=0)
    chat_entry = chat_hits[0][1]
    chat_html = format_message(chat_entry, bot_username="autoblogcarpperbot", permalink=engine.permalink_for(chat_entry))
    assert_true("start=t_Chat" in chat_html and "Chat" in chat_html, "field type Chat is a deep link", failures)
    assert_true("<blockquote expandable>" in chat_html, "single field HTML is expandable", failures)
    # A non-path query must not be hijacked
    hits, total, _ = engine.search("sendMessage", limit=5, offset=0)
    assert_true(hits and hits[0][1].get("kind") == "method" and hits[0][1].get("name") == "sendMessage", "bare sendMessage still returns the method", failures)

    # Rich formatting
    msg = format_message(sm, bot_username="autoblogcarpperbot", permalink=sm_payload)
    print("\n==== sendMessage formatted (first 700) ====\n", msg[:700], "\n==== end ====")
    assert_true("<blockquote expandable>" in msg and "chat_id" in msg, "compact required fields are expandable", failures)
    assert_true('<pre><code class="language-json">' not in msg, "compact method view omits the example", failures)
    assert_true("sendMessage(text" not in msg, "compact view has no method signature line", failures)
    assert_true("sendMessage" in msg and "chat_id" in msg and "sendMessage.*" in msg, "compact view has required names and all-fields hint", failures)
    assert_true("Parameters (16, 2 required)" in msg, "compact heading shows total and required count", failures)
    assert_true("Optional:" in msg and "parse_mode" in msg and "p_sendMessage__parse_mode" in msg, "optional names are listed and deep-linked", failures)
    assert_true("Required (2" not in msg, "heading is not Required (2, 2 required)", failures)
    assert_true("returns" in msg and "start=t_Message" in msg, "return type Message is a deep link", failures)
    assert_true("Official docs" not in msg and "Permalink" not in msg and "start=m_sendMessage" not in msg, "entry HTML has no Official docs/Permalink footer", failures)
    assert_true(len(msg) <= 4096, f"rich sendMessage length {len(msg)}", failures)
    # heavy type still compact and valid
    msg_type = format_message(engine.by_kind_name[("type", "Message")], bot_username="autoblogcarpperbot", permalink=engine.permalink_for(engine.by_kind_name[("type", "Message")]))
    assert_true(len(msg_type) <= 4096 and "Message.*" in msg_type and "Fields (120," in msg_type, "Message type heading shows the total field count", failures)
    gu = engine.by_kind_name.get(("method", "getUpdates"))
    gu_html = format_message(gu, bot_username="autoblogcarpperbot", permalink=engine.permalink_for(gu))
    assert_true("Array of" in gu_html and "start=t_Update" in gu_html, "Array of Update links Update", failures)
    assert_true("getUpdates(" not in gu_html, "getUpdates has no signature line", failures)
    # A union return: Message or Boolean — Message is linked, Boolean stays plain.
    union = next((e for e in engine.entries if e.get("kind") == "method" and (e.get("returns") or "") == "Message or Boolean"), None)
    if union:
        uhtml = format_message(union, bot_username="autoblogcarpperbot", permalink=engine.permalink_for(union))
        assert_true("start=t_Message" in uhtml and "Boolean" in uhtml and "start=t_Boolean" not in uhtml, "union returns link Message and leave Boolean plain", failures)
    err429 = next(e for e in engine.entries if e.get("title") == "429 Too Many Requests")
    msg_err = format_message(err429, bot_username="autoblogcarpperbot", permalink=engine.permalink_for(err429))
    assert_true("error_code" in msg_err and "language-json" in msg_err, "error example JSON present", failures)
    assert_true(example_snippet(sm) and example_snippet(sm)[0] == "json", "example_snippet for methods", failures)

    # /start copy
    home = start_home_text("autoblogcarpperbot", count=1775, version="10.3")
    about = about_text("autoblogcarpperbot", count=1775, version="10.3")
    print("\n==== /start text ====\n", home, "\n==== end ====")
    assert_true("Search docs" in home or "sendMessage" in home, "home text mentions search", failures)
    assert_true("10.3" in home and "Bot API 10.3" in home, "/start mentions Bot API version", failures)
    assert_true("@autoblogcarpperbot" in home and "Mini Apps" in home and "FAQ" in home, "/start explains coverage and uses the real username", failures)
    assert_true("How to use" in home and "any chat" in home and "/help" in home, "/start has how-to, any-chat, and /help", failures)
    for needle in ("sendMessage.text", "sendMessage.text,parse_mode", "Update.*", "e: FLOOD", "$sendMessage"):
        assert_true(needle in home, f"/start example includes {needle!r}", failures)
    assert_true("10.3" in about and ("1,775" in about or "1775" in about), "about mentions version and count", failures)

    # Search Guide pages
    titles = help_page_titles()
    print(f"  help page titles: {titles}")
    assert_true(help_page_count() == 5 and len(titles) == 5, "Search Guide has 5 pages", failures)
    all_help = "\n".join(help_guide_text(i, "autoblogcarpperbot") for i in range(1, 6))
    assert_true("Search Guide — Page 1/5" in help_guide_text(1, "autoblogcarpperbot"), "guide title format", failures)
    assert_true("@autoblogcarpperbot" in all_help and "TgDocsXBot" not in all_help, "examples use the real bot username", failures)
    for needle in (
        "Basic Search", "Kind Prefixes", "Property Search", "Multiple Fields Search",
        "All Fields Search", "Chained Paths", "Errors Search", "Deep Links", "Text Linking",
        "Paging", "sendMessage.text", "sendMessage.text,parse_mode", "Update.*",
        "Message.chat.id", "f: Hey use $sendMessage",
    ):
        assert_true(needle in all_help, f"guide covers {needle!r}", failures)

    # Multiple fields + all fields
    hits, total, _ = engine.search("sendMessage.text,parse_mode", limit=5, offset=0)
    assert_true(
        total == 1 and hits and hits[0][1].get("kind") == "fieldset" and hits[0][1].get("name") == "sendMessage.text,parse_mode",
        f"multi-field search { [e.get('name') for _s,e in hits] }",
        failures,
    )
    multi = hits[0][1]
    multi_html = format_message(multi, bot_username="autoblogcarpperbot", permalink=engine.permalink_for(multi))
    assert_true(
        "<blockquote expandable>" in multi_html and "parse_mode" in multi_html and "required" in multi_html and "optional" in multi_html,
        "multi-field HTML lists both fields",
        failures,
    )
    pm = encode_multi_fields_payload("sendMessage", ["text", "parse_mode"])
    assert_true(pm == "pm_sendMessage__text__parse_mode", f"multi permalink {pm}", failures)
    assert_true(engine.permalink_for(multi) == pm, "permalink_for multi entry", failures)
    assert_true(engine.entry_for_payload(pm) and engine.entry_for_payload(pm).get("name") == "sendMessage.text,parse_mode", "pm_ round-trip", failures)

    hits, total, _ = engine.search("Update.*", limit=3, offset=0)
    assert_true(
        total == 1 and hits and hits[0][1].get("kind") == "fieldset" and hits[0][1].get("name") == "Update.*" and len(hits[0][1].get("selected_fields") or []) >= 20,
        "Update.* is an all-fields result",
        failures,
    )
    all_entry = hits[0][1]
    all_html = format_message(all_entry, bot_username="autoblogcarpperbot", permalink=engine.permalink_for(all_entry))
    assert_true(
        "update_id" in all_html and "message" in all_html and "<blockquote expandable>" in all_html and "Official docs" not in all_html and "start=pa_Update" not in all_html,
        "Update.* HTML lists fields and has no text footer",
        failures,
    )
    assert_true(encode_all_fields_payload("Update") == "pa_Update", "pa_Update permalink", failures)
    assert_true(engine.entry_for_payload("pa_Update") and engine.entry_for_payload("pa_Update").get("name") == "Update.*", "pa_ round-trip", failures)

    hits, total, _ = engine.search("getMyName.*", limit=1, offset=0)
    assert_true(hits and hits[0][1].get("kind") == "fieldset", "getMyName.* is a fieldset", failures)
    gn = hits[0][1]
    gn_html = format_message(gn, bot_username="autoblogcarpperbot", permalink=engine.permalink_for(gn))
    print("  getMyName.* HTML:", gn_html.replace("\n", " | "))
    assert_true("Parameters (1)" not in gn_html and "<b>Parameters</b>" not in gn_html, "single-field .* has no outer Parameters group", failures)
    assert_true("language_code" in gn_html and "<blockquote expandable>" in gn_html, "getMyName.* shows the field details directly", failures)
    assert_true("Official docs" not in gn_html, "getMyName.* has no text footer", failures)

    hits, total, _ = engine.search("sendMessage.tex,parse_mod", limit=1, offset=0)
    assert_true(
        hits and hits[0][1].get("name") == "sendMessage.text,parse_mode",
        "multi-field is typo-tolerant per field",
        failures,
    )
    hits, total, _ = engine.search("user.id,first_name,username", limit=1, offset=0)
    assert_true(hits and hits[0][1].get("name") == "User.id,first_name,username", "multi-field is case-insensitive", failures)

    # Compose / text linking
    assert_true(parse_query_for_compose("f: Hey use $sendMessage")[0], "f: plus $ is compose", failures)
    assert_true(not parse_query_for_compose("f: payments")[0], "f: without $ stays features", failures)
    assert_true(detect_compose("See $sendMessage and more") == "See $sendMessage and more", "token plus text is compose", failures)
    assert_true(detect_compose("$sendMessage") is None, "bare $token is not compose", failures)
    resolver = make_resolver(engine)
    html, resolved, unresolved = format_compose_html(
        "Hey use *$sendamessage* and $Message.chat plus $NotAThing",
        resolver=resolver,
        bot_username="autoblogcarpperbot",
    )
    print("  compose HTML:", html)
    assert_true("start=m_sendMessage" in html and "start=p_Message__chat" in html, "compose links resolved tokens", failures)
    assert_true("<b>" in html and "sendMessage" in html, "compose keeps bold around a token", failures)
    assert_true("$NotAThing" in html and "$NotAThing" in unresolved, "unresolved tokens stay plain", failures)
    assert_true(resolved == ["sendMessage", "Message.chat"], f"resolved names {resolved}", failures)

    print("\n==== LATENCY (search only, ms) ====")
    over = []
    for q, ms in latencies.items():
        flag = ""
        if ms >= 300:
            flag = "  SLOW"
            over.append((q, ms))
        print(f"  {ms:6.1f}  {q!r}{flag}")
    assert_true(not over, f"all tested queries should be <300ms, slow={over}", failures)
    # Warm-cache average of natural queries
    warm = []
    for q in NATURAL + SHORT + ["sendMessage", ""]:
        t0 = time.perf_counter()
        engine.search(q, limit=50, offset=0)
        warm.append((time.perf_counter() - t0) * 1000)
    print(f"  warm avg {sum(warm)/len(warm):.1f}ms  max {max(warm):.1f}ms  n={len(warm)}")
    assert_true(max(warm) < 300, f"warm max {max(warm):.1f}ms < 300", failures)

    print("\n==== SUMMARY ====")
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(" -", f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
