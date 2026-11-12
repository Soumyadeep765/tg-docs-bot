"""Paginated Search Guide shown by /help (normal HTML messages)."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .textutil import escape

DEFAULT_BOT = "autoblogcarpperbot"


def _mention(bot_username: str | None) -> str:
    name = (bot_username or DEFAULT_BOT).lstrip("@")
    return f"@{name}"


def _ex(mention: str, query: str) -> str:
    return f"• <code>{escape(mention)} {escape(query)}</code>"


def _page(
    mention: str,
    *sections: tuple[str, str, list[str], str],
) -> str:
    """sections: (title, use_line, examples, returns_line)."""
    parts: list[str] = []
    for title, use, examples, returns in sections:
        block = f"<b>{escape(title)}</b>\n{use}"
        if examples:
            block += "\n<b>Examples:</b>\n" + "\n".join(examples)
        if returns:
            block += f"\n\n{returns}"
        parts.append(block)
    return "\n\n".join(parts)


def help_pages(bot_username: str | None = None) -> list[str]:
    m = _mention(bot_username)
    return [
        _page(
            m,
            (
                "Basic Search",
                "Type a method, type, or phrase after the bot’s username. Matching is case-insensitive and typo-tolerant, and also uses descriptions.",
                [
                    _ex(m, "sendMessage"),
                    _ex(m, "sendmesage"),
                    _ex(m, "how to edit a message"),
                ],
                "Basic search returns ranked suggestions that refine as you type.",
            ),
            (
                "Paging",
                "A page shows up to 50 results. Scroll down in the suggestion list to load more.",
                [
                    _ex(m, "message"),
                    _ex(m, "m:"),
                ],
                "Paging walks through every matching entry, not just the first screen.",
            ),
        ),
        _page(
            m,
            (
                "Kind Prefixes",
                "Use a prefix to limit results to one kind.",
                [
                    _ex(m, "m: send"),
                    _ex(m, "t: User"),
                    _ex(m, "e: FLOOD"),
                    _ex(m, "f: payments"),
                    _ex(m, "w: init data"),
                    _ex(m, "faq: webhook"),
                    _ex(m, "fact: privacy"),
                ],
                "Prefixes: <code>m:</code> methods, <code>t:</code> types, <code>e:</code> errors, <code>f:</code> features, <code>w:</code> Mini Apps, <code>faq:</code> FAQ, <code>fact:</code> platform facts. <code>f:</code> is features unless the text also contains a <code>$token</code> (see Text Linking).",
            ),
            (
                "Errors Search",
                "Search error titles, HTTP codes, or related method names. Or filter with <code>e:</code>.",
                [
                    _ex(m, "Too Many Requests"),
                    _ex(m, "e: 429"),
                    _ex(m, "e: FLOOD"),
                ],
                "Error results show the HTTP code, description, and related methods when known.",
            ),
        ),
        _page(
            m,
            (
                "Property Search",
                "Use the format <code>method.property</code> or <code>type.property</code>.",
                [
                    _ex(m, "sendMessage.text"),
                    _ex(m, "message.date"),
                    _ex(m, "user.first_name"),
                ],
                "Property search returns details for a single specific field, including type, description, and whether it is optional.",
            ),
            (
                "Chained Paths",
                "Walk through object types with <code>Type.field.subfield</code>. <code>Array of X</code> resolves to <code>X</code>.",
                [
                    _ex(m, "Message.chat.id"),
                    _ex(m, "Message.from.id"),
                    _ex(m, "InlineKeyboardButton.web_app.url"),
                ],
                "A chained path returns the final field, with a link back to the parent type or method.",
            ),
        ),
        _page(
            m,
            (
                "Multiple Fields Search",
                "Use <code>method.field1,field2,field3</code> to see several specific fields.",
                [
                    _ex(m, "sendMessage.text,parse_mode"),
                    _ex(m, "user.id,first_name,username"),
                    _ex(m, "Update.update_id,message"),
                ],
                "Multiple fields search shows all requested fields in one organized result.",
            ),
            (
                "All Fields Search",
                "Use <code>method.*</code> or <code>Type.*</code> to see every field at once. A trailing dot (<code>sendMessage.</code>) still lists each field as a suggestion, with an all-fields result first.",
                [
                    _ex(m, "Update.*"),
                    _ex(m, "sendMessage.*"),
                    _ex(m, "user.*"),
                ],
                "All fields search shows a complete list of all parameters with their types and descriptions.",
            ),
        ),
        _page(
            m,
            (
                "Deep Links",
                "Share a page with <code>https://t.me/bot?start=…</code>. Method and type slugs look like <code>m_sendMessage</code>; fields use <code>p_sendMessage__text</code>.",
                [
                    f"• <code>https://t.me/{escape(m.lstrip('@'))}?start=m_sendMessage</code>",
                    f"• <code>https://t.me/{escape(m.lstrip('@'))}?start=p_sendMessage__text</code>",
                    f"• <code>https://t.me/{escape(m.lstrip('@'))}?start=pa_sendMessage</code>",
                ],
                "Opening a start link in a private chat shows that entry. Each result includes a Permalink button.",
            ),
            (
                "Text Linking",
                "Write a message with <code>$tokens</code> and send it as formatted text. The safest trigger is <code>f:</code> plus <code>$Name</code> (this does not run a features search). A query is also treated as compose when it contains a <code>$token</code> and other text. <code>*bold*</code>, <code>_italic_</code>, and <code>`code`</code> are supported.",
                [
                    _ex(m, "f: Hey use $sendMessage and $Message.chat"),
                    _ex(m, "See *$sendMessage* and `_Message.from_`"),
                ],
                "Each resolved token becomes a link to this bot’s deep link for that entry. Unresolved tokens stay as plain text.",
            ),
        ),
    ]


def help_page_count(bot_username: str | None = None) -> int:
    return len(help_pages(bot_username))


def help_guide_text(page: int, bot_username: str | None = None) -> str:
    pages = help_pages(bot_username)
    n = len(pages)
    i = min(max(int(page or 1), 1), n)
    return f"<b>Search Guide — Page {i}/{n}</b>\n\n{pages[i - 1]}"


def help_guide_markup(page: int, bot_username: str | None = None) -> InlineKeyboardMarkup:
    n = help_page_count(bot_username)
    i = min(max(int(page or 1), 1), n)
    nav: list[InlineKeyboardButton] = []
    if i > 1:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"guide:{i - 1}"))
    if i < n:
        nav.append(InlineKeyboardButton("Next", callback_data=f"guide:{i + 1}"))
    rows: list[list[InlineKeyboardButton]] = []
    if nav:
        rows.append(nav[:2])
    rows.append(
        [
            InlineKeyboardButton("Search", switch_inline_query_current_chat=""),
            InlineKeyboardButton("Home", callback_data="menu:home"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def help_page_titles(bot_username: str | None = None) -> list[str]:
    """Section titles in page order, for tests and reports."""
    titles = [
        "Basic Search + Paging",
        "Kind Prefixes + Errors Search",
        "Property Search + Chained Paths",
        "Multiple Fields Search + All Fields Search",
        "Deep Links + Text Linking",
    ]
    # Keep in sync with help_pages().
    assert len(titles) == help_page_count(bot_username)
    return titles
