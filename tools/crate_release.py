#!/usr/bin/env python3
"""Bounded, standard-library-only release evidence and exact-byte publication.

No Cargo, package code, generated executables, or artifact-supplied helpers run
here. The same schema is copied into each physical producer repository.
"""
from __future__ import annotations

import argparse
import hashlib
import gzip
import getpass
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 32 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
MAX_FILES = 4096
MANIFEST = 'crate-release.json'
REGISTRY = 'https://crates.io'
INDEX = 'https://index.crates.io'
NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}')
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
SHA = re.compile(r'[0-9a-f]{40}')
DIGEST = re.compile(r'[0-9a-f]{64}')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(value):
    require(isinstance(value, str) and 0 < len(value) < 512 and '\\' not in value
            and '\x00' not in value and '\n' not in value and '\r' not in value,
            'invalid relative path')
    path = PurePosixPath(value)
    require(not path.is_absolute() and '..' not in path.parts and str(path) == value
            and value != '.', 'unsafe relative path')
    return value


def policy(root=ROOT):
    value = json.loads((root / 'policy/crate-release.json').read_text())
    require(set(value) == {'schema_version', 'repository', 'repository_id', 'workflow',
                           'environment', 'toolchain', 'crates', 'historical', 'required_checks'}, 'policy fields')
    require(value['schema_version'] == 1 and re.fullmatch(r'atrinik/[a-z0-9-]+', value['repository'])
            and re.fullmatch(r'[1-9][0-9]*', value['repository_id'])
            and value['workflow'] == 'publish-crate.yml' and value['environment'] == 'crates-io-release'
            and VERSION.fullmatch(value['toolchain']), 'policy identity')
    require(isinstance(value['required_checks'], list) and 0 < len(value['required_checks']) <= 16
            and len(set(value['required_checks'])) == len(value['required_checks'])
            and all(isinstance(name, str) and 0 < len(name) <= 128 for name in value['required_checks']), 'required check policy')
    require(isinstance(value['crates'], list) and 0 < len(value['crates']) <= 32, 'crate inventory bound')
    names, paths = set(), set()
    for crate in value['crates']:
        require(set(crate) == {'name', 'path'} and NAME.fullmatch(crate['name']), 'crate policy fields')
        safe_path(crate['path'])
        require(crate['name'] not in names and crate['path'] not in paths, 'duplicate crate policy')
        names.add(crate['name']); paths.add(crate['path'])
    require(isinstance(value['historical'], list) and len(value['historical']) <= 128, 'history bound')
    seen = set()
    for row in value['historical']:
        require(set(row) == {'name', 'version', 'source_tag', 'revision', 'asset', 'sha256'}, 'history fields')
        validate_coordinate(row)
        require(row['name'] in names and SHA.fullmatch(row['revision']) and
                re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', row['source_tag']), 'history identity')
        coordinate = (row['name'], row['version'])
        require(coordinate not in seen, 'duplicate history')
        seen.add(coordinate)
    return value


def validate_coordinate(row):
    require(isinstance(row.get('name'), str) and NAME.fullmatch(row['name'])
            and isinstance(row.get('version'), str) and VERSION.fullmatch(row['version'])
            and row.get('asset') == f"{row['name']}-{row['version']}.crate"
            and isinstance(row.get('sha256'), str) and DIGEST.fullmatch(row['sha256']), 'crate coordinate')


def boundary(config, upload=False):
    require(not os.environ.get('CARGO_REGISTRIES_CRATES_IO_TOKEN') and
            not os.environ.get('CARGO_REGISTRY_BOOTSTRAP_TOKEN'), 'unexpected registry credential')
    if not upload:
        require(not os.environ.get('CARGO_REGISTRY_TOKEN'), 'registry credential outside upload')
    if upload or os.environ.get('GITHUB_ACTIONS') == 'true':
        expected = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': config['repository'],
                    'GITHUB_REPOSITORY_ID': config['repository_id'], 'GITHUB_REF': 'refs/heads/main',
                    'GITHUB_EVENT_NAME': 'workflow_dispatch',
                    'GITHUB_WORKFLOW_REF': f"{config['repository']}/.github/workflows/{config['workflow']}@refs/heads/main"}
        require(all(os.environ.get(key) == value for key, value in expected.items()), 'publication workflow boundary')
        require(SHA.fullmatch(os.environ.get('GITHUB_SHA', '')) and
                os.environ.get('GITHUB_WORKFLOW_SHA') == os.environ['GITHUB_SHA'], 'workflow revision boundary')
        require(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
                == os.environ['GITHUB_SHA'], 'checkout revision boundary')
        require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT), 'dirty publication checkout')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('redirect refused')


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        require(parsed.scheme == 'https' and parsed.hostname in {
            'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com',
            'crates.io', 'static.crates.io', 'index.crates.io'}, 'download redirect refused')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def get(url, absent=False, maximum=MAX_JSON):
    headers = {'User-Agent': 'atrinik-crate-release/1 (https://github.com/atrinik)', 'Accept': 'application/json'}
    authenticated = url.startswith('https://api.github.com/') and bool(os.environ.get('GH_READ_TOKEN'))
    if authenticated:
        headers['Authorization'] = 'Bearer ' + os.environ['GH_READ_TOKEN']
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect() if authenticated else PublicRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            data = response.read(maximum + 1)
            require(len(data) <= maximum, 'response bound exceeded')
            return data
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if absent and code == 404:
            return None
        raise ValueError('public request failed') from None


