import ast
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from phase0.container_probe import check as container_check
from phase0.hexstrike_service import ScopedTemporaryPaths, public_process, verify_source
from phase0.http import NoRedirect, local_url
from phase0.providers import Configuration, FixtureTransport, execute_echo, round_trip, smoke
from phase0.probe import Context, decode_result


class ProviderRoundTrips(unittest.TestCase):
    nonce = "a-harmless-test-nonce"

    def test_openai_replays_all_continuation_items_and_correlates_output(self):
        fixture = FixtureTransport("openai", self.nonce)
        result = round_trip(Configuration("openai", "account-selected", "https://unused.invalid"),
                            fixture, self.nonce)
        self.assertEqual(result["tool_calls"], 1)
        first_path, first = fixture.requests[0]
        second_path, second = fixture.requests[1]
        self.assertEqual((first_path, second_path), ("/responses", "/responses"))
        self.assertFalse(first["store"])
        self.assertIn("reasoning.encrypted_content", first["include"])
        self.assertNotIn("function", first["tools"][0])
        self.assertEqual(second["input"][1]["encrypted_content"], "fixture-encrypted-continuation")
        self.assertEqual(second["input"][2]["call_id"], "call_fixture")
        self.assertEqual(second["input"][3], {"type": "function_call_output", "call_id": "call_fixture",
                                               "output": json.dumps({"text": self.nonce})})
        self.assertEqual(len(first["input"]), 1, "Earlier request was mutated")

    def test_compatible_apis_preserve_empty_content_reasoning_and_call_ids(self):
        for provider in ("deepseek", "alibaba"):
            with self.subTest(provider=provider):
                fixture = FixtureTransport(provider, self.nonce)
                options = ({"thinking": {"type": "enabled"}} if provider == "deepseek"
                           else {"enable_thinking": False})
                round_trip(Configuration(provider, "account-selected", "https://unused.invalid",
                                         options=options), fixture, self.nonce)
                path, body = fixture.requests[1]
                self.assertEqual(path, "/chat/completions")
                self.assertIsNone(body["messages"][1]["content"])
                self.assertEqual(body["messages"][1]["reasoning_content"], "fixture continuation data")
                self.assertEqual(body["messages"][2]["tool_call_id"], "call_fixture")
                self.assertEqual(json.loads(body["messages"][2]["content"]), {"text": self.nonce})
                self.assertIn("function", body["tools"][0])
                self.assertNotIn("strict", body["tools"][0]["function"])
                for key, value in options.items():
                    self.assertEqual(body[key], value)

    def test_multiple_calls_are_returned_in_order(self):
        fixture = FixtureTransport("openai", self.nonce)

        def send(path, body):
            response = fixture(path, body)
            if len(fixture.requests) == 1:
                other = deepcopy(response["output"][-1])
                other.update(id="fc_second", call_id="call_second")
                response["output"].append(other)
            return response

        result = round_trip(Configuration("openai", "account-selected", "https://unused.invalid"),
                            send, self.nonce)
        self.assertEqual(result["tool_calls"], 2)
        outputs = [item for item in fixture.requests[1][1]["input"]
                   if item.get("type") == "function_call_output"]
        self.assertEqual([item["call_id"] for item in outputs], ["call_fixture", "call_second"])

    def test_claimed_success_without_a_tool_call_fails(self):
        def send(path, body):
            return {"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": self.nonce}}]}
        with self.assertRaisesRegex(ValueError, "No verified"):
            round_trip(Configuration("deepseek", "account-selected", "https://unused.invalid"),
                       send, self.nonce)

    def test_truncated_response_cannot_pass(self):
        fixture = FixtureTransport("openai", self.nonce)

        def send(path, body):
            response = fixture(path, body)
            response["status"] = "incomplete"
            return response

        with self.assertRaisesRegex(ValueError, "did not complete"):
            round_trip(Configuration("openai", "account-selected", "https://unused.invalid"),
                       send, self.nonce)

    def test_unknown_tool_malformed_arguments_and_wrong_nonce_fail(self):
        for name, arguments in (("execute_command", "{}"), ("local_echo", "not-json"),
                                ("local_echo", '{"text":"wrong"}'),
                                ("local_echo", '["a-harmless-test-nonce"]')):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ValueError):
                execute_echo(name, arguments, self.nonce)

    def test_options_cannot_replace_protocol_fields(self):
        config = Configuration("openai", "account-selected", "https://unused.invalid",
                               options={"tools": []})
        with self.assertRaisesRegex(ValueError, "protocol fields"):
            round_trip(config, FixtureTransport("openai", self.nonce), self.nonce)

    def test_fixture_results_are_never_marked_live(self):
        results = smoke()
        self.assertEqual(len(results), 3)
        self.assertTrue(all(item["status"] == "fixture_passed" for item in results))
        self.assertTrue(all(item["live_verified"] is False for item in results))

    def test_alibaba_requires_account_region_workspace_endpoint_and_key(self):
        config = Configuration("alibaba", "", "")
        self.assertEqual(set(config.missing()), {"model", "endpoint", "api_key", "region", "workspace"})


