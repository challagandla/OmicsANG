# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
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

_FIXTURE: dict = {}


def setUpModule() -> None:
    """Point the sandbox at a throwaway home and state directory.

    Building a wrapper creates inert placeholders in the provider's config
    directory, which must never touch the developer's real one.
    """
    tmp = tempfile.TemporaryDirectory()
    home = Path(tmp.name).resolve() / "home"
    private = Path(tmp.name).resolve() / "private"
    claude = home / ".claude"
    (claude / "projects" / "-some-other-project").mkdir(parents=True)
    (claude / "projects" / "-some-other-project" / "t.jsonl").write_text("other\n")
    (claude / "ide").mkdir()
    (claude / "ide" / "1234.lock").write_text("ide-token\n")
    (claude / "skills").mkdir()
    (claude / "settings.json").write_text('{"hooks": {}}\n')
    (claude / ".credentials.json").write_text("{}\n")
    (claude / "history.jsonl").write_text("prompt from another project\n")
    (home / ".claude.json").write_text('{"mcpServers": {}}\n')
    codex = home / ".codex"
    (codex / "log").mkdir(parents=True)
    (codex / "auth.json").write_text("{}\n")
    (codex / "config.toml").write_text("")
    (codex / "openai.config.toml").write_text("")
    private.mkdir(mode=0o700)
    patches = [
        mock.patch.object(sandbox, "_home", return_value=home),
        mock.patch.object(sandbox, "_private_dir", return_value=private),
        mock.patch.dict(os.environ),
    ]
    for patch in patches:
        patch.start()
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        os.environ.pop(name, None)
    _FIXTURE.update(tmp=tmp, home=home, private=private, patches=patches)


def tearDownModule() -> None:
    for patch in reversed(_FIXTURE["patches"]):
        patch.stop()
    _FIXTURE["tmp"].cleanup()