def api(config, suffix):
    return json.loads(get(f"https://api.github.com/repos/{config['repository']}/{suffix}"))


def asset_bytes(config, release, name, maximum=MAX_BYTES):
    rows = [asset for asset in release['assets'] if asset['name'] == name]
    require(len(rows) == 1, 'missing or duplicate release asset')
    row = rows[0]
    expected = f"https://github.com/{config['repository']}/releases/download/{release['tag_name']}/{name}"
    require(row['state'] == 'uploaded' and row['browser_download_url'] == expected and
            isinstance(row['size'], int) and 0 < row['size'] <= maximum, 'release asset identity')
    data = get(expected, maximum=maximum)
    require(len(data) == row['size'] and row.get('digest') == 'sha256:' + digest(data), 'release asset checksum')
    return data


def archive(data, name, version):
    require(len(data) <= MAX_BYTES, 'archive bound')
    files = {}
    prefix = f'{name}-{version}/'
    total = 0
    # Bound decompression BEFORE tarfile consumes PAX/GNU extension headers.
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
        expanded = compressed.read(MAX_BYTES + 1)
    require(len(expanded) <= MAX_BYTES, 'expanded tar stream bound')
    with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as stream:
        for member in stream:
            require(len(files) < MAX_FILES and member.isfile() and member.name.startswith(prefix), 'archive member type')
            path = safe_path(member.name[len(prefix):])
            require(path not in files, 'duplicate archive member')
            total += member.size
            require(0 <= member.size <= MAX_BYTES and total <= MAX_BYTES, 'expanded archive bound')
            files[path] = stream.extractfile(member).read()
    require({'Cargo.toml', 'Cargo.toml.orig', 'Cargo.lock', '.cargo_vcs_info.json'} <= files.keys(), 'incomplete package')
    return files


def content_digest(files):
    return digest(canonical([[name, len(data), digest(data)] for name, data in sorted(files.items())
                             if name != '.cargo_vcs_info.json']))


def string(value, nullable=False):
    require((nullable and value is None) or (isinstance(value, str) and len(value) <= MAX_JSON), 'metadata string')
    return value


