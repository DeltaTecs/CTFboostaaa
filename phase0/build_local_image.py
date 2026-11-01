"""Import a temporary Python image from installed host files, without a registry.

Optional Linux-only helper for hosts with Docker but no cached images. The caller
owns image cleanup. No packages are installed and no network request is made.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sysconfig
import tarfile
import tempfile


def build(tag, sudo=False):
    if not tag.startswith("ctf-harness-phase0:") or not re.fullmatch(r"[a-z0-9:._-]+", tag):
        raise ValueError("Use a unique ctf-harness-phase0:TAG image name")
    binary = Path("/usr/bin/python3").resolve()
    stdlib = Path(sysconfig.get_path("stdlib"))
    linked = set()
    for executable in [binary, *stdlib.glob("lib-dynload/*.so")]:
        output = subprocess.check_output(["ldd", str(executable)], text=True)
        linked.update(re.findall(r"(/[^\s()]+)", output))
    docker = ["sudo", "-n", "docker"] if sudo else ["docker"]
    existing = subprocess.run([*docker, "image", "inspect", tag], capture_output=True, timeout=10)
    if existing.returncode == 0:
        raise ValueError("Image tag already exists; choose a new tag")
    # Build the root filesystem in a temporary archive, then remove the archive.
    with tempfile.TemporaryFile() as archive:
        with tarfile.open(fileobj=archive, mode="w", dereference=True) as tar:
            tar.add(binary, arcname="usr/bin/python3")

            def installed_runtime(info):
                if any(part in ("__pycache__", "test", "tests", "site-packages", "dist-packages")
                       for part in Path(info.name).parts):
                    return None
                return info

            tar.add(stdlib, arcname=str(stdlib).lstrip("/"), filter=installed_runtime)
            for library in sorted(linked):
                tar.add(library, arcname=library.lstrip("/"))
        archive.seek(0)
        imported = subprocess.run([*docker, "import", "-", tag], stdin=archive,
                                  capture_output=True, text=True, timeout=30, check=True)
    return {"tag": tag, "image_id": imported.stdout.strip(),
            "source": "installed host Python and linked libraries; no registry access"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sudo", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.tag, args.sudo), indent=2))


if __name__ == "__main__":
    main()
