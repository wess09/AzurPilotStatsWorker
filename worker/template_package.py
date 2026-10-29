"""Data-only template snapshots; signatures and updates never execute code."""
import base64
from datetime import datetime, timezone
import hashlib
import io
import math
import re

from PIL import Image


DEFAULT_THRESHOLDS = {"formal": 0.92, "cluster": 0.96, "margin": 0.03,
                      "suspected": 0.85, "hamming": 6, "color": 30,
                      "low_variance": 5, "temporary": 0.95}
HASH = re.compile(r"[0-9a-f]{64}")
CLUSTER = re.compile(r"U-[1-9][0-9]{0,18}")
ITEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,127}")


def thresholds(value=None):
    if value is not None and not isinstance(value, dict):
        raise ValueError("Invalid matching thresholds")
    result = {**DEFAULT_THRESHOLDS, **(value or {})}
    if set(result) != set(DEFAULT_THRESHOLDS):
        raise ValueError("Unknown matching threshold")
    if any(type(result[key]) not in (int, float) or not math.isfinite(result[key]) for key in result):
        raise ValueError("Invalid matching threshold")
    if not (0 <= result["suspected"] <= result["formal"] <= result["cluster"] <= 1
            and 0 <= result["temporary"] <= 1 and 0 <= result["margin"] <= 1
            and type(result["hamming"]) is int and 0 <= result["hamming"] <= 64
            and 0 <= result["color"] <= 255 and 0 <= result["low_variance"] <= 255):
        raise ValueError("Invalid matching threshold range")
    return result