def metadata(files, name, version):
    """Cargo Web API metadata from a normalized manifest, including target deps."""
    manifest = tomllib.loads(files['Cargo.toml'].decode())
    require(set(manifest) <= {'package', 'lib', 'bin', 'example', 'test', 'bench', 'dependencies',
            'dev-dependencies', 'build-dependencies', 'features', 'target', 'badges', 'lints', 'profile'},
            'unsupported normalized manifest section')
    package = manifest['package']
    require(package['name'] == name and package['version'] == version and package.get('publish') == ['crates-io'],
            'normalized package identity')
    allowed = {'name', 'version', 'edition', 'rust-version', 'build', 'publish', 'autolib', 'autobins',
               'autoexamples', 'autotests', 'autobenches', 'description', 'homepage', 'documentation',
               'readme', 'keywords', 'license', 'license-file', 'repository', 'authors', 'categories',
               'links', 'include', 'exclude', 'metadata', 'default-run', 'resolver'}
    require(set(package) <= allowed, 'unsupported package field')
    dependencies = []
    def add(table, target=None):
        for section, kind in [('dependencies', 'normal'), ('dev-dependencies', 'dev'), ('build-dependencies', 'build')]:
            for alias, dependency in sorted(table.get(section, {}).items()):
                if isinstance(dependency, str):
                    dependency = {'version': dependency}
                require(isinstance(dependency, dict) and set(dependency) <= {
                    'version', 'features', 'optional', 'default-features', 'package'}, 'unsupported dependency source/field')
                requirement = string(dependency.get('version'))
                require(requirement and NAME.fullmatch(alias), 'dependency version/name')
                features = dependency.get('features', [])
                require(isinstance(features, list) and all(isinstance(f, str) for f in features), 'dependency features')
                require(isinstance(dependency.get('optional', False), bool) and
                        isinstance(dependency.get('default-features', True), bool), 'dependency flags')
                dependencies.append({'name': dependency.get('package', alias), 'version_req': requirement,
                    'features': features, 'optional': dependency.get('optional', False),
                    'default_features': dependency.get('default-features', True), 'target': target,
                    'kind': kind, 'registry': None,
                    'explicit_name_in_toml': alias if 'package' in dependency else None})
    add(manifest)
    for target, table in sorted(manifest.get('target', {}).items()):
        require(set(table) <= {'dependencies', 'dev-dependencies', 'build-dependencies'}, 'unsupported target section')
        add(table, target)
    features = manifest.get('features', {})
    require(isinstance(features, dict) and all(isinstance(k, str) and isinstance(v, list)
            and all(isinstance(item, str) for item in v) for k, v in features.items()), 'package features')
    readme_path = package.get('readme')
    if readme_path is False or readme_path is None:
        readme_path = None
        readme = None
    else:
        safe_path(readme_path)
        require(readme_path in files, 'missing package readme')
        readme = files[readme_path].decode()
    license_file = package.get('license-file')
    if license_file:
        require(safe_path(license_file) in files, 'missing package license')
    result = {'name': name, 'vers': version, 'deps': dependencies, 'features': features,
              'readme': readme, 'readme_file': readme_path, 'license_file': license_file,
              'badges': manifest.get('badges', {}), 'rust_version': package.get('rust-version')}
    for key in ('description', 'documentation', 'homepage', 'license', 'repository', 'links'):
        result[key] = string(package.get(key), nullable=True)
    for key in ('authors', 'keywords', 'categories'):
        value = package.get(key, [])
        require(isinstance(value, list) and all(isinstance(item, str) for item in value), 'metadata list')
        result[key] = value
    require(len(canonical(result)) <= MAX_JSON, 'metadata bound')
    return result


