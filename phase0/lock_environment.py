"""Record the active interpreter's dependency pins; does not install anything."""
import argparse
from importlib import metadata
from pathlib import Path
import platform
import subprocess
import sys


def main():
    from packaging.requirements import Requirement

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("deploy"))
    args = parser.parse_args()
    versions = {}

    def collect(name):
        dist = metadata.distribution(name)
        key = dist.metadata["Name"].lower().replace("_", "-")
        if key in versions:
            return
        versions[key] = dist.version
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                collect(requirement.name)

    collect("mcp")
    args.output.mkdir(parents=True, exist_ok=True)
    header = (f"# Exact Phase 0 MCP dependency closure observed on {platform.system()}, "
              f"Python {platform.python_version()}.\n"
              "# Regenerate for another Python/platform; see docs/integration-findings.md.\n")
    (args.output / "requirements-phase0.lock").write_text(
        header + "".join(f"{name}=={versions[name]}\n" for name in sorted(versions)))
    freeze = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
    (args.output / "hexstrike-environment.freeze.txt").write_text(
        "# Observed installed HexStrike environment, not a new application dependency list.\n" + freeze)


if __name__ == "__main__":
    main()
