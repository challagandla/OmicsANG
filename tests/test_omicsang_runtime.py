# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import omicsang
from benchtop import __main__ as cli
from benchtop import __version__, env_compat, help_content, security, server, settings


class OmicsANGEnvironmentCompatibilityTest(unittest.TestCase):
    def test_public_name_is_primary_and_legacy_name_is_a_fallback(self) -> None:
        for suffix in (
            "ROOT",
            "STATE",
            "RESULTS_ROOTS",
            "HOST",
            "PORT",
            "LOG_RETENTION_DAYS",
            "MAX_LOCAL_CORES",
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    env_compat.environment_value(
                        suffix,
                        environ={f"BENCHTOP_{suffix}": "legacy-value"},
                    ),
                    "legacy-value",
                )
                self.assertEqual(
                    env_compat.environment_value(
                        suffix,
                        environ={f"OMICSANG_{suffix}": "public-value"},
                    ),
                    "public-value",
                )

    def test_equal_dual_definitions_are_accepted(self) -> None:
        self.assertEqual(
            env_compat.environment_value(
                "ROOT",
                environ={
                    "OMICSANG_ROOT": "/same/root",
                    "BENCHTOP_ROOT": "/same/root",
                },
            ),
            "/same/root",
        )

    def test_unequal_dual_definitions_reject_without_exposing_values(self) -> None:
        public_value = "/private/public-root-token"
        legacy_value = "/private/legacy-root-token"
        with self.assertRaises(env_compat.EnvironmentConflict) as caught:
            env_compat.environment_value(
                "ROOT",
                environ={
                    "OMICSANG_ROOT": public_value,
                    "BENCHTOP_ROOT": legacy_value,
                },
            )
        message = str(caught.exception)
        self.assertIn("OMICSANG_ROOT", message)
        self.assertIn("BENCHTOP_ROOT", message)
        self.assertNotIn(public_value, message)
        self.assertNotIn(legacy_value, message)

    def test_cli_reports_env_conflict_without_exposing_values(self) -> None:
        public_value = "public-host-token"
        legacy_value = "legacy-host-token"
        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "OMICSANG_HOST": public_value,
                    "BENCHTOP_HOST": legacy_value,
                },
                clear=True,
            ),
            mock.patch("sys.stderr", stderr),
            self.assertRaises(SystemExit) as caught,
        ):
            cli._parse_args([])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("OMICSANG_HOST", stderr.getvalue())
        self.assertIn("BENCHTOP_HOST", stderr.getvalue())
        self.assertNotIn(public_value, stderr.getvalue())
        self.assertNotIn(legacy_value, stderr.getvalue())

    def test_explicit_cli_host_overrides_conflicting_environment_names(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "OMICSANG_HOST": "public-host-token",
                "BENCHTOP_HOST": "legacy-host-token",
            },
            clear=True,
        ):
            args = cli._parse_args(["--host", "127.0.0.1"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8787)

    def test_explicit_cli_port_overrides_invalid_environment_value(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"OMICSANG_PORT": "not-a-port"},
            clear=True,
        ):
            args = cli._parse_args(["--port", "8799"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8799)


class OmicsANGStateCompatibilityTest(unittest.TestCase):
    def test_clean_install_uses_omicsang_state(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp},
                clear=True,
            ),
        ):
            self.assertEqual(settings._default_state_dir(), Path(tmp) / "omicsang")

    def test_legacy_only_state_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "benchtop"
            legacy.mkdir()
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp},
                clear=True,
            ):
                self.assertEqual(settings._default_state_dir(), legacy)

    def test_existing_omicsang_state_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "omicsang"
            current.mkdir()
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp},
                clear=True,
            ):
                self.assertEqual(settings._default_state_dir(), current)

    def test_ambiguous_dual_default_state_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("omicsang", "benchtop"):
                (Path(tmp) / name).mkdir()
            with (
                mock.patch.dict(
                    os.environ,
                    {"XDG_STATE_HOME": tmp},
                    clear=True,
                ),
                self.assertRaises(ValueError) as caught,
            ):
                settings._default_state_dir()
        self.assertIn("both OmicsANG and pre-rename state", str(caught.exception))
        self.assertIn("OMICSANG_STATE", str(caught.exception))

    def test_explicit_state_bypasses_ambiguous_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("omicsang", "benchtop"):
                (Path(tmp) / name).mkdir()
            selected = Path(tmp) / "selected-state"
            with mock.patch.dict(
                os.environ,
                {
                    "XDG_STATE_HOME": tmp,
                    "OMICSANG_STATE": str(selected),
                },
                clear=True,
            ):
                self.assertEqual(settings._configured_state_dir(), selected)


class OmicsANGIdentityTest(unittest.TestCase):
    def test_public_shim_and_cli_identity(self) -> None:
        self.assertEqual(omicsang.__version__, __version__)
        self.assertEqual(cli._parser().prog, "omicsang")
        completed = subprocess.run(
            [sys.executable, "-m", "omicsang", "--version"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(completed.stdout.strip(), f"omicsang {__version__}")

    def test_fastapi_and_help_use_public_name(self) -> None:
        self.assertEqual(server.app.title, "OmicsANG")
        rendered = json.dumps(help_content.public_help_catalog(), sort_keys=True)
        self.assertIn("OmicsANG", rendered)
        self.assertNotIn("Bench" + "top", rendered)

    def test_security_protocol_names_remain_legacy_compatible(self) -> None:
        self.assertEqual(security.AUTH_COOKIE, "benchtop_session")
        self.assertEqual(security.CSRF_HEADER, "x-benchtop-csrf")


if __name__ == "__main__":
    unittest.main()