def validate_manifest(config, value):
    require(set(value) == {'schema_version', 'repository', 'repository_id', 'revision', 'source_tag', 'crates'}
            and value['schema_version'] == 1 and value['repository'] == config['repository']
            and value['repository_id'] == config['repository_id'] and SHA.fullmatch(value['revision'])
            and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', value['source_tag']), 'manifest identity')
    require(isinstance(value['crates'], list) and len(value['crates']) <= len(config['crates']), 'manifest inventory bound')
    allowed = {row['name']: row['path'] for row in config['crates']}
    seen = set()
    for row in value['crates']:
        require(set(row) == {'name', 'version', 'path', 'asset', 'sha256', 'content_sha256', 'inventory', 'metadata'}, 'manifest crate fields')
        validate_coordinate(row)
        require(row['name'] in allowed and row['path'] == allowed[row['name']] and row['name'] not in seen, 'manifest allowlist')
        require(DIGEST.fullmatch(row['content_sha256']) and isinstance(row['inventory'], list)
                and 0 < len(row['inventory']) <= MAX_FILES, 'manifest content inventory')
        require(row['inventory'] == sorted(set(row['inventory'])), 'manifest inventory ordering')
        for name in row['inventory']:
            safe_path(name)
        require(row['metadata']['name'] == row['name'] and row['metadata']['vers'] == row['version'], 'metadata identity')
        seen.add(row['name'])
    ordered(value['crates'])
    return value


def verify_archive(row, data, revision):
    require(digest(data) == row['sha256'], 'archive digest mismatch')
    files = archive(data, row['name'], row['version'])
    require(sorted(files) == row['inventory'] and content_digest(files) == row['content_sha256'], 'archive inventory/content mismatch')
    require(json.loads(files['.cargo_vcs_info.json']) == {'git': {'sha1': revision}, 'path_in_vcs': row['path']}, 'archive source provenance')
    require(metadata(files, row['name'], row['version']) == row['metadata'], 'archive metadata mismatch')
    lock = tomllib.loads(files['Cargo.lock'].decode())
    for dependency in lock['package']:
        if (dependency['name'], dependency['version']) == (row['name'], row['version']):
            require('source' not in dependency, 'root lock source')
        else:
            require(dependency.get('source') == 'registry+https://github.com/rust-lang/crates.io-index', 'nonregistry locked dependency')


def ordered(rows):
    """Internal normal/build/optional/target dependencies precede their consumers."""
    by_name = {row['name']: row for row in rows}
    require(len(by_name) == len(rows), 'duplicate crate')
    edges = {name: {dep['name'] for dep in row['metadata']['deps']
                    if dep['kind'] != 'dev' and dep['name'] in by_name} for name, row in by_name.items()}
    output = []
    while edges:
        ready = sorted(name for name, dependencies in edges.items() if not dependencies)
        require(ready, 'cyclic publication dependencies')
        for name in ready:
            output.append(by_name[name]); del edges[name]
        for dependencies in edges.values():
            dependencies.difference_update(ready)
    return output


def index_path(name):
    return '1/' + name if len(name) == 1 else ('2/' + name if len(name) == 2 else
           ('3/' + name[0] + '/' + name if len(name) == 3 else name[:2] + '/' + name[2:4] + '/' + name))


def registry_state(row):
    raw_api = get(f"{REGISTRY}/api/v1/crates/{row['name']}/{row['version']}", absent=True)
    raw_index = get(f"{INDEX}/{index_path(row['name'])}", absent=True, maximum=MAX_BYTES)
    api_row = None if raw_api is None else json.loads(raw_api)['version']
    require(api_row is None or (api_row.get('crate') == row['name'] and api_row.get('num') == row['version']),
            'registry API coordinate mismatch')
    matches = [] if raw_index is None else [json.loads(line) for line in raw_index.splitlines()
                if line and json.loads(line)['vers'] == row['version']]
    require(len(matches) <= 1, 'duplicate index version')
    index_row = matches[0] if matches else None
    require(index_row is None or index_row.get('name') == row['name'], 'registry index coordinate mismatch')
    for value, key in ((api_row, 'checksum'), (index_row, 'cksum')):
        if value is not None:
            require(value[key] == row['sha256'] and not value.get('yanked', False), 'registry checksum/yank conflict')
    if api_row is None and index_row is None:
        return 'absent'
    if api_row is None or index_row is None:
        return 'indeterminate'
    data = get(f"{REGISTRY}/api/v1/crates/{row['name']}/{row['version']}/download", maximum=MAX_BYTES)
    require(digest(data) == row['sha256'], 'registry download mismatch')
    return 'present'


def confirm(rows, attempts=12):
    for attempt in range(attempts):
        if all(registry_state(row) == 'present' for row in rows):
            return
        if attempt + 1 < attempts:
            time.sleep(5)
    raise ValueError('registry readiness unresolved; no upload retry')


def release_identity(config, release):
    require(not release['draft'] and not release['prerelease'] and release.get('published_at')
            and release.get('immutable') is True, 'source release must be public and immutable')
    require(re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', release['tag_name']), 'source tag format')
    repo = api(config, '')
    require(str(repo['id']) == config['repository_id'] and repo['full_name'] == config['repository']
            and repo['default_branch'] == 'main', 'repository identity changed')
    branch = api(config, 'branches/main')
    require(branch['protected'] is True, 'main is not protected')


def verify_checks(config, revision):
    check_runs = api(config, f'commits/{revision}/check-runs?per_page=100&filter=latest')
    require(check_runs['total_count'] <= 100, 'source check inventory exceeds bound')
    for name in config['required_checks']:
        matching = [check for check in check_runs['check_runs'] if check['name'] == name
                    and check['head_sha'] == revision and check['app']['slug'] == 'github-actions']
        require(matching, 'required source check absent')
        latest = max(matching, key=lambda check: check['id'])
        require(latest['status'] == 'completed' and latest['conclusion'] == 'success', 'required source check not successful')


def release_evidence(config, release_id, revision, manifest_sha256):
    require(str(release_id).isdigit() and SHA.fullmatch(revision) and DIGEST.fullmatch(manifest_sha256), 'release selectors')
    release = api(config, f'releases/{release_id}')
    require(str(release['id']) == str(release_id), 'release ID mismatch')
    release_identity(config, release)
    raw = asset_bytes(config, release, MANIFEST, MAX_JSON)
    require(digest(raw) == manifest_sha256, 'manifest digest mismatch')
    manifest = validate_manifest(config, json.loads(raw))
    require(manifest['revision'] == revision and manifest['source_tag'] == release['tag_name'], 'manifest source mismatch')
    ref = api(config, 'git/ref/tags/' + release['tag_name'])['object']
    for _ in range(4):
        if ref['type'] != 'tag':
            break
        ref = api(config, 'git/tags/' + ref['sha'])['object']
    require(ref['type'] == 'commit' and ref['sha'] == revision, 'tag revision mismatch')
    comparison = api(config, f'compare/{revision}...main')
    require(comparison['status'] in ('identical', 'ahead'), 'source revision is not on main')
    # Source release only exists after the reviewed workflow's credential-free
    # aggregate succeeds; verify its exact push/manual run and source revision.
    runs = api(config, f'actions/workflows/release.yml/runs?head_sha={revision}&per_page=100')['workflow_runs']
    for attempt in range(24):
        if any(run['head_sha'] == revision and run['head_branch'] == 'main'
               and run['event'] in ('push', 'workflow_dispatch') and run['conclusion'] == 'success' for run in runs):
            break
        require(any(run['head_sha'] == revision and run['status'] != 'completed' for run in runs),
                'successful source release workflow absent')
        require(attempt < 23, 'source workflow completion timed out')
        time.sleep(5)
        runs = api(config, f'actions/workflows/release.yml/runs?head_sha={revision}&per_page=100')['workflow_runs']
    verify_checks(config, revision)
    sums = asset_bytes(config, release, 'SHA256SUMS', MAX_JSON).decode()
    checksums = {}
    for line in sums.splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.+-]+)', line)
        require(match is not None and match[2] not in checksums, 'release checksum inventory invalid')
        checksums[match[2]] = match[1]
    assets = release['assets']
    require(len({row['name'] for row in assets}) == len(assets) and
            set(checksums) == {row['name'] for row in assets if row['name'] != 'SHA256SUMS'}, 'incomplete source checksum inventory')
    for asset in assets:
        if asset['name'] != 'SHA256SUMS':
            require(asset.get('state') == 'uploaded' and asset.get('digest') == 'sha256:' + checksums[asset['name']],
                    'source asset digest disagrees with checksum inventory')
    require(checksums[MANIFEST] == manifest_sha256, 'manifest absent from source checksums')
    snapshots = {}
    for row in manifest['crates']:
        require(checksums.get(row['asset']) == row['sha256'], 'crate absent from source checksums')
        data = asset_bytes(config, release, row['asset'])
        verify_archive(row, data, revision)
        snapshots[row['name']] = data
    return release, manifest, snapshots


