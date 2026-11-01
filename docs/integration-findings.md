# Phase 0 integration findings

Assessment date: November 1, 2026 (America/New_York).

The local execution proof passed. HexStrike on **127.0.0.1:8888** executed a
harmless host command through its real MCP bridge. Two concurrent execution
contexts produced independent files with identical relative names. Six execution
services ran together and were stopped after the assessment, preserving artifacts.

Provider compatibility is **assumed per the user's instruction**. All three
prototype adapters passed deterministic tool-call fixtures; none was verified
with a live account. A subsequent Docker-to-host HTTP check passed using Linux
host networking. CTF/VPN callback verification remains outstanding, and Compose
is absent. No remote target was scanned or contacted.

Raw evidence: [successful host assessment](evidence/phase0-report.json),
[subsequent Docker check](evidence/phase0-container.json), and
[discovered MCP tool schemas](evidence/phase0-tool-catalog.json). The reports record
the interpreter, timestamps, commands, outputs, process IDs, memory samples,
interface/route inventory, and cleanup results. Full logs and local fixture files
remain in the ignored `phase0-artifacts/phase0-2026-11-01-run3/` directory.

After the user confirmed Docker availability, a
[fresh availability check](evidence/phase0-docker-availability.json) confirmed
client/engine 28.5.2+dfsg4 through noninteractive sudo. Compose remains unavailable.
The checklist now marks local container-to-host HTTP separately from the pending
CTF/VPN callback verification.

## Installed versions and pins

| Component | Observed version/status |
| --- | --- |
| Kali | 2026.3 rolling; kernel `7.1.5+kali-amd64` |
| Docker client / engine | 28.5.2+dfsg4 (became available after the host assessment) |
| Docker Compose | Not installed |
| Python | 3.14.8 |
| HexStrike | 6.0.0 |
| HexStrike Git revision | `d689933ff579d839c676c82b231f8e98326c5f04` |
| MCP SDK / separately installed fastmcp distribution | 1.30.0 / 0.4.1 |
| Flask / Werkzeug | 3.0.3 / 3.1.9 |
| requests / psutil / httpx | 2.34.2 / 5.9.8 / 0.28.1 |
| Negotiated MCP protocol | `2025-11-25` |

The tested interpreter is
`/home/u/workspace/hexstrike/hexstrike-ai/hexstrike-env/bin/python`.
System Python does not have MCP installed and has different Flask/psutil versions.
Use the tested interpreter for the host proof.

[hexstrike-pin.json](../deploy/hexstrike-pin.json) records the revision and SHA-256
of both scripts. The launcher rejects modified scripts before executing them.
There were no tracked changes in the installed HexStrike checkout.
[requirements-phase0.lock](../deploy/requirements-phase0.lock) pins the MCP SDK and
its 26-package dependency closure for the tested platform. Provider prototypes use
the Python standard library, with no additional runtime dependencies.
[hexstrike-environment.freeze.txt](../deploy/hexstrike-environment.freeze.txt)
records the larger existing HexStrike environment. It is an observation of that
installation, not a requirement to add all those packages to the application.
No new packages were installed during this work.

## Actual transport, catalog, and local tools

The installed bridge is `hexstrike_mcp.py`, launched with an absolute path and
`--server http://127.0.0.1:8888`. Its MCP transport is **stdio**. It uses requests to
forward tools to the separate Flask execution API, for example
`POST /api/command`. Port 8888 is an HTTP execution endpoint, not an MCP endpoint.

Actual `initialize` and paginated `tools/list` calls discovered **150 MCP tools**.
The `/health` executable inventory reported **73 of 127** utilities available.
These inventories measure different things. Upstream logs a duplicate
`httpx_probe` registration; the discovered catalog contains the effective registered
definition. Rediscovery should preserve schemas rather than infer them from names.

Verified tools/operations:

