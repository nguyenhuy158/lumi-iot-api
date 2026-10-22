import json
import sqlite3
import time
import urllib.request
from contextlib import closing
from pathlib import Path

CONFIG = Path('/run/lumi/d1.json')
BATCH_SIZE = 40


def initialize(db):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='d1_outbox'").fetchone()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS d1_outbox (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, record_id REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS d1_outbox_record ON d1_outbox(kind,record_id);
        CREATE TRIGGER IF NOT EXISTS d1_sample_insert AFTER INSERT ON samples BEGIN
            INSERT INTO d1_outbox(kind,record_id) VALUES ('sample',NEW.ts);
        END;
        CREATE TRIGGER IF NOT EXISTS d1_event_insert AFTER INSERT ON events BEGIN
            INSERT INTO d1_outbox(kind,record_id) VALUES ('event',NEW.id);
        END;
        CREATE TRIGGER IF NOT EXISTS d1_event_update AFTER UPDATE ON events BEGIN
            INSERT INTO d1_outbox(kind,record_id) VALUES ('event',NEW.id);
        END;
    """)
    if not exists:
        db.execute("INSERT INTO d1_outbox(kind,record_id) SELECT 'sample',ts FROM samples ORDER BY ts")
        db.execute("INSERT INTO d1_outbox(kind,record_id) SELECT 'event',id FROM events ORDER BY id")


def send_batch(path, config):
    with closing(sqlite3.connect(path, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        queued = db.execute('SELECT * FROM d1_outbox ORDER BY seq LIMIT ?', (BATCH_SIZE,)).fetchall()
        if not queued:
            return False
        records = []
        for row in queued:
            table, key = ('samples', 'ts') if row['kind'] == 'sample' else ('events', 'id')
            record = db.execute(f'SELECT * FROM {table} WHERE {key}=?', (row['record_id'],)).fetchone()
            if record:
                records.append({'seq': row['seq'], 'kind': row['kind'], 'record': dict(record)})
    if records:
        request = urllib.request.Request(
            'https://dash.huyab.click/lumi/api/ingest',
            data=json.dumps({'records': records}, allow_nan=False).encode(),
            headers={'Authorization': 'Bearer ' + config['key'], 'Content-Type': 'application/json',
                     'User-Agent': 'Mozilla/5.0 (compatible; LumiHistorySync/1.0)'})
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
        if result != {'accepted': len(records), 'last_seq': records[-1]['seq']}:
            raise RuntimeError('Unexpected D1 acknowledgment')
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute('DELETE FROM d1_outbox WHERE seq<=?', (queued[-1]['seq'],))
        db.execute("DELETE FROM samples WHERE ts<? AND NOT EXISTS (SELECT 1 FROM d1_outbox WHERE kind='sample' AND record_id=samples.ts)",
                   (time.time() - 86400,))
    return True


def run(path):
    while True:
        try:
            config = json.loads(CONFIG.read_text())
            if send_batch(path, config):
                continue
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(json.dumps({'event': 'd1_sync_failed', 'type': type(exc).__name__}), flush=True)
        time.sleep(10)
