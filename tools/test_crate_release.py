#!/usr/bin/env python3
"""Negative tests for the portable exact-archive publication boundary."""
from __future__ import annotations

import copy
from contextlib import ExitStack
import gzip
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools import crate_release as R
from tools import crate_prepare as P

REVISION = 'a' * 40
CONFIG = {'schema_version': 1, 'repository': 'atrinik/example', 'repository_id': '123',
          'workflow': 'publish-crate.yml', 'environment': 'crates-io-release', 'toolchain': '1.97.1',
          'required_checks': ['Example validation'], 'crates': [{'name': 'example', 'path': 'crates/example'}], 'historical': []}


def files():
    return {'Cargo.toml': b'''[package]
name="example"
version="0.1.0"
publish=["crates-io"]
license="MIT"
readme="README.md"
[lib]
path="src/lib.rs"
[dependencies.alias]
package="actual"
version="^1.2"
optional=true
features=["feature"]
default-features=false
[target.'cfg(windows)'.build-dependencies]
cc="1"
[dev-dependencies]
testing="2"
[features]
default=["dep:alias"]
''', 'Cargo.toml.orig': b'[package]\n', 'Cargo.lock': b'''version = 4
[[package]]
name = "example"
version = "0.1.0"
[[package]]
name = "actual"
version = "1.2.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
''', '.cargo_vcs_info.json': R.canonical({'git': {'sha1': REVISION}, 'path_in_vcs': 'crates/example'}),
        'README.md': b'Example.', 'src/lib.rs': b'pub fn example() {}\n'}