- `execute_command`: host identity, workspace writes, Python subprocesses, fresh file reads.
- `create_file`: same generated filename in two separate FileManager roots.
- `strings_extract` and `xxd_hexdump`: actual executions against local fixture files.
- Installed `nmap --version` and `file artifacts/sample.txt` through `execute_command`.
- Patched process-list polling while a local command and its child were running.

No network scan was needed. `execute_command` already supplies shell capability;
an additional Bash MCP is optional. Tool-name filtering cannot prevent a shell
entry point from invoking another executable.

With this SDK, typed dictionary tool returns appear in `structuredContent.result`;
the text content contains the unwrapped JSON. The result decoder handles both
representations and rejects tool errors, nonzero exit codes, and timeouts.

## Execution-context proof and compatibility changes

[hexstrike_service.py](../phase0/hexstrike_service.py) is a reviewable experiment
around the pinned source. It does **not** edit the installed scripts. Each service:

1. Starts lazily in its agent workspace, in its own process session/group.
2. Inherits dedicated `TMPDIR`, `TMP`, `TEMP`, `XDG_CACHE_HOME`, and `XDG_CONFIG_HOME`.
3. Redirects 27 upstream literal `/tmp/` path occurrences into that agent's `tmp/`
   directory before loading the server, including f-string prefixes and cleanup paths.
4. Disables the standard command-result cache at the module entry point, including
   calls from tool routes that otherwise default to caching.
5. Removes the internal `Popen` object from process-list/status JSON responses.
6. Binds an OS-assigned port on `127.0.0.1` and writes an atomic `ready.json` record.

The upstream main block binds `0.0.0.0` despite having a `HEXSTRIKE_HOST` variable.
The experimental launcher explicitly uses loopback. The existing port-8888 service
is left running; temporary services use different ports.

Two real MCP bridges routed to two different services. Their **identical** command
created `relative-output.txt`, `child-output.txt`, and `artifacts/sample.txt` in their
respective directories. Recorded execution intervals overlap. Temporary-file
contents, child working directories/process groups, and FileManager-generated
paths were checked on disk. Mutating a fixture between two identical reads returned
fresh content, including when the MCP argument omitted `use_cache`.

The stock service demonstrably replayed the same cached UUID for an identical
command; `use_cache=false` returned a fresh UUID. This is why cache bypass is
necessary for workspace state and eventual token evidence.

The launcher fixes process polling because upstream includes a live `Popen` object
in its JSON response (source-inspected defect). Process polling succeeded with the
compatibility change. Cleanup terminated the services' process groups, including a
deliberately still-running harmless child. Every observed child stopped and all
fixture directories remained.

This verifies the tested execution paths. Remaining Phase 5 work:

- Five bridge schemas contain explicit `/tmp` defaults (`prowler_scan`,
  `scout_suite_assessment`, `docker_bench_security_scan`, `autorecon_comprehensive`,
  and `foremost_carving`). The unmodified bridge can send those strings explicitly,
  bypassing server-side default rewriting. Dispatch must normalize output arguments
  to host workspace paths before those tools are enabled.
- Caller-supplied absolute paths, tool-specific caches, HOME-based state, fixed
  ports, listeners, remote sessions, and subprocesses that create new sessions
  require explicit handling. `TMPDIR` alone does not solve all of these.
- The alternate enhanced process manager has its own cache and `setsid` behavior;
  it is not covered by the standard `execute_command` prototype.
- Group cleanup is demonstrated for normal inherited descendants. It is not a
  production recovery service, cgroup boundary, or isolation from other host users.
- A production runner still needs durable ownership records, authentication,
  reservations, recovery, and bounded output. The experiment does not replace it.

## Output capabilities

The tested `execute_command`, file operation, strings, and xxd MCP calls return
their result after completion. A local command printed an early line, waited,
and printed a late line: the call stayed pending and returned both lines together
after approximately **1.635 seconds**, with **zero MCP progress notifications**.

