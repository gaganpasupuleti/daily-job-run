"""The retired CodeQuest-to-JobReady prune command refuses without database access. Pure tests."""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402

import prune_job_ready_now  # noqa: E402
import sync_job_ready_ui  # noqa: E402

MODULE = ROOT / "prune_job_ready_now.py"
REFUSAL = "RETIRED_COMMAND prune_job_ready_now"
SENTINEL_ENV = {
    "CODEQUEST_DATABASE_URL": "postgresql://cq_user:SENTINEL_CQ_SECRET@codequest.invalid:5432/codequest",
    "DATABASE_URL": "postgresql://legacy_user:SENTINEL_LEGACY_SECRET@legacy.invalid:5432/naukri_scraper",
    "JOB_READY_DATABASE_URL": "postgresql://catalog_user:SENTINEL_CATALOG_SECRET@catalog.invalid:5432/railway",
    "JOB_READY_APP_DATABASE_URL": "postgresql://app_user:SENTINEL_APP_SECRET@app.invalid:5432/railway",
}
OLD_INVOCATIONS = (
    [],
    ["--prune-job-ready"],
    ["--prune-job-ready", "--confirm-target", "app.invalid:5432/railway"],
    ["--confirm-target", "app.invalid:5432/railway", "--prune-job-ready"],
    ["--prune-job-ready", "--confirm-target", SENTINEL_ENV["JOB_READY_APP_DATABASE_URL"]],
    ["--force", "--yes", "--allow-delete"],
)


def _fail(name: str):
    def raiser(*_args, **_kwargs):
        raise AssertionError(f"{name} must not be called by the retired command")

    return raiser


class RetiredPruneTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, mock.MagicMock, list[str]]:
        calls: list[str] = []
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, SENTINEL_ENV), mock.patch.object(
            psycopg2, "connect", side_effect=_fail("psycopg2.connect")
        ) as connect, mock.patch.object(
            sync_job_ready_ui, "archive_ui_jobs_not_in_catalog", side_effect=lambda **_: calls.append("archive")
        ), mock.patch.object(
            sync_job_ready_ui, "apply_membership", side_effect=lambda *_a, **_k: calls.append("apply")
        ), redirect_stdout(stdout):
            code = prune_job_ready_now.main(argv)
        return code, stdout.getvalue(), connect, calls

    def test_no_arguments_fails_closed(self) -> None:
        code, output, connect, calls = self.run_main([])
        self.assertEqual(code, 2)
        self.assertEqual(output.strip(), REFUSAL)
        connect.assert_not_called()
        self.assertEqual(calls, [])

    def test_old_prune_flags_still_fail_closed(self) -> None:
        for argv in OLD_INVOCATIONS:
            code, output, connect, calls = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(output.strip().splitlines()[-1], REFUSAL, argv)
            connect.assert_not_called()
            self.assertEqual(calls, [], argv)

    def test_configured_codequest_and_catalog_urls_are_never_connected_to(self) -> None:
        for argv in OLD_INVOCATIONS:
            with mock.patch.dict(os.environ, SENTINEL_ENV):
                _code, _output, connect, _calls = self.run_main(argv)
                self.assertEqual(os.environ["CODEQUEST_DATABASE_URL"], SENTINEL_ENV["CODEQUEST_DATABASE_URL"])
                self.assertEqual(os.environ["JOB_READY_DATABASE_URL"], SENTINEL_ENV["JOB_READY_DATABASE_URL"])
            self.assertEqual(connect.call_count, 0, argv)

    def test_no_database_variable_is_required(self) -> None:
        cleared = {name: "" for name in SENTINEL_ENV}
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, cleared), mock.patch.object(
            psycopg2, "connect", side_effect=_fail("psycopg2.connect")
        ), redirect_stdout(stdout):
            code = prune_job_ready_now.main(["--prune-job-ready", "--confirm-target", "app.invalid:5432/railway"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue().strip(), REFUSAL)

    def test_application_sync_is_not_reachable(self) -> None:
        self.assertFalse(hasattr(prune_job_ready_now, "archive_ui_jobs_not_in_catalog"))
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported - {"__future__"}, {"sys"})

    def test_no_delete_from_validated_jobs_is_reachable(self) -> None:
        source = MODULE.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\bDELETE\s+FROM\b", source, re.IGNORECASE))
        self.assertNotIn("execute", source)
        self.assertNotIn("scraped_jobs WHERE", source)

    def test_refusal_is_stable_and_never_echoes_secrets(self) -> None:
        self.assertEqual(prune_job_ready_now.RETIRED_MESSAGE, REFUSAL)
        for argv in OLD_INVOCATIONS + (["--help"],):
            _code, output, _connect, _calls = self.run_main(argv)
            self.assertIn(REFUSAL, output)
            self.assertNotIn("://", output)
            self.assertNotIn("@", output)
            for value in SENTINEL_ENV.values():
                self.assertNotIn(value, output)
            self.assertNotIn("SENTINEL", output)

    def test_help_explains_retirement_and_still_fails(self) -> None:
        code, output, connect, _calls = self.run_main(["--help"])
        self.assertEqual(code, 2)
        self.assertIn("JobReady catalog deletion based on CodeQuest state is no longer supported", output)
        self.assertIn("CodeQuest is not authoritative", output)
        self.assertEqual(output.strip().splitlines()[-1], REFUSAL)
        connect.assert_not_called()

    def test_script_entrypoint_fails_without_importing_database_drivers(self) -> None:
        program = (
            "import runpy, sys\n"
            "sys.modules['psycopg2'] = None\n"
            "sys.modules['dotenv'] = None\n"
            "sys.modules['sync_job_ready_ui'] = None\n"
            f"sys.argv = [{str(MODULE)!r}, '--prune-job-ready', '--confirm-target', 'app.invalid:5432/railway']\n"
            f"runpy.run_path({str(MODULE)!r}, run_name='__main__')\n"
        )
        env = {**os.environ, **SENTINEL_ENV}
        proc = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout.strip(), REFUSAL)
        self.assertEqual(proc.stderr.strip(), "")
        self.assertNotIn("SENTINEL", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
