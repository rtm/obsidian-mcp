"""Tests for per-call vault selection. Run with: python -m unittest discover tests"""

import asyncio
import os
import sys
import unittest
from unittest import mock

os.environ["OBSIDIAN_CLI"] = "obsidian-test"  # skip binary auto-detection
os.environ["OBSIDIAN_VAULT"] = "Vault"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server  # noqa: E402
from mcp.client import Client  # noqa: E402

VAULT_FREE_TOOLS = {"list_vaults", "version"}


class _FakeProc:
    def __init__(self, stdout: bytes):
        self.returncode = 0
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, b""


class VaultArgumentTest(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple[str, ...]] = []
        self.enabled_plugins: dict[str, bytes] = {}
        server._healthy_vaults.clear()

        async def fake_exec(*cmd, **_):
            self.calls.append(cmd)
            # "plugins:enabled" is the health probe; everything else succeeds.
            if "plugins:enabled" in cmd:
                return _FakeProc(self.enabled_plugins.get(cmd[1], b"breadcrumbs\n"))
            return _FakeProc(b"ok\n")

        patcher = mock.patch.object(server.asyncio, "create_subprocess_exec", fake_exec)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, tool: str, **args):
        return asyncio.run(server.mcp.call_tool(tool, args))

    def test_every_vault_tool_takes_a_vault_argument(self):
        tools = asyncio.run(server.mcp.list_tools())
        for tool in tools:
            has_vault = "vault" in tool.input_schema.get("properties", {})
            self.assertEqual(has_vault, tool.name not in VAULT_FREE_TOOLS, tool.name)
            self.assertNotIn("vault", tool.input_schema.get("required", []), tool.name)

    def test_default_vault_when_omitted(self):
        self.call("read_note", path="a.md")
        self.assertEqual(self.calls[-1][:2], ("obsidian-test", "vault=Vault"))

    def test_explicit_vault_overrides_default(self):
        self.call("read_note", path="a.md", vault="Notebooks")
        self.assertEqual(self.calls[-1][:2], ("obsidian-test", "vault=Notebooks"))

    def test_override_does_not_leak_into_the_next_call(self):
        self.call("read_note", path="a.md", vault="Notebooks")
        self.call("read_note", path="a.md")
        self.assertEqual(self.calls[-1][1], "vault=Vault")

    def test_health_check_is_per_vault(self):
        self.call("append_to_note", path="a.md", content="x")
        self.call("append_to_note", path="a.md", content="x", vault="Notebooks")
        probes = [c[1] for c in self.calls if "plugins:enabled" in c]
        self.assertEqual(probes, ["vault=Vault", "vault=Notebooks"])

    def test_write_refused_when_vault_has_no_enabled_community_plugins(self):
        self.enabled_plugins["vault=Notebooks"] = b""
        with self.assertRaisesRegex(Exception, "restricted mode"):
            self.call("append_to_note", path="a.md", content="x", vault="Notebooks")
        self.call("append_to_note", path="a.md", content="x")  # default vault still fine

    def test_refusal_text_reaches_the_client(self):
        # The SDK forwards only ToolError messages; any other exception arrives
        # as a bare "Error executing tool", and the caller cannot tell a wedged
        # vault from a typo.
        self.enabled_plugins["vault=Vault"] = b""

        async def over_the_wire():
            async with Client(server.mcp) as client:
                return await client.call_tool(
                    "append_to_note", {"path": "a.md", "content": "x"}
                )

        result = asyncio.run(over_the_wire())
        self.assertTrue(result.is_error)
        self.assertIn("restricted mode", result.content[0].text)

    def test_spawn_failure_text_reaches_the_client(self):
        async def fail_exec(*cmd, **_):
            raise FileNotFoundError(2, "No such file or directory", cmd[0])

        with mock.patch.object(server.asyncio, "create_subprocess_exec", fail_exec):
            with self.assertRaisesRegex(Exception, "cannot run the Obsidian CLI"):
                self.call("read_note", path="a.md")


class HttpTransportTest(unittest.TestCase):
    """The HTTP path only runs on the networked host, so a bad keyword to the
    SDK's app factory would otherwise surface as a crash-looping service."""

    def test_app_factory_accepts_our_keywords(self):
        os.environ["OBSIDIAN_MCP_ALLOWED_HOSTS"] = "example.com,127.0.0.1:8788"
        self.addCleanup(os.environ.pop, "OBSIDIAN_MCP_ALLOWED_HOSTS")
        server.mcp.streamable_http_app(
            host="127.0.0.1", transport_security=server._transport_security()
        )

    def _gate(self, token):
        async def accepted(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        return server._AuthASGI(accepted, token=token)

    def _status(self, app, headers):
        import httpx

        async def go():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return (await client.post("/mcp", json={}, headers=headers)).status_code

        return asyncio.run(go())

    def test_gate_rejects_missing_or_wrong_token(self):
        app = self._gate(token="secret")
        self.assertEqual(self._status(app, {}), 401)
        self.assertEqual(self._status(app, {"Authorization": "Bearer nope"}), 401)

    def test_gate_admits_the_token(self):
        app = self._gate(token="secret")
        self.assertEqual(self._status(app, {"Authorization": "Bearer secret"}), 204)


if __name__ == "__main__":
    unittest.main()
