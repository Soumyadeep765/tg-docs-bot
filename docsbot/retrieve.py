"""Precomputed BM25 + char-ngram + optional MiniLM retrieval, blended with lexical scores."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from rank_bm25 import BM25Okapi

from .search import (
    MIN_SCORE,
    NAME_CONTAINS,
    NAME_EXACT,
    NAME_PREFIX,
    parse_query,
    score_entry,
)
from .dotpath import DotPathIndex, looks_like_dot_query
from .permalinks import build_permalink_map
from .typelinks import configure_type_links
from .textutil import split_ident

log = logging.getLogger("docsbot.retrieve")

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
CHAR_DIM = 2048
CHAR_NMIN, CHAR_NMAX = 2, 4
EMBED_BATCH = 64

STOPWORDS = frozenset(
    """
    a an the and or or of to for from in on at by with without as is are was were be been
    it this that these those you your we they how what when where which who whom why
    can could should would may might will just into over after before about than then
    also only not no yes do does did doing done use using used via per
    """.split()
)

TOKEN_RE = re.compile(r"[a-z0-9]+")
OFFSET_RE = re.compile(r"^(?:offset[=:]|o[=:]?)?(\d+)\s*$", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def parse_offset(raw: str | None, *, default: int = 0, maximum: int = 1_000_000) -> int:
    """Parse Telegram inline next_offset into a non-negative integer.

    Accepts '', '50', 'offset=50', 'o:50'. Anything else falls back to default.
    """
    if raw is None:
        return default
    s = str(raw).strip()
    if not s:
        return default
    m = OFFSET_RE.match(s)
    if not m:
        return default
    try:
        value = int(m.group(1))
    except (TypeError, ValueError):
        return default
    if value < 0:
        return 0
    if value > maximum:
        return maximum
    return value

# Curated auto-suggest list for an empty (or prefix-only) query.
POPULAR_METHODS = [
    "sendMessage",
    "sendPhoto",
    "sendDocument",
    "sendVideo",
    "sendAudio",
    "sendAnimation",
    "editMessageText",
    "editMessageCaption",
    "editMessageReplyMarkup",
    "deleteMessage",
    "forwardMessage",
    "copyMessage",
    "getUpdates",
    "setWebhook",
    "deleteWebhook",
    "getMe",
    "answerCallbackQuery",
    "answerInlineQuery",
    "answerWebAppQuery",
    "sendInvoice",
    "createInvoiceLink",
    "answerPreCheckoutQuery",
    "setMyCommands",
    "getChat",
    "banChatMember",
]
POPULAR_TYPES = [
    "Update",
    "Message",
    "User",
    "Chat",
    "InlineKeyboardMarkup",
    "InlineKeyboardButton",
    "ReplyKeyboardMarkup",
    "CallbackQuery",
    "InlineQuery",
    "InputMediaPhoto",
    "File",
    "Document",
    "PhotoSize",
    "WebAppInfo",
]
POPULAR_OTHER = [
    ("feature", "Payments"),
    ("feature", "Inline Keyboards"),
    ("feature", "Commands"),
    ("feature", "Mini Apps"),
    ("faq", "How do I get updates?"),
    ("error", "Too Many Requests"),
]


def default_embeddings_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "embeddings.npz"


def default_model_cache() -> Path:
    raw = os.environ.get("FASTEMBED_CACHE_PATH")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent / "data" / "models"


def strip_tags(text: str) -> str:
    return TAG_RE.sub(" ", text or "")


def entry_document(entry: dict[str, Any], *, limit: int = 4000) -> str:
    """Full searchable text: names, descriptions, fields, errors, section bodies."""
    parts: list[str] = [
        entry.get("name") or "",
        split_ident(entry.get("name") or ""),
        entry.get("title") or "",
        entry.get("kind") or "",
        entry.get("category") or "",
        entry.get("short") or "",
        entry.get("description") or "",
        entry.get("error_message") or "",
        " ".join(entry.get("mtproto_codes") or []),
        " ".join(entry.get("error_parameters") or []),
        entry.get("returns") or "",
    ]
    for field in entry.get("fields") or []:
        parts.append(field.get("name") or "")
        parts.append(split_ident(field.get("name") or ""))
        parts.append(field.get("type") or "")
        parts.append(field.get("description") or "")
    if entry.get("html"):
        parts.append(strip_tags(entry["html"]))
    # search_text already has spaced identifiers; keep it as a supplement.
    if entry.get("search_text"):
        parts.append(entry["search_text"])
    text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    return text[:limit]


def entry_embed_text(entry: dict[str, Any]) -> str:
    """Shorter, name-forward text used for MiniLM embeddings."""
    fields = ", ".join(f.get("name") or "" for f in (entry.get("fields") or [])[:24])
    desc = entry.get("short") or entry.get("description") or ""
    desc = re.sub(r"\s+", " ", strip_tags(desc))[:400]
    kind = entry.get("kind") or ""
    name = entry.get("title") or entry.get("name") or ""
    extra = entry.get("error_message") or entry.get("category") or ""
    return f"{kind}: {name}. {extra}. {desc}. Fields: {fields}".strip()


def expand_query(query: str) -> str:
    """Add a few conservative synonyms for natural-language queries."""
    q = query or ""
    l = q.lower()
    extra: list[str] = []
    if any(w in l for w in ("website", "webpage", "web site", "web page", "opens a url", "open a url", "open url")):
        extra.append("url inline keyboard button open link http")
    if "caption" in l:
        extra.append("caption sendPhoto sendVideo sendAnimation")
    if any(w in l for w in ("long polling", "long-polling", "get updates")):
        extra.append("getUpdates webhook")
    return q if not extra else f"{q} {' '.join(extra)}"


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    # Split camelCase leftovers just in case.
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    toks = TOKEN_RE.findall(text)
    out: list[str] = []
    for t in toks:
        if t in STOPWORDS or len(t) == 1:
            continue
        out.append(t)
        # Light stemming-ish: payments/payment, messages/message
        if len(t) > 5 and t.endswith("s"):
            out.append(t[:-1])
    return out


def _mmh3(s: str) -> int:
    try:
        import mmh3

        return mmh3.hash(s, signed=False)
    except Exception:
        return int(hashlib.blake2b(s.encode("utf-8"), digest_size=8).hexdigest(), 16)


def char_ngram_vector(text: str, dim: int = CHAR_DIM) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    s = re.sub(r"\s+", " ", (text or "").lower())
    if not s:
        return v
    for n in range(CHAR_NMIN, CHAR_NMAX + 1):
        if len(s) < n:
            continue
        for i in range(len(s) - n + 1):
            h = _mmh3(s[i : i + n])
            idx = h % dim
            sign = 1.0 if ((h // dim) & 1) == 0 else -1.0
            v[idx] += sign
    nrm = float(np.linalg.norm(v))
    if nrm:
        v /= nrm
    return v


def _char_source(entry: dict[str, Any]) -> str:
    # Compact typo-oriented string: names, short desc, field names — not the whole body.
    fields = " ".join(f.get("name") or "" for f in (entry.get("fields") or [])[:20])
    return " ".join(
        p
        for p in (
            entry.get("name") or "",
            split_ident(entry.get("name") or ""),
            entry.get("title") or "",
            split_ident(entry.get("title") or ""),
            entry.get("short") or "",
            entry.get("error_message") or "",
            fields,
            split_ident(fields),
        )
        if p
    )[:800]


def corpus_fingerprint(entries: list[dict[str, Any]], model_name: str) -> str:
    h = hashlib.sha1()
    h.update(model_name.encode())
    h.update(str(len(entries)).encode())
    for e in entries:
        h.update((e.get("id") or "").encode())
        h.update(b"\0")
    return h.hexdigest()[:20]


def _try_load_embedder(model_name: str, cache_dir: Path):
    try:
        from fastembed import TextEmbedding
    except ImportError:
        log.warning("fastembed is not installed; semantic embeddings disabled")
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))
    log.info("Loaded embedding model %s in %.2fs", model_name, time.perf_counter() - t0)
    return model


def _embed_texts(model, texts: list[str]) -> np.ndarray:
    vectors = list(model.embed(texts, batch_size=EMBED_BATCH))
    arr = np.asarray(vectors, dtype=np.float32)
    # L2 normalize for cosine via dot product.
    nrms = np.linalg.norm(arr, axis=1, keepdims=True)
    nrms[nrms == 0] = 1.0
    arr /= nrms
    return arr


class SearchEngine:
    """In-memory retrieval index. Built once at process start (and after a refresh)."""

    def __init__(
        self,
        index: dict[str, Any],
        *,
        embeddings_path: Path | None = None,
        model_cache: Path | None = None,
        model_name: str = DEFAULT_MODEL,
        load_embeddings: bool = True,
    ) -> None:
        self.index = index
        self.entries: list[dict[str, Any]] = list(index.get("entries") or [])
        self.by_id = {e["id"]: e for e in self.entries if e.get("id")}
        self.by_kind_name: dict[tuple[str, str], dict[str, Any]] = {}
        for e in self.entries:
            self.by_kind_name[(e.get("kind") or "", e.get("name") or "")] = e
        self.popular_ids: set[str] = set()
        for name in POPULAR_METHODS:
            e = self.by_kind_name.get(("method", name))
            if e and e.get("id"):
                self.popular_ids.add(e["id"])
        for name in POPULAR_TYPES:
            e = self.by_kind_name.get(("type", name))
            if e and e.get("id"):
                self.popular_ids.add(e["id"])
        for k, name in POPULAR_OTHER:
            e = self.by_kind_name.get((k, name))
            if e and e.get("id"):
                self.popular_ids.add(e["id"])
        self.id_to_payload, self.payload_to_entry = build_permalink_map(self.entries)
        self.dotpath = DotPathIndex(self.entries, payloads=self.id_to_payload)
        configure_type_links(self.entries, self.id_to_payload)
        self.empty_lists = self._build_empty_lists()
        self.model_name = model_name
        self.embeddings_path = embeddings_path or default_embeddings_path()
        self.model_cache = model_cache or default_model_cache()
        self.embedder = None
        self.embeddings: np.ndarray | None = None
        self.fingerprint = corpus_fingerprint(self.entries, model_name)

        t0 = time.perf_counter()
        self.documents = [entry_document(e) for e in self.entries]
        tokenized = [tokenize(doc) for doc in self.documents]
        # BM25Okapi needs at least one doc; empty corpus is a degenerate test case.
        self.bm25 = BM25Okapi(tokenized if any(tokenized) else [[""]])
        self._tokenized = tokenized

        char_rows = [_char_source(e) for e in self.entries]
        if self.entries:
            self.char_matrix = np.vstack([char_ngram_vector(s) for s in char_rows])
        else:
            self.char_matrix = np.zeros((0, CHAR_DIM), dtype=np.float32)

        if load_embeddings:
            self._init_embeddings()

        self.build_ms = (time.perf_counter() - t0) * 1000
        n_emb = 0 if self.embeddings is None else int(self.embeddings.shape[0])
        log.info(
            "SearchEngine ready: %s entries, embeddings=%s, build=%.0fms",
            len(self.entries),
            n_emb,
            self.build_ms,
        )

    def _init_embeddings(self) -> None:
        path = self.embeddings_path
        if path.exists():
            try:
                data = np.load(path, allow_pickle=False)
                if str(data["fingerprint"]) == self.fingerprint and data["vectors"].shape[0] == len(
                    self.entries
                ):
                    vecs = np.asarray(data["vectors"], dtype=np.float32)
                    nrms = np.linalg.norm(vecs, axis=1, keepdims=True)
                    nrms[nrms == 0] = 1.0
                    self.embeddings = vecs / nrms
                    log.info("Loaded precomputed embeddings from %s", path)
                else:
                    log.info("Embeddings cache is stale; recomputing")
            except Exception:
                log.exception("Failed to load embeddings cache; will recompute")
        if self.embeddings is None:
            self.embedder = _try_load_embedder(self.model_name, self.model_cache)
            if self.embedder is None:
                return
            texts = [entry_embed_text(e) for e in self.entries]
            t0 = time.perf_counter()
            self.embeddings = _embed_texts(self.embedder, texts)
            log.info("Computed %s embeddings in %.2fs", len(texts), time.perf_counter() - t0)
            self._save_embeddings()
        else:
            # Still need the model at query time.
            self.embedder = _try_load_embedder(self.model_name, self.model_cache)

    def _save_embeddings(self) -> None:
        if self.embeddings is None:
            return
        path = self.embeddings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # numpy appends .npz if the suffix isn't already .npz, so use a .tmp.npz name.
        tmp = path.with_name(path.stem + ".tmp.npz")
        np.savez_compressed(
            tmp,
            vectors=self.embeddings,
            fingerprint=np.asarray(self.fingerprint),
            model=np.asarray(self.model_name),
        )
        tmp.replace(path)
        log.info("Wrote embeddings cache %s (%s bytes)", path, path.stat().st_size)

    def _embed_query(self, text: str) -> np.ndarray | None:
        if self.embedder is None or self.embeddings is None:
            return None
        vecs = _embed_texts(self.embedder, [text])
        return vecs[0]

    def permalink_for(self, entry: dict[str, Any]) -> str | None:
        if entry.get("kind") in {"field", "fieldset"}:
            payload = entry.get("field_payload")
            if payload:
                path = [str(x) for x in (entry.get("path") or [])]
                if entry.get("kind") == "fieldset":
                    if entry.get("mode") == "all":
                        path = path + ["*"]
                    else:
                        names = [f.get("name") or "" for f in (entry.get("selected_fields") or [])]
                        path = path + [",".join(names)]
                if path:
                    self.dotpath.register_hash(payload, path)
                return payload
            from .dotpath import permalink_for_field

            return permalink_for_field(entry, self.dotpath)
        eid = entry.get("id")
        if not eid:
            return None
        return self.id_to_payload.get(eid)

    def entry_for_payload(self, payload: str) -> dict[str, Any] | None:
        found = self.payload_to_entry.get(payload)
        if found is not None:
            return found
        if payload.startswith(("p_", "pa_", "pm_", "ph_", "pah_", "pmh_")):
            return self.dotpath.entry_for_payload(payload)
        return None

    def entries_of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.entries if e.get("kind") == kind]

    def _build_empty_lists(self) -> dict[str | None, list[tuple[float, dict[str, Any]]]]:
        """Full browse order used for empty / prefix-only queries, popular first."""
        lists: dict[str | None, list[tuple[float, dict[str, Any]]]] = {}

        def ordered(kind: str | None) -> list[dict[str, Any]]:
            picked: list[dict[str, Any]] = []
            seen: set[str] = set()

            def add(entry: dict[str, Any] | None) -> None:
                if not entry:
                    return
                if kind and entry.get("kind") != kind:
                    return
                eid = entry.get("id")
                if not eid or eid in seen:
                    return
                seen.add(eid)
                picked.append(entry)

            if kind is None or kind == "method":
                for name in POPULAR_METHODS:
                    add(self.by_kind_name.get(("method", name)))
            if kind is None or kind == "type":
                for name in POPULAR_TYPES:
                    add(self.by_kind_name.get(("type", name)))
            if kind is None:
                for k, name in POPULAR_OTHER:
                    add(self.by_kind_name.get((k, name)))
            elif kind in {"feature", "faq", "error", "webapp", "fact"}:
                for k, name in POPULAR_OTHER:
                    if k == kind:
                        add(self.by_kind_name.get((k, name)))

            if kind:
                for e in self.entries:
                    if e.get("kind") == kind:
                        add(e)
            else:
                # After popular items, walk the rest of the catalog so scrolling
                # can reach every entry (methods, types, then everything else).
                for k in ("method", "type", "feature", "webapp", "faq", "fact", "error"):
                    for e in self.entries:
                        if e.get("kind") == k:
                            add(e)
            return picked

        for kind in (None, "method", "type", "error", "feature", "webapp", "faq", "fact"):
            items = ordered(kind)
            lists[kind] = [(float(10_000 - i), e) for i, e in enumerate(items)]
        return lists

    def suggest_empty(
        self, kind: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[tuple[float, dict[str, Any]]], int]:
        """Popular-first catalog for an empty or prefix-only query, with a true total."""
        full = self.empty_lists.get(kind)
        if full is None:
            full = self.empty_lists.get(None) or []
        return full[offset : offset + limit], len(full)

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
        min_score: float | None = None,
    ) -> tuple[list[tuple[float, dict[str, Any]]], int, dict[str, float]]:
        """Return (page, total, timing_ms)."""
        t0 = time.perf_counter()
        kind, q = parse_query(query)
        timings: dict[str, float] = {}
        try:
            offset = int(offset or 0)
        except (TypeError, ValueError):
            offset = 0
        if offset < 0:
            offset = 0
        if limit is None or int(limit) <= 0:
            limit = 50

        if kind:
            mask = [i for i, e in enumerate(self.entries) if e.get("kind") == kind]
        else:
            mask = list(range(len(self.entries)))
        pool_entries = [self.entries[i] for i in mask]

        if not q:
            offset = max(0, int(offset or 0))
            ranked, total = self.suggest_empty(kind, limit=limit, offset=offset)
            timings["total_ms"] = (time.perf_counter() - t0) * 1000
            return ranked, total, timings

        q_norm = re.sub(r"\s+", " ", q).strip()
        if looks_like_dot_query(q_norm):
            t1 = time.perf_counter()
            path_result = self.dotpath.search(q_norm, kind=kind, limit=limit, offset=offset)
            timings["dotpath_ms"] = (time.perf_counter() - t1) * 1000
            if path_result is not None:
                ranked, total = path_result
                timings["total_ms"] = (time.perf_counter() - t0) * 1000
                return ranked, total, timings
        compact_q = q_norm.lower().replace(" ", "")
        short = len(compact_q) <= 2

        # --- lexical ---
        t1 = time.perf_counter()
        lexical = np.zeros(len(pool_entries), dtype=np.float32)
        for i, e in enumerate(pool_entries):
            lexical[i] = score_entry(e, q_norm, short_query=short)
        timings["lexical_ms"] = (time.perf_counter() - t1) * 1000

        # Popular prefix boost so "@bot s" / "se" surfaces sendMessage, not just the shortest names.
        if compact_q and self.popular_ids:
            for i, e in enumerate(pool_entries):
                if e.get("id") in self.popular_ids and lexical[i] >= (NAME_PREFIX - 500):
                    lexical[i] += 80.0

        # --- BM25 + char n-grams + embeddings (skip 1–2 char prefixes; too vague) ---
        bm25_s = np.zeros(len(pool_entries), dtype=np.float32)
        char_s = np.zeros(len(pool_entries), dtype=np.float32)
        emb_s = np.zeros(len(pool_entries), dtype=np.float32)
        if not short and len(compact_q) >= 3:
            q_expanded = expand_query(q_norm)
            t1 = time.perf_counter()
            q_tokens = tokenize(q_expanded)
            if q_tokens:
                raw = self.bm25.get_scores(q_tokens)
                bm25_s = np.asarray([raw[i] for i in mask], dtype=np.float32)
            timings["bm25_ms"] = (time.perf_counter() - t1) * 1000

            t1 = time.perf_counter()
            q_char = char_ngram_vector(q_norm)
            if mask and self.char_matrix.shape[0]:
                char_s = self.char_matrix[mask] @ q_char
            timings["char_ms"] = (time.perf_counter() - t1) * 1000

            if self.embeddings is not None:
                t1 = time.perf_counter()
                q_vec = self._embed_query(q_expanded)
                if q_vec is not None:
                    emb_s = self.embeddings[mask] @ q_vec
                timings["embed_ms"] = (time.perf_counter() - t1) * 1000

        # Convert similarity channels onto a 0–~2500 scale.
        def squash_bm25(x: np.ndarray) -> np.ndarray:
            # Typical related hits land in the 4–15 range.
            return (x / (x + 4.0)) * 2400.0

        def squash_cos(x: np.ndarray, floor: float, span: float) -> np.ndarray:
            return np.clip((x - floor) / span, 0.0, 1.0) * 2400.0

        combined = (
            lexical
            + squash_bm25(bm25_s)
            + squash_cos(char_s, 0.12, 0.55)
            + squash_cos(emb_s, 0.28, 0.50)
        )

        # Exact / prefix lexical hits must stay clearly in front of pure semantic matches.
        # lexical already uses 8000–10000 for those; combined just adds a small bonus.

        if short:
            # 1–2 characters: keep prefix/partial name hits, drop weak semantic leftovers.
            threshold = 100.0 if min_score is None else min_score
            keep = lexical >= threshold
        else:
            threshold = (MIN_SCORE if len(compact_q) >= 4 else 200.0) if min_score is None else min_score
            keep = combined >= threshold
            # Always keep solid name-level lexical hits.
            keep = keep | (lexical >= NAME_CONTAINS)

        scored: list[tuple[float, dict[str, Any]]] = [
            (float(combined[i]), pool_entries[i]) for i in range(len(pool_entries)) if keep[i]
        ]

        kind_rank = {
            "method": 0,
            "type": 1,
            "error": 2,
            "feature": 3,
            "webapp": 3,
            "faq": 4,
            "fact": 5,
        }

        def sort_key(item: tuple[float, dict[str, Any]]) -> tuple:
            s, e = item
            return (-s, kind_rank.get(e.get("kind") or "", 9), len(e.get("name") or ""), e.get("name") or "")

        scored.sort(key=sort_key)

        # If the query looks like an identifier and we have a name-level hit, don't
        # flood the list with weak semantic matches. Natural-language queries skip this.
        looks_like_name = " " not in q_norm and len(compact_q) <= 40
        if looks_like_name and scored and scored[0][0] >= NAME_CONTAINS:
            floor = max(threshold, 2000.0)
            scored = [item for item in scored if item[0] >= floor]

        total = len(scored)
        timings["total_ms"] = (time.perf_counter() - t0) * 1000
        return scored[offset : offset + limit], total, timings


# Module-level engine used by tests and the bot; built explicitly via build_engine().
_ENGINE: SearchEngine | None = None


def build_engine(index: dict[str, Any], **kwargs: Any) -> SearchEngine:
    global _ENGINE
    _ENGINE = SearchEngine(index, **kwargs)
    return _ENGINE


def get_engine() -> SearchEngine | None:
    return _ENGINE


def search(
    index: dict[str, Any] | list[dict[str, Any]] | SearchEngine,
    query: str,
    *,
    limit: int = 50,
    offset: int = 0,
    min_score: float | None = None,
) -> tuple[list[tuple[float, dict[str, Any]]], int]:
    """Back-compatible wrapper used by tests and the bot.

    Prefer a prepared SearchEngine; fall back to lexical-only scoring.
    """
    if isinstance(index, SearchEngine):
        hits, total, _ = index.search(query, limit=limit, offset=offset, min_score=min_score)
        return hits, total
    engine = _ENGINE
    if engine is not None and not isinstance(index, list) and index is engine.index:
        hits, total, _ = engine.search(query, limit=limit, offset=offset, min_score=min_score)
        return hits, total
    # Lexical fallback (e.g. unit tests without an engine).
    from . import search as lexical

    return lexical.lexical_search(index, query, limit=limit, offset=offset, min_score=min_score or MIN_SCORE)
