# Telegram Bot API docs bot

An inline Telegram bot that searches the official Bot API reference, errors, platform facts, features, Mini Apps / Web Apps docs, and the Bot FAQ.

Type `@your_bot sendMessage` in any chat to get formatted results with a deep link back to the official docs.

## Features

- **Inline search** (`@bot <query>`) over a local index, with fuzzy / ranked matching and exact-name hits first.
- **Prefixes** to filter by kind:
  - `m:` methods — `m: sendMessage`
  - `t:` types — `t: InlineKeyboard`
  - `e:` errors — `e: Too Many Requests`
  - `f:` features — `f: payments`
  - `w:` Mini Apps / Web Apps — `w: init data`
  - `faq:` FAQ — `faq: webhooks`
  - `fact:` intro / platform facts from [core.telegram.org/bots](https://core.telegram.org/bots)
- Empty query shows a short help / hint result.
- Private-chat pages (`/start`, menus, deep links) are sent as **Bot API 10.x rich messages** (`sendRichMessage` / `editMessageText` with `rich_message`): headings, paragraphs, lists, `<details>`-style collapsible sections for parameters and fields, preformatted examples, footers, and button rows. If a rich call fails, the bot falls back to HTML.
- Inline results use `InputRichMessageContent` when the API accepts it; otherwise the existing HTML article is used.
- `/start` and `/help` in a private chat, with a browse menu.
- The index is built by a separate script and **refreshed automatically every 24 hours** when the bot is running.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1. Build the index

```bash
python build_index.py
```

This fetches the sources below and writes `data/index.json`. You can also point it at local files:

```bash
python build_index.py --api-json api.json --errors-json errors.json --html-dir ./html
```

### 2. Create the bot and enable inline mode

1. Talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. **Enable inline mode:** send `/setinline` to @BotFather, pick the bot, and set a placeholder such as `Search Bot API docs…`.
   Without this step Telegram will not send inline queries to your bot.
3. Optional: `/setinlinefeedback` if you want chosen-result feedback (not required).

### 3. Configure the token and run

```bash
cp .env.example .env
# edit .env and set BOT_TOKEN=...

python bot.py
```

`BOT_TOKEN` can also be set in the environment. The bot is not useful until a token is provided; the index builder and `test_search.py` run fully offline once an index exists.

Optional environment variables (see `.env.example`):

- `INDEX_PATH` — index file (default `data/index.json`)
- `REFRESH_HOURS` — rebuild interval (default `24`; `0` disables)
- `LOG_LEVEL` — logging level

## Data sources

| Kind | Source |
| --- | --- |
| Methods, types, parameters / fields | `https://corefork.telegram.org/bots/api.json` (OpenAPI 3 schema of the Bot API) |
| Errors (HTTP code, message, related methods, MTProto codes) | `https://corefork.telegram.org/bots/errors.json` |
| Platform facts | [https://core.telegram.org/bots](https://core.telegram.org/bots) |
| Features | [https://core.telegram.org/bots/features](https://core.telegram.org/bots/features) |
| Mini Apps / Web Apps | [https://core.telegram.org/bots/webapps](https://core.telegram.org/bots/webapps) |
| FAQ | [https://core.telegram.org/bots/faq](https://core.telegram.org/bots/faq) |

HTML pages are split into searchable entries by `h3` / `h4` / `h5` heading anchors. Each entry keeps a deep link of the form `https://core.telegram.org/bots/…#anchor`.

**Notes on the data sources:**

- The `http://` form of the corefork URLs (`http://corefork.telegram.org/bots/api.json` and `errors.json`) currently returns **HTTP 500**. The builder uses **HTTPS**, which works.
- `api.json` is an **OpenAPI 3.2** document (methods under `paths`, types under `components.schemas`). Method parameters live in `*Params` schemas; HTTP error envelope schemas (`BadRequest`, `TooManyRequests`, etc.) are skipped because they are not documented Bot API types.
- `errors.json` is grouped by HTTP status (`400`, `403`, `429`, `500`). Each message maps to a list of related methods, optional response `parameters` (e.g. `retry_after`), and sometimes MTProto codes plus descriptions. There is no `FLOOD_WAIT` string in this file; the Bot API exposes that condition as `429 Too Many Requests: retry after %d`. The indexer adds “flood / FLOOD_WAIT / rate limit” as search aliases for that entry.
- The corefork snapshot used when this README was written was **Bot API 10.3**. Official core.telegram.org pages may be slightly ahead.

## Search behavior

- Inline suggestions appear and refine on **every keystroke** (`cache_time` is 2 seconds). 1–2 character queries return prefix matches (with popular methods such as `sendMessage` boosted). An empty query lists popular methods and types plus a short help card. `/start` includes a **Search docs** button (`switch_inline_query_current_chat`).
- **Dot-path targeting:** type `sendMessage.chat_id`, `sendmessage.reply`, or `Message.chat.id` to jump to a parameter or field (case-insensitive, typo-tolerant on the parent, prefix/fuzzy after the last dot). `sendMessage.` lists every parameter; `Array of X` chains through `X`. Field pages have their own permalinks (`p_sendMessage__chat_id`).
- Optional kind prefix is stripped first (`m:`, `t:`, `e:`, `f:`, `w:`, `faq:`, `fact:`).
- Search covers names, descriptions, field/parameter names and descriptions, error texts, and HTML section bodies.
- Identifiers are split for matching (`WebAppInitData` ↔ `web app init data`, `sendMessage` ↔ `send message`). Typo-tolerant matching covers queries such as `sendMes` or `inlin kybrd`.
- Ranking: exact name (case-insensitive) first, then name prefix / contains, then a blend of [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz), **BM25**, hashed character n-grams (typos), and **MiniLM** embeddings (`sentence-transformers/all-MiniLM-L6-v2` via fastembed, precomputed into `data/embeddings.npz`). Natural-language queries such as “how to edit a message” use the semantic channels. If the embedding model cannot be loaded, BM25 + char n-grams still run.
- Up to **50** inline results per page; further pages use Telegram’s `next_offset`. An empty query pages through the whole catalog (popular entries first). Rich-message layouts use collapsible **details** (not tables) for parameters/fields, plus lists and a preformatted JSON example. The HTML fallback is sanitized and kept under the **4096** character limit.
- Deep links: `https://t.me/<bot>?start=m_sendMessage` (or a short `eh_…` hash for long names) opens that page in a private chat. `start=s_<base64url>` shares a search. Every result includes a **Permalink** button.
- `/start` shows a short menu with Search / Methods / Types / Errors / Features / Mini Apps / FAQ / How to use / About. `/help` covers usage. The bot sets its commands and public description at startup.

## Tests (offline, no bot token)

```bash
python build_index.py        # if you don't already have data/index.json
python test_search.py
python test_rich.py     # schema-validate rich-message payloads (no live sends)
```

Sample queries exercised by the test: `sendMessage`, `InlineKeyboard`, `Too Many Requests`, `web app init data`, `payments`, plus prefix filters.

## Project layout

```
build_index.py     Fetch sources and write data/index.json
bot.py             python-telegram-bot v21 application (inline + /start + /help)
test_search.py     Offline ranking / formatting checks
test_rich.py       Offline Bot API 10.x rich-message schema checks
docsbot/           Index builder, search, HTML + rich-message formatting
data/index.json    Generated catalog (created by build_index.py)
```
