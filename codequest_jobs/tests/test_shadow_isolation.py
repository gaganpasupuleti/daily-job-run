"""Shadow path must not import catalog writers."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "application_route_validation"


class ShadowIsolationTests(unittest.TestCase):
    def test_shadow_modules_do_not_reference_apply_link_observations(self) -> None:
        offenders: list[str] = []
        for path in PKG.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id == "apply_link_observations":
                        offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_shadow_engine_has_no_catalog_import(self) -> None:
        text = (PKG / "shadow_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("jobready_catalog", text)
        self.assertNotIn("apply_link_observations", text)


if __name__ == "__main__":
    unittest.main()