def put_once(data, metadata_value, token):
    encoded = canonical(metadata_value)
    payload = struct.pack('<I', len(encoded)) + encoded + struct.pack('<I', len(data)) + data
    endpoint = REGISTRY + '/api/v1/crates/new'
    request = urllib.request.Request(endpoint, data=payload, method='PUT', headers={
        'Authorization': token, 'Content-Type': 'application/octet-stream', 'Accept': 'application/json',
        'User-Agent': 'atrinik-crate-release/1 (https://github.com/atrinik)'})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(65537)
            return response.geturl() == endpoint and 200 <= response.status < 300 and len(raw) <= 65536 and 'errors' not in json.loads(raw)
    except urllib.error.HTTPError as error:
        error.close()
    except Exception:
        pass
    return False


def upload(config, release_id, revision, manifest_sha256):
    boundary(config, upload=True)
    token = os.environ.pop('CARGO_REGISTRY_TOKEN', None)
    require(isinstance(token, str) and 0 < len(token) <= 8192 and all(33 <= ord(c) <= 126 for c in token), 'missing upload credential')
    release, manifest, snapshots = release_evidence(config, release_id, revision, manifest_sha256)
    for row in ordered(manifest['crates']):
        state = registry_state(row)
        if state == 'present':
            continue
        require(state == 'absent', 'registry state indeterminate; no write')
        # No source execution, registry credential subprocess or repackaging.
        require(api(config, f'releases/{release_id}') == release, 'release changed during upload preflight')
        put_once(snapshots[row['name']], row['metadata'], token)
        # Both acknowledged and uncertain PUTs are reconciled by reads only.
        # A timeout aborts this graph; never perform a second PUT in this run.
        confirm([row])
    return manifest


