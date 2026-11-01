"""Repeatable host-only HexStrike Phase 0 assessment. No target scans.

Uses the existing service on 127.0.0.1:8888, then launches six lazy, temporary
services on OS-assigned loopback ports. Retains evidence; stops owned processes.
Run on the Kali host with the pinned HexStrike Python environment.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shlex
import signal
import subprocess
import sys
import time
import uuid

from .hexstrike_service import verify_source
from .http import local_url, request_json
from .providers import smoke

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(script):
    return shlex.join([sys.executable, "-c", script])


def run(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return {"returncode": result.returncode, "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip()}
    except FileNotFoundError:
        return {"status": "not_installed"}


def process_metrics(pid):
    fields = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("VmRSS", "Threads"):
            fields[key] = int(value.split()[0])
    rollup = Path(f"/proc/{pid}/smaps_rollup")
    if rollup.exists():
        for line in rollup.read_text().splitlines():
            key, _, value = line.partition(":")
            if key in ("Pss", "Private_Clean", "Private_Dirty"):
                fields[key] = int(value.split()[0])
    return fields


def alive(pid):
    try:
        # A reaped/dead process and a zombie are both no longer executing.
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False


class Context:
    def __init__(self, directory, source, agent):
        self.directory, self.source, self.agent = directory, source, agent
        for name in ("artifacts", "tmp", "cache", "config"):
            (directory / name).mkdir(parents=True)
        self.process = None
        self.log = None
        self.ready = None
        self.startup_seconds = None
        self.observed_children = []
        self.stopped = False

    def start(self):
        require(self.process is None, "Execution context already started")
        env = {**os.environ, "TMPDIR": str(self.directory / "tmp"),
               "TMP": str(self.directory / "tmp"), "TEMP": str(self.directory / "tmp"),
               "XDG_CACHE_HOME": str(self.directory / "cache"),
               "XDG_CONFIG_HOME": str(self.directory / "config"),
               "HARNESS_AGENT_ID": self.agent, "PYTHONUNBUFFERED": "1",
               "PYTHONPATH": str(ROOT)}
        self.log = (self.directory / "service.log").open("w")
        start = time.monotonic()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "phase0.hexstrike_service",
             "--source", str(self.source)], cwd=self.directory, env=env,
            stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = start + 30
        ready_path = self.directory / "ready.json"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"HexStrike startup failed; see {self.directory / 'service.log'}")
            if ready_path.exists():
                self.ready = json.loads(ready_path.read_text())
                request_json(self.endpoint + "/health", timeout=5)
                self.startup_seconds = round(time.monotonic() - start, 3)
                return
            time.sleep(0.05)
        raise TimeoutError("HexStrike did not become ready within 30 seconds")

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.ready['port']}"

    def stop(self):
        if self.stopped:
            return
        if self.process:
            pgid = self.process.pid  # start_new_session makes this process the group leader.
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            # Also settle normal command descendants if the service already exited.
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=3)
        if self.log:
            self.log.close()
        self.stopped = True


@asynccontextmanager
async def bridge(source, endpoint, directory):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    local_url(endpoint)
    params = StdioServerParameters(command=sys.executable,
                                   args=[str(source / "hexstrike_mcp.py"),
                                         "--server", endpoint, "--timeout", "20"],
                                   cwd=str(directory),
                                   env={**os.environ, "NO_PROXY": "127.0.0.1,::1"})
    with (directory / "bridge.log").open("w") as log:
        async with stdio_client(params, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer,
                                     read_timeout_seconds=timedelta(seconds=30)) as session:
                initialization = await session.initialize()
                yield session, initialization


def decode_result(result):
    require(not result.isError, "MCP tool returned an error")
    if result.structuredContent is not None:
        data = result.structuredContent
        # The installed SDK wraps Dict[str, Any] return annotations under result;
        # its text block contains the unwrapped object.
        if set(data) == {"result"} and isinstance(data["result"], dict):
            data = data["result"]
    else:
        data = json.loads("".join(item.text for item in result.content if item.type == "text"))
    require(data.get("success", False), "HexStrike tool execution failed")
    require(not data.get("timed_out", False), "HexStrike command timed out")
    if "return_code" in data:
        require(data["return_code"] == 0, "HexStrike command exited nonzero")
    return data


CONTEXT_COMMAND = """
import json, os, pathlib, subprocess, sys, tempfile, time
start = time.time()
agent = os.environ['HARNESS_AGENT_ID']
pathlib.Path('relative-output.txt').write_text(agent)
pathlib.Path('artifacts/sample.txt').write_text('phase0-local-fixture-' + agent)
with tempfile.NamedTemporaryFile(prefix='phase0-', delete=False) as f:
    f.write(agent.encode())
    temporary = f.name