def _bwrap_args(argv: list[str]) -> list[str]:
    """Drop the fixed shell prefix that opens the socket filter, if present."""
    return argv[argv.index(sandbox.BWRAP) :]


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
        # An unrecognised value must never degrade to an uncontained launch.
        for value in ("banana", "required", "on", "1", "strict"):
            with mock.patch.dict(os.environ, {"OMICSANG_AGENT_SANDBOX": value}):
                self.assertEqual(sandbox.mode(), sandbox.MODE_REQUIRE)
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
        home = str(sandbox._home())
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
        self.assertEqual(_bwrap_args(argv)[0], sandbox.BWRAP)
        self.assertEqual(argv[-2:], ["--", "/bin/true"])

    def test_socket_filter_is_opened_by_a_fixed_prefix(self) -> None:
        argv = self._argv()
        self.assertEqual(argv[:2], ["/bin/sh", "-c"])
        self.assertEqual(argv[2], 'exec 9<"$0" && exec "$@"')
        self.assertTrue(Path(argv[3]).is_file())
        self.assertEqual(argv[4], sandbox.BWRAP)
        self.assertEqual(argv[argv.index("--seccomp") + 1], "9")
        with mock.patch.dict(
            os.environ, {"OMICSANG_AGENT_SANDBOX_UNIX_SOCKETS": "allow"}
        ):
            allowed = self._argv()
        self.assertEqual(allowed[0], sandbox.BWRAP)
        self.assertNotIn("--seccomp", allowed)

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
class ProviderConfigTests(unittest.TestCase):
    """Only the provider's live state is writable; what it executes is not."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.home = _FIXTURE["home"]

    def _mounts(self, tool: str) -> dict[str, tuple[str, str]]:
        argv = sandbox.build_argv(["/bin/true"], cwd=str(self.repo), tool=tool)
        mounts: dict[str, tuple[str, str]] = {}
        index = 0
        while index < len(argv):
            flag = argv[index]
            if flag in {"--bind", "--ro-bind", "--bind-try", "--ro-bind-try"}:
                mounts[argv[index + 2]] = (flag, argv[index + 1])
                index += 3
            elif flag == "--tmpfs":
                mounts[argv[index + 1]] = (flag, "")
                index += 2
            else:
                index += 1
        return mounts

    def test_claude_settings_and_loaded_content_are_read_only(self) -> None:
        mounts = self._mounts("claude")
        claude = self.home / ".claude"
        self.assertEqual(mounts[str(claude)][0], "--bind")
        for name in ("settings.json", "CLAUDE.md", "skills", "plugins", "commands"):
            with self.subTest(name=name):
                self.assertEqual(mounts[str(claude / name)][0], "--ro-bind")

    def test_missing_executable_settings_get_inert_placeholders(self) -> None:
        self._mounts("claude")
        claude = self.home / ".claude"
        self.assertEqual((claude / "CLAUDE.md").read_text(), "")
        self.assertTrue((claude / "plugins").is_dir())
        self.assertEqual(list((claude / "plugins").iterdir()), [])
        # An existing file is never overwritten by a placeholder.
        self.assertEqual((claude / "settings.json").read_text(), '{"hooks": {}}\n')

    def test_other_projects_and_control_state_are_replaced_by_scratch(self) -> None:
        mounts = self._mounts("claude")
        claude = self.home / ".claude"
        self.assertEqual(mounts[str(claude / "projects")][0], "--tmpfs")
        self.assertEqual(mounts[str(claude / "ide")][0], "--tmpfs")
        self.assertNotIn(str(claude / "projects" / "-some-other-project"), mounts)
        own = claude / "projects" / str(self.repo).replace("/", "-").replace("_", "-")
        self.assertEqual(mounts[str(own)], ("--bind", str(own)))
        history = mounts[str(claude / "history.jsonl")]
        self.assertEqual(history[0], "--bind")
        self.assertTrue(history[1].startswith(str(_FIXTURE["private"])))

    def test_mixed_state_file_is_a_private_copy(self) -> None:
        flag, source = self._mounts("claude")[str(self.home / ".claude.json")]
        self.assertEqual(flag, "--bind")
        self.assertTrue(source.startswith(str(_FIXTURE["private"])))
        self.assertEqual(Path(source).read_text(), '{"mcpServers": {}}\n')
        self.assertEqual(os.stat(source).st_mode & 0o777, 0o600)

    def test_codex_configuration_files_are_read_only(self) -> None:
        mounts = self._mounts("codex")
        codex = self.home / ".codex"
        self.assertEqual(mounts[str(codex)][0], "--bind")
        for name in ("config.toml", "openai.config.toml", "AGENTS.md", "plugins"):
            with self.subTest(name=name):
                self.assertEqual(mounts[str(codex / name)][0], "--ro-bind")
        self.assertEqual(mounts[str(codex / "log")][0], "--tmpfs")
        self.assertNotIn(str(codex / "auth.json"), mounts)

    def test_symlinked_entries_are_never_used_as_mount_points(self) -> None:
        link = self.home / ".claude" / "rules"
        if link.is_dir() and not link.is_symlink():
            link.rmdir()
        link.symlink_to(self.repo)
        self.addCleanup(link.unlink)
        self.assertNotIn(str(link), self._mounts("claude"))


class SocketFilterProgramTests(unittest.TestCase):
    def test_program_shape(self) -> None:
        for machine, length in (("x86_64", 13), ("aarch64", 11)):
            with self.subTest(machine=machine):
                program = sandbox._socket_filter_program(machine)
                self.assertEqual(len(program), length * 8)
        self.assertIsNone(sandbox._socket_filter_program("riscv64"))


@unittest.skipUnless(shutil.which("git"), "git is required")
class GitDirectoryBindingTests(unittest.TestCase):
    """Only a worktree the main repository itself registered may add a bind."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.main = self._repo("main")
        self.sibling = self._repo("sibling")

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    def _repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir()
        self._git(repo, "init", "-q", "-b", "main")
        (repo / "f").write_text("1\n", encoding="utf-8")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-qm", "one")
        return repo

    def _extra_binds(self, workdir: Path) -> list[str]:
        if not _sandbox_usable():
            self.skipTest("bubblewrap sandbox unavailable")
        argv = sandbox.build_argv(["/bin/true"], cwd=str(workdir), tool="claude")
        # Provider-config binds live under the fixture home, not this root.
        return [
            argv[i + 1]
            for i, flag in enumerate(argv)
            if flag == "--bind"
            and argv[i + 1] != str(workdir)
            and argv[i + 1].startswith(str(self.root))
        ]

    def test_main_checkout_adds_no_git_bind(self) -> None:
        self.assertIsNone(sandbox._git_common_dir(self.main))

    def test_registered_worktree_binds_its_own_repository(self) -> None:
        worktree = self.root / "wt"
        self._git(self.main, "worktree", "add", "-q", "-b", "side", str(worktree))
        self.assertEqual(sandbox._git_common_dir(worktree), self.main / ".git")
        self.assertEqual(self._extra_binds(worktree), [str(self.main / ".git")])

    def test_symlinked_git_directory_is_not_followed(self) -> None:
        shutil.rmtree(self.main / ".git")
        (self.main / ".git").symlink_to(self.sibling / ".git")
        self.assertIsNone(sandbox._git_common_dir(self.main))
        self.assertEqual(self._extra_binds(self.main), [])

    def test_forged_gitdir_pointer_cannot_bind_a_sibling(self) -> None:
        shutil.rmtree(self.main / ".git")
        (self.main / ".git").write_text(
            f"gitdir: {self.sibling / '.git'}\n", encoding="utf-8"
        )
        self.assertIsNone(sandbox._git_common_dir(self.main))
        self.assertEqual(self._extra_binds(self.main), [])

    def test_forged_commondir_cannot_bind_a_sibling(self) -> None:
        # The agent owns its repository, so it can fabricate worktree metadata
        # there whose commondir names another repository.
        fake = self.main / ".git" / "worktrees" / "fake"
        fake.mkdir(parents=True)
        (fake / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (fake / "commondir").write_text(f"{self.sibling / '.git'}\n", encoding="utf-8")
        victim = self.root / "victim"
        victim.mkdir()
        (victim / ".git").write_text(f"gitdir: {fake}\n", encoding="utf-8")
        (fake / "gitdir").write_text(f"{victim / '.git'}\n", encoding="utf-8")
        self.assertIsNone(sandbox._git_common_dir(victim))
        self.assertNotIn(str(self.sibling / ".git"), self._extra_binds(victim))

    def test_unregistered_pointer_into_sibling_worktrees_is_rejected(self) -> None:
        other = self.root / "other-wt"
        self._git(self.sibling, "worktree", "add", "-q", "-b", "side", str(other))
        admin = self.sibling / ".git" / "worktrees" / "other-wt"
        shutil.rmtree(self.main / ".git")
        (self.main / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
        self.assertIsNone(sandbox._git_common_dir(self.main))


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

    def test_provider_settings_cannot_be_modified_or_created(self) -> None:
        claude = _FIXTURE["home"] / ".claude"
        script = (
            f"echo x >> {claude}/settings.json 2>/dev/null && echo W1;"
            f"echo x > {claude}/plugins/evil 2>/dev/null && echo W2;"
            f"rm -rf {claude}/skills 2>/dev/null && echo W3;"
            f"mv {claude}/settings.json {claude}/s2 2>/dev/null && echo W4;"
            "echo DONE"
        )
        self.assertEqual(self._run(script), "DONE")
        self.assertEqual((claude / "settings.json").read_text(), '{"hooks": {}}\n')
        self.assertTrue((claude / "skills").is_dir())

    def test_credentials_can_still_be_replaced_atomically(self) -> None:
        claude = _FIXTURE["home"] / ".claude"
        script = (
            f"echo new > {claude}/.credentials.json.tmp && "
            f"mv {claude}/.credentials.json.tmp {claude}/.credentials.json && echo OK"
        )
        self.assertEqual(self._run(script), "OK")
        self.assertEqual((claude / ".credentials.json").read_text(), "new\n")
        (claude / ".credentials.json").write_text("{}\n")

    def test_other_projects_history_and_ide_state_are_absent(self) -> None:
        claude = _FIXTURE["home"] / ".claude"
        script = (
            f"ls {claude}/projects/-some-other-project 2>/dev/null && echo LEAK1;"
            f"cat {claude}/ide/1234.lock 2>/dev/null && echo LEAK2;"
            f"cat {claude}/history.jsonl; echo DONE"
        )
        self.assertEqual(self._run(script), "DONE")

    def test_own_project_transcripts_persist(self) -> None:
        claude = _FIXTURE["home"] / ".claude"
        slug = str(self.repo).replace("/", "-").replace("_", "-")
        script = f"echo t > {claude}/projects/{slug}/session.jsonl && echo OK"
        self.assertEqual(self._run(script), "OK")
        self.assertEqual(
            (claude / "projects" / slug / "session.jsonl").read_text(), "t\n"
        )

    def test_mixed_state_file_changes_never_reach_the_real_file(self) -> None:
        real = _FIXTURE["home"] / ".claude.json"
        script = f'echo \'{{"mcpServers":{{"x":1}}}}\' > {real} && cat {real}'
        self.assertIn('"x"', self._run(script))
        self.assertEqual(real.read_text(), '{"mcpServers": {}}\n')

    def test_unix_domain_sockets_are_refused_but_network_sockets_work(self) -> None:
        probe = (
            "import socket\n"
            "def attempt(label, make):\n"
            "    try:\n"
            "        make(); print(label, 'ok')\n"
            "    except OSError as exc:\n"
            "        print(label, type(exc).__name__)\n"
            "attempt('unix', lambda: socket.socket(socket.AF_UNIX))\n"
            "attempt('inet', lambda: socket.socket(socket.AF_INET))\n"
            "attempt('pair', lambda: socket.socketpair())\n"
        )
        (self.repo / "probe.py").write_text(probe, encoding="utf-8")
        out = self._run("/usr/bin/python3 probe.py")
        self.assertEqual(
            out.splitlines(), ["unix PermissionError", "inet ok", "pair ok"]
        )
        with mock.patch.dict(
            os.environ, {"OMICSANG_AGENT_SANDBOX_UNIX_SOCKETS": "allow"}
        ):
            self.assertEqual(
                self._run("/usr/bin/python3 probe.py").splitlines()[0], "unix ok"
            )

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
