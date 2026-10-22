import json
import math
import os
import sqlite3
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import replay
import d1_sync

ORIGIN = os.environ.get('LUMI_ORIGIN', 'https://dash.huyab.click')
DB = Path(os.environ.get('LUMI_DB', '/data/history.sqlite3'))
POLL_SECONDS = 10
RETENTION_SECONDS = 30 * 86400
RANGES = {'1h': 3600, '24h': 86400, '7d': 7 * 86400, '30d': RETENTION_SECONDS}
METRICS = ('voltage_v', 'current_a', 'power_w')
SYNC_PENDING = 'api_sync_pending'
COMMAND_LOCK = Lock()
POLL_WAKE = Event()
HOST_STATUS = Path('/run/nixostail-monitor/status.json')


def host_status():
    unknown = {'state': 'unknown', 'checked_at': None, 'last_seen_at': None, 'stale': True}
    try:
        value = json.loads(HOST_STATUS.read_text())
        checked = value['checked_at']
        seen = value.get('last_seen_at')
        valid_time = lambda ts: type(ts) in (int, float) and math.isfinite(ts) and 0 < ts <= time.time() + 5
        if not valid_time(checked) or (seen is not None and not valid_time(seen)):
            return unknown
        state = value['state']
        if state not in ('online', 'checking', 'unreachable', 'unknown'):
            return unknown
        stale = time.time() - checked > 30
        return {'state': 'unknown' if stale else state, 'checked_at': checked, 'last_seen_at': seen, 'stale': stale}
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return unknown


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript("""
            CREATE TABLE IF NOT EXISTS samples (
                ts REAL PRIMARY KEY, on_state INTEGER, online INTEGER,
                voltage_v REAL, current_a REAL, power_w REAL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, on_state INTEGER,
                kind TEXT NOT NULL, result TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        """)
        db.execute("UPDATE events SET result='unconfirmed' WHERE result='pending'")
        columns = {row['name'] for row in db.execute('PRAGMA table_info(events)')}
        if 'synced_at' not in columns:
            db.execute('ALTER TABLE events ADD COLUMN synced_at REAL')
            commands = db.execute("SELECT * FROM events WHERE kind='command' ORDER BY id").fetchall()
            for index, command in enumerate(commands):
                if command['result'] != 'confirmed':
                    continue
                end = commands[index + 1]['ts'] if index + 1 < len(commands) else time.time()
                match = db.execute('SELECT MIN(ts) FROM samples WHERE ts>=? AND ts<=? AND on_state=? AND error IS NULL',
                                   (command['ts'], end, command['on_state'])).fetchone()[0]
                if match is not None:
                    db.execute('UPDATE events SET synced_at=? WHERE id=?', (match, command['id']))
                db.execute('UPDATE samples SET error=? WHERE ts>=? AND ts<? AND error IS NULL AND on_state!=?',
                           (SYNC_PENDING, command['ts'], match if match is not None else end, command['on_state']))

        d1_sync.initialize(db)


def sample(now=None):
    error = None
    value = {}
    try:
        value = replay.get_status()
    except HTTPError as exc:
        error = 'Phiên Lumi hết hạn; cần cập nhật đăng nhập.' if exc.code in (401, 403) else 'Lumi API đang báo lỗi.'
    except Exception:
        error = 'Không đọc được Lumi API; chưa thể kết luận ổ cắm ngoại tuyến.'
    ts = time.time() if now is None else now
    metrics = value.get('metrics', {})
    with closing(connect()) as db, db:
        previous = db.execute('SELECT on_state FROM samples WHERE on_state IS NOT NULL ORDER BY ts DESC LIMIT 1').fetchone()
        current = value.get('on')
        command = db.execute("SELECT * FROM events WHERE kind='command' ORDER BY id DESC LIMIT 1").fetchone()
        if not error and command and command['result'] == 'confirmed' and command['synced_at'] is None:
            if current is not None and current == command['on_state']:
                db.execute('UPDATE events SET synced_at=? WHERE id=?', (ts, command['id']))
            else:
                error = SYNC_PENDING
        db.execute('INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)',
                   (ts, current, value.get('online'), *(metrics.get(k) for k in METRICS), error))
        if not error and current is not None and previous is not None and previous['on_state'] != current:
            db.execute("INSERT INTO events(ts,on_state,kind,result) VALUES (?,?,'observed','observed')", (ts, current))
        db.execute('DELETE FROM samples WHERE ts < ?', (ts - RETENTION_SECONDS,))
        db.execute('DELETE FROM events WHERE ts < ?', (ts - RETENTION_SECONDS,))
    return ts


