# Telegram Bot API docs bot

An inline Telegram bot that searches the official Bot API documentation: methods, types, errors, features, Mini Apps, and the FAQ. The catalog is built from `https://corefork.telegram.org/bots/api.json` and `errors.json`, plus the pages at [core.telegram.org/bots](https://core.telegram.org/bots), [/features](https://core.telegram.org/bots/features), [/webapps](https://core.telegram.org/bots/webapps), and [/faq](https://core.telegram.org/bots/faq). Type `@autoblogcarpperbot sendMessage` in any chat to search.

## Features

- Fuzzy, typo-tolerant search with BM25, character n-grams, and MiniLM semantic ranking (names, descriptions, and field text).
- Kind prefixes: `m:` methods, `t:` types, `e:` errors, `f:` features, `w:` Mini Apps, `faq:` FAQ, `fact:` platform facts.
- Paging through all matches (50 inline results per page, via `next_offset`).
- Property search (`sendMessage.text`), multiple fields (`sendMessage.text,parse_mode`), and all fields (`Update.*` or a trailing `sendMessage.`).
- Chained paths such as `Message.chat.id` (`Array of X` resolves to `X`).
- Text linking: `f:` plus `$tokens`, or a `$token` plus other text, sends formatted copy with deep links to matched entries. `f:` without a `$` stays a features search.
- Deep links (`https://t.me/autoblogcarpperbot?start=m_sendMessage`) and Permalink buttons. Return types and field types link to their catalog entries when they exist.
- API doc entries and inline results are Bot API 10.x rich messages (`sendRichMessage` / `InputRichMessageContent`), with an HTML fallback. Command pages (`/start`, `/help`, menus) are normal HTML messages.
- `/help` is a paged Search Guide, edited in place.
- The index is rebuilt automatically on a schedule (24 hours by default) while the bot is running. If no index is present at startup, one is built then.

## Usage

Type the bot username in any chat, then a query:

```
@autoblogcarpperbot sendMessage
@autoblogcarpperbot sendMessage.text
@autoblogcarpperbot sendMessage.text,parse_mode
@autoblogcarpperbot Update.*
@autoblogcarpperbot Message.chat.id
@autoblogcarpperbot e: FLOOD
@autoblogcarpperbot f: payments
@autoblogcarpperbot f: Hey use $sendMessage and $Message.chat
```

`/start` opens a short menu. `/help` opens the Search Guide.

## Setup

Python 3.10 or later is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

1. In [@BotFather](https://t.me/BotFather), send `/newbot` and copy the token.
2. Enable inline mode: send `/setinline`, choose the bot, and set a placeholder such as `Search Bot API docs…`. Without this, Telegram will not deliver inline queries.
3. Copy the env file and set the token (do not commit `.env`):

```bash
cp .env.example .env
```

4. Optionally pre-build the catalog (otherwise the bot builds it on first start):

```bash
python build_index.py
```

5. Run the bot:

```bash
python bot.py
```

To run it in the background:

```bash
nohup .venv/bin/python bot.py > bot.log 2>&1 &
```

`build_index.py` and the test scripts do not need a bot token. They work offline once `data/index.json` exists.

## Configuration

Values are read from `.env` (which overrides a pre-existing environment) or the process environment.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOT_TOKEN` | (required) | Token from @BotFather. Also accepted as `--token`. |
| `INDEX_PATH` | `data/index.json` | Catalog path. Also accepted as `--index`. |
| `REFRESH_HOURS` | `24` | Hours between automatic rebuilds. `0` disables. Also accepted as `--refresh-hours`. |
| `LOG_LEVEL` | `INFO` | Logging level. |
| `FASTEMBED_CACHE_PATH` | `data/models` | Cache directory for the MiniLM embedding model. |

## Project structure

```
bot.py                 Telegram application: inline queries, /start, /help
build_index.py         Fetch sources and write data/index.json
docsbot/builder.py     Source fetchers and catalog construction
docsbot/search.py      Query prefixes and lexical scoring
docsbot/retrieve.py    Blended search (BM25, n-grams, embeddings)
docsbot/dotpath.py     Property, multi-field, all-fields, and chained paths
docsbot/compose.py     $token text linking
docsbot/formatters.py  HTML fallback for entries and compact views
docsbot/rich.py        sendRichMessage / InputRichMessageContent layouts
docsbot/helpguide.py   Paged /help Search Guide
docsbot/menus.py       /start and menu copy
docsbot/permalinks.py  Deep-link payloads
docsbot/typelinks.py   Type-name links in return and field types
test_search.py         Offline search, formatting, and path checks
test_rich.py           Offline rich-message schema checks (no live sends)
data/index.json        Generated catalog
.env.example           Environment template
```

## Tests

No bot token is required.

```bash
python build_index.py    # if data/index.json is not already present
python test_search.py
python test_rich.py
```
