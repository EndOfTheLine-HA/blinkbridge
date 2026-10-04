#!/usr/bin/env python3
"""Fail when the prospective public Git tree contains local or secret data."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
TEXT_SUFFIXES = {
    '', '.cfg', '.conf', '.env', '.example', '.ini', '.json', '.md', '.py',
    '.sh', '.toml', '.txt', '.yaml', '.yml',
}
FORBIDDEN_NAMES = {'.cred.json', '.needs-2fa', 'config.json'}
FORBIDDEN_SUFFIXES = {'.bak', '.codex-candidate', '.mp4', '.jpg', '.concat', '.part'}
CONTENT_RULES = {
    'private IPv4 address': re.compile(r'(?<![0-9])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2}(?![0-9])'),
    'Windows user path': re.compile(r'(?i)[A-Z]:\\Users\\[^\\\s]+'),
    'Unix home path': re.compile(r'(?<![\w/])/home/[a-z_][a-z0-9_-]*'),
    'email address': re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
}


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard'],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line]


def main() -> int:
    failures: list[str] = []
    for path in candidate_files():
        relative = path.relative_to(ROOT)
        parts_lower = {part.lower() for part in relative.parts}
        if '__pycache__' in parts_lower:
            failures.append(f'{relative}: Python cache directory')
        if path.name.lower() in FORBIDDEN_NAMES:
            failures.append(f'{relative}: forbidden runtime filename')
        if any(path.name.lower().endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            failures.append(f'{relative}: forbidden local artifact suffix')

        if path.resolve() == SELF or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            failures.append(f'{relative}: unexpected binary file')
            continue
        for label, pattern in CONTENT_RULES.items():
            for match in pattern.finditer(text):
                value = match.group(0)
                if label == 'email address' and value.lower().endswith(('.example', '.invalid')):
                    continue
                line = text.count('\n', 0, match.start()) + 1
                failures.append(f'{relative}:{line}: {label}')

    example_path = ROOT / 'config' / 'config.example.json'
    example = json.loads(example_path.read_text(encoding='utf-8'))
    login = example.get('blink', {}).get('login', {})
    if login.get('username') or login.get('password'):
        failures.append('config/config.example.json: Blink login values must be empty')

    if failures:
        print('Public-tree hygiene check failed:', file=sys.stderr)
        for failure in failures:
            print(f'  - {failure}', file=sys.stderr)
        return 1

    print(f'Public-tree hygiene check passed for {len(candidate_files())} files.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
