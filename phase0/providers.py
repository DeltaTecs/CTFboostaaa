"""Non-streaming provider tool-round-trip prototypes, independent of HexStrike.

Offline fixtures are the default. Live calls require an explicit CLI switch,
an account-selected model and a key. Provider responses/reasoning stay in memory.
"""
import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import urllib.parse

from .http import request_json

PROVIDERS = ("openai", "deepseek", "alibaba")
FUNCTION = {
    "name": "local_echo",
    "description": "Echo the requested text using a harmless local Python function.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                   "required": ["text"], "additionalProperties": False},
    "strict": True,
}


@dataclass(frozen=True)
class Configuration:
    provider: str
    model: str
    endpoint: str
    api_key: str = ""
    region: str = ""
    workspace: str = ""
    # Explicit, account/model-specific settings; no guessed thinking defaults.
    options: dict | None = None

    @classmethod
    def from_environment(cls, provider):
        prefix = provider.upper()
        defaults = {"openai": "https://api.openai.com/v1",
                    "deepseek": "https://api.deepseek.com/v1", "alibaba": ""}
        options = json.loads(os.getenv(prefix + "_OPTIONS_JSON", "{}"))
        if not isinstance(options, dict):
            raise ValueError("Provider options must be a JSON object")
        return cls(provider, os.getenv(prefix + "_MODEL", ""),
                   os.getenv(prefix + "_BASE_URL", defaults[provider]),
                   os.getenv(prefix + "_API_KEY", ""),
                   os.getenv(prefix + "_REGION", ""),
                   os.getenv(prefix + "_WORKSPACE", ""), options)

    def missing(self):
        required = ["model", "endpoint", "api_key"]
        if self.provider == "alibaba":
            required += ["region", "workspace"]
        return [field for field in required if not getattr(self, field)]


def live_transport(config):
    parsed = urllib.parse.urlsplit(config.endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Provider base URL must be an HTTPS URL without credentials/query")

    def send(path, body):
        return request_json(config.endpoint.rstrip("/") + path, body,
                            {"Authorization": "Bearer " + config.api_key}, timeout=90)
    return send


def execute_echo(name, raw_arguments, nonce):
    if name != "local_echo":
        raise ValueError("Provider requested an unknown smoke-test tool")
    arguments = json.loads(raw_arguments)
    if arguments != {"text": nonce}:
        raise ValueError("Provider tool arguments do not match the local echo challenge")
    return json.dumps({"text": arguments["text"]})


def round_trip(config, send, nonce=None):
    if config.provider not in PROVIDERS or not config.model:
        raise ValueError("Select a supported provider and an explicit account model")
    nonce = nonce or secrets.token_hex(16)
    prompt = (f"Call local_echo with text {nonce}. Then reply with the exact text returned "
              "by the tool. Do not answer until you have called the tool.")
    responses = config.provider == "openai"
    history = [{"role": "user", "content": prompt}]
    tools = ([{"type": "function", **deepcopy(FUNCTION)}] if responses else
             [{"type": "function", "function": deepcopy(FUNCTION)}])
    # Strict mode on compatible APIs is optional/provider-specific.
    if not responses:
        tools[0]["function"].pop("strict")
    called = 0
    for _ in range(4):
        body = deepcopy(config.options or {})
        reserved = {"model", "input", "messages", "tools", "stream", "store", "include"}
        if reserved.intersection(body):
            raise ValueError("Provider options cannot replace protocol fields")
        body.update(model=config.model, tools=tools, stream=False)
        if responses:
            body.update(input=deepcopy(history), store=False,
                        include=["reasoning.encrypted_content"])
            reply = send("/responses", body)
            if reply.get("status") != "completed":
                raise ValueError("Provider response did not complete")
            output = reply["output"]
            # Replay every output item, including encrypted reasoning and call IDs.
            history.extend(deepcopy(output))
            calls = [item for item in output if item["type"] == "function_call"]
            text = "".join(part.get("text", "") for item in output
                           if item["type"] == "message" for part in item.get("content", [])
                           if part["type"] == "output_text")
            for call in calls:
                result = execute_echo(call["name"], call["arguments"], nonce)
                history.append({"type": "function_call_output", "call_id": call["call_id"],
                                "output": result})
        else:
            body["messages"] = deepcopy(history)
            reply = send("/chat/completions", body)
            choice = reply["choices"][0]
            message = choice["message"]
            # Retain provider-specific continuation fields such as reasoning_content.
            history.append(deepcopy(message))
            calls = message.get("tool_calls") or []
            text = message.get("content") or ""
            if choice.get("finish_reason") not in ("stop", "tool_calls"):
                raise ValueError("Provider completion was truncated or refused")
            for call in calls:
                function = call["function"]
                result = execute_echo(function["name"], function["arguments"], nonce)
                history.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        called += len(calls)
        if not calls:
            if not called or nonce not in text:
                raise ValueError("No verified local tool call and matching final response")
            return {"provider": config.provider, "tool_calls": called,
                    "round_trip": "passed", "streaming": False}
    raise ValueError("Provider smoke-test turn limit reached")


class FixtureTransport:
    """Deterministic protocol fixtures; these are never live verification."""
    def __init__(self, provider, nonce):
        self.provider, self.nonce = provider, nonce
        self.requests = []

    def __call__(self, path, body):
        self.requests.append((path, deepcopy(body)))
        first = len(self.requests) == 1
        if self.provider == "openai":
            output = ([{"type": "reasoning", "id": "rs_fixture", "summary": [],
                        "encrypted_content": "fixture-encrypted-continuation"},
                       {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
                        "name": "local_echo", "arguments": json.dumps({"text": self.nonce})}]
                      if first else [{"type": "message", "role": "assistant",
                                      "content": [{"type": "output_text", "text": self.nonce}]}])
            return {"status": "completed", "output": output}
        message = ({"role": "assistant", "content": None,
                    "reasoning_content": "fixture continuation data",
                    "tool_calls": [{"id": "call_fixture", "type": "function", "function": {
                        "name": "local_echo", "arguments": json.dumps({"text": self.nonce})}}]}
                   if first else {"role": "assistant", "content": self.nonce})
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if first else "stop"}]}


def smoke(live=False):
    results = []
    for provider in PROVIDERS:
        if live:
            config = Configuration.from_environment(provider)
            if missing := config.missing():
                results.append({"provider": provider, "status": "skipped_missing_configuration",
                                "missing": missing, "live_verified": False})
                continue
            try:
                result = round_trip(config, live_transport(config))
                results.append({**result, "status": "live_passed", "live_verified": True})
            except Exception as error:
                # Error type is sufficient; do not expose secrets in arbitrary error messages.
                results.append({"provider": provider, "status": "failed",
                                "error_type": type(error).__name__, "live_verified": False})
        else:
            nonce = secrets.token_hex(16)
            config = Configuration(provider, "fixture-model", "https://unused.invalid")
            result = round_trip(config, FixtureTransport(provider, nonce), nonce)
            results.append({**result, "status": "fixture_passed", "live_verified": False,
                            "assumption": "Live account compatibility assumed per user instruction"})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly enable provider API calls")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = smoke(args.live)
    rendered = json.dumps(results, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    if any(item["status"] == "failed" for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
