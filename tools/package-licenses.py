#!/usr/bin/env python3
# Copyright 2026 The Atrinik Project
# SPDX-License-Identifier: MIT
"""Retain exact locked dependency license and copyright notices in releases."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


def main():
    destination = Path(sys.argv[1])
    metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--locked", "--offline", "--format-version", "1"]))
    packages = sorted((p for p in metadata["packages"] if p["source"]),
                      key=lambda p: (p["name"], p["version"]))
    if len(packages) > 1024:
        raise ValueError("dependency notice inventory exceeds limit")
    destination.mkdir()
    manifest = []
    for package in packages:
        identity = package["name"] + "-" + package["version"]
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", identity):
            raise ValueError("invalid dependency identity")
        source = Path(package["manifest_path"]).parent
        notices = sorted(p for p in source.iterdir() if p.name.upper().startswith(
            ("LICENSE", "COPYING", "NOTICE", "COPYRIGHT", "UNLICENSE")))
        if not notices or len(notices) > 32:
            raise ValueError("dependency notice inventory is missing or excessive")
        target = destination / identity
        target.mkdir()
        files = {}
        for notice in notices:
            if notice.is_symlink() or not notice.is_file() or notice.stat().st_size > 1024 * 1024:
                raise ValueError("dependency notice is not a bounded regular file")
            data = notice.read_bytes()
            if len(data) > 1024 * 1024:
                raise ValueError("dependency notice exceeds limit")
            (target / notice.name).write_bytes(data)
            files[notice.name] = hashlib.sha256(data).hexdigest()
        manifest.append({"name": package["name"], "version": package["version"],
                         "license": package["license"], "files": files})
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
