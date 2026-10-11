#!/usr/bin/env python3
"""Exercise validation imports and the real dirty-source gate in writable Git."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WritableSourceTests(unittest.TestCase):
    def test_validation_imports_preserve_clean_source_and_dirty_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'
            root.mkdir()
            files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).split(b'\0')
            for name in filter(None, files):
                path = Path(os.fsdecode(name))
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / path, root / path)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL)
            git('init', '--quiet')
            git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'fixture')
            binaries = Path(temporary) / 'bin'
            binaries.mkdir()
            for name, body in [('rustc', 'echo rustc 1.97.1'), ('cargo', 'exit 73')]:
                path = binaries / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            environment = os.environ.copy()
            environment.pop('PYTHONDONTWRITEBYTECODE', None)
            environment.pop('PYTHONPYCACHEPREFIX', None)
            environment['PATH'] = str(binaries) + os.pathsep + environment['PATH']
            # Run the actual validation entry point through all Python imports;
            # stop at the first Cargo command, before recursive fixture testing.
            result = subprocess.run(['bash', 'tools/validate.sh'], cwd=root,
                env=environment, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
            self.assertIn('Ran 37 tests', result.stderr)
            self.assertIn('Ran 4 tests', result.stderr)
            self.assertEqual(git('status', '--porcelain'), b'')
            self.assertEqual(list(root.rglob('*.pyc')), [])
            # Stop immediately after the real clean-source check. No Cargo,
            # public catalog, source release or registry write is involved.
            code = '''from tools import crate_prepare as P
from pathlib import Path
from unittest.mock import patch
with patch.object(P, 'versions', side_effect=SystemExit('clean gate passed')):
    P.prepare(P.R.policy(), 'v1.0.0', Path('target/candidate'))
'''
            def gate():
                return subprocess.run([sys.executable, '-B', '-c', code], cwd=root,
                    env=environment, capture_output=True, text=True, timeout=10)
            result = gate()
            self.assertIn('clean gate passed', result.stderr)
            (root / 'unexpected-source-input').write_text('unreviewed')
            result = gate()
            self.assertIn('source packaging requires clean revision', result.stderr)
            self.assertNotIn('clean gate passed', result.stderr)
            (root / 'unexpected-source-input').unlink()
            (root / 'Cargo.toml').write_text((root / 'Cargo.toml').read_text() + '\n# unreviewed change\n')
            result = gate()
            self.assertIn('source packaging requires clean revision', result.stderr)
            self.assertNotIn('clean gate passed', result.stderr)


if __name__ == '__main__':
    unittest.main()
