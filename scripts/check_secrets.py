"""Check Git-tracked source files; report paths/rules only, never matched secrets."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'provider-key': re.compile(rb'\bsk-[A-Za-z0-9_-]{24,}'),
    'github-token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{24,})'),
    'private-key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'aws-access-id': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
}


def main():
    tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True, capture_output=True).stdout
    findings = []
    files = [Path(name.decode('utf-8')) for name in tracked.split(b'\0') if name]
    for relative in files:
        name = relative.name.lower()
        if (relative.parts[0] in {'data', '.venv', 'node_modules', 'dist'}
                or (name.startswith('.env') and name != '.env.example')
                or name in {'runtime-settings.json', 'admin-token.txt'}
                or name.endswith(('.sqlite3', '.sqlite', '.db', '.log', '.pem', '.key', '.p12', '.pfx'))):
            findings.append((relative.as_posix(), 'private-runtime-file'))
        raw = (ROOT / relative).read_bytes()
        for rule, pattern in PATTERNS.items():
            if pattern.search(raw):
                findings.append((relative.as_posix(), rule))
    for path, rule in findings:
        print(f'{path}: {rule}')
    if findings:
        print(f'FAILED: {len(findings)} possible credential/private-file findings; no values printed.')
        return 1
    print(f'Passed: {len(files)} tracked files checked; no configured patterns or private runtime files found.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
