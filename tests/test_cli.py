"""CLI smoke tests. No network, ssh, sbuild, or mock."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from rocky2deb.cli import main as rocky_main
from rocky2deb.rockify_cli import main as rockify_main

ROOT = Path(__file__).resolve().parents[1]
HELLO = ROOT / "tests" / "fixtures" / "hello.spec"
REPO = ROOT / "tests" / "fixtures" / "repo.json"


def run_rocky(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rocky_main(argv)
    return code, out.getvalue(), err.getvalue()


def run_rockify(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rockify_main(argv)
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_profiles_import_apply_and_build(self) -> None:
        code, out, err = run_rocky(["profiles"])
        self.assertEqual(code, 0, err)
        self.assertIn("trixie", out)
        self.assertIn("forky", out)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir_path = root / "hello.json"
            code, out, err = run_rocky(["import-spec", str(HELLO), "-o", str(ir_path)])
            self.assertEqual(code, 0, err)
            data = json.loads(ir_path.read_text(encoding="utf-8"))
            self.assertEqual(data["name"], "hello")
            os_release = root / "os-release"
            os_release.write_text('ID=debian\nVERSION_CODENAME=bookworm\n', encoding="utf-8")
            code, out, err = run_rocky(
                ["apply-check", "--suite", "bookworm", "--os-release", str(os_release)]
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(out.strip(), "ok")
            code, out, err = run_rocky(["build", "deb", "--execute"])
            self.assertEqual(code, 3)
            self.assertIn("sbuild is not installed", err)
            code, out, err = run_rocky(["build", "deb"])
            self.assertEqual(code, 3)
            self.assertIn("without --execute", err)
            code, out, err = run_rocky(["import-spec", str(root / "x.src.rpm"), "-o", str(ir_path)])
            self.assertEqual(code, 3)
            self.assertIn("src.rpm", err)

    def test_remap_does_not_print_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "chrony.conf"
            conf.write_text("pool pool.ntp.org iburst\nnotakey secret-value\n", encoding="utf-8")
            code, out, err = run_rocky(["remap", "--map", "chrony", "--file", str(conf)])
        self.assertEqual(code, 0, err)
        self.assertIn("unmapped notakey", out)
        self.assertNotIn("secret-value", out)

    def test_rockify_plan(self) -> None:
        code, out, err = run_rockify(
            [
                "plan",
                "widget",
                "--rocky",
                "9",
                "--suite",
                "trixie",
                "--repo",
                str(REPO),
                "--index",
                "gcc=12.2.0-1",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("runs", out)
        self.assertIn("libwidget widget", out)

    def test_module_entrypoint(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        proc = subprocess.run(
            [sys.executable, "-m", "rocky2deb", "profiles"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("trixie", proc.stdout)

    def test_suite_upgrade_is_plan_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os_release = root / "os-release"
            os_release.write_text(BOOKWORM, encoding="utf-8")
            decisions = root / "decisions.json"
            decisions.write_text(
                json.dumps(
                    [
                        {
                            "rpm": "widget",
                            "action": "rebuild",
                            "deb": "widget",
                            "reason": "free license token",
                            "license_class": "dfsg",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            code, out, err = run_rocky(
                [
                    "suite-upgrade",
                    "--from",
                    "bookworm",
                    "--to",
                    "trixie",
                    "--os-release",
                    str(os_release),
                    "--decisions",
                    str(decisions),
                    "--index",
                    "widget=1.2-1",
                ]
            )
        self.assertEqual(code, 0, err)
        self.assertIn("plan-only", out)
        self.assertIn("flip-to-debian", out)

    def test_fetch_refuses_userinfo_without_echo(self) -> None:
        code, _out, err = run_rocky(["fetch", "https://user:pass@example.com/a.src.rpm", "--execute"])
        self.assertEqual(code, 3)
        self.assertNotIn("user:pass", err)
        self.assertIn("userinfo", err)


BOOKWORM = 'ID=debian\nVERSION_CODENAME="bookworm"\n'


if __name__ == "__main__":
    unittest.main()
