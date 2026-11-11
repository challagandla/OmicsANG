# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Security regressions for pipeline identity and Git worktree reuse."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchtop import git_ops, pipelines


def _make_pipeline(path: Path) -> None:
    path.mkdir()
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")


@unittest.skipUnless(shutil.which("git"), "git is required")
def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "OmicsANG Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "benchtop-test@example.invalid"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=path,
        check=True,
        capture_output=True,
    )


class PipelineIdentitySecurityTest(unittest.TestCase):
    def test_get_rejects_traversal_absolute_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            root.mkdir()
            _make_pipeline(root / "alpha")
            outside = base / "outside"
            _make_pipeline(outside)
            (root / "linked").symlink_to(outside, target_is_directory=True)

            with mock.patch.object(pipelines.settings, "ROOT", root):
                self.assertEqual(
                    pipelines.get("alpha").path, (root / "alpha").resolve()
                )
                for hostile in (".", "..", str(outside), "../outside", "a/b", "a\\b"):
                    with self.subTest(name=hostile):
                        self.assertIsNone(pipelines.get(hostile))
                self.assertIsNone(pipelines.get("linked"))
                self.assertNotIn("linked", {item.name for item in pipelines.discover()})

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_local_names_ignore_colliding_and_hostile_github_remotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for local_name in ("alpha", "beta", "gamma"):
                pipeline = root / local_name
                _make_pipeline(pipeline)
                subprocess.run(
                    ["git", "init"], cwd=pipeline, check=True, capture_output=True
                )
            for local_name in ("alpha", "beta"):
                subprocess.run(
                    [
                        "git",
                        "remote",
                        "add",
                        "origin",
                        "https://github.com/example/shared.git",
                    ],
                    cwd=root / local_name,
                    check=True,
                )
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/example/../../-hostile.git",
                ],
                cwd=root / "gamma",
                check=True,
            )
            for hostile_name in ("-option", "bad name", "bad\nname"):
                _make_pipeline(root / hostile_name)

            with mock.patch.object(pipelines.settings, "ROOT", root):
                found = pipelines.discover()
                self.assertEqual(
                    [item.name for item in found], ["alpha", "beta", "gamma"]
                )
                self.assertEqual(
                    [item.path.name for item in found], ["alpha", "beta", "gamma"]
                )
                for hostile_name in ("-option", "bad name", "bad\nname"):
                    self.assertIsNone(pipelines.get(hostile_name))


class GitRemoteRedactionSecurityTest(unittest.TestCase):
    def test_scp_like_remote_userinfo_is_not_exposed(self) -> None:
        redacted = git_ops._redact_remote(
            "oauth-token@github.com:example/private-repo.git"
        )
        self.assertEqual(redacted, "github.com:example/private-repo.git")
        self.assertNotIn("oauth-token", redacted)
        self.assertEqual(git_ops.github_slug(redacted), "example/private-repo")


