"""Offline validation of rich-message payloads against Bot API OpenAPI schemas."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("rich_schemas.json")


class SchemaError(ValueError):
    """Raised when a payload does not match the Bot API schema."""


@lru_cache(maxsize=1)
def load_schemas() -> tuple[str, dict[str, Any]]:
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return str(data.get("version") or ""), data["schemas"]


def schema_version() -> str:
    version, _ = load_schemas()
    return version


def resolve(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    name = str(ref).rsplit("/", 1)[-1]
    target = schemas.get(name)
    if target is None:
        raise SchemaError(f"Unknown $ref: {ref}")
    return target


def validate(obj: Any, schema_name: str) -> list[str]:
    _, schemas = load_schemas()
    schema = schemas.get(schema_name)
    if schema is None:
        return [f"unknown schema {schema_name}"]
    errors: list[str] = []
    _check(obj, schema, schemas, "$", errors)
    return errors


def assert_valid(obj: Any, schema_name: str) -> None:
    errors = validate(obj, schema_name)
    if errors:
        preview = "\n".join(f"  - {e}" for e in errors[:24])
        more = f"\n  … and {len(errors) - 24} more" if len(errors) > 24 else ""
        raise SchemaError(f"{schema_name} is invalid:\n{preview}{more}")


def collect_block_types(obj: Any, found: set[str] | None = None) -> set[str]:
    """Walk a rich message / block tree and collect block `type` values."""
    found = found if found is not None else set()
    if isinstance(obj, dict):
        typ = obj.get("type")
        if isinstance(typ, str) and (
            "blocks" in obj
            or typ
            in {
                "heading",
                "paragraph",
                "pre",
                "footer",
                "divider",
                "anchor",
                "list",
                "blockquote",
                "expandable_blockquote",
                "details",
                "table",
                "buttons",
                "thinking",
                "pullquote",
                "collage",
                "slideshow",
                "map",
            }
        ):
            found.add(typ)
        for value in obj.values():
            collect_block_types(value, found)
    elif isinstance(obj, list):
        for item in obj:
            collect_block_types(item, found)
    return found


def _check(
    obj: Any,
    schema: dict[str, Any],
    schemas: dict[str, Any],
    path: str,
    errors: list[str],
    *,
    depth: int = 0,
) -> None:
    if depth > 80:
        errors.append(f"{path}: schema recursion too deep")
        return
    if "$ref" in schema:
        _check(obj, resolve(schema, schemas), schemas, path, errors, depth=depth + 1)
        return

    if "allOf" in schema:
        for i, part in enumerate(schema["allOf"]):
            # Skip OpenAPI if/then conditionals; they constrain chat_id formats, etc.
            if isinstance(part, dict) and ("if" in part or "then" in part) and "properties" not in part and "required" not in part and "oneOf" not in part and "anyOf" not in part:
                continue
            _check(obj, part, schemas, f"{path}.allOf[{i}]", errors, depth=depth + 1)

    if "anyOf" in schema:
        if _matches_any(obj, schema["anyOf"], schemas, path):
            return
        errors.append(f"{path}: value does not match anyOf")
        return

    if "oneOf" in schema:
        if "discriminator" in schema and isinstance(obj, dict) and schema["discriminator"].get("propertyName"):
            prop = schema["discriminator"]["propertyName"]
            mapping = schema["discriminator"].get("mapping") or {}
            key = obj.get(prop)
            ref = mapping.get(key)
            if ref:
                _check(obj, {"$ref": ref}, schemas, path, errors, depth=depth + 1)
                return
            errors.append(f"{path}: unknown {prop}={key!r} for discriminator")
            return
        matches = 0
        for part in schema["oneOf"]:
            probe: list[str] = []
            _check(obj, part, schemas, path, probe, depth=depth + 1)
            if not probe:
                matches += 1
        if matches == 1:
            return
        if matches == 0:
            errors.append(f"{path}: value matches none of oneOf")
        else:
            errors.append(f"{path}: value matches {matches} oneOf branches")
        return

    typ = schema.get("type")
    if typ == "string":
        if not isinstance(obj, str):
            errors.append(f"{path}: expected string, got {type(obj).__name__}")
            return
        _const_enum(obj, schema, path, errors)
        if "minLength" in schema and len(obj) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(obj) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        return
    if typ == "integer":
        if type(obj) is not int or isinstance(obj, bool):
            errors.append(f"{path}: expected integer, got {type(obj).__name__}")
            return
        if "minimum" in schema and obj < schema["minimum"]:
            errors.append(f"{path}: {obj} < minimum {schema['minimum']}")
        if "maximum" in schema and obj > schema["maximum"]:
            errors.append(f"{path}: {obj} > maximum {schema['maximum']}")
        return
    if typ == "boolean":
        if not isinstance(obj, bool):
            errors.append(f"{path}: expected boolean, got {type(obj).__name__}")
            return
        if "const" in schema and obj != schema["const"]:
            errors.append(f"{path}: expected const {schema['const']!r}")
        return
    if typ == "array":
        if not isinstance(obj, list):
            errors.append(f"{path}: expected array, got {type(obj).__name__}")
            return
        if "minItems" in schema and len(obj) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        item_schema = schema.get("items") or {}
        for i, item in enumerate(obj):
            _check(item, item_schema, schemas, f"{path}[{i}]", errors, depth=depth + 1)
        return
    if typ == "object" or "properties" in schema or "required" in schema:
        if not isinstance(obj, dict):
            errors.append(f"{path}: expected object, got {type(obj).__name__}")
            return
        required = schema.get("required") or []
        for key in required:
            if key not in obj or obj[key] is None:
                errors.append(f"{path}: missing required field {key}")
        props = schema.get("properties") or {}
        for key, value in obj.items():
            if key in props:
                _check(value, props[key], schemas, f"{path}.{key}", errors, depth=depth + 1)
        return
    if typ == "number":
        if not isinstance(obj, (int, float)) or isinstance(obj, bool):
            errors.append(f"{path}: expected number, got {type(obj).__name__}")
        return
    # Schema with only description / empty — accept.
    if typ:
        errors.append(f"{path}: unhandled schema type {typ}")


def _matches_any(obj: Any, variants: list[Any], schemas: dict[str, Any], path: str) -> bool:
    for part in variants:
        probe: list[str] = []
        _check(obj, part, schemas, path, probe, depth=0)
        if not probe:
            return True
    return False


def _const_enum(obj: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if "const" in schema and obj != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {obj!r}")
    if "enum" in schema and obj not in schema["enum"]:
        errors.append(f"{path}: {obj!r} not in enum {schema['enum']}")
