# Copyright 2026 The Atrinik Project
# SPDX-License-Identifier: MIT

"""Compare persistent stdio semantic queries with one fresh process per query.

Usage: python3 benchmark.py /absolute/path/to/atrinik-content-mcp
Only synthetic temporary Git fixtures are read or written.
"""

import hashlib, json, pathlib, select, statistics, subprocess, sys, tempfile, time
binary = pathlib.Path(sys.argv[1]).resolve()
iterations = 30
meta = {'io.modelcontextprotocol/protocolVersion': '2026-07-28', 'io.modelcontextprotocol/clientInfo': {'name': 'synthetic-benchmark', 'version': '1.0.0'}, 'io.modelcontextprotocol/clientCapabilities': {}}
request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'_meta': meta, 'name': 'content_query', 'arguments': {'selector': 'synthetic', 'operation': 'search', 'query': 'Display 123'}}}
payload = (json.dumps(request, separators=(',', ':')) + '\n').encode()

semantic_reference = None

def check(data):
    global semantic_reference
    value = json.loads(data)
    if 'error' in value:
        raise RuntimeError(value['error'])
    result = value['result']
    if result.get('isError'):
        raise RuntimeError(result)
    records = result['structuredContent']['records']
    if len(records) != 1 or records[0]['identity'] != 'archetype:synthetic/item123':
        raise RuntimeError('unexpected semantic identity')
    semantic = result['structuredContent']
    if semantic_reference is None:
        semantic_reference = semantic
    elif semantic != semantic_reference:
        raise RuntimeError('persistent and fresh semantic results differ')
    return len(data)

def summarize(values):
    values = sorted(values)
    return {'p50_ms': statistics.median(values), 'p95_ms': values[int((len(values) - 1) * 0.95)], 'total_ms': sum(values)}
with tempfile.TemporaryDirectory(prefix='atrinik-content-mcp-benchmark-') as directory:
    root = pathlib.Path(directory)
    source = root / 'source'
    source.mkdir()
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_LAZY_FETCH': '1'}

    def git(*args):
        return subprocess.check_output(['git', *args], cwd=source, env=env, stderr=subprocess.DEVNULL, text=True).strip()
    git('init', '-b', 'main')
    git('remote', 'add', 'origin', 'https://github.com/atrinik/content.git')
    (source / 'synthetic.arc').write_text(''.join((f'Object item{i:03}\nname Display {i}\nend\n' for i in range(300))))
    git('add', 'synthetic.arc')
    git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-m', 'synthetic benchmark fixture')
    commit = git('rev-parse', 'HEAD')
    identity = {'repository': 'atrinik/content', 'branch': 'refs/heads/main', 'commit': commit, 'main_base_commit': commit, 'worktree': 'synthetic', 'source_role': 'main', 'view_role': 'replacement', 'dirty_fingerprint': None, 'authorization': 'synthetic', 'manifest': 'synthetic', 'profile': 'synthetic', 'registry': 'synthetic', 'schema_version': 1, 'provider_version': 'atrinik-content-mcp/v1'}
    config = root / 'config.json'
    config.write_text(json.dumps({'snapshots': [{'root': str(source), 'identity': identity, 'files': [{'path': 'synthetic.arc', 'domain': 'archetype', 'namespace': 'synthetic', 'rules': {'name': {'kind': 'label'}}}]}]}))
    command = [str(binary), '--config', str(config)]
    warm = []
    output_bytes = []
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        for i in range(iterations + 1):
            start = time.perf_counter_ns()
            process.stdin.write(payload)
            process.stdin.flush()
            if not select.select([process.stdout], [], [], 15)[0]:
                raise TimeoutError('synthetic request timed out')
            line = process.stdout.readline()
            if not line:
                raise RuntimeError('provider exited: ' + process.stderr.read().decode())
            elapsed = (time.perf_counter_ns() - start) / 1000000.0
            output_bytes.append(check(line))
            if i:
                warm.append(elapsed)
            else:
                startup = elapsed
    finally:
        process.stdin.close()
        process.wait(timeout=15)
    if process.returncode != 0:
        raise RuntimeError('provider did not exit successfully')
    cold = []
    for i in range(iterations):
        start = time.perf_counter_ns()
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            process.stdin.write(payload)
            process.stdin.flush()
            if not select.select([process.stdout], [], [], 15)[0]:
                raise TimeoutError('synthetic cold request timed out')
            line = process.stdout.readline()
            if not line:
                raise RuntimeError('provider exited: ' + process.stderr.read().decode())
            output_bytes.append(check(line))
            cold.append((time.perf_counter_ns() - start) / 1000000.0)
        finally:
            process.stdin.close()
            process.wait(timeout=15)
        if process.returncode != 0:
            raise RuntimeError('provider did not exit successfully')
    result = {'schema_version': 1, 'iterations': iterations, 'fixtures': 300, 'correct': True, 'external_network': False, 'baseline': 'fresh stdio provider process per identical semantic query', 'warm': 'one persistent stdio provider, each request independently fences live source state', 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'startup_query_ms': startup, 'persistent': summarize(warm), 'process_per_query': summarize(cold), 'max_response_bytes': max(output_bytes), 'p50_speedup': statistics.median(cold) / statistics.median(warm)}
    print(json.dumps(result, sort_keys=True))
