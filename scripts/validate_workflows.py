#!/usr/bin/env python3
"""Validate the exported n8n workflow graphs.

An n8n export is a file nobody reads carefully and everybody edits through a
UI, so it drifts. These checks catch the drift that actually breaks a run:
a connection pointing at a renamed node, a node nothing reaches, or more
than one trigger.

Run locally:  python scripts/validate_workflows.py
"""

import json
import sys
from pathlib import Path

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / "workflows"
TRIGGER_MARKERS = ("trigger", "webhook")


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        workflow = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{path.name}: not valid JSON: {exc}"]

    nodes = workflow.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return [f"{path.name}: has no nodes"]

    names = [n.get("name") for n in nodes]
    if len(names) != len(set(names)):
        errors.append(f"{path.name}: duplicate node names")

    name_set = set(names)
    connections = workflow.get("connections", {})
    targets: set[str] = set()

    for source, conn in connections.items():
        if source not in name_set:
            errors.append(f"{path.name}: connection source '{source}' is not a node")
        for branch in conn.get("main", []):
            for link in branch:
                target = link.get("node")
                targets.add(target)
                if target not in name_set:
                    errors.append(
                        f"{path.name}: '{source}' connects to unknown node '{target}'"
                    )

    triggers = {
        n["name"]
        for n in nodes
        if any(marker in n.get("type", "").lower() for marker in TRIGGER_MARKERS)
    }
    if not triggers:
        errors.append(f"{path.name}: no trigger node")

    unreached = name_set - targets
    orphans = unreached - triggers
    if orphans:
        errors.append(
            f"{path.name}: nodes nothing connects to: {sorted(orphans)}"
        )

    for node in nodes:
        if not node.get("type"):
            errors.append(f"{path.name}: node '{node.get('name')}' has no type")
        if node.get("typeVersion") is None:
            errors.append(
                f"{path.name}: node '{node.get('name')}' has no typeVersion"
            )

    return errors


def main() -> int:
    files = sorted(WORKFLOW_DIR.glob("*.json"))
    if not files:
        print(f"No workflow files found in {WORKFLOW_DIR}", file=sys.stderr)
        return 1

    all_errors: list[str] = []
    for path in files:
        errors = validate(path)
        status = "FAIL" if errors else "ok"
        print(f"[{status}] {path.name}")
        all_errors.extend(errors)

    for error in all_errors:
        print(f"  {error}", file=sys.stderr)

    if all_errors:
        print(f"\n{len(all_errors)} problem(s) found.", file=sys.stderr)
        return 1
    print(f"\n{len(files)} workflow(s) validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