@unittest.skipUnless(shutil.which("git"), "git is required")
class WorktreeIdentitySecurityTest(unittest.TestCase):
    def test_verified_worktree_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _init_repo(repo)
            with mock.patch.object(git_ops.settings, "STATE_DIR", base / "state"):
                created = git_ops.add_worktree(repo, "feature/verified")
                self.assertTrue(created["ok"], created)
                self.assertFalse(created["reused"])
                reused = git_ops.add_worktree(repo, "feature/verified")
                self.assertTrue(reused["ok"], reused)
                self.assertTrue(reused["reused"])
                self.assertEqual(reused["path"], created["path"])

    def test_preexisting_unregistered_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _init_repo(repo)
            with mock.patch.object(git_ops.settings, "STATE_DIR", base / "state"):
                destination, error = git_ops._worktree_destination(
                    repo, "feature/claimed"
                )
                self.assertFalse(error)
                self.assertIsNotNone(destination)
                destination.mkdir()
                marker = destination / "do-not-touch"
                marker.write_text("preserved\n", encoding="utf-8")

                result = git_ops.add_worktree(repo, "feature/claimed")
                self.assertFalse(result["ok"], result)
                self.assertTrue(marker.is_file())
                self.assertIn("different Git repository", result["output"])

    def test_symlink_destination_and_symlink_state_parent_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _init_repo(repo)
            state = base / "state"
            with mock.patch.object(git_ops.settings, "STATE_DIR", state):
                destination, error = git_ops._worktree_destination(
                    repo, "feature/symlink"
                )
                self.assertFalse(error)
                external = base / "external"
                external.mkdir()
                destination.symlink_to(external, target_is_directory=True)
                result = git_ops.add_worktree(repo, "feature/symlink")
                self.assertFalse(result["ok"], result)
                self.assertIn("regular directory", result["output"])

            second_state = base / "second-state"
            second_state.mkdir()
            external_parent = base / "external-parent"
            external_parent.mkdir()
            (second_state / "worktrees").symlink_to(
                external_parent, target_is_directory=True
            )
            with mock.patch.object(git_ops.settings, "STATE_DIR", second_state):
                result = git_ops.add_worktree(repo, "feature/parent-symlink")
                self.assertFalse(result["ok"], result)
                self.assertIn("must not be a symlink", result["output"])

    def test_registered_worktree_with_wrong_branch_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _init_repo(repo)
            with mock.patch.object(git_ops.settings, "STATE_DIR", base / "state"):
                destination, error = git_ops._worktree_destination(
                    repo, "feature/expected"
                )
                self.assertFalse(error)
                subprocess.run(
                    [
                        "git",
                        "worktree",
                        "add",
                        "-b",
                        "feature/other",
                        str(destination),
                        "HEAD",
                    ],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                )
                result = git_ops.add_worktree(repo, "feature/expected")
                self.assertFalse(result["ok"], result)
                self.assertIn("unexpected branch", result["output"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


@unittest.skipUnless(shutil.which("git"), "git is required")
class HostileRepositoryConfigTests(unittest.TestCase):
    """Displaying a pipeline must never execute what its git config names."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        (self.repo / "f").write_text("1\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "one")
        self.marker = self.root / "executed"
        self.script = self.root / "payload.sh"
        self.script.write_text(f"#!/bin/sh\ntouch {self.marker}\n", encoding="utf-8")
        self.script.chmod(0o755)
        git_ops._config_cache.clear()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

    def _display(self) -> None:
        from benchtop import provenance

        git_ops.status(self.repo)
        git_ops.diff(self.repo)
        git_ops.diffstat(self.repo)
        git_ops.list_worktrees(self.repo)
        provenance._git_snapshot(self.repo)

    def test_executable_settings_are_neutralized(self) -> None:
        cases = {
            "fsmonitor": lambda: self._git(
                "config", "core.fsmonitor", str(self.script)
            ),
            "clean filter": lambda: (
                self._git("config", "filter.evil.clean", f"{self.script}; cat"),
                (self.repo / ".gitattributes").write_text("* filter=evil\n"),
            ),
            "filter subsection containing '='": lambda: (
                self._git("config", "filter.a=b.clean", f"{self.script}; cat"),
                (self.repo / ".gitattributes").write_text("* filter=a=b\n"),
            ),
            "external diff": lambda: self._git(
                "config", "diff.external", str(self.script)
            ),
            "textconv": lambda: (
                self._git("config", "diff.evil.textconv", str(self.script)),
                (self.repo / ".gitattributes").write_text("* diff=evil\n"),
            ),
            "included file": lambda: (
                (self.repo / "inc").write_text(
                    f"[core]\n\tfsmonitor = {self.script}\n", encoding="utf-8"
                ),
                self._git("config", "include.path", "../inc"),
            ),
        }
        for label, arrange in cases.items():
            with self.subTest(label=label):
                arrange()
                (self.repo / "f").write_text(f"changed {label}\n", encoding="utf-8")
                self._display()
                self.assertFalse(self.marker.exists(), label)

    def test_hooks_do_not_run_for_worktree_creation(self) -> None:
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {self.marker}\n", encoding="utf-8")
        hook.chmod(0o755)
        env = git_ops.hardened_git_env(self.repo)
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "side", str(self.root / "wt")],
            cwd=self.repo,
            env=env,
            check=True,
            capture_output=True,
        )
        self.assertFalse(self.marker.exists())

    def test_user_level_configuration_is_preserved(self) -> None:
        home = self.root / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(
            "[credential]\n\thelper = trusted-helper\n", encoding="utf-8"
        )
        self._git("config", "credential.helper", "!evil-helper")
        with mock.patch.dict(
            "os.environ", {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg")}
        ):
            env = git_ops.hardened_git_env(self.repo)
            values = subprocess.run(
                ["git", "config", "-z", "--get-all", "credential.helper"],
                cwd=self.repo,
                env=env,
                capture_output=True,
                text=True,
            ).stdout.split("\0")[:-1]
        # Git discards every helper listed before an empty value.
        reset = len(values) - 1 - values[::-1].index("")
        self.assertIn("!evil-helper", values[:reset])
        self.assertEqual(values[reset + 1 :], ["trusted-helper"])

    def test_unreadable_configuration_refuses_to_run_git(self) -> None:
        with mock.patch.object(git_ops, "_scan_repository_config", return_value=None):
            self.assertIsNone(git_ops.hardened_git_env(self.repo))
            self.assertFalse(git_ops.status(self.repo)["is_repo"])

    def test_status_matches_porcelain_v1_rendering(self) -> None:
        (self.repo / "f").write_text("2\n", encoding="utf-8")
        (self.repo / "new file.txt").write_text("n\n", encoding="utf-8")
        (self.repo / "staged.txt").write_text("s\n", encoding="utf-8")
        self._git("add", "staged.txt")
        expected = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        status = git_ops.status(self.repo)
        self.assertEqual(status["dirty_files"], expected)
        self.assertEqual(status["dirty"], len(expected))
        self.assertEqual(status["branch"], "main")
        self.assertIsNone(status["upstream"])
        self.assertIsNone(status["ahead"])