def poll():
    while True:
        POLL_WAKE.clear()
        started = time.monotonic()
        try:
            sample()
        except Exception as exc:
            print(json.dumps({'event': 'sample_failed', 'type': type(exc).__name__}), flush=True)
        POLL_WAKE.wait(max(0, POLL_SECONDS - (time.monotonic() - started)))


def status():
    with closing(connect()) as db:
        row = db.execute('SELECT * FROM samples ORDER BY ts DESC LIMIT 1').fetchone()
        command = db.execute("SELECT id,ts,on_state,result,synced_at FROM events WHERE kind='command' ORDER BY id DESC LIMIT 1").fetchone()
    stale = row is None or time.time() - row['ts'] > 30
    error = 'Đang chờ mẫu đầu tiên.' if row is None else row['error']
    if stale and row is not None:
        error = 'Dữ liệu quá 30 giây; đang chờ cập nhật.'
    pending = bool(command and command['result'] == 'confirmed' and command['synced_at'] is None)
    if error == SYNC_PENDING:
        error = None
    usable = row is not None and not error and not stale
    api_on = bool(row['on_state']) if usable and row['on_state'] is not None else None
    on = bool(command['on_state']) if usable and pending else api_on
    return {
        'on': on, 'api_on': api_on, 'sync_pending': pending and usable,
        'online': bool(row['online']) if usable and row['online'] is not None else None,
        'metrics': {k: row[k] if usable and not pending else None for k in METRICS},
        'checked_at': row['ts'] if row else None,
        'stale': stale, 'error': error, 'poll_seconds': POLL_SECONDS,
        'last_command': dict(command) if command else None,
        'nixostail': host_status(),
    }


