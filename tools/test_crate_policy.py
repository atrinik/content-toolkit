#!/usr/bin/env python3
"""Repository contract tests for the intentionally small public crate family."""
import json
import subprocess
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import crate_release as R

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

    def test_production_checksum_writer_is_accepted_by_remote_release_verifier(self):
        config = R.policy()
        revision = 'a' * 40
        manifest = {'schema_version': 1, 'repository': config['repository'],
                    'repository_id': config['repository_id'], 'revision': revision,
                    'source_tag': 'v1.0.0', 'crates': []}
        release = {'id': 123, 'tag_name': 'v1.0.0', 'draft': False, 'prerelease': False,
                   'published_at': '2026-10-11T00:00:00Z', 'immutable': True}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / R.MANIFEST).write_bytes(R.canonical(manifest))
            (directory / 'source-1.0.0.tar.gz').write_bytes(b'source fixture')
            sums = subprocess.check_output([sys.executable, str(ROOT / 'tools/release-checksums.py'), str(directory)])
            (directory / 'SHA256SUMS').write_bytes(sums)
            release['assets'] = [{'name': path.name, 'state': 'uploaded',
                                  'digest': 'sha256:' + R.digest(path.read_bytes())}
                                 for path in directory.iterdir()]
            def api(_config, suffix):
                if suffix == 'releases/123': return release
                if suffix == '': return {'id': config['repository_id'], 'full_name': config['repository'], 'default_branch': 'main'}
                if suffix == 'branches/main': return {'protected': True}
                if suffix.startswith('git/ref/tags/'): return {'object': {'type': 'commit', 'sha': revision}}
                if suffix.startswith('compare/'): return {'status': 'identical'}
                if suffix.startswith('actions/workflows/'):
                    return {'workflow_runs': [{'head_sha': revision, 'head_branch': 'main', 'event': 'push', 'conclusion': 'success'}]}
                if suffix.startswith('commits/'):
                    return {'total_count': len(config['required_checks']), 'check_runs': [
                        {'id': index, 'name': name, 'head_sha': revision, 'app': {'slug': 'github-actions'},
                         'status': 'completed', 'conclusion': 'success'}
                        for index, name in enumerate(config['required_checks'], start=1)]}
                self.fail('unexpected API query: ' + suffix)
            def asset(_config, _release, name, *_args):
                return (directory / name).read_bytes()
            with patch.object(R, 'api', side_effect=api), patch.object(R, 'asset_bytes', side_effect=asset), \
                 patch.object(R, 'verified_attestation', return_value=[{'run_id': 7, 'run_attempt': 1}]), \
                 patch.object(R, 'producing_run', return_value={'run_id': 7, 'run_attempt': 1}):
                R.release_evidence(config, '123', revision, R.digest((directory / R.MANIFEST).read_bytes()))
                (directory / 'SHA256SUMS').write_bytes(sums.replace(b'  source-', b'  ./source-'))
                with self.assertRaisesRegex(ValueError, 'checksum inventory invalid'):
                    R.release_evidence(config, '123', revision, R.digest((directory / R.MANIFEST).read_bytes()))

    def test_preparation_precedes_source_publication(self):
        release = json.loads((ROOT / ".releaserc.json").read_text())
        execution = next(p[1] for p in release["plugins"] if isinstance(p, list) and p[0] == "@semantic-release/exec")
        self.assertIn("crate_release.py finalize", execution["prepareCmd"])
        self.assertNotIn("publishCmd", execution)
        self.assertIn("crate_release.py dispatch", execution["successCmd"])
        assets = next(p[1]["assets"] for p in release["plugins"] if isinstance(p, list) and p[0] == "@semantic-release/github")
        self.assertIn({"path": "build/release/*"}, assets)

    def test_release_catalog_reads_use_only_the_read_only_job_token(self):
        # On main, planning and preparation list releases through api.github.com.
        # Without a token they share the runner IP's anonymous limit and can fail.
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        header, jobs = workflow.split("\njobs:\n", 1)
        self.assertIn("\npermissions:\n  contents: read\n\n", header)
        package = jobs.split("\n  attest:\n", 1)[0]
        self.assertTrue(package.startswith("  package:\n"))
        self.assertNotIn("permissions", package)
        steps = [step.partition("\n") for step in package.split("\n      - ")[1:]]
        token = "\n          GH_READ_TOKEN: ${{ github.token }}\n"
        carrying = [(name, body) for name, _, body in steps if token in body + "\n"]
        self.assertEqual([name for name, _ in carrying], [
            "name: Plan the actual source version without write credentials",
            "name: Prepare every immutable source and crate asset",
        ])
        self.assertIn("node scripts/release/source-version.mjs", carrying[0][1])
        self.assertIn("run: tools/package-release.sh ", carrying[1][1])
        # Count every spelling, so no other step, job env, secret, context dump
        # or GITHUB_ENV export hands the credential to another step.
        expressions = "".join(package.lower().split())
        self.assertEqual(expressions.count("github.token"), 2)
        self.assertEqual(package.count("GH_READ_TOKEN"), 2)
        for fragment in ("github[", "tojson(github", "secrets", "github_env", "gh_token",
                         "github_token", "gh_intent_token", "cargo_registr"):
            self.assertNotIn(fragment, expressions)
        # package-release.sh compiles and runs tools; only crate_prepare.py sees the token.
        script = (ROOT / "tools/package-release.sh").read_text()
        prefix, separator, _ = script.partition("\nunset GH_READ_TOKEN\n")
        self.assertTrue(separator)
        self.assertEqual([line for line in prefix.splitlines() if line and not line.startswith("#")], [
            "set -euo pipefail", "export PYTHONDONTWRITEBYTECODE=1", "catalog_read_token=${GH_READ_TOKEN-}",
        ])
        self.assertEqual(script.count("GH_READ_TOKEN"), 3)
        self.assertEqual(script.count("catalog_read_token"), 2)
        self.assertEqual(script.count("\nGH_READ_TOKEN=${catalog_read_token} python3 tools/crate_prepare.py "), 1)

if __name__ == "__main__":
    unittest.main()
