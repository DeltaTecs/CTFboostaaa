"""Run the pinned HexStrike service in one host execution context.

This is a Phase 0 compatibility experiment, not a production host runner.
No installed files are changed. Never run this launcher against an unreviewed pin.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import sys

PIN = Path(__file__).resolve().parents[1] / "deploy/hexstrike-pin.json"


def verify_source(source):
    pin = json.loads(PIN.read_text())
    for filename, expected in pin["sha256"].items():
        actual = hashlib.sha256((source / filename).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"HexStrike source differs from reviewed pin: {filename}")
    return pin


class ScopedTemporaryPaths(ast.NodeTransformer):
    """Remap upstream literal /tmp paths, including f-string constant prefixes."""
    def __init__(self, tmp):
        self.tmp = str(tmp)
        self.replacements = 0

    def visit_Constant(self, node):
        if isinstance(node.value, str) and node.value.startswith("/tmp/"):
            self.replacements += 1
            return ast.copy_location(ast.Constant(self.tmp + node.value[4:]), node)
        return node


def public_process(info):
    return {key: value for key, value in info.items() if key != "process"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    pin = verify_source(source)
    cwd = Path.cwd()
    tmp = Path(os.environ["TMPDIR"]).resolve()
    if not tmp.is_relative_to(cwd) or tmp == cwd:
        raise ValueError("TMPDIR must be a dedicated directory inside the agent workspace")
    tmp.mkdir(parents=True, exist_ok=True)
    filename = source / "hexstrike_server.py"
    tree = ast.parse(filename.read_text(), filename=str(filename))
    rewrite = ScopedTemporaryPaths(tmp)
    tree = rewrite.visit(tree)
    ast.fix_missing_locations(tree)
    namespace = {"__name__": "harness_hexstrike", "__file__": str(filename)}
    sys.path.insert(0, str(source))
    exec(compile(tree, str(filename), "exec"), namespace)

    original_execute = namespace["execute_command"]

    def uncached_execute(command, use_cache=True):
        return original_execute(command, use_cache=False)

    # All upstream routes resolve this module-global at execution time.
    namespace["execute_command"] = uncached_execute
    # Upstream process APIs otherwise try to JSON-serialize Popen instances.
    manager = namespace["ProcessManager"]
    original_list = manager.list_active_processes
    original_status = manager.get_process_status
    manager.list_active_processes = staticmethod(
        lambda: {pid: public_process(info) for pid, info in original_list().items()})
    manager.get_process_status = staticmethod(
        lambda pid: public_process(info) if (info := original_status(pid)) else None)

    from werkzeug.serving import make_server
    # The upstream __main__ binds 0.0.0.0 and ignores HEXSTRIKE_HOST.
    # Bind an OS-assigned loopback port atomically instead.
    server = make_server("127.0.0.1", 0, namespace["app"], threaded=True)
    ready = {"pid": os.getpid(), "pgid": os.getpgrp(), "port": server.server_port,
             "cwd": str(cwd), "tmp": str(tmp), "revision": pin["revision"],
             "temporary_path_replacements": rewrite.replacements,
             "cache_disabled": True, "process_json_fixed": True}
    pending = cwd / "ready.pending"
    pending.write_text(json.dumps(ready, indent=2) + "\n")
    pending.replace(cwd / "ready.json")
    server.serve_forever()


if __name__ == "__main__":
    main()
