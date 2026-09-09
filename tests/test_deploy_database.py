"""Exercise deployment backup/restore against real temporary SQLite files."""
from pathlib import Path
import sqlite3

import pytest

from app import db
from deploy import database


def test_backup_migration_probe_and_restore_preserve_records(tmp_path, monkeypatch):
    files = tmp_path / 'files'
    files.mkdir()
    (files / 'ticket.pdf').write_bytes(b'original ticket')
    monkeypatch.setattr(database, 'FILES_DIR', files)
    original = db.add_reminder(title='Keep me', file_paths=[str(files / 'ticket.pdf')])
    backup = tmp_path / 'backup'
    engine = db.engine
    database.main('backup', backup)
    assert (backup / 'complete').exists()
    assert (backup / 'files/ticket.pdf').read_bytes() == b'original ticket'
    try:
        database.main('check-migration', backup)
        db.add_reminder(title='Only in probe')
    finally:
        monkeypatch.setattr(db, 'engine', engine)
    assert [r.title for r in db.list_board()] == ['Keep me']
    db.add_reminder(title='Failed migration change')
    database.main('restore', backup)
    assert [r.title for r in db.list_board()] == ['Keep me']
    assert db.get_reminder(original.id).file_paths == [str(files / 'ticket.pdf')]
    with sqlite3.connect(database.database_path()) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_missing_database_cannot_silently_create_new_production(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    monkeypatch.setattr(db, 'engine', create_engine(f'sqlite:///{tmp_path}/absent.db'))
    monkeypatch.setattr(database, 'DATA_DIR', tmp_path)
    with pytest.raises(RuntimeError, match='not found'):
        database.main('backup', tmp_path / 'backup')
    assert not (tmp_path / 'absent.db').exists()
