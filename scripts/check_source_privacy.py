"""Check tracked source, reporting locations/categories without matched values.

Structural guard only: reviewers must still classify prose. --revision SHA checks
an immutable tree without checking it out.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
UUID_RE = re.compile(rb'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', re.I)
# Generic branding, visually reviewed including embedded SVG raster layers.
APPROVED_IMAGES = {
    'public/apple-touch-icon.png': 'f22606da978f12ed98bee4d8a5cf2b4e0479efb5dd8bb5e91b4c3debf88c6d00',
    'public/assets/branding/logo.png': 'cc708c6c4e43e74f01c49cd637821e6d1e10ccaff01cb222be82f6fde5dc6176',
    'public/assets/branding/logo-house.svg': 'e2995b8b41f10cdd6b267a48aa2823997ff81e23cb648ddde61e09081a4faea8',
    'public/assets/branding/logo-house-transparent.svg': '34fcbcda3f2fe2d4e41b877771b158a4f997e66cb39fab69c22e23e15b0557ad',
    'public/assets/branding/logo-house2.svg': '7329a4e63d60329791cb94c2f48d77806b115d89a3bd0dc1e5934449cf7fda05',
    'public/favicon.svg': '7e82d30c1f7b537d8f28958b113ea8013e8cc05042b6d7ed83112c01a58f8401',
}

def image_digest(data):
    if data.lstrip().startswith((b"<svg", b"<?xml")):
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()

SECRET_RE = re.compile(rb'(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16})')
RUNTIME_RE = re.compile(r'(^|/)(?:\.private-evidence|\.promotion-artifacts|data|models|storage-slots|__pycache__|node_modules|\.output|\.pytest[^/]*)/|\.(?:dump|sqlite3?|db|log|tar|zip|mp4|mkv|flac|wma|mp3|wav)$', re.I)


def synthetic_uuid(value):
    value = value.decode().lower()
    compact = value.replace('-', '')
    return (value.startswith(('00000000-', '10000000-0000-0000-0000-'))
            or len(set(compact)) == 1
            or value.endswith('-1234-5678-1234-567812345678')
            or value in {'12345678-1234-4123-8123-123456789abc',
                         '87654321-4321-4321-8321-cba987654321',
                         '11111111-2222-3333-4444-555555555555',
                         '11111111-2222-4333-8444-555555555555',
                         '66666666-7777-4888-8999-000000000000',
                         'aaaaaaaa-1234-5678-1234-567812345678',
                         '11111111-1111-4111-8111-111111111111',
                         '22222222-2222-4222-8222-222222222222'})


def inspect(path, data):
    findings = []
    test = path.startswith(('backend/tests/', 'tests/')) or '/test_' in path
    if RUNTIME_RE.search(path): findings.append({'file': path, 'kind': 'runtime-artifact'})
    if (data.startswith((b'\x89PNG', b'\xff\xd8\xff', b'GIF8', b'RIFF', b'%PDF', b'PK\x03\x04'))
            or re.search(rb'data:image/[^;,\s]+;base64,', data)):
        if APPROVED_IMAGES.get(path) != image_digest(data):
            findings.append({'file': path, 'kind': 'unreviewed-binary'})
        return findings
    for line, text in enumerate(data.splitlines(), 1):
        for match in UUID_RE.finditer(text):
            value = match.group()
            if value == b'00000000-0000-0000-0000-000000000000': continue
            if (test or path == 'scripts/check_source_privacy.py') and synthetic_uuid(value): continue
            findings.append({'file': path, 'line': line, 'kind': 'non-synthetic-uuid'})
        if SECRET_RE.search(text): findings.append({'file': path, 'line': line, 'kind': 'secret-material'})
        if re.search(rb'(?:[A-Za-z]:\\{1,2}Users\\{1,2}|/home/)[a-zA-Z][^ /\\]+', text):
            findings.append({'file': path, 'line': line, 'kind': 'personal-home-path'})
    return findings


def scan(revision=None):
    command = ['git', 'ls-tree', '-rz', '--name-only', revision] if revision else ['git', 'ls-files', '-z']
    paths = [p for p in subprocess.check_output(command, cwd=ROOT).decode().split('\0') if p]
    findings = []
    for path in paths:
        data = subprocess.check_output(['git', 'show', f'{revision}:{path}'], cwd=ROOT) if revision else (ROOT/path).read_bytes()
        findings.extend(inspect(path, data))
    return {'files': len(paths), 'findings': findings}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision')
    args = parser.parse_args()
    result = scan(args.revision)
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(result['findings']))