def catalog(config):
    found = {}
    for page in range(1, 11):
        releases = api(config, f'releases?per_page=100&page={page}')
        require(isinstance(releases, list), 'release inventory response')
        for release in releases:
            if release['draft'] or release['prerelease']:
                continue
            if not any(row['name'] == MANIFEST for row in release['assets']):
                continue
            raw = asset_bytes(config, release, MANIFEST, MAX_JSON)
            manifest = validate_manifest(config, json.loads(raw))
            require(manifest['source_tag'] == release['tag_name'], 'catalog source tag')
            if manifest['crates']:
                require(release.get('immutable') is True, 'mutable automatic source release')
            for row in manifest['crates']:
                key = (row['name'], row['version'])
                require(key not in found, 'duplicate crate source lineage')
                found[key] = (release, manifest, row, digest(raw))
        if len(releases) < 100:
            return found
    raise ValueError('release inventory exceeds pagination bound')


def dispatch(config, release, manifest, manifest_sha256):
    token = os.environ.pop('GH_TOKEN', None)
    require(token and os.environ.get('GITHUB_REPOSITORY') == config['repository']
            and os.environ.get('GITHUB_REPOSITORY_ID') == config['repository_id']
            and os.environ.get('GITHUB_REF') == 'refs/heads/main', 'dispatch identity')
    body = canonical({'ref': 'main', 'inputs': {'operation': 'automatic', 'source_release_id': str(release['id']),
                      'source_revision': manifest['revision'], 'manifest_sha256': manifest_sha256}})
    request = urllib.request.Request(f"https://api.github.com/repos/{config['repository']}/actions/workflows/{config['workflow']}/dispatches",
        data=body, method='POST', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                                        'Content-Type': 'application/json', 'User-Agent': 'atrinik-crate-release/1'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=30) as response:
        require(response.status == 204, 'dispatch response unresolved')


