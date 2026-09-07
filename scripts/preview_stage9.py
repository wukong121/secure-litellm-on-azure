#!/usr/bin/env python3
"""Run read-only Azure What-if; never deploy or change DNS."""

import argparse
import json
from pathlib import Path
import subprocess

try:
    from scripts.stage9_release import ROOT, generate, validate_what_if
except ModuleNotFoundError:
    from stage9_release import ROOT, generate, validate_what_if


def preview(resource_group: str, layer: str, output_dir: Path, config: dict | None = None) -> dict:
    if layer not in {"edge", "edge-origin"} or (layer != "edge" and config is not None):
        raise ValueError("Release configuration applies only to edge; origin preview uses disabled defaults")
    destination = output_dir.resolve()
    if not destination.is_relative_to((ROOT / "temp").resolve()) or destination == (ROOT / "temp").resolve():
        raise ValueError("What-if output must stay in an ignored temp/ subdirectory")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    parameters = ROOT / f"infra/{layer}/main.bicepparam"
    if config is not None:
        generate(config, destination)
        parameters = destination / "edge.parameters.json"
    command = ["az", "deployment", "group", "what-if", "--resource-group", resource_group]
    if config is not None:
        command.extend(["--template-file", str(ROOT / "infra/edge/main.bicep")])
    command.extend(["--parameters", str(parameters), "--result-format", "FullResourcePayloads", "--no-pretty-print", "--output", "json"])
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    for name, content in ((f"{layer}.what-if.json", result.stdout), (f"{layer}.diagnostics.txt", result.stderr)):
        path = destination / name
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(content, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("Azure What-if failed; inspect the ignored local diagnostics file")
    counts = validate_what_if(json.loads(result.stdout))
    return {"layer": layer, "counts": counts, "enabledScenario": config is not None, "deploymentPerformed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--layer", choices=("edge", "edge-origin"), default="edge")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/stage9-what-if")
    args = parser.parse_args()
    print(json.dumps(preview(args.resource_group, args.layer, args.output_dir, json.loads(args.config.read_text()) if args.config else None)))