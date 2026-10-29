"""Stateless matching for 49x49 unknown icons; global keys belong to C."""
import base64
from collections import OrderedDict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

import cv2
import numpy as np
if __package__:
    from .template_package import thresholds
else:
    from template_package import thresholds


def decode_icon(encoded, allow_cell=False):
    if not isinstance(encoded, str) or len(encoded) > 180000:
        raise ValueError("Invalid icon encoding")
    raw = base64.b64decode(encoded, validate=True)
    if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > 128 * 1024:
        raise ValueError("Icon must be a bounded PNG")
    # Check the PNG header before a decoder can allocate a large pixel buffer.
    width, height = int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if (width, height) not in ((49, 49), (96, 96)) or ((width, height) == (96, 96) and not allow_cell):
        raise ValueError("Icon must be 49x49")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Invalid PNG icon")
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    if image.shape[:2] == (96, 96):
        image = image[21:70, 40:89].copy()
    return image


def fingerprint(image):
    if image.shape != (49, 49, 3) or image.dtype != np.uint8:
        raise ValueError("Icon must be 49x49 RGB8")
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    low = cv2.dct(small.astype(np.float32))[:8, :8]
    return hashlib.sha256(image.tobytes()).hexdigest(), np.packbits(low > np.median(low)).tobytes().hex()


def color_difference(left, right):
    delta = np.asarray(left) - np.asarray(right)
    return float(max(0, float(delta.max())) - min(0, float(delta.min())))


def identity(name):
    return re.sub(r"_\d+$", "", name)


@lru_cache(maxsize=2048)
def template_features(encoded):
    image = decode_icon(encoded, allow_cell=True)
    pixel_hash, phash = fingerprint(image)
    image.flags.writeable = False
    return {"image": image, "hash": pixel_hash, "phash": int(phash, 16),
            "color": cv2.mean(image)[:3],
            "texture": float(np.sqrt(image.astype(np.float32).var(axis=(0, 1)).mean()))}


class IconIndex:
    def __init__(self, entries, parameters=None):
        self.parameters = thresholds(parameters)
        self.entries = []
        self.exact = {}
        self.cache = OrderedDict()
        for entry in entries:
            kind = entry.get("kind", "formal")
            name = entry.get("item") if kind == "formal" else entry.get("key")
            if kind not in ("formal", "cluster") or not isinstance(name, str) or not 0 < len(name) <= 128:
                raise ValueError("Invalid template identity")
            record = {"kind": kind, "identity": identity(name) if kind == "formal" else name,
                      **template_features(entry["image"])}
            self.entries.append(record)
            self.exact.setdefault(record["hash"], []).append(record)

    def _result(self, kind, name, score, stage, pixel_hash, phash, margin=None):
        result = {"match": kind, "score": float(score), "stage": stage,
                  "icon_sha256": pixel_hash, "phash": phash}
        if kind == "formal":
            result["item"] = name
        elif kind == "cluster":
            result["key"] = name
        if margin is not None:
            result["margin"] = float(margin)
        return result

    def match(self, image):
        if image.shape != (49, 49, 3) or image.dtype != np.uint8:
            raise ValueError("Icon must be 49x49 RGB8")
        pixel_hash = hashlib.sha256(image.tobytes()).hexdigest()
        if pixel_hash in self.cache:
            self.cache.move_to_end(pixel_hash)
            return {**self.cache[pixel_hash], "stage": "L0"}
        direct = self.exact.get(pixel_hash, [])
        phash = f"{direct[0]['phash']:016x}" if direct else fingerprint(image)[1]
        for kind in ("formal", "cluster"):
            names = {record["identity"] for record in direct if record["kind"] == kind}
            if len(names) == 1:
                result = self._result(kind, names.pop(), 1, "L0", pixel_hash, phash)
                self._cache(pixel_hash, result)
                return result
            if len(names) > 1:
                # Identical pixels with conflicting labels need human review.
                return self._result("new", None, 1, "ambiguous_exact", pixel_hash, phash, 0)
        value, color = int(phash, 16), cv2.mean(image)[:3]
        parameters = self.parameters
        texture = float(np.sqrt(image.astype(np.float32).var(axis=(0, 1)).mean()))
        scored = {"formal": {}, "cluster": {}}
        for record in self.entries:
            if ((value ^ record["phash"]).bit_count() > parameters["hamming"]
                    or color_difference(color, record["color"]) > parameters["color"]):
                continue
            if min(texture, record["texture"]) < parameters["low_variance"]:
                # Spatially constant RGB images have zero NCC denominator even
                # if their different R/G/B channels have a high overall std.
                delta = image.astype(np.float32) - record["image"].astype(np.float32)
                score = 1 - float(np.sqrt(np.mean(delta * delta))) / 255
            else:
                score = float(cv2.matchTemplate(image, record["image"], cv2.TM_CCOEFF_NORMED)[0, 0])
            if np.isfinite(score):
                group = scored[record["kind"]]
                group[record["identity"]] = max(score, group.get(record["identity"], -1))
        suspected = None
        best_score = 0
        for kind in ("formal", "cluster"):
            ranking = sorted(scored[kind].items(), key=lambda pair: (-pair[1], pair[0]))
            if not ranking:
                continue
            name, score = ranking[0]
            margin = score - ranking[1][1] if len(ranking) > 1 else 1
            best_score = max(best_score, score)
            if score >= parameters[kind] and margin >= parameters["margin"]:
                result = self._result(kind, name, score, "L2", pixel_hash, phash, margin)
                self._cache(pixel_hash, result)
                return result
            if kind == "formal" and score >= parameters["suspected"] and margin >= parameters["margin"]:
                suspected = name
        result = self._result("new", None, best_score, "L2", pixel_hash, phash)
        if suspected:
            result["suspected_item"] = suspected
        # Only confirmed identities enter L0's bounded cache.
        return result

    def _cache(self, key, value):
        self.cache[key] = dict(value)
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)


@lru_cache(maxsize=2)
def index_for_snapshot(snapshot):
    data = json.loads(snapshot)
    return IconIndex(data["entries"], data.get("thresholds"))


@lru_cache(maxsize=1)
def base_entries(root):
    entries = []
    for folder in ("commission_items", "research_items"):
        for path in sorted((Path(root) / "assets/stats" / folder).glob("*.png")):
            image = cv2.imread(str(path))
            if image is None or image.shape[:2] != (96, 96):
                raise ValueError("Invalid pinned base template")
            ok, raw = cv2.imencode(".png", image[21:70, 40:89])
            if not ok:
                raise ValueError("Base icon encoding failed")
            entries.append({"kind": "formal", "item": identity(path.stem),
                            "image": base64.b64encode(raw).decode()})
    return entries


def match_icons(request):
    icons, entries = request.get("icons"), request.get("entries", [])
    if not isinstance(icons, list) or not 1 <= len(icons) <= 50 or not isinstance(entries, list) or len(entries) > 10000:
        raise ValueError("Expected 1..50 icons and at most 10000 templates")
    if request.get("include_base"):
        entries = [*base_entries(str(Path(request["alas_root"]).resolve())), *entries]
        if len(entries) > 10000:
            raise ValueError("Too many icon templates")
    snapshot = json.dumps({"entries": entries, "thresholds": thresholds(request.get("thresholds"))}, sort_keys=True)
    if len(snapshot) > 32 * 1024 * 1024:
        raise ValueError("Icon template snapshot too large")
    index = index_for_snapshot(snapshot)
    return {"revision": request.get("revision", 0),
            "results": [index.match(decode_icon(image)) for image in icons]}