def finalize(config, source_tag, directory):
    """Inert pre-tag check of same-run artifacts, before semantic-release writes."""
    require(re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', source_tag or ''), 'final source tag')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(os.environ.get('EXPECTED_SOURCE_REVISION', revision) == revision and
            os.environ.get('EXPECTED_SOURCE_VERSION', source_tag[1:]) == source_tag[1:], 'source plan drift')
    manifest = validate_manifest(config, json.loads((directory / MANIFEST).read_text()))
    require(manifest['revision'] == revision and manifest['source_tag'] == source_tag, 'prepared source identity')
    provenance = json.loads((directory / 'provenance.json').read_text())
    require(provenance['revision'] == revision and provenance['version'] == source_tag[1:], 'prepared provenance')
    checksums = {}
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (?:\./)?(.+)', line)
        require(match is not None, 'prepared checksum syntax')
        path = safe_path(match[2])
        require(path not in checksums, 'duplicate prepared checksum')
        checksums[path] = match[1]
    actual = {str(path.relative_to(directory)) for path in directory.rglob('*') if path.is_file()
              and path.name != 'SHA256SUMS'}
    require(set(checksums) == actual, 'prepared inventory coverage')
    for path, expected in checksums.items():
        file = directory / path
        require(not file.is_symlink() and file.stat().st_size <= 1024 * MAX_BYTES, 'prepared file boundary')
        with file.open('rb') as stream:
            actual_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        require(actual_digest == expected, 'prepared asset digest')
    for row in manifest['crates']:
        path = directory / row['asset']
        if not path.is_file():
            path = directory / 'crates' / row['asset']
        verify_archive(row, path.read_bytes(), revision)
    return manifest


