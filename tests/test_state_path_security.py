# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchtop import fs_policy, settings


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class StatePathSecurityTest(unittest.TestCase):
    def _settings(self, *, root: Path, state: Path):
        return mock.patch.multiple(
            settings,
            ROOT=root,
            STATE_DIR=state,
            RUN_LOG_DIR=state / "sessions",
            STATE_FILE=state / "state.json",
        )

    def test_clear_state_rejects_symlink_and_preserves_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "pipelines"
            root.mkdir()
            external = base / "external-state"
            external.mkdir()
            marker = external / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            state = base / "state-link"
            state.symlink_to(external, target_is_directory=True)

            with self._settings(root=root, state=state):
                with self.assertRaises(ValueError):
                    settings.clear_state(confirmed=True)

            self.assertTrue(state.is_symlink())
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_clear_state_rejects_symlinked_ancestor_and_preserves_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "pipelines"
            root.mkdir()
            external_parent = base / "external-parent"
            state_target = external_parent / "state"
            state_target.mkdir(parents=True)
            marker = state_target / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            linked_parent = base / "linked-parent"
            linked_parent.symlink_to(external_parent, target_is_directory=True)
            state = linked_parent / "state"

            with self._settings(root=root, state=state):
                with self.assertRaises(ValueError):
                    settings.clear_state(confirmed=True)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_clear_state_rejects_pipeline_root_and_preserves_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pipeline-root"
            root.mkdir()
            marker = root / "Snakefile"
            marker.write_text("rule all:\n", encoding="utf-8")

            with self._settings(root=root, state=root):
                with self.assertRaises(ValueError):
                    settings.clear_state(confirmed=True)

            self.assertEqual(marker.read_text(encoding="utf-8"), "rule all:\n")

    def test_ensure_dirs_refuses_state_inside_pipeline_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pipeline-root"
            root.mkdir()
            state = root / "private-state"

            with self._settings(root=root, state=state):
                with self.assertRaises(ValueError):
                    settings.ensure_dirs()

            self.assertFalse(state.exists())

    def test_hardening_does_not_follow_external_symlinks_or_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "pipelines"
            root.mkdir()
            state = base / "state"
            sessions = state / "sessions"
            sessions.mkdir(parents=True)
            external = base / "external.txt"
            external.write_text("external", encoding="utf-8")
            external.chmod(0o644)
            (sessions / "external-link").symlink_to(external)
            hardlink = sessions / "external-hardlink"
            os.link(external, hardlink)

            with self._settings(root=root, state=state):
                settings.harden_existing_state_permissions()

            self.assertEqual(_mode(external), 0o644)
            self.assertEqual(_mode(hardlink), 0o644)
            self.assertTrue((sessions / "external-link").is_symlink())

    def test_atomic_write_preserves_mode_and_defaults_new_source_to_0644(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "executable.sh"
            existing.write_text("old\n", encoding="utf-8")
            existing.chmod(0o755)

            fs_policy.atomic_write_text(existing, "new\n")
            created = root / "new.py"
            fs_policy.atomic_write_text(created, "print('ok')\n")
            private = root / "private.json"
            fs_policy.atomic_write_text(private, "{}\n", mode=0o600)

            self.assertEqual(existing.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(_mode(existing), 0o755)
            self.assertEqual(_mode(created), 0o644)
            self.assertEqual(_mode(private), 0o600)

    def test_registered_root_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real_root = base / "real-root"
            real_root.mkdir()
            (real_root / "Snakefile").write_text("rule all:\n", encoding="utf-8")
            linked_root = base / "linked-root"
            linked_root.symlink_to(real_root, target_is_directory=True)

            with self.assertRaises(fs_policy.PathPolicyError):
                fs_policy.resolve_relative(
                    linked_root,
                    "Snakefile",
                    must_exist=True,
                    expected="file",
                )

    def test_bind_host_uses_exact_loopback_allowlist(self) -> None:
        self.assertEqual(settings.validate_bind_host("localhost"), "localhost")
        self.assertEqual(settings.validate_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(settings.validate_bind_host("::1"), "::1")
        for host in ("127.0.0.2", "0.0.0.0", "::", "example.test"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                settings.validate_bind_host(host)


if __name__ == "__main__":
    unittest.main()
