"""Dot-path targeting: sendMessage.chat_id, Message.chat.id, typo-tolerant parents."""

from __future__ import annotations

import re
from typing import Any, Iterable

from rapidfuzz import fuzz

from .permalinks import (
    encode_all_fields_payload,
    encode_field_payload,
    encode_multi_fields_payload,
)
from .search import NAME_CONTAINS, NAME_EXACT, NAME_PREFIX
from .textutil import first_sentence

IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
SPLIT_DOTS = re.compile(r"\s*\.\s*")
SPLIT_OR = re.compile(r"\s+or\s+", re.I)
ARRAY_OF = re.compile(r"^array\s+of\s+", re.I)

PRIMITIVES = frozenset({
    "string", "integer", "boolean", "float", "int", "long", "number",
    "true", "false", "null", "object", "value",
})

# Minimum parent score to treat the query as a dot-path (else regular search).
MIN_PARENT_SCORE = 1800
MAX_AMBIGUOUS_PARENTS = 8


def looks_like_dot_query(query: str) -> bool:
    """True when the query is Parent.field… with an identifier parent."""
    q = (query or "").strip()
    if "." not in q:
        return False
    parent = SPLIT_DOTS.split(q, 1)[0].strip()
    return bool(IDENT.fullmatch(parent))


def split_dot_query(query: str) -> tuple[str, list[str]] | None:
    """Return (parent, field_parts) including a trailing empty part if the query ends with '.'."""
    q = (query or "").strip()
    if "." not in q:
        return None
    trailing_dot = q.endswith(".")
    parts = [p for p in SPLIT_DOTS.split(q) if p != "" or False]
    # SPLIT_DOTS.split("sendMessage.") -> ["sendMessage", ""] 
    raw = SPLIT_DOTS.split(q)
    if not raw or not raw[0]:
        return None
    parent = raw[0].strip()
    if not IDENT.fullmatch(parent):
        return None
    rest = [p.strip() for p in raw[1:]]
    # "Message..id" has an empty intermediate — reject.
    if any(p == "" for p in rest[:-1] if rest):
        return None
    if rest and rest[-1] == "":
        rest = rest[:-1] + [""]
    elif trailing_dot and (not rest or rest[-1] != ""):
        rest = rest + [""]
    for i, part in enumerate(rest):
        if part == "":
            continue
        if i == len(rest) - 1:
            if parse_field_selector(part) is None:
                return None
        elif not IDENT.fullmatch(part):
            return None
    return parent, rest


def parse_field_selector(seg: str) -> tuple[str, list[str]] | None:
    """Classify the last path segment: ('all', []), ('multi', names), ('one', [name])."""
    s = (seg or "").strip()
    if s == "" or s == "*":
        return "all", []
    if "," in s:
        names = [p.strip() for p in s.split(",") if p.strip()]
        if not names or not all(IDENT.fullmatch(n) for n in names):
            return None
        return "multi", names
    if IDENT.fullmatch(s):
        return "one", [s]
    return None


def unwrap_type_names(type_str: str) -> list[str]:
    """Array of X -> X; 'A or B' -> [A, B]; primitives dropped from object-name list."""
    typ = (type_str or "").strip()
    if not typ:
        return []
    while True:
        m = ARRAY_OF.match(typ)
        if not m:
            break
        typ = typ[m.end():].strip()
    names: list[str] = []
    for part in SPLIT_OR.split(typ):
        name = part.strip()
        if not name or name.lower() in PRIMITIVES:
            continue
        # Skip empty generic wrappers
        if IDENT.fullmatch(name) and name.lower() not in PRIMITIVES:
            names.append(name)
    return names


