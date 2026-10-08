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
            has_vault = "vault" in tool.inputSchema.get("properties", {})
            self.assertEqual(has_vault, tool.name not in VAULT_FREE_TOOLS, tool.name)
            self.assertNotIn("vault", tool.inputSchema.get("required", []), tool.name)

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


if __name__ == "__main__":
    unittest.main()
