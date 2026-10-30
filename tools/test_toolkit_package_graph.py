#!/usr/bin/env python3
"""Toolkit integration of published libraries with source-only applications and adapter."""
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import crate_prepare as P
from tools import crate_release as R

REVISION = 'a' * 40
OLD_REVISION = 'b' * 40
APPLICATIONS = ('atrinik-content', 'atrinik-content-mcp', 'atrinik-classic-adapter')
# Follow the workspace version so reviewed version bumps keep this fixture valid.
VERSION = P.versions(R.policy())['atrinik-artifact']


def archive(name, revision):
    publish = 'false' if name in APPLICATIONS else '["crates-io"]'
    files = {
        'Cargo.toml': f'[package]\nname="{name}"\nversion="{VERSION}"\npublish={publish}\n'.encode(),
        'Cargo.lock': f'version=4\n[[package]]\nname="{name}"\nversion="{VERSION}"\n'.encode(),
        'src/lib.rs': b'pub fn fixture() {}\n',
        '.cargo_vcs_info.json': R.canonical({'git': {'sha1': revision}, 'path_in_vcs': 'crates/' + name}),
    }
    files['Cargo.toml.orig'] = files['Cargo.toml']
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as stream:
        for path, data in sorted(files.items()):
            member = tarfile.TarInfo(f'{name}-{VERSION}/{path}')
            member.size = len(data)
            stream.addfile(member, io.BytesIO(data))
    return output.getvalue()


class ToolkitPackageGraphTests(unittest.TestCase):
    def prepare(self, old_names, maintenance=False):
        config = R.policy()
        public = [row['name'] for row in config['crates']]
        originals = {name: archive(name, OLD_REVISION) for name in old_names}
        candidates = {name: archive(name, REVISION) for name in public + list(APPLICATIONS)}
        previous = {}
        for crate in config['crates']:
            name = crate['name']
            if name in originals:
                row = P.package_row(crate, VERSION, originals[name])
                previous[(name, VERSION)] = ({'id': 1}, {'revision': OLD_REVISION}, row, 'c' * 64)
        extras = {name: {'name': name, 'version': VERSION, 'path': 'crates/' + name}
                  for name in APPLICATIONS}
        selections, graphs = [], []
        def command(argv, **_):
            if argv[0] == 'cargo':
                names = [argv[index + 1] for index, value in enumerate(argv) if value == '--package']
                selections.append(names)
                directory = Path(argv[argv.index('--target-dir') + 1]) / 'package'
                directory.mkdir(parents=True)
                for name in names:
                    (directory / f'{name}-{VERSION}.crate').write_bytes(candidates[name])
            else:
                graphs.append({path.name: path.read_bytes() for path in Path(argv[-1]).glob('*.crate')})
        branch = '2.9.x' if maintenance else 'main'
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/' + branch,
                                    'ATRINIK_RELEASE_BRANCH': branch}, clear=True), \
             patch.object(P, 'git', side_effect=lambda *args: REVISION if args[0] == 'rev-parse' else ''), \
             patch.object(P, 'known', return_value=previous) as known, \
             patch.object(P, 'source_only_packages', return_value=extras), \
             patch.object(R, 'asset_bytes', side_effect=lambda _config, _release, asset: originals[asset[:-12]]), \
             patch.object(R, 'registry_state', side_effect=lambda row: 'present' if row['name'] in originals else 'absent') as registry, \
             patch.object(P.subprocess, 'run', side_effect=command):
            output = Path(temporary)
            manifest = P.prepare(config, 'v1.0.1', output, archives=output, source_packages=APPLICATIONS)
            retained = {path.name: path.read_bytes() for path in output.glob('*.crate')}
            self.assertEqual(len(graphs), 1)
            self.assertEqual(set(graphs[0]), {f'{name}-{VERSION}.crate' for name in candidates})
            self.assertTrue(all(name not in {row['name'] for row in manifest['crates']} for name in APPLICATIONS))
            for name in APPLICATIONS:
                self.assertEqual(retained[f'{name}-{VERSION}.crate'], candidates[name])
            if maintenance:
                known.assert_not_called()
                registry.assert_not_called()
            else:
                for name in originals:
                    self.assertEqual(graphs[0][f'{name}-{VERSION}.crate'], originals[name])
                    self.assertNotIn(f'{name}-{VERSION}.crate', retained)
            return manifest, retained, selections

    def test_known_libraries_are_original_bytes_and_both_apps_remain_source_assets(self):
        names = [row['name'] for row in R.policy()['crates']]
        manifest, retained, selections = self.prepare(names)
        self.assertEqual(manifest['crates'], [])
        self.assertEqual(len(retained), len(APPLICATIONS))
        self.assertEqual(selections[:-2], [[name] for name in names for _ in range(2)])
        self.assertEqual(selections[-2:], [list(APPLICATIONS)] * 2)

    def test_mixed_graph_excludes_published_library_from_new_app_overlay(self):
        manifest, retained, selections = self.prepare(['atrinik-diagnostics'])
        self.assertEqual(len(manifest['crates']), 6)
        self.assertEqual(len(retained), 6 + len(APPLICATIONS))
        self.assertEqual(selections[:2], [['atrinik-diagnostics']] * 2)
        self.assertNotIn('atrinik-diagnostics', selections[2])
        self.assertEqual(selections[2], selections[3])
        self.assertEqual(set(selections[2]), {row['name'] for row in R.policy()['crates']} - {'atrinik-diagnostics'} | set(APPLICATIONS))

    def test_maintenance_verifies_all_packages_then_retains_only_source_packages(self):
        manifest, retained, selections = self.prepare([row['name'] for row in R.policy()['crates']], True)
        self.assertEqual(manifest['crates'], [])
        self.assertEqual(len(retained), len(APPLICATIONS))
        self.assertEqual(len(selections), 2)
        self.assertEqual(set(selections[0]), {row['name'] for row in R.policy()['crates']} | set(APPLICATIONS))


if __name__ == '__main__':
    unittest.main()