child_code = "import json,os,pathlib; pathlib.Path('child-output.txt').write_text(os.environ['HARNESS_AGENT_ID']); print(json.dumps({'pid':os.getpid(),'ppid':os.getppid(),'pgid':os.getpgrp(),'cwd':os.getcwd()}))"
child = json.loads(subprocess.check_output([sys.executable, '-c', child_code], text=True))
time.sleep(1)
print(json.dumps({'agent':agent, 'cwd':os.getcwd(), 'tmp':temporary,
                  'cache':os.environ['XDG_CACHE_HOME'], 'pid':os.getpid(),
                  'ppid':os.getppid(), 'pgid':os.getpgrp(), 'child':child,
                  'start':start, 'end':time.time()}))
"""


async def check_context(context, barrier):
    async with bridge(context.source, context.endpoint, context.directory) as (session, init):
        await barrier.wait()
        # Identical command and relative filenames in two concurrently active services.
        result = decode_result(await session.call_tool("execute_command", {
            "command": command(CONTEXT_COMMAND), "use_cache": False}))
        observation = json.loads(result["stdout"])
        require(observation["cwd"] == str(context.directory), "Wrong host execution directory")
        require(observation["pgid"] == context.process.pid, "Command has wrong process owner")
        require(observation["child"]["pgid"] == context.process.pid, "Child escaped process group")
        require(observation["child"]["cwd"] == str(context.directory), "Child cwd mismatch")
        require(Path(observation["tmp"]).is_relative_to(context.directory / "tmp"),
                "Temporary output escaped context")
        for filename in ("relative-output.txt", "child-output.txt"):
            require((context.directory / filename).read_text() == context.agent,
                    "Relative outputs collided")
        require(Path(observation["tmp"]).read_text() == context.agent, "Temp output mismatch")
        made = decode_result(await session.call_tool("create_file", {
            "filename": "same-generated.txt", "content": context.agent}))
        require(Path(made["path"]).is_relative_to(context.directory / "tmp"),
                "HexStrike FileManager used a shared /tmp path")
        require(Path(made["path"]).read_text() == context.agent, "Generated file mismatch")
        strings = decode_result(await session.call_tool("strings_extract", {
            "file_path": "artifacts/sample.txt", "min_len": 4}))
        require("phase0-local-fixture-" + context.agent in strings["stdout"], "strings tool failed")
        xxd = decode_result(await session.call_tool("xxd_hexdump", {
            "file_path": "artifacts/sample.txt", "length": "32"}))
        require("00000000" in xxd["stdout"], "xxd tool failed")
        versions = decode_result(await session.call_tool("execute_command", {
            "command": "nmap --version && file artifacts/sample.txt", "use_cache": False}))
        require("Nmap version" in versions["stdout"] and "text" in versions["stdout"],
                "Installed tool version/file checks failed")

        # Default use_cache=True must still make fresh observations in patched services.
        read = command("from pathlib import Path; print(Path('cache-evidence.txt').read_text())")
        (context.directory / "cache-evidence.txt").write_text("first")
        first = decode_result(await session.call_tool("execute_command", {"command": read}))
        (context.directory / "cache-evidence.txt").write_text("second")
        second = decode_result(await session.call_tool("execute_command", {"command": read}))
        require(first["stdout"].strip() == "first" and second["stdout"].strip() == "second",
                "Stale tool result cache is enabled")
        return {"initialization": init.model_dump(mode="json"), "execution": observation,
                "generated_file": made["path"], "tools": {"strings_extract": strings,
                "xxd_hexdump": xxd, "version_and_file": versions}, "cache_freshness": "passed"}


async def catalog_and_existing(source, endpoint, directory):
    async with bridge(source, endpoint, directory) as (session, init):
        catalog = []
        cursor = None
        while True:
            page = await session.list_tools(cursor=cursor)
            catalog.extend(tool.model_dump(mode="json") for tool in page.tools)
            cursor = page.nextCursor
            if not cursor:
                break
        require(any(tool["name"] == "execute_command" for tool in catalog),
                "Installed MCP bridge lacks arbitrary-command support")
        identity = decode_result(await session.call_tool("execute_command", {
            "command": command("import json,os,sys; print(json.dumps({'cwd':os.getcwd(), 'uid':os.getuid(), 'python':sys.version, 'pid':os.getpid(), 'ppid':os.getppid()}))"),
            "use_cache": False}))
        return {"initialization": init.model_dump(mode="json"), "tool_count": len(catalog),
                "transport": "stdio MCP bridge -> HTTP execution API",
                "identity": json.loads(identity["stdout"])}, catalog


async def check_buffering(context):
    async with bridge(context.source, context.endpoint, context.directory) as (session, _):
        notifications = []

        async def progress(*args):
            notifications.append(args)

        script = """
