#!/usr/bin/env python3
"""Write the exact flat downloadable release inventory in sha256sum format."""
import hashlib
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
for path in sorted(root.iterdir(), key=lambda entry: entry.name):
    if path.name == 'SHA256SUMS':
        continue
    if path.is_symlink() or not path.is_file() or not re.fullmatch(r'[A-Za-z0-9_.+-]+', path.name):
        raise SystemExit('release inventory must contain only flat regular assets')
    with path.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    print(f'{checksum}  {path.name}')
