"""Commission page pairing using the pinned client's assets and OCR runtime."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import unicodedata

if __package__:
    from .commission_rules import ITEM_ALIASES, parse_amount, revise_amount, revise_commission
else:
    from commission_rules import ITEM_ALIASES, parse_amount, revise_amount, revise_commission


def normalize_name(value):
    value = unicodedata.normalize("NFKC", value).upper().replace("曰", "日")
    return re.sub(r"[\s\\/_\-'\"“”.,、，。()（）\[\]]", "", value)


@lru_cache(maxsize=1)
def catalog_entries():
    return json.loads(Path(__file__).with_name("commission_names.json").read_text(encoding="utf-8"))["entries"]


@lru_cache(maxsize=1)
def name_catalog():
    entries = catalog_entries()
    aliases = {}
    for entry in entries:
        for server, names in entry["aliases"].items():
            for name in names:
                key = server, normalize_name(name)
                # An alias shared by different names cannot establish identity.
                if key in aliases and aliases[key] != entry["name"]:
                    aliases[key] = None
                else:
                    aliases[key] = entry["name"]
    return aliases


def resolve_name(value, server, allow_escort_ambiguity=False):
    server = "cn" if server.startswith("cn") else server
    name = normalize_name(value)
    variants = {name}
    if name.endswith("1"):
        variants.add(name[:-1] + "I")
    matches = {name_catalog().get((server, variant)) for variant in variants} - {None}
    if not matches and server == "jp" and allow_escort_ambiguity:
        # These two JP names intentionally identify both 2h and 8h variants.
        # Start with the VIP identity, then disambiguate using its reward slots.
        entries = [entry for entry in catalog_entries()
                   if any(normalize_name(alias) in variants for alias in entry["aliases"].get(server, []))]
        codes = {entry.get("code") for entry in entries}
        for family in ("BIW", "NYB"):
            if codes == {family + " VIP Escort", family + " Patrol Escort"}:
                return next(entry["name"] for entry in entries if entry["code"] == family + " VIP Escort")
    if len(matches) != 1:
        raise ValueError("unknown_or_ambiguous_commission_name")
    return matches.pop()


def commission_identity(name, entries=()):
    entry = next((entry for entry in catalog_entries() if normalize_name(entry["name"]) == normalize_name(name)), None)
    if entry is None:
        raise ValueError("unknown_or_ambiguous_commission_name")
    code = revise_commission(entry.get("code"), entries)
    base_code = code.removesuffix(" N") if code else None
    display = next((candidate for candidate in catalog_entries() if base_code is not None
                    and candidate.get("code") == base_code), entry)
    duration = display.get("duration")
    seconds = sum(int(part) * factor for part, factor in zip(duration.split(":"), (3600, 60, 1))) if duration else 0
    return code, display["name"], seconds


class NativeCommissionReader:
    def __init__(self, stats, server):
        from module.azur_stats.assets import COMMISSION_DONE, COMMISSION_NAME, COMMISSION_PERFECT
        from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2, GET_ITEMS_3, GET_SHIP
        from module.commission.assets import EXP_INFO_S_REWARD
        from module.ocr.ocr import Ocr
        from module.ui.assets import COMMISSION_CHECK

        self.stats = stats
        self.server = "cn" if server.startswith("cn") else server
        self.success_markers = (COMMISSION_DONE, COMMISSION_PERFECT)
        self.reward_markers = (GET_ITEMS_1, GET_ITEMS_2, GET_ITEMS_3)
        self.boundary_markers = (("exp", EXP_INFO_S_REWARD), ("ship", GET_SHIP))
        self.exit_marker = COMMISSION_CHECK
        language = {"cn": "cnocr", "en": "ppocr_v6", "jp": "jp", "tw": "tw"}[self.server]
        self.name_ocr = Ocr(COMMISSION_NAME, lang=language, name="Commission_name")

    def classify(self, image):
        for status, marker in zip(("success", "perfect"), self.success_markers):
            if marker.match_template_color(image, offset=(10, 10)):
                return status
        if any(marker.match_template_color(image, offset=(5, 0)) for marker in self.reward_markers):
            return "reward"
        for kind, marker in self.boundary_markers:
            if marker.match_template_color(image, offset=(10, 10)):
                return kind
        if self.exit_marker.match_template_color(image, offset=(20, 20)):
            return "exit"
        return "unknown"

    def read_name(self, image):
        return resolve_name(self.name_ocr.ocr(image), self.server, allow_escort_ambiguity=True)

    def read_items(self, image):
        return self.stats.stats_get_items(image, tag=True)


def recognize_commissions(frame_iterator, reader, used_templates, diagnostics, candidate_builder, source_sha256=None,
                          acknowledged_cluster=None):
    """Parse one source group in file/frame order, independently of its batch."""
    events, skipped, candidates = [], [], []
    pending = None
    seen = set()

    def skip(location, reason, error=True):
        # Diagnostics determine group status, independently of the bounded log.
        if error:
            diagnostics.append(f"file:{location[0]}:frame:{location[1]}:{reason}")
        if len(skipped) < 1000:
            skipped.append({"file_index": location[0], "frame_index": location[1], "reason": reason})

    def finish_pending():
        nonlocal pending
        current, pending = pending, None
        if current is None:
            return
        if current["reward_count"] > 1:
            return
        if current["reward_count"] == 0:
            current["items"] = [{"item": "PlaceHolder", "tag": "", "amount": 1}]
            current["reward_entries"] = (("PlaceHolder", "", 1),)
        if current["name"] is None or current["items"] is None:
            return
        try:
            code, name, duration = commission_identity(current["name"], current["reward_entries"])
        except ValueError:
            skip(current["location"], "commission_name_error:unknown_or_ambiguous")
            return
        merged, corrected = {}, []
        for item, tag, amount in current["reward_entries"]:
            try:
                amount = revise_amount(code, item, amount)
                if not 0 < amount <= 1_000_000_000:
                    raise ValueError("invalid_amount")
            except ValueError as exc:
                skip(current["location"], "commission_reward_error:" + str(exc))
                current["failed"] = True
                continue
            corrected.append((item, tag, amount))
            merged[(item, tag)] = merged.get((item, tag), 0) + amount
        signature = current["status"], code, name, tuple(sorted(corrected))
        if not current["failed"] and signature in seen:
            skip(current["location"], "duplicate_commission", error=False)
            return
        if len(events) >= 1000:
            raise ValueError("Recognition output exceeds safety limit")
        if not current["failed"]:
            seen.add(signature)
        event = {"event_type": "drop", "series": 0, "project": name, "name": name, "comm": code,
                 "status": int(current["status"] == "perfect"), "focus_series": 0,
                 "duration_seconds": duration,
                 "items": [{"item": item, "tag": tag, "amount": amount}
                           for (item, tag), amount in sorted(merged.items())]}
        if source_sha256 is not None:
            event["record_id"] = hashlib.sha256(
                f"commission:{source_sha256}:{current['location'][0]}:{current['location'][1]}".encode()).hexdigest()
        unresolved = list(current["registered_items"])
        for index in current["candidate_indexes"]:
            candidate = candidates[index]
            candidate["metadata"].update(commission_name=name, commission_comm=code)
            if 0 < candidate["metadata"].get("amount", 0) <= 1_000_000_000:
                unresolved.append({"local_key": candidate["metadata"]["icon_sha256"],
                                   "candidate_id": candidate["id"], "tag": candidate["metadata"].get("tag", ""),
                                   "amount": candidate["metadata"]["amount"]})
        if unresolved:
            event["unresolved_items"] = unresolved
        events.append(event)

    for file_index, frame_index, image in frame_iterator:
        location = file_index, frame_index
        if image is None:
            finish_pending()
            skip(location, "unreadable_frame")
            continue
        try:
            kind = reader.classify(image)
        except Exception as exc:
            finish_pending()
            skip(location, "classification_error:" + type(exc).__name__)
            continue
        if kind in ("success", "perfect"):
            finish_pending()
            pending = {"location": location, "status": kind, "name": None, "reward_count": 0,
                       "items": None, "reward_entries": None, "failed": False, "candidate_indexes": []}
            pending["registered_items"] = []
            try:
                pending["name"] = reader.read_name(image)
            except Exception as exc:
                reason = ("unknown_or_ambiguous" if isinstance(exc, ValueError)
                          and str(exc) == "unknown_or_ambiguous_commission_name"
                          else type(exc).__name__)
                skip(location, "commission_name_error:" + reason)
            continue
        if kind in ("exp", "ship"):
            # Zero-reward commissions carry the upstream PlaceHolder sample.
            finish_pending()
            continue
        if kind == "exit":
            finish_pending()
            break
        if kind != "reward":
            # Losing a page boundary must not attach a later reward to an old name.
            finish_pending()
            skip(location, "unclassified_frame")
            continue
        if pending is None:
            skip(location, "reward_without_commission")
            continue
        pending["reward_count"] += 1
        # Delay acceptance until the next boundary so a second reward invalidates
        # the whole record instead of leaving a seemingly complete first page.
        if pending["reward_count"] != 1:
            if pending["reward_count"] == 2:
                skip(location, "invalid_pairing:reward_count:2")
            pending["items"] = None
            pending["reward_entries"] = None
            pending["failed"] = True
            continue
        try:
            found = list(reader.read_items(image))
            if not found:
                raise ValueError("empty_reward")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                "empty_reward"} else type(exc).__name__
            skip(location, "commission_reward_error:" + reason)
            continue
        merged, original_entries = {}, []
        for slot, item in enumerate(found):
            try:
                amount = parse_amount(item.amount)
            except (TypeError, ValueError, OverflowError):
                amount = None
                skip(location, "commission_reward_error:invalid_amount")
                pending["failed"] = True
            registered = acknowledged_cluster(item) if acknowledged_cluster else None
            if registered:
                diagnostics.append("unmapped_cluster:" + item.name)
                pending["failed"] = True
                if amount is not None and 0 < amount <= 1_000_000_000:
                    pending["registered_items"].append({"local_key": registered, "tag": item.tag or "", "amount": amount})
                else:
                    skip(location, "commission_reward_error:invalid_amount")
                continue
            if not item.is_known_item() or item.name.startswith("U-"):
                if amount is None or not 0 < amount <= 1_000_000_000:
                    skip(location, "commission_reward_error:invalid_amount")
                if len(candidates) >= 1000:
                    raise ValueError("Recognition output exceeds safety limit")
                candidate = candidate_builder(item, file_index, frame_index, slot)
                candidate["metadata"]["commission_status"] = int(pending["status"] == "perfect")
                candidate["metadata"]["commission_file_index"] = pending["location"][0]
                candidate["metadata"]["commission_frame_index"] = pending["location"][1]
                if pending["name"] is not None:
                    candidate["metadata"]["commission_name"] = pending["name"]
                candidate["metadata"]["tag"] = item.tag or ""
                if item.name.startswith("U-"):
                    candidate["metadata"]["suspected_key"] = item.name
                pending["candidate_indexes"].append(len(candidates))
                candidates.append(candidate)
                diagnostics.append("missing_template:" + candidate["id"])
                pending["failed"] = True
                continue
            if amount is None:
                continue
            key = ITEM_ALIASES.get(item.name, item.name), item.tag or ""
            # Upstream can repair a zero OCR result for CognitiveChips. Validate
            # the final value after commission-specific correction at the boundary.
            if not 0 <= amount <= 1_000_000_000 or merged.get(key, 0) + amount > 1_000_000_000:
                skip(location, "commission_reward_error:invalid_amount")
                pending["failed"] = True
                continue
            original_entries.append((*key, amount))
            merged[key] = merged.get(key, 0) + amount
        # Keep recognized rows for review, including when another slot is unknown.
        # Only a group without diagnostics can contribute to C's statistics.
        pending["reward_entries"] = tuple(sorted(original_entries))
        pending["items"] = [{"item": name, "tag": tag, "amount": amount}
                            for (name, tag), amount in sorted(merged.items())]
    finish_pending()
    if not events:
        diagnostics.append("no_complete_commission")
    failures = sorted(set(diagnostics))
    if len(candidates) > 1000 or len(failures) > 1000:
        raise ValueError("Recognition output exceeds safety limit")
    return {"events": events, "diagnostics": failures,
            "candidates": candidates, "used_templates": sorted(used_templates),
            "status": "needs_review" if failures or not events else "complete",
            "skipped_frames": skipped}