def make_archive(members=None, special=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as stream:
        for name, data in (files() if members is None else members).items():
            member = tarfile.TarInfo('example-0.1.0/' + name)
            member.size = len(data)
            stream.addfile(member, io.BytesIO(data))
        if special:
            stream.addfile(special)
    return output.getvalue()


def row():
    data = make_archive()
    contents = R.archive(data, 'example', '0.1.0')
    return {'name': 'example', 'version': '0.1.0', 'path': 'crates/example',
            'asset': 'example-0.1.0.crate', 'sha256': R.digest(data),
            'content_sha256': R.content_digest(contents), 'inventory': sorted(contents),
            'metadata': R.metadata(contents, 'example', '0.1.0')}, data


class MetadataTests(unittest.TestCase):
    def test_full_dependency_mapping(self):
        value = R.metadata(files(), 'example', '0.1.0')
        alias, dev, build = value['deps']
        self.assertEqual((alias['name'], alias['explicit_name_in_toml']), ('actual', 'alias'))
        self.assertTrue(alias['optional']); self.assertFalse(alias['default_features'])
        self.assertEqual(alias['features'], ['feature'])
        self.assertEqual((dev['kind'], build['kind'], build['target']), ('dev', 'build', 'cfg(windows)'))
        self.assertEqual(value['features'], {'default': ['dep:alias']})

    def test_no_path_git_or_unknown_dependency_field(self):
        for addition in ('path="../sibling"', 'git="https://example.org/repo"', 'registry="other"', 'future=true'):
            with self.subTest(addition=addition):
                value = files()
                value['Cargo.toml'] = value['Cargo.toml'].replace(b'package="actual"', ('package="actual"\n' + addition).encode())
                with self.assertRaisesRegex(ValueError, 'dependency source'):
                    R.metadata(value, 'example', '0.1.0')

    def test_metadata_cannot_lie_about_archive(self):
        entry, data = row()
        entry['metadata']['deps'][0]['optional'] = False
        with self.assertRaisesRegex(ValueError, 'metadata mismatch'):
            R.verify_archive(entry, data, REVISION)


class ArchiveTests(unittest.TestCase):
    def test_valid_snapshot(self):
        entry, data = row()
        R.verify_archive(entry, data, REVISION)

    def test_revision_inventory_and_hash_must_match(self):
        entry, data = row()
        for key, value in [('sha256', '0' * 64), ('inventory', ['Cargo.toml']), ('content_sha256', '0' * 64)]:
            changed = dict(entry, **{key: value})
            with self.assertRaises(ValueError):
                R.verify_archive(changed, data, REVISION)
        with self.assertRaisesRegex(ValueError, 'source provenance'):
            R.verify_archive(entry, data, 'b' * 40)

    def test_unsafe_links_duplicates_and_paths_rejected(self):
        for name, kind in [('example-0.1.0/../outside', tarfile.REGTYPE),
                           ('example-0.1.0/src/lib.rs', tarfile.REGTYPE),
                           ('example-0.1.0/link', tarfile.SYMTYPE)]:
            with self.subTest(name=name):
                member = tarfile.TarInfo(name); member.type = kind
                with self.assertRaises(ValueError):
                    R.archive(make_archive(special=member), 'example', '0.1.0')

    def test_pax_expansion_is_bounded_before_parser(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT,
                          pax_headers={'comment': 'a' * 100000}) as stream:
            member = tarfile.TarInfo('example-0.1.0/Cargo.toml')
            stream.addfile(member)
        compressed = gzip.compress(output.getvalue())
        with patch.object(R, 'MAX_BYTES', 64000), self.assertRaisesRegex(ValueError, 'tar stream bound'):
            R.archive(compressed, 'example', '0.1.0')

    def test_content_hash_ignores_only_revision_metadata(self):
        value = files(); original = R.content_digest(value)
        value['.cargo_vcs_info.json'] = b'different revision'
        self.assertEqual(R.content_digest(value), original)
        value['Cargo.lock'] += b'\n# dependency graph changed\n'
        self.assertNotEqual(R.content_digest(value), original)

    def test_locked_git_dependency_rejected(self):
        entry, _ = row(); contents = files()
        contents['Cargo.lock'] = contents['Cargo.lock'].replace(b'registry+https://github.com/rust-lang/crates.io-index', b'git+https://example.com')
        data = make_archive(contents)
        entry.update(sha256=R.digest(data), content_sha256=R.content_digest(contents))
        with self.assertRaisesRegex(ValueError, 'nonregistry'):
            R.verify_archive(entry, data, REVISION)


class GraphTests(unittest.TestCase):
    def test_leaves_before_optional_target_build_dependency(self):
        leaf = {'name': 'leaf', 'metadata': {'deps': []}}
        top = {'name': 'top', 'metadata': {'deps': [{'name': 'leaf', 'kind': 'build', 'optional': True}]}}
        self.assertEqual([item['name'] for item in R.ordered([top, leaf])], ['leaf', 'top'])

    def test_cycles_fail_before_upload(self):
        values = [{'name': a, 'metadata': {'deps': [{'name': b, 'kind': 'normal'}]}}
                  for a, b in [('a', 'b'), ('b', 'a')]]
        with self.assertRaisesRegex(ValueError, 'cyclic'):
            R.ordered(values)


class RegistryTests(unittest.TestCase):
    def observations(self, entry, data, api=True, index=True, checksum=None, yanked=False):
        checksum = checksum or entry['sha256']
        return [R.canonical({'version': {'crate': entry['name'], 'num': entry['version'], 'checksum': checksum, 'yanked': yanked}}) if api else None,
                R.canonical({'name': entry['name'], 'vers': entry['version'], 'cksum': checksum, 'yanked': yanked}) if index else None,
                data]

    def test_registry_requires_both_views_and_download(self):
        entry, data = row()
        for api, index, state in [(False, False, 'absent'), (True, False, 'indeterminate'),
                                  (False, True, 'indeterminate'), (True, True, 'present')]:
            with self.subTest(api=api, index=index), patch.object(R, 'get', side_effect=self.observations(entry, data, api, index)):
                self.assertEqual(R.registry_state(entry), state)

    def test_conflicting_checksum_yanked_or_download_mismatch_fail(self):
        entry, data = row()
        for observations in [self.observations(entry, data, checksum='0' * 64),
                             self.observations(entry, data, yanked=True),
                             self.observations(entry, b'changed')]:
            with patch.object(R, 'get', side_effect=observations), self.assertRaises(ValueError):
                R.registry_state(entry)

    def test_uncertain_upload_never_puts_twice_or_advances(self):
        entry, data = row()
        manifest = {'crates': [entry]}
        release = {'id': 10}
        with patch.object(R, 'boundary'), patch.object(R, 'release_evidence', return_value=(release, manifest, {'example': data})), \
             patch.object(R, 'registry_state', return_value='absent'), patch.object(R, 'api', return_value=release), \
             patch.object(R, 'put_once', return_value=False) as put, \
             patch.object(R, 'confirm', side_effect=ValueError('unresolved')), \
             patch.dict(os.environ, {'CARGO_REGISTRY_TOKEN': 'test-token'}):
            with self.assertRaisesRegex(ValueError, 'unresolved'):
                R.upload(CONFIG, 10, REVISION, 'b' * 64)
            self.assertEqual(put.call_count, 1)

    def test_already_present_skips_upload(self):
        entry, data = row()
        with patch.object(R, 'boundary'), patch.object(R, 'release_evidence', return_value=({}, {'crates': [entry]}, {'example': data})), \
             patch.object(R, 'registry_state', return_value='present'), patch.object(R, 'put_once') as put, \
             patch.dict(os.environ, {'CARGO_REGISTRY_TOKEN': 'test-token'}):
            R.upload(CONFIG, 10, REVISION, 'b' * 64)
            put.assert_not_called()


class BoundaryTests(unittest.TestCase):
    def test_no_registry_or_oidc_credentials_during_preparation(self):
        for name in ['CARGO_REGISTRY_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN', 'GH_TOKEN']:
            with patch.dict(os.environ, {name: 'secret'}, clear=True), self.assertRaises(ValueError):
                P.preparation_boundary()

    def test_upload_disallows_local_pr_fork_and_other_ref(self):
        expected = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': CONFIG['repository'],
                    'GITHUB_REPOSITORY_ID': '123', 'GITHUB_REF': 'refs/heads/main',
                    'GITHUB_EVENT_NAME': 'workflow_dispatch',
                    'GITHUB_WORKFLOW_REF': 'atrinik/example/.github/workflows/publish-crate.yml@refs/heads/main',
                    'GITHUB_SHA': REVISION, 'GITHUB_WORKFLOW_SHA': REVISION}
        for change in [{}, {'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_EVENT_NAME': 'workflow_run'},
                       {'GITHUB_REPOSITORY_ID': '456'}, {'GITHUB_REF': 'refs/tags/v1.0.0'}]:
            environment = {} if not change else dict(expected, **change)
            with patch.dict(os.environ, environment, clear=True), self.assertRaises(ValueError):
                R.boundary(CONFIG, upload=True)

    def test_manifest_name_allowlist_and_source_identity(self):
        entry, _ = row()
        value = {'schema_version': 1, 'repository': CONFIG['repository'], 'repository_id': '123',
                 'revision': REVISION, 'source_tag': 'v1.0.0', 'crates': [entry]}
        R.validate_manifest(CONFIG, value)
        for key, other in [('repository_id', '456'), ('revision', 'main'), ('source_tag', 'latest')]:
            with self.assertRaises(ValueError):
                R.validate_manifest(CONFIG, dict(value, **{key: other}))
        changed = copy.deepcopy(value); changed['crates'][0]['name'] = 'not-allowed'
        with self.assertRaises(ValueError):
            R.validate_manifest(CONFIG, changed)

    def test_bootstrap_cannot_run_in_actions_or_noninteractive_context(self):
        for environment in ({'GITHUB_ACTIONS': 'true'}, {'CARGO_REGISTRY_TOKEN': 'secret'}, {}):
            with patch.dict(os.environ, environment, clear=True), patch.object(R.sys.stdin, 'isatty', return_value=False):
                with self.assertRaises(ValueError):
                    R.bootstrap(CONFIG, 10, REVISION, 'b' * 64, 'owner', '123')

    def test_bootstrap_existing_name_and_uncertain_upload_fail_closed(self):
        entry, data = row()
        release = {'id': 10}
        for existing, uncertain in [(True, False), (False, True)]:
            with self.subTest(existing=existing), ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {}, clear=True))
                stack.enter_context(patch.object(R.sys.stdin, 'isatty', return_value=True))
                stack.enter_context(patch.object(R.sys.stderr, 'isatty', return_value=True))
                stack.enter_context(patch.object(R.subprocess, 'check_output', side_effect=[b'', REVISION]))
                stack.enter_context(patch.object(R, 'api', side_effect=[{'status': 'identical'}, release]))
                stack.enter_context(patch.object(R, 'release_evidence', return_value=(release, {'crates': [entry]}, {'example': data})))
                stack.enter_context(patch.object(R, 'registry_state', return_value='absent'))
                stack.enter_context(patch.object(R, 'get', side_effect=[R.canonical({'user': {'id': 123, 'login': 'owner'}}),
                    b'existing' if existing else None, None]))
                stack.enter_context(patch('builtins.input', return_value='PUBLISH AS owner'))
                stack.enter_context(patch.object(R.getpass, 'getpass', return_value='test-only-token'))
                stack.enter_context(patch.object(R, 'confirm', side_effect=ValueError('unresolved')))
                put = stack.enter_context(patch.object(R, 'put_once', return_value=False))
                with self.assertRaisesRegex(ValueError, 'name already exists' if existing else 'unresolved'):
                    R.bootstrap(CONFIG, 10, REVISION, 'b' * 64, 'owner', '123')
                self.assertEqual(put.call_count, 0 if existing else 1)

    def test_required_checks_exact_revision_name_app_and_latest_success(self):
        check = {'name': 'Example validation', 'head_sha': REVISION, 'app': {'slug': 'github-actions'},
                 'id': 1, 'status': 'completed', 'conclusion': 'success'}
        with patch.object(R, 'api', return_value={'total_count': 1, 'check_runs': [check]}):
            R.verify_checks(CONFIG, REVISION)
        for altered in [dict(check, head_sha='b' * 40), dict(check, name='Other validation'),
                        dict(check, app={'slug': 'other-app'}), dict(check, status='in_progress'),
                        dict(check, conclusion='failure')]:
            with patch.object(R, 'api', return_value={'total_count': 1, 'check_runs': [altered]}), self.assertRaises(ValueError):
                R.verify_checks(CONFIG, REVISION)
        newer_failure = dict(check, id=2, conclusion='failure')
        with patch.object(R, 'api', return_value={'total_count': 2, 'check_runs': [check, newer_failure]}), self.assertRaises(ValueError):
            R.verify_checks(CONFIG, REVISION)

    def test_inert_finalizer_requires_complete_prepared_inventory(self):
        entry, data = row()
        value = {'schema_version': 1, 'repository': CONFIG['repository'], 'repository_id': '123',
                 'revision': REVISION, 'source_tag': 'v1.0.0', 'crates': [entry]}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / R.MANIFEST).write_bytes(R.canonical(value))
            (directory / 'provenance.json').write_bytes(R.canonical({'revision': REVISION, 'version': '1.0.0'}))
            (directory / entry['asset']).write_bytes(data)
            sums = ''.join(f'{R.digest(path.read_bytes())}  {path.name}\n' for path in sorted(directory.iterdir()))
            (directory / 'SHA256SUMS').write_text(sums)
            with patch.object(R.subprocess, 'check_output', return_value=REVISION), \
                 patch.object(R, 'api', return_value={'enabled': True, 'enforced_by_owner': True}):
                R.finalize(CONFIG, 'v1.0.0', directory)
                for settings in ({'enabled': False, 'enforced_by_owner': True},
                                 {'enabled': True, 'enforced_by_owner': False}):
                    with patch.object(R, 'api', return_value=settings), self.assertRaisesRegex(ValueError, 'before tagging'):
                        R.finalize(CONFIG, 'v1.0.0', directory)
                (directory / 'unchecked').write_text('extra')
                with self.assertRaisesRegex(ValueError, 'inventory coverage'):
                    R.finalize(CONFIG, 'v1.0.0', directory)


if __name__ == '__main__':
    unittest.main()
