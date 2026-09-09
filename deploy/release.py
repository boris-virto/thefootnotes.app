"""Install a CI artifact and switch a single systemd service. Linux, Python 3.13."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tarfile
import time
import urllib.request


def run(*args, cwd=None, env=None):
    subprocess.run(args, check=True, cwd=cwd, env=env)


def switch(root: Path, target: Path):
    temporary = root / 'current.next'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, target_is_directory=True)
    temporary.replace(root / 'current')


def ready(sha: str, url='http://127.0.0.1:8000/health/ready', attempts=60):
    # One-time bootstrap snapshot predates health endpoints. Only this explicitly
    # labelled rollback target uses the old login page as a weaker startup check.
    legacy = sha.startswith('legacy-')
    if legacy:
        url = 'http://127.0.0.1:8000/login'
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if legacy:
                    return response.status == 200
                result = json.load(response)
            if result.get('status') == 'ok' and result.get('release') == sha:
                return True
        except (OSError, ValueError):
            pass
        time.sleep(2)
    return False


def unpack(archive: Path, destination: Path):
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError(f'Unsafe archive member: {member.name}')
        bundle.extractall(destination, filter='data')


def deploy(root: Path, archive: Path, sha: str, sequence: int, public_url=''):
    if not re.fullmatch(r'[a-f0-9]{40}', sha) or sequence < 1:
        raise ValueError('Expected a full commit SHA and positive workflow run number')
    if not (root / 'current').is_symlink():
        raise RuntimeError('Run the documented one-time systemd bootstrap first')
    previous = (root / 'current').resolve(strict=True)
    previous_sha = (previous / 'RELEASE').read_text().strip()
    previous_sequence = int((root / 'deployed-sequence').read_text()) if (root / 'deployed-sequence').exists() else 0
    if sequence < previous_sequence:
        raise RuntimeError('Refusing an older workflow after a newer deployment')
    if sha == previous_sha and ready(sha, attempts=1):
        print('This release is already healthy', flush=True)
        return
    release = root / 'releases' / f'{sha}-{sequence}-{time.time_ns()}'
    release.mkdir(parents=True)
    unpack(archive, release)
    if (release / 'RELEASE').read_text().strip() != sha:
        raise RuntimeError('Artifact SHA mismatch')
    manifest = json.loads((release / 'runtime.json').read_text())
    import platform
    if manifest['python'] != list(__import__('sys').version_info[:2]) or manifest['machine'] != platform.machine():
        raise RuntimeError('CI artifact Python/architecture does not match this host')
    run('python3', '-m', 'venv', str(release / '.venv'))
    python = str(release / '.venv/bin/python')
    run(python, '-m', 'pip', 'install', '--no-index', '--find-links', str(release / 'wheels'),
        '--require-hashes', '-r', str(release / 'requirements.lock'))
    run(python, '-m', 'pip', 'check')
    environment = dict(os.environ, PYTHONPATH=str(release), PYTHONSAFEPATH='1',
                       DOTENV_PATH=str(root / '.env'))
    run(python, '-c', 'import app.main; import faster_whisper', cwd=root, env=environment)
    backup = root / 'backups' / f'{sha}-{time.time_ns()}'
    stopped = False
    started_candidate = False
    switched = False
    try:
        # No second poller and no writes during the backup/migration window.
        run('sudo', '-n', 'systemctl', 'stop', 'thefootnotes')
        stopped = True
        run(python, '-m', 'deploy.database', 'backup', str(backup), cwd=root, env=environment)
        run(python, '-m', 'deploy.database', 'check-migration', str(backup), cwd=root, env=environment)
        run(python, '-m', 'deploy.database', 'migrate', str(backup), cwd=root, env=environment)
        switch(root, release)
        switched = True
        started_candidate = True  # conservatively assume writes once start is attempted
        run('sudo', '-n', 'systemctl', 'start', 'thefootnotes')
        if not ready(sha):
            raise RuntimeError('New release did not become ready')
        if public_url and not ready(sha, public_url, attempts=15):
            raise RuntimeError('Public readiness check failed')
        (root / 'deployed-sequence').write_text(str(sequence))
        (root / 'previous').unlink(missing_ok=True)
        (root / 'previous').symlink_to(previous, target_is_directory=True)
        print(f'Deployed {sha}; previous={previous.name}; backup={backup.name}', flush=True)
    except BaseException:
        if stopped:
            run('sudo', '-n', 'systemctl', 'stop', 'thefootnotes')
            if not started_candidate and (backup / 'complete').exists():
                # Safe only before the candidate has accepted any new writes.
                run(python, '-m', 'deploy.database', 'restore', str(backup), cwd=root, env=environment)
            if switched:
                switch(root, previous)
            run('sudo', '-n', 'systemctl', 'start', 'thefootnotes')
            if not ready(previous_sha):
                print('ERROR: previous release is not ready; operator intervention required', flush=True)
            else:
                print('Previous release restored; deployment remains FAILED', flush=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('sha')
    parser.add_argument('sequence', type=int)
    parser.add_argument('--root', type=Path, default=Path('/opt/thefootnotes'))
    parser.add_argument('--public-url', default='https://thefootnotes.app/health/ready')
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    # Also serializes manual invocations, independently of GitHub concurrency.
    with (args.root / '.deploy.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        deploy(args.root, args.archive.resolve(), args.sha, args.sequence, args.public_url)


if __name__ == '__main__':
    main()