def score_name(name: str, query: str, *, typo_partial: bool = False) -> float:
    n = (name or "").lower()
    q = (query or "").lower()
    if not q or not n:
        return 0.0
    if n == q:
        return float(NAME_EXACT)
    if n.startswith(q):
        # Prefer shorter remainder so chat_id beats chat_join_request for "chat_".
        return float(NAME_PREFIX) - min(80, len(n) - len(q))
    compact_n = n.replace("_", "")
    compact_q = q.replace("_", "")
    if compact_n.startswith(compact_q) and compact_q:
        return float(NAME_PREFIX) - 120 - min(80, len(compact_n) - len(compact_q))
    if q in n:
        return float(NAME_CONTAINS) - min(80, n.find(q))
    # Typos: use full-string ratio so short names like "Message" do not
    # piggy-back on a longer query like "sendmesage" via partial_ratio.
    ratio = fuzz.ratio(n, q)
    token = fuzz.token_sort_ratio(n, q)
    best = max(ratio, token)
    if typo_partial:
        # Fields may reasonably use a softer typo score; still require
        # similar length so "from" does not fuzzy-match a 20-char name.
        if abs(len(n) - len(q)) <= max(3, len(q) // 2):
            best = max(best, fuzz.WRatio(n, q) * 0.95)
    if best >= 82:
        return 2000 + best
    return 0.0


def score_parent_name(name: str, query: str) -> float:
    return score_name(name, query, typo_partial=False)


def score_field_name(name: str, query: str) -> float:
    if not query:
        return 0.0
    return score_name(name, query, typo_partial=True)


def object_types_for(type_str: str, types_by_lower: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in unwrap_type_names(type_str):
        entry = types_by_lower.get(name.lower())
        if not entry:
            continue
        eid = entry.get("id") or name
        if eid in seen:
            continue
        seen.add(eid)
        out.append(entry)
    return out


def pick_intermediate_field(fields: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """Resolve one path segment uniquely enough to walk through."""
    ranked: list[tuple[float, dict[str, Any]]] = []
    for field in fields:
        s = score_field_name(field.get("name") or "", query)
        if s >= NAME_CONTAINS or (s >= 2000 and query.lower() in (field.get("name") or "").lower()):
            ranked.append((s, field))
        elif s >= 2080:  # strong typo (2000+80)
            ranked.append((s, field))
    if not ranked:
        # allow slightly weaker unique typo for intermediate
        weak = []
        for field in fields:
            s = score_field_name(field.get("name") or "", query)
            if s >= 2000:
                weak.append((s, field))
        ranked = weak
    if not ranked:
        return None
    ranked.sort(key=lambda r: (-r[0], r[1].get("name") or ""))
    best_s, best = ranked[0]
    # Unique if only one exact/prefix, or best clearly ahead of the rest.
    if best_s >= NAME_EXACT:
        return best
    if len(ranked) == 1:
        return best
    if best_s >= NAME_PREFIX and ranked[1][0] < NAME_PREFIX:
        return best
    if best_s - ranked[1][0] >= 80:
        return best
    # Ambiguous — still take exact-prefix winner if the query is a unique full name typo
    if best_s >= 2060 and ranked[1][0] < best_s - 40:
        return best
    return None


def make_field_entry(
    *,
    root: dict[str, Any],
    container: dict[str, Any],
    field: dict[str, Any],
    path: list[str],
    type_entries: list[dict[str, Any]],
    payloads: dict[str, str] | None = None,
) -> dict[str, Any]:
    payloads = payloads or {}
    path_name = ".".join(path)
    typ = field.get("type") or "unknown"
    required = bool(field.get("required"))
    req = "required" if required else "optional"
    role = "parameter" if container.get("kind") == "method" else "field"
    desc = (field.get("description") or "").strip()
    short = f"{typ}, {req}" + (f" — {first_sentence(desc, limit=120)}" if desc else "")
    type_names = [t.get("name") for t in type_entries if t.get("name")]
    entry: dict[str, Any] = {
        "id": "field:" + path_name,
        "kind": "field",
        "name": path_name,
        "title": path_name,
        "short": short,
        "description": desc,
        "url": container.get("url") or root.get("url") or "",
        "path": list(path),
        "field_name": field.get("name") or path[-1],
        "field_type": typ,
        "required": required,
        "role": role,
        "parent_name": root.get("name") or path[0],
        "parent_kind": root.get("kind") or "",
        "parent_id": root.get("id") or "",
        "parent_url": root.get("url") or "",
        "container_name": container.get("name") or "",
        "container_kind": container.get("kind") or "",
        "container_id": container.get("id") or "",
        "container_url": container.get("url") or "",
        "type_names": type_names,
        "type_ids": [t.get("id") for t in type_entries if t.get("id")],
        "type_urls": [t.get("url") for t in type_entries if t.get("url")],
        "chainable": bool(type_entries),
        "chain_query": (path_name + ".") if type_entries else "",
    }
    if payloads:
        if root.get("id") and root["id"] in payloads:
            entry["parent_payload"] = payloads[root["id"]]
        if container.get("id") and container["id"] in payloads:
            entry["container_payload"] = payloads[container["id"]]
        type_payloads = []
        for t in type_entries:
            pid = payloads.get(t.get("id") or "")
            if pid:
                type_payloads.append({"name": t.get("name"), "payload": pid, "url": t.get("url") or ""})
        entry["type_payloads"] = type_payloads
    entry["field_payload"] = encode_field_payload(path)
    return entry



def make_fieldset_entry(
    *,
    root: dict[str, Any],
    container: dict[str, Any],
    fields: list[dict[str, Any]],
    path_prefix: list[str],
    mode: str,
    payloads: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A combined view of several (or all) fields of one parent."""
    payloads = payloads or {}
    parent_name = root.get("name") or (path_prefix[0] if path_prefix else "")
    container_name = container.get("name") or parent_name
    names = [f.get("name") or "" for f in fields if f.get("name")]
    if mode == "all":
        title = ".".join(path_prefix + ["*"]) if path_prefix else f"{parent_name}.*"
        payload = encode_all_fields_payload(".".join(path_prefix) if path_prefix else parent_name)
        short = f"{len(fields)} {'parameters' if container.get('kind') == 'method' else 'fields'}"
    else:
        title = ".".join(path_prefix) + "." + ",".join(names)
        payload = encode_multi_fields_payload(".".join(path_prefix) if path_prefix else parent_name, names)
        short = ", ".join(names)
    role = "parameters" if container.get("kind") == "method" else "fields"
    entry: dict[str, Any] = {
        "id": f"fieldset:{title}",
        "kind": "fieldset",
        "mode": mode,
        "name": title,
        "title": title,
        "short": short,
        "description": short,
        "url": container.get("url") or root.get("url") or "",
        "path": list(path_prefix),
        "selected_fields": list(fields),
        "role": role,
        "parent_name": parent_name,
        "parent_kind": root.get("kind") or "",
        "parent_id": root.get("id") or "",
        "parent_url": root.get("url") or "",
        "container_name": container_name,
        "container_kind": container.get("kind") or "",
        "container_id": container.get("id") or "",
        "container_url": container.get("url") or "",
        "field_payload": payload,
        "chain_query": (title if title.endswith(".*") else ".".join(path_prefix) + ".*") if container.get("fields") else "",
    }
    if payloads:
        if root.get("id") and root["id"] in payloads:
            entry["parent_payload"] = payloads[root["id"]]
        if container.get("id") and container["id"] in payloads:
            entry["container_payload"] = payloads[container["id"]]
    return entry


class DotPathIndex:
    """Resolved against methods/types that have parameters or fields."""

    def __init__(self, entries: Iterable[dict[str, Any]], payloads: dict[str, str] | None = None) -> None:
        self.payloads = payloads or {}
        self.parents: list[dict[str, Any]] = []
        self.types_by_lower: dict[str, dict[str, Any]] = {}
        self.parents_by_lower: dict[str, list[dict[str, Any]]] = {}
        self.by_id: dict[str, dict[str, Any]] = {}
        for entry in entries:
            kind = entry.get("kind") or ""
            name = entry.get("name") or ""
            if entry.get("id"):
                self.by_id[entry["id"]] = entry
            if kind == "type" and name:
                self.types_by_lower.setdefault(name.lower(), entry)
            if kind in {"method", "type"} and entry.get("fields"):
                self.parents.append(entry)
                if name:
                    self.parents_by_lower.setdefault(name.lower(), []).append(entry)
        # Prefer methods first when the same name exists (unusual).
        for lst in self.parents_by_lower.values():
            lst.sort(key=lambda e: 0 if e.get("kind") == "method" else 1)

        # Hash overflow map: ph_… -> path tuple, for 1-level fields that don't fit p_…
        self.hash_paths: dict[str, tuple[str, ...]] = {}
        for parent in self.parents:
            pname = parent.get("name") or ""
            for field in parent.get("fields") or []:
                fname = field.get("name") or ""
                if not pname or not fname:
                    continue
                payload = encode_field_payload([pname, fname])
                if payload.startswith("ph_"):
                    self.hash_paths[payload] = (pname, fname)

    def register_hash(self, payload: str, path: list[str]) -> None:
        if payload.startswith("ph_"):
            self.hash_paths[payload] = tuple(path)

    def score_parents(
        self, query: str, *, kind: str | None = None
    ) -> list[tuple[float, dict[str, Any]]]:
        hits: list[tuple[float, dict[str, Any]]] = []
        for entry in self.parents:
            if kind and entry.get("kind") != kind:
                continue
            s = score_parent_name(entry.get("name") or "", query)
            if s >= MIN_PARENT_SCORE:
                hits.append((s, entry))
        hits.sort(key=lambda r: (-r[0], 0 if r[1].get("kind") == "method" else 1, r[1].get("name") or ""))
        if not hits:
            return []
        best = hits[0][0]
        # Exact name: only those exact matches.
        if best >= NAME_EXACT:
            return [h for h in hits if h[0] >= NAME_EXACT]
        # Unique strong prefix.
        prefixes = [h for h in hits if h[0] >= NAME_PREFIX]
        if len(prefixes) == 1 and (len(hits) == 1 or hits[1][0] < NAME_PREFIX):
            return prefixes
        # Keep close contenders, capped.
        kept = [h for h in hits if h[0] >= best - 400]
        return kept[:MAX_AMBIGUOUS_PARENTS]

    def search(
        self, query: str, *, kind: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[tuple[float, dict[str, Any]]], int] | None:
        parsed = split_dot_query(query)
        if parsed is None:
            return None
        parent_q, field_parts = parsed
        parents = self.score_parents(parent_q, kind=kind)
        if not parents:
            return None

        last_filter = ""
        intermediates = field_parts
        if field_parts:
            last_filter = field_parts[-1]
            intermediates = field_parts[:-1]

        ranked: list[tuple[float, dict[str, Any]]] = []
        seen: set[str] = set()
        for parent_score, root in parents:
            container = root
            path_prefix = [root.get("name") or parent_q]
            failed = False
            for seg in intermediates:
                field = pick_intermediate_field(container.get("fields") or [], seg)
                if not field:
                    failed = True
                    break
                types = object_types_for(field.get("type") or "", self.types_by_lower)
                # Prefer a type that actually has fields.
                types = [t for t in types if t.get("fields")] or types
                if not types:
                    failed = True
                    break
                path_prefix.append(field.get("name") or seg)
                container = types[0]
            if failed:
                continue

            fields = list(container.get("fields") or [])
            if not fields:
                continue
            selector = parse_field_selector(last_filter)
            if selector is None:
                continue
            mode, wanted = selector

            if mode in {"all", "multi"}:
                if mode == "all":
                    chosen = list(fields)
                else:
                    chosen = []
                    used: set[str] = set()
                    for want in wanted:
                        best: tuple[float, dict[str, Any]] | None = None
                        for field in fields:
                            fname = field.get("name") or ""
                            if fname in used:
                                continue
                            fs = score_field_name(fname, want)
                            if fs <= 0:
                                continue
                            if best is None or fs > best[0]:
                                best = (fs, field)
                        if best and best[0] >= 2000:  # typo floor or better
                            used.add(best[1].get("name") or "")
                            chosen.append(best[1])
                if not chosen:
                    continue
                title_key = (
                    ".".join(path_prefix + ["*"])
                    if mode == "all"
                    else ".".join(path_prefix) + "." + ",".join(f.get("name") or "" for f in chosen)
                )
                if title_key not in seen:
                    seen.add(title_key)
                    fs_entry = make_fieldset_entry(
                        root=root,
                        container=container,
                        fields=chosen,
                        path_prefix=path_prefix,
                        mode=mode,
                        payloads=self.payloads,
                    )
                    payload = fs_entry.get("field_payload") or ""
                    self.register_hash(payload, path_prefix + (["*"] if mode == "all" else [f.get("name") or "" for f in chosen]))
                    ranked.append((parent_score + 20_000, fs_entry))
                # Trailing-dot autosuggest still lists individual fields.
                # An explicit * is the all-fields view only.
                if mode == "multi" or last_filter == "*":
                    continue
                scored_fields = [(float(1000 - i), field) for i, field in enumerate(fields)]
            else:
                scored_fields = []
                for i, field in enumerate(fields):
                    fs = score_field_name(field.get("name") or "", wanted[0] if wanted else "")
                    if fs <= 0:
                        continue
                    scored_fields.append((fs - i * 0.01, field))
                scored_fields.sort(key=lambda r: -r[0])
                if any(s >= NAME_EXACT for s, _ in scored_fields):
                    scored_fields = [(s, f) for s, f in scored_fields if s >= NAME_PREFIX]
                elif any(s >= NAME_PREFIX for s, _ in scored_fields):
                    scored_fields = [(s, f) for s, f in scored_fields if s >= NAME_PREFIX]

            for fs, field in scored_fields:
                fname = field.get("name") or ""
                path = path_prefix + [fname]
                key = ".".join(path)
                if key in seen:
                    continue
                seen.add(key)
                types = object_types_for(field.get("type") or "", self.types_by_lower)
                entry = make_field_entry(
                    root=root,
                    container=container,
                    field=field,
                    path=path,
                    type_entries=types,
                    payloads=self.payloads,
                )
                payload = entry.get("field_payload") or ""
                self.register_hash(payload, path)
                score = parent_score + fs
                ranked.append((score, entry))

        if not ranked:
            # Parent resolved but the path could not be walked / no field matched.
            # Still a "dot-path" query: return empty rather than falling back to
            # a regular catalog search (which would ignore the path).
            return [], 0

        # Stable: already grouped roughly by parent score then field score.
        ranked.sort(key=lambda r: -r[0])
        total = len(ranked)
        offset = max(0, int(offset or 0))
        limit = 50 if limit is None or int(limit) <= 0 else int(limit)
        return ranked[offset : offset + limit], total

    def entry_for_payload(self, payload: str) -> dict[str, Any] | None:
        p = (payload or "").strip()
        if p in self.hash_paths:
            path = list(self.hash_paths[p])
            return self.entry_for_path(path)
        if p.startswith("pa_"):
            name = p[3:]
            if not name:
                return None
            return self.entry_for_path([name, "*"])
        if p.startswith("pm_"):
            parts = p[3:].split("__")
            if len(parts) >= 2 and all(parts):
                parent, fields = parts[0], parts[1:]
                return self.entry_for_path([parent, ",".join(fields)])
            return None
        if p.startswith("p_") and not p.startswith("ph_"):
            parts = p[2:].split("__")
            if len(parts) >= 2 and all(parts):
                return self.entry_for_path(parts)
        return None

    def entry_for_path(self, path: list[str]) -> dict[str, Any] | None:
        if len(path) < 2:
            return None
        parent_name, *fields = path
        roots = self.parents_by_lower.get(parent_name.lower()) or []
        if not roots:
            # slug may have folded characters; try exact among slugged names
            lowered = parent_name.replace("_", "").lower()
            for entry in self.parents:
                if (entry.get("name") or "").replace("_", "").lower() == lowered:
                    roots = [entry]
                    break
        if not roots:
            return None
        root = roots[0]
        # Search as an exact path so typo-tolerant listing does not kick in.
        query = ".".join([root.get("name") or parent_name] + fields)
        result = self.search(query, kind=root.get("kind"), limit=5, offset=0)
        if not result or not result[0]:
            return None
        # Prefer exact path match.
        want = ".".join([root.get("name") or parent_name] + fields)
        for _s, entry in result[0]:
            if entry.get("name") == want or [root.get("name"), *fields] == entry.get("path"):
                return entry
        return result[0][0][1]


def permalink_for_field(entry: dict[str, Any], index: DotPathIndex | None = None) -> str | None:
    path = entry.get("path")
    if not path:
        name = entry.get("name") or ""
        path = name.split(".") if name else []
    if not path:
        return None
    payload = encode_field_payload([str(p) for p in path])
    if index is not None:
        index.register_hash(payload, [str(p) for p in path])
    return payload
