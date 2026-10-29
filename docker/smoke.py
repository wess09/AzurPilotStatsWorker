"""Offline image acceptance: actual OCR, unknown crop, and template reuse."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont


def main():
    root = Path(os.environ["ALAS_ROOT"])
    adapter = Path(__file__).resolve().parents[1] / "worker/recognize.py"
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        screen = Image.open(root / "assets/cn/combat/GET_ITEMS_1.png").convert("RGB")
        item = Image.open(root / "assets/stats/research_items/BlueprintHalford.png").convert("RGB")
        draw = ImageDraw.Draw(item)
        draw.rectangle((60, 71, 91, 95), fill="black")
        draw.text((80, 71), "1", font=ImageFont.load_default(size=20), fill="white")
        screen.paste(item, (400, 298))
        screenshot = work / "screenshot.png"
        screen.save(screenshot)
        request = {"alas_root": str(root), "files": [str(screenshot)], "templates": [], "source": {"sha256": "a" * 64, "metadata": {"project": "drop", "scene": "campaign", "server": "cn"}}}

        # All scenarios share a resident process, verifying that publishing and
        # removing overlays does not leak prior templates into the next task.
        sys.path.insert(0, str(adapter.parents[1]))
        from worker.pool import RecognizerPool
        pool = RecognizerPool(1)
        def parse():
            source, target = work / "request.json", work / "result.json"
            source.write_text(json.dumps(request), encoding="utf-8")
            target.unlink(missing_ok=True)
            pool.recognize(source,target,'cn',timeout=180)
            return json.loads(target.read_text(encoding="utf-8"))

        first = parse()
        assert first["status"] == "needs_review" and first["candidates"], first
        candidate = first["candidates"][0]
        request["templates"] = [{"id": candidate["id"], "item_id": "ImageSmokeBlueprint", "scene": "all", "enabled": True, "image": candidate["image"]}]
        second = parse()
        assert second["status"] == "complete", second
        assert second["events"][0]["items"] == [{"item": "ImageSmokeBlueprint", "amount": 1, "tag": ""}], second
        assert second["used_templates"] == [candidate["id"]], second
        assert len(pool.processes) == 1
        request['templates'] = []
        removed = parse()
        assert removed['status'] == 'needs_review' and removed['candidates'], removed
        assert removed['used_templates'] == [], removed
        request['templates'] = [{"id": candidate["id"], "item_id": "ImageSmokeBlueprint", "scene": "all", "enabled": True, "image": candidate["image"]}]

        # Operation Siren uses its own grids and sums equal drops from distinct
        # frames. The generic reward-page overlap merger must not remove them.
        request["source"]["metadata"]["scene"] = "opsi_meowfficer_farming"
        request["files"] = [str(screenshot), str(screenshot)]
        opsi = parse()
        assert opsi["status"] == "complete", opsi
        assert opsi["events"][0]["items"] == [{"item": "ImageSmokeBlueprint", "amount": 2, "tag": "log"}], opsi
        assert opsi["events"][0]["hazard_level"] == 0, opsi

        # Unknown file-only scenes are retained for review, not guessed into a
        # random statistics project.
        request["source"]["metadata"]["scene"] = "legacy_auto"
        ambiguous = parse()
        assert ambiguous["status"] == "needs_review", ambiguous
        assert "legacy_scene_requires_classification" in ambiguous["diagnostics"], ambiguous

        sys.path.insert(0, str(root))
        os.chdir(root)
        sys.path.insert(0, str(adapter.parent))
        import numpy as np
        from recognize import research_focus
        focused = np.zeros((720, 1280, 3), dtype=np.uint8)
        focused[652:690, 821:859] = (41, 50, 57)
        assert research_focus(focused, [9, 9, 9, 8, 7]) == 9
        assert research_focus(focused, [9, 9, 8, 8, 7]) == 0
        focused[652:690, 821:859] = (255, 255, 255)
        assert research_focus(focused, [9, 9, 9, 8, 7]) == 0

        # Exercise the actual research-list path in the minimal image too. A
        # generic reward-page smoke test misses accidental Android dependencies.
        request["source"]["metadata"].update(project="research", scene="research_refresh")
        request["files"] = [str(screenshot)]
        request["templates"] = []
        refresh = parse()
        assert not any("Traceback" in d for d in refresh["diagnostics"]), refresh
        assert refresh["status"] == "needs_review", refresh
        assert "research_refresh:incomplete_project_list" in refresh["diagnostics"], refresh

        pool.close()
    print("Offline OCR and template smoke test passed")


if __name__ == "__main__":
    main()
