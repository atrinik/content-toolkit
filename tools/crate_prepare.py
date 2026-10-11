#!/usr/bin/env python3
"""Credential-free candidate packaging. Never requests OIDC or uploads."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib

if __package__:
    from . import crate_release as R
else:
    import crate_release as R

ROOT = R.ROOT


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def clean_environment():
    environment = os.environ.copy()
    for name in tuple(environment):
        if name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_READ_TOKEN', 'GH_INTENT_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_URL',
                    'ACTIONS_ID_TOKEN_REQUEST_TOKEN') or name.startswith('CARGO_REGISTR'):
            environment.pop(name)
    return environment


def preparation_boundary():
    R.require(not any(os.environ.get(key) for key in ('CARGO_REGISTRY_TOKEN',
        'CARGO_REGISTRIES_CRATES_IO_TOKEN', 'CARGO_REGISTRY_BOOTSTRAP_TOKEN',
        'ACTIONS_ID_TOKEN_REQUEST_URL', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN', 'GH_INTENT_TOKEN')),
        'preparation requires a credential-free job')


def versions(config, root=ROOT):
    workspace = tomllib.loads((root / 'Cargo.toml').read_text()).get('workspace', {}).get('package', {})
    result = {}
    for crate in config['crates']:
        package = tomllib.loads((root / crate['path'] / 'Cargo.toml').read_text())['package']
        R.require(package['name'] == crate['name'] and package.get('publish') == ['crates-io'], 'publishable crate policy')
        version = package['version']
        if isinstance(version, dict):
            R.require(version == {'workspace': True}, 'unsupported version inheritance')
            version = workspace['version']
        R.require(isinstance(version, str) and R.VERSION.fullmatch(version), 'crate version')
        result[crate['name']] = version
    return result


def known(config):
    entries = R.catalog(config)
    for old in config['historical']:
        key = (old['name'], old['version'])
        R.require(key not in entries, 'historical version has duplicate automatic lineage')
        entries[key] = old
    return entries


def pending(config):
    if not R.registry_source_branch():
        return False
    existing = known(config)
    return any((name, version) not in existing for name, version in versions(config).items())


def check_version(config, base):
    R.require(R.SHA.fullmatch(base), 'base must be immutable revision')
    git('cat-file', '-e', base + '^{commit}')
    # First onboarding has no previous public-package policy. Real preparation
    # still compares every coordinate against public release/registry history.
    if subprocess.run(['git', 'cat-file', '-e', base + ':policy/crate-release.json'],
                      cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        return
    previous = json.loads(git('show', base + ':policy/crate-release.json'))
    previous_names = {row['name']: row for row in previous['crates']}
    current_versions = versions(config)
    changed = set(git('diff', '--name-only', base, '--').splitlines())
    old_workspace = tomllib.loads(git('show', base + ':Cargo.toml')).get('workspace', {}).get('package', {})
    for row in config['crates']:
        if row['name'] not in previous_names:
            continue
        prior = previous_names[row['name']]
        old_package = tomllib.loads(git('show', base + ':' + prior['path'] + '/Cargo.toml'))['package']
        old_version = old_package['version']
        if isinstance(old_version, dict):
            old_version = old_workspace['version']
        version = current_versions[row['name']]
        R.require(tuple(map(int, version.split('.'))) >= tuple(map(int, old_version.split('.'))), 'crate version moved backward')
        package_changed = (row['path'] != prior['path'] or config['toolchain'] != previous['toolchain']
            or any(path.startswith((row['path'] + '/', prior['path'] + '/', '.cargo/')) for path in changed)
            or bool(changed & {'Cargo.toml', 'Cargo.lock', 'rust-toolchain', 'rust-toolchain.toml'}))
        R.require(not package_changed or version != old_version, 'package content changed without reviewed version bump')


def write_exact(path, data):
    if path.exists():
        R.require(path.is_file() and not path.is_symlink() and path.read_bytes() == data, 'prepared output collision')
    else:
        path.write_bytes(data)


def old_content(config, entry):
    if isinstance(entry, dict):
        release = R.api(config, 'releases/tags/' + entry['source_tag'])
        data = R.asset_bytes(config, release, entry['asset'])
        R.require(R.digest(data) == entry['sha256'], 'historical artifact changed')
        return R.content_digest(R.archive(data, entry['name'], entry['version']))
    release, manifest, row, _ = entry
    data = R.asset_bytes(config, release, row['asset'])
    R.verify_archive(row, data, manifest['revision'])
    return row['content_sha256']


def prepare(config, tag, output, archives=None, dry_run=False):
    preparation_boundary()
    R.require(dry_run or not git('status', '--porcelain'), 'source packaging requires clean revision')
    revision = git('rev-parse', 'HEAD')
    if dry_run:
        tag = 'v0.0.0'
    R.require(__import__('re').fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag), 'source version')
    output.mkdir(parents=True, exist_ok=True)
    current = versions(config)
    eligible = dry_run or R.registry_source_branch()
    previous = {} if dry_run or not eligible else known(config)
    manifest = {'schema_version': 1, 'repository': config['repository'],
                'repository_id': config['repository_id'], 'revision': revision, 'source_tag': tag, 'crates': []}
    # Cargo packages the complete public graph together, resolving internal
    # dependencies through its packaging overlay for the production registry.
    with tempfile.TemporaryDirectory(prefix='atrinik-crate-prepare-') as temporary:
        package_sets = []
        for number in (1, 2):
            target = Path(temporary) / str(number)
            command = ['cargo', '+' + config['toolchain'], 'package', '--locked', '--no-verify', '--registry', 'crates-io',
                       '--target-dir', str(target)]
            for crate in config['crates']:
                command += ['--package', crate['name']]
            if dry_run:
                command += ['--allow-dirty', '--offline']
            subprocess.run(command, cwd=ROOT, env=clean_environment(), check=True, timeout=1200)
            package_sets.append(target / 'package')
        # Byte reproduction precedes one independent build of the exact staged graph.
        for crate in config['crates']:
            asset = f"{crate['name']}-{current[crate['name']]}.crate"
            R.require((package_sets[0] / asset).read_bytes() == (package_sets[1] / asset).read_bytes(), 'package reproduction mismatch')
        if archives is not None:
            for extra in archives.glob('*.crate'):
                if not (package_sets[0] / extra.name).exists():
                    shutil.copyfile(extra, package_sets[0] / extra.name)
        verifier = ROOT / 'tools/verify-packages.sh'
        R.require(verifier.is_file(), 'independent packaged graph verifier required')
        subprocess.run(['bash', str(verifier), str(package_sets[0])], cwd=ROOT,
                       env=clean_environment(), check=True, timeout=1800)
        for crate in config['crates']:
            name, version = crate['name'], current[crate['name']]
            asset = f'{name}-{version}.crate'
            first = (package_sets[0] / asset).read_bytes()
            second = (package_sets[1] / asset).read_bytes()
            R.require(first == second, 'package reproduction mismatch')
            files = R.archive(first, name, version)
            metadata = R.metadata(files, name, version)
            row = {'name': name, 'version': version, 'path': crate['path'], 'asset': asset,
                   'sha256': R.digest(first), 'content_sha256': R.content_digest(files),
                   'inventory': sorted(files), 'metadata': metadata}
            if not dry_run:
                R.verify_archive(row, first, revision)
            if archives is not None:
                write_exact(archives / asset, first)
            if not eligible:
                # Maintenance still verifies its complete source package graph,
                # but cannot reserve names/versions for the main-only publisher.
                if archives is not None:
                    (archives / asset).unlink()
                continue
            key = (name, version)
            if key in previous:
                R.require(old_content(config, previous[key]) == row['content_sha256'], 'owned crate version has changed package contents')
                # Verification used the complete staged graph. A later source
                # release must not attach this version with new VCS metadata.
                if archives is not None:
                    (archives / asset).unlink()
                continue
            if not dry_run:
                state = R.registry_state(row)
                R.require(state == 'absent', 'new crate coordinate already exists or is unresolved')
            write_exact(output / asset, first)
            manifest['crates'].append(row)
    R.validate_manifest(config, manifest)
    manifest['crates'] = R.ordered(manifest['crates'])
    write_exact(output / R.MANIFEST, R.canonical(manifest) + b'\n')
    R.require(dry_run or not git('status', '--porcelain'), 'preparation changed source')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['check-version', 'prepare', 'pending'])
    parser.add_argument('--base'); parser.add_argument('--source-tag')
    parser.add_argument('--output', type=Path); parser.add_argument('--archives', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    config = R.policy()
    if args.operation == 'check-version':
        check_version(config, args.base)
    elif args.operation == 'pending':
        print(json.dumps({'pending': pending(config)}))
    else:
        R.require(args.output is not None, 'output required')
        prepare(config, args.source_tag, args.output, args.archives, args.dry_run)


if __name__ == '__main__':
    main()