class LocalExecutionBoundaries(unittest.TestCase):
    def test_cleanup_is_idempotent_and_preserves_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            context = Context(Path(directory) / "agent", Path(directory), "main")
            artifact = context.directory / "artifacts/evidence.txt"
            artifact.write_text("retained")
            context.process = Mock(pid=123456)
            with patch("phase0.probe.os.killpg") as killpg:
                context.stop()
                calls = killpg.call_count
                context.stop()
                self.assertEqual(killpg.call_count, calls, "Repeated cleanup can target a reused process group")
            self.assertEqual(artifact.read_text(), "retained")

    def test_container_probe_executes_inspected_image_without_a_registry_pull(self):
        inspect = SimpleNamespace(returncode=0, stdout="sha256:local-image\n")
        executed = SimpleNamespace(returncode=0, stdout='{"status":"healthy","version":"6.0.0"}')
        with patch("phase0.container_probe.shutil.which", return_value="/usr/bin/docker"), \
                patch("phase0.container_probe.subprocess.run", side_effect=[inspect, executed]) as run:
            result = container_check("locally-cached:test")
        self.assertEqual(result["status"], "passed")
        args = run.call_args_list[1].args[0]
        self.assertIn("--pull=never", args)
        self.assertIn("--network=host", args)
        self.assertIn("sha256:local-image", args)
        self.assertNotIn("locally-cached:test", args)
        self.assertIn("NoRedirect", args[-1])

    def test_installed_mcp_structured_result_wrapper_and_text_fallback(self):
        output = {"success": True, "stdout": "local-proof", "return_code": 0}
        wrapped = SimpleNamespace(isError=False, structuredContent={"result": output})
        self.assertEqual(decode_result(wrapped), output)
        text = SimpleNamespace(isError=False, structuredContent=None, content=[
            SimpleNamespace(type="text", text=json.dumps(output))])
        self.assertEqual(decode_result(text), output)

    def test_assessment_rejects_remote_urls_and_hostnames(self):
        for url in ("http://example.com:8888", "http://192.0.2.1:8888", "http://localhost:8888",
                    "https://127.0.0.1:8888", "http://127.0.0.1:8888/path",
                    "http://user:pass@127.0.0.1:8888", "http://127.0.0.1:8888?target=example.com"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                local_url(url)
        self.assertEqual(local_url("http://127.0.0.1:8888/"), "http://127.0.0.1:8888")
        self.assertEqual(local_url("http://[::1]:8888"), "http://[::1]:8888")

    def test_redirects_cannot_escape_localhost(self):
        with self.assertRaisesRegex(ValueError, "Redirects"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "http://example.com")

    def test_temporary_path_rewrite_includes_fstrings_and_cleanup_paths(self):
        tree = ast.parse("a = '/tmp/hexstrike_files'\nb = f'/tmp/project/{42}'\nc='/usr/bin/strings'")
        rewrite = ScopedTemporaryPaths(Path("/agent/tmp"))
        changed = rewrite.visit(tree)
        ast.fix_missing_locations(changed)
        namespace = {}
        exec(compile(changed, "<test>", "exec"), namespace)
        self.assertEqual(namespace["a"], "/agent/tmp/hexstrike_files")
        self.assertEqual(namespace["b"], "/agent/tmp/project/42")
        self.assertEqual(namespace["c"], "/usr/bin/strings")
        self.assertEqual(rewrite.replacements, 2)

    def test_unreviewed_hexstrike_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "hexstrike_server.py").write_text("# different source")
            with self.assertRaisesRegex(ValueError, "differs from reviewed pin"):
                verify_source(source)

    def test_process_metadata_removes_popen_without_mutating_registry(self):
        process = object()
        original = {"pid": 42, "process": process, "status": "running"}
        public = public_process(original)
        self.assertEqual(json.loads(json.dumps(public)), {"pid": 42, "status": "running"})
        self.assertIs(original["process"], process)


if __name__ == "__main__":
    unittest.main()