import json,os,pathlib,subprocess,sys,time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1.5)'])
pathlib.Path('running.json').write_text(json.dumps({'pid':os.getpid(), 'child_pid':child.pid, 'pgid':os.getpgrp()}))
print('early-local-output', flush=True)
time.sleep(1.5)
child.wait()
print('late-local-output', flush=True)
"""
        start = time.monotonic()
        task = asyncio.create_task(session.call_tool("execute_command", {
            "command": command(script), "use_cache": False}, progress_callback=progress))
        marker = context.directory / "running.json"
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        require(marker.exists(), "Timed command never started")
        owner = json.loads(marker.read_text())
        context.observed_children.extend([owner["pid"], owner["child_pid"]])
        require(os.getpgid(owner["child_pid"]) == context.process.pid, "Nested process ownership lost")
        status = await asyncio.to_thread(request_json, context.endpoint + "/api/processes/list")
        require(status["success"] and status["total_count"] >= 1,
                "Process polling failed while a tool was running")
        await asyncio.sleep(0.2)
        pending_at_early_output = not task.done()
        result = decode_result(await task)
        elapsed = time.monotonic() - start
        require("early-local-output" in result["stdout"] and "late-local-output" in result["stdout"],
                "Buffered command output was lost")
        require(pending_at_early_output and not notifications, "Streaming capability changed; review findings")
        return {"elapsed_seconds": round(elapsed, 3), "pending_after_early_stdout": pending_at_early_output,
                "mcp_progress_notifications": len(notifications), "output": "completion_only",
                "process_polling": status, "ownership": owner}


def stock_cache_check(endpoint):
    payload = {"command": command("import uuid; print(uuid.uuid4())"), "use_cache": True}
    first = request_json(endpoint + "/api/command", payload)
    second = request_json(endpoint + "/api/command", payload)
    fresh = request_json(endpoint + "/api/command", {**payload, "use_cache": False})
    require(all(item.get("success") for item in (first, second, fresh)), "Stock cache probe failed")
    return {"default_replays_cached_result": first["stdout"] == second["stdout"],
            "use_cache_false_is_fresh": second["stdout"] != fresh["stdout"]}


def inventory(source):
    packages = {}
    for name in ("mcp", "fastmcp", "flask", "requests", "psutil", "httpx", "werkzeug"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "not_installed"
    return {"os_release": Path("/etc/os-release").read_text(), "kernel": platform.release(),
            "python": sys.version, "python_executable": sys.executable, "packages": packages,
            "docker": run(["docker", "version"]), "compose": run(["docker", "compose", "version"]),
            "hexstrike_revision": run(["git", "-C", str(source), "rev-parse", "HEAD"]),
            "hexstrike_tracked_changes": run(["git", "-C", str(source), "diff", "--stat", "HEAD"]),
            "network_addresses": run(["ip", "-json", "address", "show"]),
            "network_routes": run(["ip", "-json", "route", "show"])}


def assess(source, endpoint, output, services=6):
    require(services == 6, "Phase 0 capacity proof requires a main service plus five children")
    pin = verify_source(source)
    output.mkdir(parents=True, exist_ok=False)
    report = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "assessment_scope": "local host only; no remote targets or provider API calls",
              "source_pin": pin, "existing_endpoint": endpoint, "inventory": inventory(source),
              "providers": smoke(), "checks": {}, "status": "running"}
    contexts = []
    try:
        report["existing_health"] = request_json(endpoint + "/health", timeout=10)
        report["existing_mcp"], catalog = asyncio.run(catalog_and_existing(source, endpoint, output))
        (output / "tool-catalog.json").write_text(json.dumps(catalog, indent=2) + "\n")
        report["checks"]["catalog_discovery"] = "passed"
        report["stock_cache"] = stock_cache_check(endpoint)
        # Construct all six contexts first; no execution services exist until start().
        for i in range(services):
            name = "main" if i == 0 else "subagent-" + str(i)
            contexts.append(Context(output / name, source, name))
        report["checks"]["lazy_before_start"] = all(c.process is None for c in contexts)
        for context in contexts:
            context.start()
            print(f"Started {context.agent}: {context.startup_seconds}s", flush=True)
        report["services"] = [{**c.ready, "agent": c.agent, "startup_seconds": c.startup_seconds,
                               "memory_kib": process_metrics(c.process.pid)} for c in contexts]
        report["aggregate_rss_mib"] = round(sum(s["memory_kib"]["VmRSS"] for s in report["services"]) / 1024, 2)
        report["aggregate_pss_mib"] = round(sum(s["memory_kib"].get("Pss", 0) for s in report["services"]) / 1024, 2)

        async def parallel():
            barrier = asyncio.Barrier(2)
            return await asyncio.gather(*(check_context(c, barrier) for c in contexts[:2]))

        report["contexts"] = asyncio.run(parallel())
        first, second = [item["execution"] for item in report["contexts"]]
        require(max(first["start"], second["start"]) < min(first["end"], second["end"]),
                "The two execution contexts did not overlap")
        report["checks"]["concurrent_contexts"] = "passed"
        report["checks"]["temporary_generated_cache_and_subprocesses"] = "passed"
        report["buffering"] = asyncio.run(check_buffering(contexts[0]))
        report["checks"]["buffered_output_and_process_polling"] = "passed"
        # Leave one harmless child running, then prove cleanup settles its process group.
        child_code = "import os,pathlib,time; pathlib.Path('cleanup-child.pid').write_text(str(os.getpid())); time.sleep(20)"
        cleanup_command = command("import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child_code) + "]); import time; time.sleep(20)")
        cleanup_task = subprocess.Popen(
            [sys.executable, "-c", "from phase0.http import request_json; request_json(" +
             repr(contexts[0].endpoint + "/api/command") + ", {'command':" + repr(cleanup_command) +
             ", 'use_cache':False})"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            marker = contexts[0].directory / "cleanup-child.pid"
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            require(marker.exists(), "Cleanup child did not start")
            pid = int(marker.read_text())
            require(os.getpgid(pid) == contexts[0].process.pid, "Cleanup child is not owned")
            contexts[0].observed_children.append(pid)
            contexts[0].stop()
            cleanup_task.wait(timeout=5)
            require(not alive(pid), "Cleanup left an owned child running")
            report["checks"]["running_child_cleanup"] = "passed"
        finally:
            if cleanup_task.poll() is None:
                cleanup_task.kill()
                cleanup_task.wait()
        report["container_networking"] = {
            "status": "unverified", "reason": "Docker/CTF-VPN callback proof requires the deployed runtime; no remote target was contacted"}
        report["status"] = "local_checks_passed_live_credentials_assumed_container_networking_unverified"
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        for context in reversed(contexts):
            context.stop()
        report["cleanup"] = [{"agent": c.agent, "service_stopped": c.process is None or c.process.poll() is not None,
                              "observed_children_stopped": all(not alive(pid) for pid in c.observed_children),
                              "artifacts_retained": c.directory.exists()} for c in contexts]
        if not all(c["service_stopped"] and c["observed_children_stopped"] for c in report["cleanup"]):
            report["status"] = "failed"
            report["cleanup_error"] = "Owned processes remain after assessment"
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    require(all(c["service_stopped"] and c["observed_children_stopped"] for c in report["cleanup"]),
            "Owned processes remain after assessment")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Pinned HexStrike installation directory")
    parser.add_argument("--url", default="http://127.0.0.1:8888", type=local_url)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "phase0-artifacts" / uuid.uuid4().hex
    report = assess(args.source.resolve(), args.url, output.resolve())
    print(json.dumps({"status": report["status"], "report": str(output / 'report.json'),
                      "checks": report["checks"], "aggregate_rss_mib": report["aggregate_rss_mib"]}, indent=2))


if __name__ == "__main__":
    main()
