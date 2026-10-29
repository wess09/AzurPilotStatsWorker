"""Runs in ALAS_PYTHON, isolating the existing OCR runtime from FastAPI.

Input/output JSON files are private work files; original PNGs are removed by
the supervisor in a finally block. No game/device automation is started.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback
from functools import lru_cache


_template_cache_installed = False


@lru_cache(maxsize=512)
def published_template(encoded):
    import cv2
    import numpy as np
    raw = base64.b64decode(encoded, validate=True)
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('Invalid published template')
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    if image.shape[:2] == (96, 96):
        # Folder assets use a full item cell; candidate publications use its ROI.
        image = image[21:70, 40:89].copy()
    image.flags.writeable = False
    return image, cv2.mean(image)[:3]


def item_candidate(item, sample_sha256, metadata, file_index, frame_index, slot):
    import cv2
    if __package__:
        from .icons import fingerprint
        from .commission_rules import parse_amount
    else:
        from icons import fingerprint
        from commission_rules import parse_amount
    icon = item.image[21:70, 40:89]
    if icon.shape != (49, 49, 3):
        raise ValueError("Candidate icon must be 49x49 RGB")
    pixel_hash, phash = fingerprint(icon)
    ok, encoded = cv2.imencode(".png", cv2.cvtColor(icon, cv2.COLOR_RGB2BGR))
    if not ok:
        raise ValueError("Candidate encoding failed")
    raw = encoded.tobytes()
    candidate = {
        "id": hashlib.sha256(raw).hexdigest(),
        "scene": metadata["project"],
        "image": base64.b64encode(raw).decode(),
        "sample_sha256": sample_sha256,
        "occurrence": f"{file_index}:{frame_index}:{slot}",
        "metadata": {"file_index": file_index, "frame_index": frame_index, "slot": slot,
                     "box": [int(v) for v in item._button.area], "server": metadata["server"],
                     "scene": metadata["scene"],
                     "icon_sha256": pixel_hash,
                     "phash": phash, "phash_algorithm": "dct64-gray-area32-v1"},
    }
    try:
        candidate["metadata"]["amount"] = parse_amount(item.amount)
    except (TypeError, ValueError, OverflowError):
        pass  # An unreadable quantity must not prevent classifying the icon.
    return candidate


def cache_base_templates(item_grid):
    global _template_cache_installed
    if _template_cache_installed:
        return
    original = item_grid.load_template_folder

    @lru_cache(maxsize=8)
    def load(folder, area):
        grid = item_grid(None, {}, template_area=area)
        original(grid, folder)
        return grid.templates, grid.colors, grid.next_template_index

    def cached(self, folder):
        templates, colors, next_index = load(str(folder), tuple(self.template_area))
        for name, template in templates.items():
            if name not in self.templates:
                self.templates[name] = template
                self.colors[name] = colors[name]
                self.templates_hit[name] = 0
        self.next_template_index = max(self.next_template_index, next_index)

    item_grid.load_template_folder = cached
    _template_cache_installed = True


def research_focus(image, series_list):
    # Same visual check and >=3 majority rule as AzurStats ResearchList.
    from module.azur_stats.image.base import ImageBase
    from module.azur_stats.assets import FOCUS_SERIES
    if not ImageBase().image_color_count(image, FOCUS_SERIES, (41, 50, 57), threshold=235, count=100):
        return 0
    return next((int(s) for s in series_list if s and series_list.count(s) >= 3), 0)


def frames(paths, diagnostics, recover=False):
    import numpy as np
    from PIL import Image
    for file_index, path in enumerate(paths):
        try:
            with Image.open(path) as png:
                if png.format != "PNG" or png.width != 1280 or png.height % 720 or not 1 <= png.height // 720 <= 10:
                    diagnostics.append(f"file:{file_index}:unsupported_resolution")
                    if recover:
                        yield file_index, 0, None
                    continue
                for index in range(png.height // 720):
                    yield file_index, index, np.asarray(png.crop((0, index * 720, 1280, (index + 1) * 720)).convert("RGB"))
        except (OSError, ValueError) as exc:
            if not recover:
                raise
            diagnostics.append(f"file:{file_index}:unreadable_image:{type(exc).__name__}")
            yield file_index, 0, None


def recognize(request):
    root = Path(request["alas_root"]).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.chdir(root)
    import cv2
    import numpy as np
    from PIL import Image
    import module.config.server as server
    server.server = "cn" if request["source"]["metadata"]["server"].startswith("cn") else request["source"]["metadata"]["server"]
    from module.statistics.get_items import GetItemsStatistics
    from module.statistics.item import ItemGrid
    from module.statistics.research_drop import ResearchDropParser
    cache_base_templates(ItemGrid)
    cv2.setNumThreads(int(os.getenv('OCR_THREADS', '1')))

    source = request["source"]
    meta = dict(source["metadata"])
    diagnostics = []
    overrides = {}
    if meta["scene"] == "legacy_auto":
        from module.research.assets import QUEUE_CHECK
        from module.ui.assets import RESEARCH_CHECK
        from module.azur_stats.scene.operation_siren import SceneOperationSiren
        detector = SceneOperationSiren()
        for _, _, frame in frames(request["files"], diagnostics):
            if QUEUE_CHECK.match(frame, offset=(10, 10)):
                overrides = {"project": "research", "scene": "research_complete"}
                break
            if RESEARCH_CHECK.match(frame, offset=(10, 10)):
                overrides = {"project": "research", "scene": "research_refresh"}
            elif detector.is_opsi_zone(frame) and not overrides:
                overrides = {"project": "drop", "scene": "opsi_meowfficer_farming"}
        if not overrides:
            return {"events": [], "diagnostics": ["legacy_scene_requires_classification"], "candidates": [], "used_templates": [], "status": "needs_review"}
        meta.update(overrides)
    research = meta["project"] == "research"
    opsi = meta["scene"].startswith("opsi_")
    parser = ResearchDropParser() if research else None
    zone = {"zone": "", "zone_type": "UNKNOWN", "zone_id": 0, "hazard_level": 0}
    cleared = (-1, -1)
    if parser:
        stats = parser.stats
        grids = [stats._target_grid()]
    elif opsi:
        from module.azur_stats.scene.operation_siren import SceneOperationSiren
        stats = SceneOperationSiren()
        grids = [stats.item_grid, stats.auto_search_item_group]
        for file_index, frame_index, frame in frames(request["files"], diagnostics):
            if stats.is_opsi_zone(frame):
                try:
                    parsed_zone = stats.parse_opsi_zone(frame)
                    zone = {key: getattr(parsed_zone, key) for key in zone}
                    cleared = (file_index, frame_index)
                except Exception:
                    diagnostics.append("unrecognized_opsi_zone")
                break
    else:
        stats = GetItemsStatistics()
        stats.grid = ItemGrid(None, {})
        if meta["scene"].startswith("commission"):
            stats.load_template_folder("./assets/stats/commission_items")
        else:
            stats.load_template_folder("./assets/stats/battle_items")
        grids = [stats._target_grid()]
    overlay, temporary = {}, {}
    for t in request["templates"]:
        if not t["enabled"] or t["scene"] not in (meta["project"], "all"):
            continue
        image, color = published_template(t['image'])
        key = t["item_id"] + "_" + str(int(t["id"][:12], 16))
        overlay[key] = t["id"]
        if t.get("kind") == "temporary":
            temporary[key] = float(t.get("temporary_similarity", 0.95))
        for grid in grids:
            grid.templates[key] = image
            grid.colors[key] = color
            grid.templates_hit[key] = 0
    if request.get("match_thresholds") is not None or temporary:
        if __package__:
            from .template_package import thresholds
            from .template_matching import install_priorities
        else:
            from template_package import thresholds
            from template_matching import install_priorities
        parameters = thresholds(request.get("match_thresholds"))
        for grid in grids:
            install_priorities(grid, temporary, parameters["formal"] if not opsi else None)
    used = set()
    def track(match):
        def tracked_match(image, *args, **kwargs):
            key = match(image, *args, **kwargs)
            if key in overlay:
                used.add(overlay[key])
            return key
        return tracked_match
    for grid in grids:
        grid.match_template = track(grid.match_template)
    def build_candidate(item, file_index, frame_index, slot):
        return item_candidate(item, source["sha256"], meta, file_index, frame_index, slot)
    known_icons, overrides = request.get("known_icons", {}), request.get("hash_overrides", {})
    def acknowledged_cluster(item):
        if not overrides and not item.name.startswith("U-"):
            return None
        pixel_hash = hashlib.sha256(item.image[21:70, 40:89].tobytes()).hexdigest()
        if pixel_hash in overrides:
            item.name = overrides[pixel_hash]
            used.add(pixel_hash)
            return pixel_hash if item.name.startswith("U-") else None
        return pixel_hash if item.name.startswith("U-") and known_icons.get(pixel_hash) == item.name else None
    if not research and meta["scene"].startswith("commission"):
        if __package__:
            from .commission import NativeCommissionReader, recognize_commissions
        else:
            from commission import NativeCommissionReader, recognize_commissions
        return recognize_commissions(frames(request["files"], diagnostics, recover=True),
                                     NativeCommissionReader(stats, meta["server"]), used, diagnostics,
                                     build_candidate, source_sha256=source["sha256"],
                                     acknowledged_cluster=acknowledged_cluster)
    candidates = []
    unresolved = {}
    events = []
    # Repeated reward pages can overlap. The upstream merger identifies the
    # repeated item by name, amount and reward tag within one reward event.
    rewards = {}
    project = ""
    series = 0
    reward_frames = 0
    from module.research.assets import QUEUE_CHECK
    from module.research.project_data import LIST_RESEARCH_PROJECT
    catalog = {(x["series"], x["name"]): x for x in LIST_RESEARCH_PROJECT}

    for file_index, frame_index, frame in frames(request["files"], diagnostics):
        if research and meta["scene"] == "research_refresh":
            from module.research.project import get_research_name
            from module.research.series import get_research_series_3
            names = get_research_name(frame)
            series_list = get_research_series_3(frame)
            if len(names) != 5 or len(series_list) != 5 or any((int(s), n) not in catalog for s, n in zip(series_list, names)):
                diagnostics.append("research_refresh:incomplete_project_list")
            else:
                focus = research_focus(frame, series_list)
                for s, n in zip(series_list, names):
                    events.append({"event_type": "refresh", "series": int(s), "project": n, "focus_series": focus, "duration_seconds": 0, "items": []})
            continue
        if research and QUEUE_CHECK.match(frame, offset=(10, 10)):
            project = parser._read_project(frame)
            series = parser._read_series(frame)
            continue
        try:
            forced_tag = None
            if opsi:
                if (file_index, frame_index) == cleared:
                    continue
                if stats.is_get_items(frame):
                    found = list(stats.parse_get_items(frame))
                    forced_tag = "log" if (file_index, frame_index) > cleared else None
                elif stats.is_opsi_reward(frame):
                    found = list(stats.parse_auto_search_reward(frame))
                    forced_tag = "scan" if (file_index, frame_index) > cleared else None
                else:
                    continue
            else:
                found = stats.stats_get_items(frame, tag=True)
        except Exception as exc:
            if type(exc).__name__ != "ImageError":
                diagnostics.append(f"file:{file_index}:frame:{frame_index}:recognition_error:{type(exc).__name__}:{exc}")
            continue
        reward_frames += 1
        for slot, item in enumerate(found):
            registered = acknowledged_cluster(item)
            if registered:
                diagnostics.append("unmapped_cluster:" + item.name)
                if item.amount > 0:
                    tag = forced_tag or item.tag or ""
                    unresolved[(file_index, frame_index, slot)] = {"local_key": registered, "tag": tag, "amount": int(item.amount)}
            elif not item.is_known_item() or item.name.startswith("U-"):
                candidate = build_candidate(item, file_index, frame_index, slot)
                candidates.append(candidate)
                diagnostics.append("missing_template:" + candidate["id"])
                tag = forced_tag or item.tag or ""
                candidate["metadata"]["tag"] = tag
                if item.name.startswith("U-"):
                    candidate["metadata"]["suspected_key"] = item.name
                amount = candidate["metadata"].get("amount", 0)
                if 0 < amount <= 1_000_000_000:
                    key = (file_index, frame_index, slot) if opsi else (candidate["metadata"]["icon_sha256"], tag, amount)
                    unresolved[key] = {"local_key": candidate["metadata"]["icon_sha256"],
                                       "candidate_id": candidate["id"], "tag": tag, "amount": amount}
                else:
                    diagnostics.append("invalid_amount:" + candidate["id"])
            elif item.amount <= 0:
                diagnostics.append("invalid_amount:" + item.name)
            else:
                tag = forced_tag or item.tag or ""
                key = (file_index, frame_index, slot) if opsi else (item.name, tag, int(item.amount))
                rewards[key] = {"item": item.name, "tag": tag, "amount": int(item.amount)}
    if meta["scene"] != "research_refresh":
        if not reward_frames or not rewards and not unresolved:
            diagnostics.append("no_complete_reward_frame")
        duration = 0
        if research:
            entry = catalog.get((series, project))
            if entry is None:
                diagnostics.append("unknown_research_project_or_series")
            else:
                value = entry.get("time", entry.get("duration", 0))
                duration = int(value) if isinstance(value, (int, float)) else 0
        merged = {}
        for reward in rewards.values():
            key = reward["item"], reward["tag"]
            if key not in merged:
                merged[key] = dict(reward)
            else:
                merged[key]["amount"] += reward["amount"]
        if not research or (series and project):
            event = {"event_type": "complete" if research else "drop", "series": int(series), "project": project, "focus_series": 0, "duration_seconds": duration, "items": sorted(merged.values(), key=lambda x: (x["item"], x["tag"]))}
            if unresolved:
                event["unresolved_items"] = list(unresolved.values())
            if opsi:
                event.update(zone)
            events.append(event)
    if len(events) > 1000 or len(candidates) > 1000:
        raise ValueError("Recognition output exceeds safety limit")
    diagnostics = sorted(set(diagnostics))
    return {"events": events, "metadata_overrides": overrides, "diagnostics": diagnostics, "candidates": candidates, "used_templates": sorted(used), "status": "needs_review" if diagnostics or not events else "complete"}


def run_request(request_path, output_path):
    try:
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        if request.get("operation") == "match_icons":
            if __package__:
                from .icons import match_icons
            else:
                from icons import match_icons
            result = match_icons(request)
        else:
            result = recognize(request)
    except Exception:
        result = {"events": [], "diagnostics": [traceback.format_exc()[-2000:]], "candidates": [], "used_templates": [], "status": "needs_review"}
    destination = Path(output_path)
    staged = destination.with_suffix('.tmp')
    staged.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    os.replace(staged, destination)


if __name__ == '__main__':
    if sys.argv[1:] == ['--serve']:
        # Keep imports, OCR models and base icon crops warm; no original images
        # survive a request. Retire periodically to bound third-party caches.
        maximum = int(os.getenv('OCR_PROCESS_MAX_JOBS', '1000'))
        for index, line in enumerate(sys.stdin):
            run_request(*json.loads(line))
            if index + 1 >= maximum:
                break
    else:
        run_request(*sys.argv[1:3])
