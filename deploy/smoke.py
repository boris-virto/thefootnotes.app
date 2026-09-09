"""Exercise actual uvicorn lifespan without contacting external APIs."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from deploy.release import ready

with tempfile.TemporaryDirectory(prefix='footnotes-smoke-') as directory:
    env = dict(os.environ, DATA_DIR=directory, DATABASE_URL=f'sqlite:///{directory}/test.db',
               DOTENV_PATH='/dev/null', PYTHON_DOTENV_DISABLED='1', BOT_ENABLED='false',
               MIGRATE_ON_START='true', TELEGRAM_BOT_TOKEN='', ANTHROPIC_API_KEY='test', OPENAI_API_KEY='test')
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '18080'], env=env)
    try:
        sha = Path('RELEASE').read_text().strip() if Path('RELEASE').exists() else 'development'
        if not ready(sha, 'http://127.0.0.1:18080/health/ready', attempts=15):
            raise SystemExit('Uvicorn readiness check failed')
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
