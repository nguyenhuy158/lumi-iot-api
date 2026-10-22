import io
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import d1_sync


def check():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'history.sqlite3'
        now = time.time()
        with sqlite3.connect(path) as db:
            db.executescript('''
                CREATE TABLE samples(ts REAL PRIMARY KEY,on_state INTEGER,online INTEGER,voltage_v REAL,current_a REAL,power_w REAL,error TEXT);
                CREATE TABLE events(id INTEGER PRIMARY KEY,ts REAL,on_state INTEGER,kind TEXT,result TEXT,synced_at REAL);
            ''')
            db.execute('INSERT INTO samples VALUES (?,1,1,220,0,0,NULL)', (now - 90000,))
            db.execute("INSERT INTO events VALUES (1,?,1,'command','pending',NULL)", (now,))
            d1_sync.initialize(db)
        with patch.object(d1_sync.urllib.request, 'urlopen', side_effect=TimeoutError):
            try:
                d1_sync.send_batch(path, {'key': 'local-test'})
                raise AssertionError('Expected failure')
            except TimeoutError:
                pass
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT COUNT(*) FROM d1_outbox').fetchone()[0] == 2
            d1_sync.initialize(db)
            assert db.execute('SELECT COUNT(*) FROM d1_outbox').fetchone()[0] == 2
        sent = []

        def reply(request, timeout):
            assert request.get_header('User-agent') == 'Mozilla/5.0 (compatible; LumiHistorySync/1.0)'
            records = json.loads(request.data)['records']
            sent.append(records)
            if len(sent) == 1:
                with sqlite3.connect(path) as db:
                    db.execute("UPDATE events SET result='confirmed',synced_at=? WHERE id=1", (now + 1,))
                    db.execute('INSERT INTO samples VALUES (?,1,1,220,0,0,NULL)', (now + 1,))
            return io.BytesIO(json.dumps({'accepted': len(records), 'last_seq': records[-1]['seq']}).encode())

        with patch.object(d1_sync.urllib.request, 'urlopen', side_effect=reply):
            assert d1_sync.send_batch(path, {'key': 'local-test'})
            with sqlite3.connect(path) as db:
                assert db.execute('SELECT COUNT(*) FROM d1_outbox').fetchone()[0] == 2
                assert db.execute('SELECT COUNT(*) FROM samples WHERE ts<?', (now - 86400,)).fetchone()[0] == 0
            assert d1_sync.send_batch(path, {'key': 'local-test'})
            assert not d1_sync.send_batch(path, {'key': 'local-test'})
        assert sent[0][1]['record']['result'] == 'pending'
        assert sent[1][0]['record']['result'] == 'confirmed'
        assert sent[1][1]['record']['power_w'] == 0
        assert sent[1][0]['seq'] > sent[0][-1]['seq']
    print('PASS: backfill, timeout preserves queue, restart, concurrent updates, exact acknowledgment and local pruning')


if __name__ == '__main__':
    check()
