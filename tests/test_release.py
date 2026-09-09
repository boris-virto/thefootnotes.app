"""Failure-path tests for the release transaction; never call real systemctl."""
import io
from pathlib import Path
import tarfile

import pytest

from deploy import release

SHA = 'a' * 40
OLD = 'b' * 40


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    old = tmp_path / 'releases/old'
    old.mkdir(parents=True)
    (old / 'RELEASE').write_text(OLD)
    (tmp_path / 'current').symlink_to(old)
    def unpack(_, destination):
        (destination / 'RELEASE').write_text(SHA)
        import platform, json, sys
        (destination / 'runtime.json').write_text(json.dumps({'python': list(sys.version_info[:2]), 'machine': platform.machine()}))
    monkeypatch.setattr(release, 'unpack', unpack)
    commands = []
    def run(*args, **kwargs):
        commands.append(args)
        if 'backup' in args:
            backup = Path(args[-1])
            backup.mkdir(parents=True)
            (backup / 'complete').touch()
    monkeypatch.setattr(release, 'run', run)
    return tmp_path, old, commands


def test_success_switches_release_and_records_previous(deployment, monkeypatch):
    root, old, commands = deployment
    monkeypatch.setattr(release, 'ready', lambda *a, **k: True)
    release.deploy(root, root / 'archive', SHA, 10)
    assert (root / 'current/RELEASE').read_text() == SHA
    assert (root / 'previous').resolve() == old
    assert (root / 'deployed-sequence').read_text() == '10'
    assert commands.index(('sudo', '-n', 'systemctl', 'stop', 'thefootnotes')) < next(i for i, c in enumerate(commands) if 'backup' in c)


def test_failed_readiness_rolls_back_code_without_restoring_database(deployment, monkeypatch):
    root, old, commands = deployment
    monkeypatch.setattr(release, 'ready', lambda sha, **k: sha == OLD)
    with pytest.raises(RuntimeError, match='ready'):
        release.deploy(root, root / 'archive', SHA, 10)
    assert (root / 'current').resolve() == old
    assert not any('restore' in c for c in commands)
    assert not (root / 'deployed-sequence').exists()


def test_public_readiness_failure_also_rolls_back(deployment, monkeypatch):
    root, old, commands = deployment
    monkeypatch.setattr(release, 'ready', lambda sha, url='', **k: not url)
    with pytest.raises(RuntimeError, match='Public'):
        release.deploy(root, root / 'archive', SHA, 10, 'https://example.invalid/health/ready')
    assert (root / 'current').resolve() == old
    assert not any('restore' in c for c in commands)


def test_failed_migration_restores_backup_before_old_process_starts(deployment, monkeypatch):
    root, old, commands = deployment
    original = release.run
    def run(*args, **kwargs):
        original(*args, **kwargs)
        if 'migrate' in args:
            raise RuntimeError('migration failed')
    monkeypatch.setattr(release, 'run', run)
    monkeypatch.setattr(release, 'ready', lambda *a, **k: True)
    with pytest.raises(RuntimeError, match='migration failed'):
        release.deploy(root, root / 'archive', SHA, 10)
    assert (root / 'current').resolve() == old
    assert any('restore' in c for c in commands)


def test_stale_workflow_cannot_replace_newer_deployment(deployment):
    root, old, commands = deployment
    (root / 'deployed-sequence').write_text('11')
    with pytest.raises(RuntimeError, match='older workflow'):
        release.deploy(root, root / 'archive', SHA, 10)
    assert commands == []


def test_dependency_failure_does_not_stop_running_service(deployment, monkeypatch):
    root, old, commands = deployment
    def fail(*args, **kwargs):
        raise RuntimeError('dependency installation failed')
    monkeypatch.setattr(release, 'run', fail)
    with pytest.raises(RuntimeError, match='dependency'):
        release.deploy(root, root / 'archive', SHA, 10)
    assert (root / 'current').resolve() == old
    assert commands == []


def test_archive_cannot_escape_release_directory(tmp_path):
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        member = tarfile.TarInfo('../escape')
        member.size = 1
        bundle.addfile(member, io.BytesIO(b'x'))
    with pytest.raises(ValueError, match='Unsafe'):
        release.unpack(archive, tmp_path / 'release')
    assert not (tmp_path / 'escape').exists()
