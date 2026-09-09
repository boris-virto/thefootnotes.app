"""Deployment-only SQLite backup/migration commands; never logs credentials."""
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from app import db
from app.config import DATA_DIR, FILES_DIR


def database_path():
    if db.engine.dialect.name != 'sqlite' or not db.engine.url.database:
        raise RuntimeError('This release procedure supports file-backed SQLite only')
    path = Path(db.engine.url.database).resolve()
    if not path.is_relative_to(DATA_DIR.resolve()):
        raise RuntimeError('Database must be inside DATA_DIR; review custom storage before deploying')
    if not path.is_file():
        raise RuntimeError('Existing production database was not found; refusing an empty replacement')
    return path


def main(action, backup):
    backup = Path(backup)
    source = database_path()
    if action == 'backup':
        backup.mkdir(parents=True, mode=0o700)
        with sqlite3.connect(source) as origin, sqlite3.connect(backup / 'database.sqlite') as target:
            origin.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
        shutil.copytree(FILES_DIR, backup / 'files')
        (backup / 'manifest.json').write_text(json.dumps({'database': str(source)}))
        (backup / 'complete').touch()
    elif action == 'check-migration':
        from sqlalchemy import create_engine
        clone = backup / 'migration-check.sqlite'
        shutil.copy2(backup / 'database.sqlite', clone)
        db.engine = create_engine(f'sqlite:///{clone}')
        db.init_db()
        db.list_board()
        db.engine.dispose()
    elif action == 'migrate':
        db.init_db()
    elif action == 'restore':
        manifest = json.loads((backup / 'manifest.json').read_text())
        if manifest['database'] != str(source):
            raise RuntimeError('Backup destination mismatch')
        db.engine.dispose()
        # Called only while the single writer is stopped, before candidate startup.
        for suffix in ('-wal', '-shm'):
            Path(str(source) + suffix).unlink(missing_ok=True)
        shutil.copy2(backup / 'database.sqlite', source)
    else:
        raise ValueError(action)


if __name__ == '__main__':
    main(*sys.argv[1:])