Upstream prints stdout incrementally into its **host logs**, but this is not an
incremental stdout channel to the MCP caller. Process polling provides activity
metadata, with upstream estimated progress/ETA values; those estimates are not
reliable output progress. No genuine incremental stdout interface was identified
in the pinned bridge. UI work should show running/waiting state and completed
output, without simulated stdout or promises of streaming for untested tools.

## Capacity measurement

Six contexts existed without service processes before explicit startup. All six
services were then alive together; memory was sampled before the tool exercises.

| Service | Startup to first successful health response | RSS MiB | PSS MiB |
| --- | ---: | ---: | ---: |
| Main | 3.577 s | 152.98 | 123.72 |
| Child 1 | 3.336 s | 153.10 | 123.82 |
| Child 2 | 3.237 s | 152.82 | 123.50 |
| Child 3 | 3.172 s | 152.98 | 123.81 |
| Child 4 | 3.273 s | 152.96 | 123.67 |
| Child 5 | 3.274 s | 153.16 | 123.85 |
| **Total** | **19.869 s sequential startup** | **918.01** | **742.36** |

Each service had eight threads at sampling. RSS double-counts shared pages;
PSS allocates shared pages proportionally. This is an idle execution-service
baseline, excluding MCP bridges, application/worker memory, browsers, and tool
workloads. It supports retaining the lazy per-agent service design provisionally;
capacity limits and idle teardown remain necessary. It does not establish that
six heavyweight tools can run simultaneously on the VM.

## Container, CTF/VPN, and callback routing

Docker was absent during the six-service host assessment, as preserved in its
timestamped report. Docker client/engine **28.5.2+dfsg4** became available during
final verification; Compose remained unavailable. The normal user's daemon socket
access was denied, but noninteractive sudo permitted the local Docker check.
Read-only interface/route inspection found loopback and `eth0`, with no active VPN
interface or VPN routes at assessment time.

For the initial Linux deployment, use **host networking** for the worker/bridge and
host-runner client so `127.0.0.1:8888` and per-agent loopback service ports refer to
the host. An ordinary Docker bridge container has its own loopback; simply keeping
that URL there will fail. A bridge/host-gateway deployment needs an explicit host
bind/access policy and routing for the dynamically allocated service ports.

The provided `phase0.container_probe` verified `/health` from a real container
using Linux host networking, a read-only root filesystem, UID/GID 65534, and all
capabilities dropped. The response was healthy, HexStrike 6.0.0. No cached image
existed, so `phase0.build_local_image` imported a temporary image from the VM's
installed Python runtime and linked libraries, without registry access or downloads.
The temporary container was removed automatically and the temporary image was
removed afterward; the image ID and cleanup output are preserved in the Docker
evidence report.

This proves local container-to-host HTTP routing. It cannot prove that a CTF peer
can reach a listener. Callback addresses must come from the active CTF/VPN
interface and owned listener-port reservations; no remote callback test was
attempted under the user's scope.

## Provider prototypes and configuration

[providers.py](../phase0/providers.py) implements bounded, non-streaming tool-call
loops using a harmless in-process `local_echo` function. Unknown tools, malformed
arguments, incorrect echo values, missing tool calls, incomplete responses, and
overlong loops fail the proof. No provider executes HexStrike directly.

| Provider | Interface and continuation | Verification |
| --- | --- | --- |
| OpenAI | Responses; replay all output items and correlate `function_call_output` by `call_id`; request encrypted reasoning continuation | Fixture passed; live assumed |
| DeepSeek | Chat Completions; retain assistant message, tool IDs, and `reasoning_content` | Fixture passed; live assumed |
| Alibaba Model Studio | Configurable compatible Chat Completions endpoint; preserve assistant/tool continuation and explicit model options | Fixture passed; live assumed |

