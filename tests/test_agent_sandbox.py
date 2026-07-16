# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Containment tests for external-agent sessions.

These tests never launch a provider CLI.  The wrapper is verified by inspecting
the argv OmicsANG would exec, plus one real bubblewrap round-trip using
``/bin/sh`` so the policy is proven against the kernel rather than a mock.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchtop import sandbox, sessions


def _sandbox_usable() -> bool:
    sandbox.available.cache_clear()
    try:
        return sandbox.available()
    finally:
        sandbox.available.cache_clear()


class ModeTests(unittest.TestCase):
    def test_defaults_to_auto_and_rejects_unknown_modes(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OMICSANG_AGENT_SANDBOX", None)
            os.environ.pop("BENCHTOP_AGENT_SANDBOX", None)
            self.assertEqual(sandbox.mode(), sandbox.MODE_AUTO)
        with mock.patch.dict(os.environ, {"OMICSANG_AGENT_SANDBOX": "banana"}):
            self.assertEqual(sandbox.mode(), sandbox.MODE_AUTO)
        with mock.patch.dict(os.environ, {"OMICSANG_AGENT_SANDBOX": "REQUIRE"}):
            self.assertEqual(sandbox.mode(), sandbox.MODE_REQUIRE)

    def test_legacy_prefix_still_read(self) -> None:
        with mock.patch.dict(os.environ, {"BENCHTOP_AGENT_SANDBOX": "off"}):
            os.environ.pop("OMICSANG_AGENT_SANDBOX", None)
            self.assertEqual(sandbox.mode(), sandbox.MODE_OFF)


class ArgvPolicyTests(unittest.TestCase):
    """`_child_argv` decides *whether* to contain; these pin that decision."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _child_argv(self, kind: str, tool: str):
        return sessions._child_argv(
            kind,
            ["/usr/bin/claude"],
            cwd=self.tmp.name,
            tool=tool,
        )

    def test_non_agent_sessions_are_never_wrapped(self) -> None:
        for kind in ("run", "shell"):
            argv, note = self._child_argv(kind, "claude")
            self.assertEqual(argv, ["/usr/bin/claude"])
            self.assertEqual(note, "")

    def test_operator_shell_tool_is_not_an_external_agent(self) -> None:
        # A `shell` session is the operator's own terminal; containing it would
        # silently change their environment rather than any agent's.
        argv, note = self._child_argv("agent", "shell")
        self.assertEqual(argv, ["/usr/bin/claude"])
        self.assertEqual(note, "")

    def test_off_mode_runs_uncontained_but_says_so(self) -> None:
        with mock.patch.object(sandbox, "mode", return_value=sandbox.MODE_OFF):
            argv, note = self._child_argv("agent", "claude")
        self.assertEqual(argv, ["/usr/bin/claude"])
        self.assertIn("NOT contained", note)

    def test_auto_mode_downgrades_with_a_visible_note(self) -> None:
        with (
            mock.patch.object(sandbox, "mode", return_value=sandbox.MODE_AUTO),
            mock.patch.object(
                sandbox,
                "build_argv",
                side_effect=sandbox.SandboxUnavailable("no userns"),
            ),
        ):
            argv, note = self._child_argv("agent", "claude")
        self.assertEqual(argv, ["/usr/bin/claude"])
        self.assertIn("NOT contained", note)
        self.assertIn("no userns", note)

    def test_require_mode_refuses_to_launch_uncontained(self) -> None:
        with (
            mock.patch.object(sandbox, "mode", return_value=sandbox.MODE_REQUIRE),
            mock.patch.object(
                sandbox,
                "build_argv",
                side_effect=sandbox.SandboxUnavailable("no userns"),
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self._child_argv("agent", "claude")
        self.assertIn("required but unavailable", str(caught.exception))


@unittest.skipUnless(_sandbox_usable(), "bubblewrap sandbox unavailable")
class BuildArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()

    def _argv(self, tool: str = "claude") -> list[str]:
        return sandbox.build_argv(["/bin/true"], cwd=str(self.repo), tool=tool)

    def test_repository_is_bound_writable_and_is_the_workdir(self) -> None:
        argv = self._argv()
        self.assertIn("--bind", argv)
        self.assertIn(str(self.repo), argv)
        self.assertEqual(argv[argv.index("--chdir") + 1], str(self.repo))

    def test_home_is_replaced_by_a_tmpfs(self) -> None:
        argv = self._argv()
        home = str(Path(os.path.expanduser("~")).resolve())
        self.assertEqual(
            argv[argv.index("--tmpfs") : argv.index("--tmpfs") + 2][0], "--tmpfs"
        )
        self.assertIn(home, argv)
        self.assertEqual(argv[argv.index(home) - 1], "--tmpfs")

    def test_provider_configs_are_not_shared_between_tools(self) -> None:
        # Mirrors the per-tool split already enforced for environment variables.
        claude_argv = " ".join(self._argv("claude"))
        codex_argv = " ".join(self._argv("codex"))
        self.assertNotIn("/.codex", claude_argv)
        self.assertNotIn("/.claude", codex_argv)

    def test_command_is_separated_from_wrapper_flags(self) -> None:
        argv = self._argv()
        self.assertEqual(argv[0], sandbox.BWRAP)
        self.assertEqual(argv[-2:], ["--", "/bin/true"])

    def test_network_is_not_unshared(self) -> None:
        # Agents must reach their provider; egress control is the CLI's job.
        self.assertNotIn("--unshare-net", self._argv())

    def test_bare_command_name_is_resolved_and_bound(self) -> None:
        # $HOME is a tmpfs inside the namespace, so a CLI named only by its
        # command (`claude`) must still be bound or it cannot be exec'd at all.
        argv = sandbox.build_argv(["sh"], cwd=str(self.repo), tool="claude")
        located = shutil.which("sh")
        self.assertIsNotNone(located)
        self.assertIn(located, argv)

    def test_unresolvable_command_does_not_abort_the_wrapper(self) -> None:
        # bwrap reports the missing executable; the wrapper should not guess.
        argv = sandbox.build_argv(
            ["definitely-not-installed-xyz"], cwd=str(self.repo), tool="claude"
        )
        self.assertEqual(argv[-1], "definitely-not-installed-xyz")

    def test_unusable_workdir_is_reported_not_silently_dropped(self) -> None:
        with self.assertRaises(sandbox.SandboxUnavailable):
            sandbox.build_argv(["/bin/true"], cwd="/nonexistent/repo", tool="claude")

    def test_empty_command_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            sandbox.build_argv([], cwd=str(self.repo), tool="claude")


@unittest.skipUnless(_sandbox_usable(), "bubblewrap sandbox unavailable")
class RealContainmentTests(unittest.TestCase):
    """Prove the boundary against the kernel, not against our own argv builder."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.outside = self.repo.parent / f"{self.repo.name}-sibling"
        self.outside.mkdir()
        self.addCleanup(lambda: self.outside.rmdir())
        (self.outside / "secret.txt").write_text("private", encoding="utf-8")
        self.addCleanup(lambda: (self.outside / "secret.txt").unlink(missing_ok=True))

    def _run(self, script: str) -> str:
        argv = sandbox.build_argv(
            ["/bin/sh", "-c", script], cwd=str(self.repo), tool="claude"
        )
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, check=False
        )
        return result.stdout.strip()

    def test_repository_is_writable(self) -> None:
        self.assertEqual(self._run("touch ./w && echo OK"), "OK")

    def test_sibling_directory_is_not_present(self) -> None:
        out = self._run(f"cat {self.outside / 'secret.txt'} 2>/dev/null || echo DENIED")
        self.assertEqual(out, "DENIED")

    def test_home_secrets_are_absent(self) -> None:
        out = self._run("ls ~/.ssh >/dev/null 2>&1 && echo LEAKED || echo ABSENT")
        self.assertEqual(out, "ABSENT")

    def test_extra_read_root_is_honoured_read_only(self) -> None:
        with mock.patch.object(sandbox, "read_roots", return_value=(self.outside,)):
            readable = self._run(f"cat {self.outside / 'secret.txt'}")
            writable = self._run(
                f"touch {self.outside / 'x'} 2>/dev/null && echo RW || echo RO"
            )
        self.assertEqual(readable, "private")
        self.assertEqual(writable, "RO")