def bootstrap(config, release_id, revision, manifest_sha256, expected_owner, expected_owner_id):
    """One-time owner-terminal bootstrap; never an Actions fallback credential."""
    require(not os.environ.get('GITHUB_ACTIONS') and not os.environ.get('CI') and
            not os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL') and
            not any(os.environ.get(key) for key in ('CARGO_REGISTRY_TOKEN',
                'CARGO_REGISTRIES_CRATES_IO_TOKEN', 'CARGO_REGISTRY_BOOTSTRAP_TOKEN')),
            'bootstrap requires a local owner terminal without environment credentials')
    require(sys.stdin.isatty() and sys.stderr.isatty(), 'bootstrap requires interactive terminal')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,63}', expected_owner or '')
            and str(expected_owner_id).isdigit(), 'explicit owner identity required')
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT), 'dirty bootstrap checkout')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(api(config, f'compare/{head}...main')['status'] in ('identical', 'ahead'), 'bootstrap tooling must be reviewed on main')
    release, manifest, snapshots = release_evidence(config, release_id, revision, manifest_sha256)
    historical = {row['name'] for row in config['historical']}
    require(manifest['crates'] and all(row['name'] not in historical for row in manifest['crates']),
            'bootstrap cannot publish established historical crate families')
    public_owner = json.loads(get(f'{REGISTRY}/api/v1/users/{expected_owner}'))['user']
    require(str(public_owner['id']) == str(expected_owner_id) and public_owner['login'] == expected_owner,
            'bootstrap owner identity mismatch')
    def owned(name):
        users = json.loads(get(f'{REGISTRY}/api/v1/crates/{name}/owners'))['users']
        require(any(user.get('kind') == 'user' and str(user.get('id')) == str(expected_owner_id)
                    and user.get('login') == expected_owner for user in users), 'bootstrap owner mismatch')
    to_publish = []
    for row in ordered(manifest['crates']):
        if registry_state(row) == 'present':
            owned(row['name'])
            continue
        # A name that exists at any other coordinate is not a first publication.
        require(get(f"{REGISTRY}/api/v1/crates/{row['name']}", absent=True) is None,
                'bootstrap name already exists; configure Trusted Publishing instead')
        to_publish.append(row)
    if not to_publish:
        return
    coordinates = ', '.join(f"{row['name']}@{row['version']}" for row in to_publish)
    print(f'First publication of {coordinates} as {expected_owner} ({expected_owner_id}).', file=sys.stderr)
    print('Use a short-lived publish-new token created in this owner account; no token identity API is available.', file=sys.stderr)
    require(input(f'Type PUBLISH AS {expected_owner}: ') == f'PUBLISH AS {expected_owner}', 'bootstrap not confirmed')
    token = getpass.getpass('One-use crates.io token (hidden): ')
    require(0 < len(token) <= 8192 and all(33 <= ord(char) <= 126 for char in token), 'invalid bootstrap token')
    for row in to_publish:
        require(api(config, f'releases/{release_id}') == release, 'bootstrap source changed')
        require(registry_state(row) == 'absent' and
                get(f"{REGISTRY}/api/v1/crates/{row['name']}", absent=True) is None,
                'bootstrap coordinate is no longer absent')
        put_once(snapshots[row['name']], row['metadata'], token)
        confirm([row])
        owned(row['name'])
    print('Bootstrap confirmed; revoke this one-use token and register Trusted Publishing.', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['verify', 'upload', 'confirm', 'dispatch', 'reconcile', 'finalize', 'bootstrap'])
    parser.add_argument('--release-id'); parser.add_argument('--revision'); parser.add_argument('--manifest-sha256')
    parser.add_argument('--source-tag'); parser.add_argument('--archives', type=Path)
    parser.add_argument('--expected-owner'); parser.add_argument('--expected-owner-id')
    args = parser.parse_args()
    config = policy()
    if args.operation == 'bootstrap':
        bootstrap(config, args.release_id, args.revision, args.manifest_sha256, args.expected_owner, args.expected_owner_id)
    elif args.operation == 'finalize':
        require(args.archives is not None, 'prepared directory required')
        finalize(config, args.source_tag, args.archives)
    elif args.operation in ('verify', 'confirm'):
        boundary(config)
        _, manifest, _ = release_evidence(config, args.release_id, args.revision, args.manifest_sha256)
        if args.operation == 'confirm':
            confirm(manifest['crates'])
        else:
            for row in manifest['crates']:
                require(registry_state(row) != 'indeterminate', 'registry state unresolved')
    elif args.operation == 'upload':
        upload(config, args.release_id, args.revision, args.manifest_sha256)
    elif args.operation == 'dispatch':
        if os.environ.get('GITHUB_REF') != 'refs/heads/main':
            return
        require(re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', args.source_tag or ''), 'dispatch source tag')
        release = api(config, 'releases/tags/' + args.source_tag)
        raw = asset_bytes(config, release, MANIFEST, MAX_JSON)
        manifest = validate_manifest(config, json.loads(raw))
        if manifest['crates']:
            dispatch(config, release, manifest, digest(raw))
    else:
        seen = set()
        for release, manifest, row, manifest_sha256 in catalog(config).values():
            if release['id'] in seen:
                continue
            seen.add(release['id'])
            if any(registry_state(item) != 'present' for item in manifest['crates']):
                # Each dispatch is an explicit side effect; no publication credential here.
                dispatch(config, release, manifest, manifest_sha256)
                # One release per run keeps failure/retry and concurrency bounded.
                break
    print(json.dumps({'operation': args.operation, 'status': 'complete'}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never emit credentials, registry responses or attacker-controlled metadata.
        print('Crate release failed validation or remains unresolved.', file=sys.stderr)
        raise SystemExit(1)