No API tokens were supplied and no provider API requests were made. The fixture
model ID is not a suggested live model. Live execution requires `--live`, an
explicit account-available model, and the relevant key. Alibaba additionally
requires region, workspace, and endpoint configuration. Region/workspace values
are intentionally blank in [phase0.env.example](../deploy/phase0.env.example).
Use `ALIBABA_API_KEY` for the Model Studio key in this prototype.

Alibaba endpoints and keys must match the account's region and billing plan;
workspace-dedicated endpoints additionally require a key for that workspace.
For example, Singapore's legacy compatible endpoint is
`https://dashscope-intl.aliyuncs.com/compatible-mode/v1`, and its dedicated form is
`https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`.
Select the actual endpoint in the account before live verification.
[Alibaba endpoint documentation](https://www.alibabacloud.com/help/en/model-studio/base-url).

No thinking/reasoning settings are guessed. `*_OPTIONS_JSON` provides explicit
model-supported request fields, for example DeepSeek's documented `thinking`
object or Alibaba's model-dependent thinking options. It cannot override the
prototype's protocol fields. Provider keys, responses, and reasoning are not
written to the smoke-test report.

The original live-provider acceptance condition remains unmet. Per the user,
this is a recorded assumption that permits proceeding with later implementation.

## Reproduce the checks

Run from the repository root on the Kali host. No scan target argument exists.
Assessment URLs must be loopback IPs; HTTP proxy inheritance and redirects are
disabled. The successful run used the existing interpreter and installed tools.

```sh
python3 -m unittest discover -s tests -v
python3 -m phase0.providers

/home/u/workspace/hexstrike/hexstrike-ai/hexstrike-env/bin/python -m phase0.probe \
  --source /home/u/workspace/hexstrike/hexstrike-ai \
  --url http://127.0.0.1:8888

# Optional, when the selected image is already cached:
python3 -m phase0.container_probe --image python:3.14-slim
```

The host probe chooses a random output directory under `phase0-artifacts/`. An
explicit `--output` path must not already exist. Failures still produce a report
and trigger cleanup of owned services. It never stops the existing port-8888
service. Only the six temporary services bind additional ports.

For a separate MCP client environment on the tested Python/platform:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r deploy/requirements-phase0.lock
```

If Docker requires noninteractive sudo, add `--sudo` to the container probe.
To reproduce the registry-free check when no Python image is cached:

```sh
python3 -m phase0.build_local_image --tag ctf-harness-phase0:local-proof --sudo
python3 -m phase0.container_probe --image ctf-harness-phase0:local-proof --sudo
sudo -n docker image rm ctf-harness-phase0:local-proof
```

The helper refuses an existing image tag and does not modify host packages or
daemon configuration. The imported runtime is a local test fixture, not a
production application image.

The host launcher also needs the installed HexStrike dependencies; a client-only
venv does not replace the HexStrike environment. Regenerate observed pins from
the tested HexStrike interpreter with `python -m phase0.lock_environment`. This
uses its installed `packaging` library and `pip`; it does not install or upgrade
anything. Review pin changes deliberately rather than silently accepting a new
HexStrike source hash.

For future live provider checks, export the fields from the environment example,
then run `python3 -m phase0.providers --live`. The file is not loaded automatically.
Missing configuration is reported as skipped, never as live success. No live
provider run is needed to reproduce the current assumed-compatibility delivery.

## Primary references checked

- [Installed upstream revision](https://github.com/0x4m4/hexstrike-ai/tree/d689933ff579d839c676c82b231f8e98326c5f04): local source and hashes are authoritative for this proof.
- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling): Responses function-call and continuation format.
- [DeepSeek thinking-mode tools](https://api-docs.deepseek.com/guides/thinking_mode/): preservation of provider continuation fields.
- [Alibaba function calling](https://www.alibabacloud.com/help/en/model-studio/qwen-function-calling) and [regional endpoints](https://www.alibabacloud.com/help/en/model-studio/base-url): compatible tool messages and matching account configuration.