@unittest.skipUnless(_sandbox_usable(), "bubblewrap sandbox unavailable")
class SessionIntegrationTests(unittest.TestCase):
    """The containment must apply through the real PTY session path."""

    def _output(self, tool: str, script: str) -> str:
        async def drive() -> str:
            session = sessions.Session(
                kind="agent",
                title="test",
                cwd=os.getcwd(),
                argv=["/bin/sh", "-c", script],
                meta={"tool": tool},
            )
            session.start()
            # The containment note is appended before any subscriber attaches,
            # so it arrives in the replay snapshot rather than the live queue.
            queue, snapshot = session.subscribe()
            chunks: list[bytes] = [snapshot]
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    break
                if not isinstance(item, bytes):
                    break
                chunks.append(item)
            return b"".join(chunks).decode(errors="replace")

        return asyncio.run(drive())

    def test_agent_session_cannot_read_home_secrets(self) -> None:
        out = self._output(
            "claude", "ls ~/.ssh >/dev/null 2>&1 && echo LEAKED || echo ABSENT"
        )
        self.assertIn("ABSENT", out)
        self.assertNotIn("LEAKED", out)

    def test_agent_session_announces_containment_in_scrollback(self) -> None:
        self.assertIn("contained", self._output("claude", "true"))

    def test_session_argv_still_describes_the_agent_not_the_wrapper(self) -> None:
        # Redaction, durable job payloads, and the UI all read Session.argv.
        session = sessions.Session(
            kind="agent",
            title="test",
            cwd=os.getcwd(),
            argv=["/usr/bin/claude", "prompt"],
            meta={"tool": "claude"},
        )
        self.assertEqual(session.argv[0], "/usr/bin/claude")
        self.assertEqual(session.to_dict()["argv"][0], "/usr/bin/claude")
        self.assertNotIn(sandbox.BWRAP, session.to_dict()["command"])


if __name__ == "__main__":
    unittest.main()
