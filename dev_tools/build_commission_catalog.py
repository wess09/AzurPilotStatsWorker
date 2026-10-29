"""Enrich the OCR alias snapshot using a pinned AzurStats data.py, offline."""
import argparse
import ast
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker.commission import normalize_name


def build(snapshot, upstream):
    tree = ast.parse(upstream.read_text(encoding="utf-8"))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "LIST_COMMISSION_DATA"
                              for target in node.targets))
    rows = ast.literal_eval(assignment.value)
    catalog = {}
    for row in rows:
        name = normalize_name("".join(row["cn"]))
        facts = {"code": row["comm"], "duration": row["duration"], "expiration": row["expiration"]}
        if name in catalog and catalog[name] != facts:
            raise ValueError("Conflicting source rows: " + name)
        catalog[name] = facts
    for entry in snapshot["entries"]:
        entry.update(catalog.get(normalize_name(entry["name"]),
                                 {"code": None, "duration": None, "expiration": None}))
    snapshot["sources"]["catalog"] = snapshot["sources"]["translations"]
    return snapshot


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("--catalog", type=Path,
                        default=Path(__file__).resolve().parents[1] / "worker/commission_names.json")
    args = parser.parse_args()
    snapshot = json.loads(args.catalog.read_text(encoding="utf-8"))
    args.catalog.write_text(json.dumps(build(snapshot, args.upstream), ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
