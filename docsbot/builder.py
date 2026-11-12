"""Fetch official Telegram Bot API sources and build a local search index."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from .textutil import first_sentence, html_fragment_to_telegram, plain_text_from_html_nodes, slug_id, split_ident

log = logging.getLogger("docsbot.builder")

API_JSON_URL = "https://corefork.telegram.org/bots/api.json"
ERRORS_JSON_URL = "https://corefork.telegram.org/bots/errors.json"
# HTTP (as listed in some docs) currently returns 500; HTTPS works.
HTML_SOURCES = [
    {
        "kind": "fact",
        "url": "https://core.telegram.org/bots",
        "title": "Bots: An introduction for developers",
    },
    {
        "kind": "feature",
        "url": "https://core.telegram.org/bots/features",
        "title": "Telegram Bot Features",
    },
    {
        "kind": "webapp",
        "url": "https://core.telegram.org/bots/webapps",
        "title": "Telegram Mini Apps",
    },
    {
        "kind": "faq",
        "url": "https://core.telegram.org/bots/faq",
        "title": "Bots FAQ",
    },
]
DOCS_API_BASE = "https://core.telegram.org/bots/api"
USER_AGENT = "tg-docs-bot/1.0 (docs index builder; +https://core.telegram.org/bots)"
HEADING_TAGS = ("h3", "h4", "h5")
TIMEOUT = 60.0

# OpenAPI helper aliases that are not documented as Bot API types.
SKIP_SCHEMAS = {"ChatId", "FileInput", "UploadInput"}
# HTTP error envelope schemas (ok / error_code / description) from the OpenAPI spec.
ERROR_ENVELOPE_FIELDS = {"ok", "error_code"}

ALIAS_TYPES = {
    "ChatId": "Integer or String",
    "FileInput": "InputFile or String",
    "UploadInput": "InputFile or String",
}


def default_index_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "index.json"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_bytes(url: str, client: httpx.Client) -> tuple[bytes, str]:
    log.info("Fetching %s", url)
    resp = client.get(url, follow_redirects=True)
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    return resp.content, ctype


def is_error_envelope(schema: dict[str, Any] | None) -> bool:
    """True for OpenAPI HTTP error objects that are not documented Bot API types."""
    if not schema:
        return False
    props = set((schema.get("properties") or {}).keys())
    if ERROR_ENVELOPE_FIELDS <= props and not (schema.get("description") or "").strip():
        return True
    return False


def type_name(schema: dict[str, Any] | None, resolve_aliases: bool = True) -> str:
    if not schema:
        return "unknown"
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if resolve_aliases and name in ALIAS_TYPES:
            return ALIAS_TYPES[name]
        return name
    if "anyOf" in schema or "oneOf" in schema:
        parts = [type_name(p, resolve_aliases) for p in schema.get("anyOf") or schema.get("oneOf") or []]
        # de-duplicate while preserving order
        seen: set[str] = set()
        uniq = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return " or ".join(uniq)
    if "allOf" in schema and "type" not in schema:
        # Rare; try to combine object pieces, else fall through.
        refs = [type_name(p, resolve_aliases) for p in schema["allOf"] if "$ref" in p or "type" in p]
        if refs:
            return " + ".join(refs)
    typ = schema.get("type")
    if typ == "array":
        return f"Array of {type_name(schema.get('items') or {}, resolve_aliases)}"
    if typ == "integer":
        return "Integer"
    if typ == "number":
        return "Float"
    if typ == "boolean":
        return "Boolean"
    if typ == "string":
        return "String"
    if typ == "object":
        if schema.get("properties") or schema.get("additionalProperties") is not None:
            return "Object"
        return "Object"
    if schema.get("contentMediaType"):
        return "InputFile"
    return typ or "unknown"


def iter_fields(schema: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not schema:
        return []
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: list[dict[str, Any]] = []
    for name, prop in props.items():
        desc = (prop or {}).get("description") or ""
        typ = type_name(prop or {})
        enum = (prop or {}).get("enum")
        if enum and "enum" not in desc.lower() and "must be one of" not in desc.lower():
            desc = (desc + " " if desc else "") + "One of: " + ", ".join(str(x) for x in enum)
        fields.append(
            {
                "name": name,
                "type": typ,
                "required": name in required,
                "description": desc,
            }
        )
    # Required first, original order within each group.
    fields.sort(key=lambda f: (not f["required"],))
    return fields


def resolve_schema(spec: dict[str, Any], ref_or_schema: Any) -> dict[str, Any] | None:
    if not ref_or_schema:
        return None
    if isinstance(ref_or_schema, str):
        return _by_ref(spec, ref_or_schema)
    if "$ref" in ref_or_schema:
        return _by_ref(spec, ref_or_schema["$ref"])
    return ref_or_schema


def _by_ref(spec: dict[str, Any], ref: str) -> dict[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    node: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def method_return_type(spec: dict[str, Any], operation: dict[str, Any]) -> str:
    responses = operation.get("responses") or {}
    ok = responses.get("200") or {}
    if "$ref" in ok:
        ok = resolve_schema(spec, ok) or {}
    content = (ok.get("content") or {}).get("application/json") or {}
    media = resolve_schema(spec, content) or content
    schema = media.get("schema") or {}
    if "$ref" in schema:
        schema = resolve_schema(spec, schema) or schema
    result = (schema.get("properties") or {}).get("result")
    if not result:
        return ""
    return type_name(result, resolve_aliases=True)


def docs_anchor(name: str) -> str:
    return name.lower()


def short_from_rendered(tg_html: str, limit: int = 200) -> str:
    """Build a plain short description from already-converted Telegram HTML."""
    import html as _html

    if not tg_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", tg_html)
    text = _html.unescape(text)
    return short_from(text, limit=limit)


def short_from(text: str, limit: int = 200) -> str:
    # Strip markdown/HTML-ish for the inline subtitle.
    t = html_unescape_keep(text)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = t.replace("**", "").replace("*", "")
    t = re.sub(r"`+", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    return first_sentence(t, limit=limit)


def html_unescape_keep(text: str) -> str:
    import html as _html

    return _html.unescape(text or "")


def build_search_text(*parts: str) -> str:
    chunks: list[str] = []
    seen: set[str] = set()
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        low = p.lower()
        if low in seen:
            continue
        seen.add(low)
        chunks.append(p)
        spaced = split_ident(p)
        if spaced.lower() != low:
            chunks.append(spaced)
    return " \n ".join(chunks)


def entries_from_api(spec: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    schemas = (spec.get("components") or {}).get("schemas") or {}
    paths = spec.get("paths") or {}

    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        post = item.get("post")
        if not isinstance(post, dict):
            continue
        name = post.get("operationId") or path.lstrip("/")
        if not name:
            continue
        desc = post.get("description") or ""
        tags = post.get("tags") or []
        category = tags[0] if tags else ""
        params_name = name[:1].upper() + name[1:] + "Params"
        param_schema = schemas.get(params_name)
        fields = iter_fields(param_schema)
        returns = method_return_type(spec, post)
        url = f"{DOCS_API_BASE}#{docs_anchor(name)}"
        field_names = " ".join(f["name"] + " " + split_ident(f["name"]) for f in fields)
        entries.append(
            {
                "id": slug_id("m", name),
                "kind": "method",
                "name": name,
                "title": name,
                "short": short_from(desc),
                "description": desc,
                "url": url,
                "fields": fields,
                "returns": returns,
                "category": category,
                "search_text": build_search_text(
                    name, split_ident(name), "method", category, desc, field_names, returns
                ),
            }
        )

    for sname, schema in schemas.items():
        if sname.endswith("Params"):
            continue
        if sname in SKIP_SCHEMAS:
            continue
        if not isinstance(schema, dict):
            continue
        if is_error_envelope(schema):
            continue
        desc = schema.get("description") or ""
        fields = iter_fields(schema)
        subtypes: list[str] = []
        for key in ("anyOf", "oneOf"):
            for part in schema.get(key) or []:
                if isinstance(part, dict) and "$ref" in part:
                    subtypes.append(part["$ref"].rsplit("/", 1)[-1])
        url = f"{DOCS_API_BASE}#{docs_anchor(sname)}"
        field_names = " ".join(f["name"] + " " + split_ident(f["name"]) for f in fields)
        short = short_from(desc) or f"Bot API type {sname}"
        entries.append(
            {
                "id": slug_id("t", sname),
                "kind": "type",
                "name": sname,
                "title": sname,
                "short": short,
                "description": desc,
                "url": url,
                "fields": fields,
                "subtypes": subtypes,
                "search_text": build_search_text(
                    sname, split_ident(sname), "type", "object", desc, field_names, " ".join(subtypes)
                ),
            }
        )
    return entries


def error_title(http_code: str, message: str) -> tuple[str, str]:
    """Return (title, display_name). Title is shown in the result list."""
    if ": " in message:
        prefix, rest = message.split(": ", 1)
        # SCREAMING_SNAKE mtproto-style codes.
        codeish = rest.split()[0] if rest else rest
        if re.match(r"^[A-Z][A-Z0-9_%]*$", codeish) and (
            "_" in codeish or re.match(r"^[A-Z]{3,}[A-Z0-9_%]*$", codeish)
        ):
            return f"{http_code} {codeish}", codeish
        # Human phrases: "Too Many Requests: retry after %d"
        if len(prefix) <= 40:
            return f"{http_code} {prefix}", prefix
    trimmed = message if len(message) <= 50 else message[:47] + "…"
    return f"{http_code} {trimmed}", message


def entries_from_errors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    errors = payload.get("errors") or {}
    parameters = payload.get("parameters") or {}
    mtproto_errors = payload.get("mtproto_errors") or {}
    mtproto_descriptions = payload.get("mtproto_descriptions") or {}

    for http_code, messages in errors.items():
        if not isinstance(messages, dict):
            continue
        for message, methods in messages.items():
            methods = list(methods or [])
            params = list(parameters.get(message) or [])
            mt_codes = list(mtproto_errors.get(message) or [])
            mt_descs = [mtproto_descriptions[c] for c in mt_codes if c in mtproto_descriptions]
            title, display = error_title(str(http_code), message)
            # Prefer the official / more readable description.
            desc_parts = []
            if mt_descs:
                desc_parts.extend(mt_descs)
            if message not in " ".join(desc_parts):
                desc_parts.append(message)
            description = "\n".join(desc_parts)
            url = "https://core.telegram.org/bots/api#making-requests"  # errors have no per-item anchors
            # Do not put the full related-method list in search_text: many errors
            # list dozens of methods and would then match every method-name query.
            # search.py still boosts entries whose related_methods match the query.
            alias_terms = ""
            # Bot API exposes MTProto FLOOD_WAIT as this 429 message; keep common
            # developer search terms even though errors.json does not mention FLOOD.
            if str(http_code) == "429" or message.lower().startswith("too many requests"):
                alias_terms = "flood flood wait FLOOD_WAIT FLOOD_WAIT_X rate limit retry after"
            search_text = build_search_text(
                title,
                display,
                message,
                "error",
                f"HTTP {http_code}",
                str(http_code),
                description,
                " ".join(mt_codes),
                " ".join(split_ident(c) for c in mt_codes),
                " ".join(params),
                alias_terms,
            )
            entries.append(
                {
                    "id": slug_id("e", str(http_code), message),
                    "kind": "error",
                    "name": display,
                    "title": title,
                    "short": short_from(description) or message,
                    "description": description,
                    "url": url,
                    "http_code": int(http_code) if str(http_code).isdigit() else http_code,
                    "error_message": message,
                    "related_methods": methods,
                    "mtproto_codes": mt_codes,
                    "error_parameters": params,
                    "search_text": search_text,
                }
            )
    return entries


def entries_from_html(kind: str, page_url: str, page_title: str, html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one("#dev_page_content")
    if content is None:
        log.warning("No #dev_page_content in %s", page_url)
        return []

    canonical = page_url.rstrip("/")
    entries: list[dict[str, Any]] = []

    def collect_until_heading(start: Tag | None, after: Tag) -> list:
        nodes: list = []
        # If start is the container, walk its children until `after` then collect until a heading.
        sibling = after.next_sibling
        while sibling is not None:
            name = getattr(sibling, "name", None)
            if name in HEADING_TAGS:
                break
            nodes.append(sibling)
            sibling = sibling.next_sibling
        return nodes

    # Intro (content before the first heading).
    intro_nodes: list = []
    first_heading = None
    for child in content.children:
        if getattr(child, "name", None) in HEADING_TAGS:
            first_heading = child
            break
        intro_nodes.append(child)
    intro_plain = plain_text_from_html_nodes(intro_nodes)
    intro_html = html_fragment_to_telegram(intro_nodes, canonical + "/")
    if intro_plain:
        entries.append(
            {
                "id": slug_id(kind, "intro", canonical),
                "kind": kind,
                "name": page_title,
                "title": page_title,
                "short": short_from_rendered(intro_html) or short_from(intro_plain),
                "description": intro_plain,
                "html": intro_html,
                "url": canonical,
                "search_text": build_search_text(page_title, kind, intro_plain),
            }
        )

    # Track parent heading titles by level so subsections get a breadcrumb.
    stack: list[tuple[int, str]] = []
    for heading in content.find_all(HEADING_TAGS):
        level = int(heading.name[1])
        a = heading.find("a", attrs={"name": True})
        anchor = (a.get("name") if a else "") or heading.get("id") or ""
        title = heading.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_titles = [t for _, t in stack]
        stack.append((level, title))
        nodes = collect_until_heading(content, heading)
        plain = plain_text_from_html_nodes(nodes)
        body_html = html_fragment_to_telegram(nodes, canonical + "/")
        url = f"{canonical}#{anchor}" if anchor else canonical
        breadcrumb = " › ".join(parent_titles + [title]) if parent_titles else title
        short = short_from_rendered(body_html) or short_from(plain) or breadcrumb
        entries.append(
            {
                "id": slug_id(kind, anchor or title),
                "kind": kind,
                "name": title,
                "title": title,
                "short": short,
                "description": plain,
                "html": body_html,
                "url": url,
                "category": " › ".join(parent_titles) if parent_titles else "",
                "anchor": anchor,
                "search_text": build_search_text(
                    title, split_ident(title), breadcrumb, kind, anchor.replace("-", " ") if anchor else "", plain
                ),
            }
        )
    return entries


def build_index(
    *,
    api_json_url: str = API_JSON_URL,
    errors_json_url: str = ERRORS_JSON_URL,
    html_sources: Iterable[dict[str, str]] | None = None,
    client: httpx.Client | None = None,
    api_path: Path | None = None,
    errors_path: Path | None = None,
    html_paths: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Fetch (or read local files) and return the index document."""
    html_sources = list(html_sources or HTML_SOURCES)
    own_client = client is None
    if client is None:
        client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True)
    sources_meta: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []

    try:
        # API JSON
        try:
            if api_path:
                api_spec = json.loads(Path(api_path).read_text(encoding="utf-8"))
            else:
                raw, _ = fetch_bytes(api_json_url, client)
                api_spec = json.loads(raw.decode("utf-8"))
            version = (api_spec.get("info") or {}).get("version") or api_spec.get("version")
            api_entries = entries_from_api(api_spec)
            entries.extend(api_entries)
            sources_meta["api"] = {
                "url": api_json_url if not api_path else str(api_path),
                "version": version,
                "entries": sum(1 for e in api_entries if e["kind"] == "method"),
                "types": sum(1 for e in api_entries if e["kind"] == "type"),
                "fetched_at": utcnow(),
            }
        except Exception as exc:  # noqa: BLE001 — isolated source failure should not abort the rest
            log.exception("Failed to load Bot API JSON")
            warnings.append(f"api.json failed: {exc}")

        # Errors JSON
        try:
            if errors_path:
                err_payload = json.loads(Path(errors_path).read_text(encoding="utf-8"))
            else:
                raw, _ = fetch_bytes(errors_json_url, client)
                err_payload = json.loads(raw.decode("utf-8"))
            err_entries = entries_from_errors(err_payload)
            entries.extend(err_entries)
            sources_meta["errors"] = {
                "url": errors_json_url if not errors_path else str(errors_path),
                "version": err_payload.get("version"),
                "entries": len(err_entries),
                "fetched_at": utcnow(),
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to load errors JSON")
            warnings.append(f"errors.json failed: {exc}")

        for src in html_sources:
            kind = src["kind"]
            url = src["url"]
            title = src["title"]
            try:
                if html_paths and kind in html_paths:
                    html = Path(html_paths[kind]).read_text(encoding="utf-8")
                    used = str(html_paths[kind])
                else:
                    raw, _ = fetch_bytes(url, client)
                    html = raw.decode("utf-8", errors="replace")
                    used = url
                html_entries = entries_from_html(kind, url, title, html)
                entries.extend(html_entries)
                sources_meta[kind] = {
                    "url": used,
                    "entries": len(html_entries),
                    "fetched_at": utcnow(),
                }
            except Exception as exc:  # noqa: BLE001
                log.exception("Failed to load HTML source %s", url)
                warnings.append(f"{url} failed: {exc}")
    finally:
        if own_client:
            client.close()

    # Deduplicate ids if any collision slipped through.
    seen_ids: dict[str, int] = {}
    for e in entries:
        eid = e["id"]
        n = seen_ids.get(eid, 0)
        seen_ids[eid] = n + 1
        if n:
            e["id"] = slug_id(e["kind"], e.get("anchor") or e["name"], str(n))

    index = {
        "built_at": utcnow(),
        "builder_version": "1.0.0",
        "sources": sources_meta,
        "warnings": warnings,
        "entry_count": len(entries),
        "entries": entries,
    }
    return index


def save_index(index: dict[str, Any], path: Path | None = None) -> Path:
    path = path or default_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=None, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return path


def load_index(path: Path | None = None) -> dict[str, Any]:
    path = path or default_index_path()
    return json.loads(path.read_text(encoding="utf-8"))
