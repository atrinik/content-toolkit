#!/usr/bin/env python3
"""Repository contract tests for the intentionally small public crate family."""
import json
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = {
    "atrinik-artifact", "atrinik-catalog", "atrinik-diagnostics", "atrinik-schema",
    "atrinik-source", "atrinik-testkit", "atrinik-transaction",
}


class PolicyTests(unittest.TestCase):
    def test_allowlist_matches_only_reusable_library_manifests(self):
        policy = json.loads((ROOT / "policy/crate-release.json").read_text())
        self.assertEqual({item["name"] for item in policy["crates"]}, PUBLIC)
        self.assertEqual(len(policy["crates"]), len(PUBLIC))
        for manifest in sorted((ROOT / "crates").glob("*/Cargo.toml")):
            package = tomllib.loads(manifest.read_text())["package"]
            self.assertEqual(package["publish"], ["crates-io"] if package["name"] in PUBLIC else False)

    def test_family_uses_one_version_with_exact_internal_dependencies(self):
        workspace = tomllib.loads((ROOT / "Cargo.toml").read_text())["workspace"]
        version = workspace["package"]["version"]
        for path in workspace["members"]:
            package = tomllib.loads((ROOT / path / "Cargo.toml").read_text())["package"]
            self.assertEqual(package["version"], {"workspace": True})
        for name, dependency in workspace["dependencies"].items():
            if name.startswith("atrinik-"):
                self.assertEqual(dependency["version"], "=" + version)

    def test_preparation_precedes_source_publication(self):
        release = json.loads((ROOT / ".releaserc.json").read_text())
        execution = next(p[1] for p in release["plugins"] if isinstance(p, list) and p[0] == "@semantic-release/exec")
        self.assertIn("crate_release.py finalize", execution["prepareCmd"])
        self.assertNotIn("publishCmd", execution)
        self.assertIn("crate_release.py dispatch", execution["successCmd"])
        assets = next(p[1]["assets"] for p in release["plugins"] if isinstance(p, list) and p[0] == "@semantic-release/github")
        self.assertIn({"path": "build/release/*"}, assets)


if __name__ == "__main__":
    unittest.main()