def verify_signature(raw, revision, signature, public_key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        signed = base64.b64decode(signature, validate=True)
        # Bind a signature to both immutable content and its requested revision.
        message = f"azurpilot-templates-v1\n{revision}\n{hashlib.sha256(raw).hexdigest()}".encode("ascii")
        key.verify(signed, message)
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise ValueError("Template signature verification failed") from exc


def canonical_key(key, aliases):
    visited = set()
    while key in aliases:
        if key in visited:
            raise ValueError("Cyclic cluster alias")
        visited.add(key)
        key = aliases[key]
    return key


def normalize_bundle(data, revision, previous=None):
    if (type(revision) is not int or revision < 1 or not isinstance(data, dict)
            or type(data.get("schema_version")) is not int or data["schema_version"] != 1
            or type(data.get("revision")) is not int or data["revision"] != revision):
        raise ValueError("Invalid template snapshot revision/schema")
    previous = previous or {}
    if "base_revision" in data and data["base_revision"] != previous.get("revision"):
        raise ValueError("Template delta base revision mismatch")
    if "expires" in data:
        expiry = datetime.fromisoformat(data["expires"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError("Template snapshot expired")
    delta = "base_revision" in data
    templates = {entry["id"]: entry for entry in previous.get("templates", [])} if delta else {}
    if not isinstance(data.get("deleted", []), list):
        raise ValueError("Invalid template tombstones")
    for key in data.get("deleted", []):
        if not isinstance(key, str) or not HASH.fullmatch(key):
            raise ValueError("Invalid template tombstone")
        templates.pop(key, None)
    entries = data.get("templates", [])
    if not isinstance(entries, list) or len(entries) > 10000:
        raise ValueError("Invalid template count")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not HASH.fullmatch(entry["id"]) or entry["id"] in seen:
            raise ValueError("Invalid/duplicate template id")
        seen.add(entry["id"])
        if entry.get("scene") not in ("research", "drop", "all") or type(entry.get("enabled")) is not bool:
            raise ValueError("Invalid template context")
        kind, item = entry.get("kind", "formal"), entry.get("item_id")
        if not isinstance(item, str) or not ITEM.fullmatch(item) or kind not in ("formal", "temporary"):
            raise ValueError("Invalid template item")
        if kind == "temporary" and not CLUSTER.fullmatch(item):
            raise ValueError("Temporary template requires a server-assigned U key")
        if kind == "formal" and CLUSTER.fullmatch(item):
            raise ValueError("Formal template cannot use a temporary key")
        raw = base64.b64decode(entry["image"], validate=True)
        if len(raw) > 128 * 1024 or hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            raise ValueError("Template file checksum/size mismatch")
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.mode != "RGB" or image.size not in ((49, 49), (96, 96)):
                raise ValueError("Template must be 49x49 or 96x96 RGB PNG")
            image.load()
            if entry.get("icon_sha256") is not None:
                icon = image.crop((40, 21, 89, 70)) if image.width == 96 else image
                if hashlib.sha256(icon.tobytes()).hexdigest() != entry["icon_sha256"]:
                    raise ValueError("Template pixel checksum mismatch")
        templates[entry["id"]] = dict(entry)
    if len(templates) > 10000:
        raise ValueError("Too many active templates")
    combined = {}
    for field in ("mappings", "aliases"):
        values = dict(previous.get(field, {})) if delta else {}
        updates = data.get(field, {})
        if not isinstance(updates, dict) or len(updates) > 10000:
            raise ValueError("Invalid mapping count")
        values.update(updates)
        if not isinstance(data.get("deleted_" + field, []), list):
            raise ValueError("Invalid mapping tombstones")
        for key in data.get("deleted_" + field, []):
            if not isinstance(key, str) or not CLUSTER.fullmatch(key):
                raise ValueError("Invalid mapping tombstone")
            values.pop(key, None)
        if len(values) > 10000:
            raise ValueError("Too many mappings")
        for key, target in values.items():
            if not CLUSTER.fullmatch(key) or not isinstance(target, str):
                raise ValueError("Invalid mapping key")
            if field == "aliases" and not CLUSTER.fullmatch(target):
                raise ValueError("Invalid cluster alias")
            if field == "mappings" and (not ITEM.fullmatch(target) or CLUSTER.fullmatch(target)):
                raise ValueError("Invalid formal item mapping")
        combined[field] = values
    for key in combined["aliases"]:
        canonical_key(key, combined["aliases"])
    canonical_mappings = {}
    for key, item in combined["mappings"].items():
        key = canonical_key(key, combined["aliases"])
        item = re.sub(r"_\d+$", "", item)
        if key in canonical_mappings and canonical_mappings[key] != item:
            raise ValueError("Conflicting mappings for one cluster")
        canonical_mappings[key] = item
    combined["mappings"] = canonical_mappings
    overrides = dict(previous.get("hash_overrides", {})) if delta else {}
    updates = data.get("hash_overrides", {})
    if not isinstance(updates, dict) or len(updates) > 10000:
        raise ValueError("Invalid hash override count")
    overrides.update(updates)
    deleted_overrides = data.get("deleted_hash_overrides", [])
    if not isinstance(deleted_overrides, list):
        raise ValueError("Invalid hash override tombstones")
    for key in deleted_overrides:
        if not isinstance(key, str) or not HASH.fullmatch(key):
            raise ValueError("Invalid hash override tombstone")
        overrides.pop(key, None)
    if len(overrides) > 10000:
        raise ValueError("Too many hash overrides")
    for key, target in overrides.items():
        if not HASH.fullmatch(key) or not isinstance(target, str) or not CLUSTER.fullmatch(target):
            raise ValueError("Invalid hash override")
        overrides[key] = canonical_key(target, combined["aliases"])
    parameters = thresholds(data.get("match_thresholds", previous.get("match_thresholds") if delta else None))
    return {"schema_version": 1, "revision": revision,
            "templates": sorted(templates.values(), key=lambda entry: entry["id"]),
            **combined, "hash_overrides": overrides, "match_thresholds": parameters}


def effective_templates(bundle):
    result = []
    for entry in bundle["templates"]:
        entry = dict(entry)
        if entry.get("icon_sha256"):
            entry["known_icon_sha256"] = entry["icon_sha256"]
        entry["match_thresholds"] = dict(bundle["match_thresholds"])
        if entry.get("kind") == "temporary":
            key = canonical_key(entry["item_id"], bundle["aliases"])
            mapped = bundle["mappings"].get(key)
            entry["item_id"] = re.sub(r"_\d+$", "", mapped) if mapped else key
            entry["kind"] = "formal" if mapped else "temporary"
            entry["scene"] = "all"  # Temporary identity is global across scenes.
            entry["temporary_similarity"] = bundle["match_thresholds"]["temporary"]
        result.append(entry)
    return result