def history(range_name, before=None):
    seconds = RANGES[range_name]
    now = time.time()
    cutoff = now - seconds
    with closing(connect()) as db:
        first = db.execute('SELECT MIN(ts) FROM samples WHERE ts >= ?', (cutoff,)).fetchone()[0]
        start = max(cutoff, first) if first is not None else cutoff
        step = max(POLL_SECONDS, int((now - start) / 600) + 1)
        start = int(start // step) * step
        rows = db.execute("""
            SELECT CAST((ts - ?) / ? AS INTEGER) AS bucket, COUNT(*) AS count,
                   CASE WHEN COUNT(CASE WHEN error IS NULL THEN voltage_v END)=COUNT(*) THEN AVG(voltage_v) END AS voltage_v,
                   CASE WHEN COUNT(CASE WHEN error IS NULL THEN current_a END)=COUNT(*) THEN AVG(current_a) END AS current_a,
                   CASE WHEN COUNT(CASE WHEN error IS NULL THEN power_w END)=COUNT(*) THEN AVG(power_w) END AS power_w
            FROM samples WHERE ts >= ? AND ts <= ? GROUP BY bucket ORDER BY bucket
        """, (start, step, cutoff, now)).fetchall()
        buckets = {r['bucket']: r for r in rows}
        points = []
        for i in range(int((now - start) // step) + 1):
            row = buckets.get(i)
            complete = row is not None and (i in (0, int((now - start) // step)) or row['count'] >= max(1, int(step / POLL_SECONDS) - 1))
            points.append({'ts': start + i * step, **{k: row[k] if complete else None for k in METRICS}})
        events = db.execute("""
            SELECT * FROM events WHERE ts >= ? AND id < ? ORDER BY id DESC LIMIT 101
        """, (cutoff, before if before is not None else 9223372036854775807)).fetchall()
        count = db.execute('SELECT COUNT(*) FROM samples WHERE ts >= ?', (cutoff,)).fetchone()[0]
    return {'from': start, 'to': now, 'first_sample_at': first, 'range': range_name,
            'bucket_seconds': step, 'sample_count': count, 'points': points,
            'events': [dict(e) for e in events[:100]],
            'next_before': events[99]['id'] if len(events) > 100 else None}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value, content_type='application/json; charset=utf-8'):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, write=False):
        return (self.headers.get('Host') == urlsplit(ORIGIN).netloc
                and self.headers.get('Origin', '' if write else ORIGIN) == ORIGIN
                and self.headers.get('Sec-Fetch-Site') not in ('cross-site', 'same-site')
                and (not write or self.headers.get('X-Lumi-Control') == '1'))

    def do_GET(self):
        if not self.allowed():
            return self.reply(403, {'error': 'Nguồn truy cập không hợp lệ.'})
        url = urlsplit(self.path)
        try:
            if url.path == '/lumi/':
                return self.reply(200, (Path(__file__).parent / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            if url.path == '/lumi/api/status':
                return self.reply(200, status())
            if url.path == '/lumi/api/history':
                query = parse_qs(url.query, strict_parsing=True)
                if set(query) - {'range', 'before'} or any(len(v) != 1 for v in query.values()):
                    raise ValueError()
                period = query.get('range', ['24h'])[0]
                if period not in RANGES:
                    raise ValueError()
                before = int(query['before'][0]) if 'before' in query else None
                if before is not None and not 0 < before <= 9223372036854775807:
                    raise ValueError()
                return self.reply(200, history(period, before))
            return self.reply(404, {'error': 'Không tìm thấy.'})
        except ValueError:
            return self.reply(400, {'error': 'Khoảng thời gian không hợp lệ.'})
        except Exception:
            return self.reply(503, {'error': 'Không đọc được dữ liệu lịch sử.'})

    def do_POST(self):
        if not self.allowed(write=True):
            return self.reply(403, {'error': 'Yêu cầu điều khiển không hợp lệ.'})
        if self.path != '/lumi/api/power':
            return self.reply(404, {'error': 'Không tìm thấy.'})
        self.connection.settimeout(5)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 128 or self.headers.get('Content-Type') != 'application/json' or self.headers.get('Transfer-Encoding'):
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or set(data) != {'on'} or type(data['on']) is not bool:
                raise ValueError()
        except (ValueError, TypeError, OSError):
            return self.reply(400, {'error': 'Gửi JSON với on là true hoặc false.'})
        if not COMMAND_LOCK.acquire(blocking=False):
            return self.reply(409, {'error': 'Một lệnh đang chờ xác nhận.'})
        event_id = None
        try:
            current = status()
            if current['error'] or current['on'] is None:
                return self.reply(409, {'error': 'Chưa có trạng thái mới để điều khiển.'})
            with closing(connect()) as db, db:
                last = db.execute("SELECT ts FROM events WHERE kind='command' ORDER BY id DESC LIMIT 1").fetchone()
                if last and time.time() - last['ts'] < 3:
                    return self.reply(429, {'error': 'Chờ 3 giây trước lệnh tiếp theo.'})
                event_id = db.execute("INSERT INTO events(ts,on_state,kind,result) VALUES (?,?,'command','pending')", (time.time(), data['on'])).lastrowid
            result = replay.run(True, on=data['on'], signed=True)
            confirmed = result.get('matching_status') is True and result.get('reported_on') is data['on']
            with closing(connect()) as db, db:
                db.execute('UPDATE events SET result=? WHERE id=?', ('confirmed' if confirmed else 'unconfirmed', event_id))
            if not confirmed:
                return self.reply(504, {'error': 'Chưa nhận xác nhận; không tự gửi lại. Kiểm tra trạng thái trước lệnh tiếp theo.', 'confirmed': False})
            return self.reply(200, {'confirmed': True, 'on': data['on']})
        except Exception:
            if event_id is not None:
                try:
                    with closing(connect()) as db, db:
                        db.execute("UPDATE events SET result='unconfirmed' WHERE id=?", (event_id,))
                except Exception:
                    pass
            return self.reply(503, {'error': 'Không hoàn tất lệnh; kiểm tra trạng thái trước khi thử lại.', 'confirmed': False})
        finally:
            POLL_WAKE.set()
            COMMAND_LOCK.release()


if __name__ == '__main__':
    os.umask(0o077)
    initialize()
    Thread(target=poll, daemon=True).start()
    Thread(target=d1_sync.run, args=(DB,), daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8765), Handler).serve_forever()
