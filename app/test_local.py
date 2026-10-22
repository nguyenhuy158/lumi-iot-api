import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

import replay
import server
import time
import sqlite3
from contextlib import closing


def check():
    replay.self_test()
    command = {'reqid': 'phone-1700000000000-1', 'source': 'phone-1700000000000-1',
               'control_source': {'id': 'phone-1700000000000-1', 'phoneId': 'phone'},
               'objects': [{'type': 'devices', 'data': ['only-device'],
                            'execution': {'command': 'OnOff', 'params': {'on': True}}}]}
    body = replay.field(b'original/control') + json.dumps(command).encode()
    with patch('replay.subprocess.run') as signer:
        signer.return_value.stdout = b'signature'
        fresh, value = replay.signed_command(b'', body, False)
        assert replay.publish(fresh)[0] == b'original/control'
        assert value['reqid'] == value['source'] == value['control_source']['id']
        assert value['reqid'] != command['reqid']
        assert value['objects'][0]['data'] == ['only-device']
        assert value['objects'][0]['execution']['params']['on'] is False
        assert command['objects'][0]['execution']['params']['on'] is True
        assert signer.call_args.kwargs['input'].startswith(b'eyJ')
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'state').mkdir()
        config = {'url': 'https://example.invalid/status', 'headers': {},
                  'payload': {'payload': {'cmd': 'get', 'reqid': 'phone-1700000000000-1'}}}
        (root / 'state/status-request.json').write_text(json.dumps(config))
        response = {'success': True, 'data': [{'devid': 'only-device', 'status': 1,
                                             'states': {'OnOff': {'on': False}}}]}
        with patch('replay.SECRETS', root / 'state'), patch('replay.prepare', return_value=[command]), \
                patch('replay.urllib.request.urlopen') as upstream:
            upstream.return_value.__enter__.return_value.read.return_value = json.dumps(response)
            status = replay.get_status()
            assert status['on'] is False and status['online'] is True
            assert status['metrics'] == {'voltage_v': None, 'current_a': None, 'power_w': None}
            sent = json.loads(upstream.call_args.args[0].data)
            assert sent['payload']['reqid'] != config['payload']['payload']['reqid']
            response['data'][0]['status'] = 0
            upstream.return_value.__enter__.return_value.read.return_value = json.dumps(response)
            assert replay.get_status()['online'] is None
            states = response['data'][0]['states']
            states.update({'Voltage': {'voltage': 230.75}, 'Ampe': {'ampe': 0.12}, 'Power': {'power': 0}})
            upstream.return_value.__enter__.return_value.read.return_value = json.dumps(response)
            assert replay.get_status()['metrics'] == {'voltage_v': 230.75, 'current_a': 0.12, 'power_w': 0}
            for invalid in (None, True, '230.75', float('nan'), float('inf'), 10 ** 400):
                for group, key in [('Voltage', 'voltage'), ('Ampe', 'ampe'), ('Power', 'power')]:
                    states[group] = {key: invalid}
                upstream.return_value.__enter__.return_value.read.return_value = json.dumps(response)
                assert all(value is None for value in replay.get_status()['metrics'].values())
            response['data'][0]['states'] = {'Voltage': None, 'Ampe': [], 'Power': 42}
            upstream.return_value.__enter__.return_value.read.return_value = json.dumps(response)
            assert all(value is None for value in replay.get_status()['metrics'].values())

    with TemporaryDirectory() as directory, patch('server.DB', Path(directory) / 'history.sqlite3'):
        server.initialize()
        now = time.time() - 5
        good = {'on': True, 'online': True, 'metrics': {'voltage_v': 230.5, 'current_a': 0.12, 'power_w': 0}}
        with patch('replay.get_status', return_value=good):
            server.sample(now - server.RETENTION_SECONDS - 1)
            server.sample(now - 40)
            server.sample(now - 30)
        with patch('replay.get_status', side_effect=OSError('private token')):
            server.sample(now - 20)
            value = server.status()
            assert value['on'] is None and all(v is None for v in value['metrics'].values())
            assert 'private' not in value['error']
        with patch('replay.get_status', return_value=good):
            server.sample(now - 10)
            server.sample(now)
        value = server.status()
        assert value['on'] is True and value['metrics']['power_w'] == 0
        with closing(server.connect()) as db, db:
            assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 5
            db.execute("INSERT INTO events(ts,on_state,kind,result) VALUES (?,0,'command','pending')", (now - 100,))
            db.execute("INSERT INTO events(ts,on_state,kind,result) VALUES (?,0,'observed','observed')", (now - server.RETENTION_SECONDS - 1,))
        server.initialize()
        assert server.status()['last_command']['result'] == 'unconfirmed'
        with patch('replay.get_status', return_value={**good, 'on': False}):
            server.sample(now + 1)
        value = server.history('30d')
        assert value['sample_count'] == 6
        assert any(p['power_w'] is None for p in value['points'])
        assert any(p['power_w'] == 0 for p in value['points'])
        assert any(e['kind'] == 'observed' and e['on_state'] == 0 for e in value['events'])
        assert all(e['ts'] >= now - server.RETENTION_SECONDS for e in value['events'])
        with closing(server.connect()) as db, db:
            db.executemany("INSERT INTO events(ts,on_state,kind,result) VALUES (?,0,'observed','observed')", [(now,)] * 105)
        page = server.history('1h')
        assert len(page['events']) == 100 and page['next_before']
        page2 = server.history('1h', page['next_before'])
        assert page2['events'] and max(e['id'] for e in page2['events']) < page['next_before']
        with patch('server.time.time', return_value=now + 100):
            assert server.status()['stale'] and server.status()['metrics']['power_w'] is None
        with closing(server.connect()) as db, db:
            db.execute("INSERT INTO events(ts,on_state,kind,result) VALUES (?,0,'command','confirmed')", (now - 30,))
        with patch('replay.get_status', return_value=good):
            server.sample(now + 1.1)
        with patch('server.time.time', return_value=now + 25):
            value = server.status()
            assert value['sync_pending'] and value['on'] is False and value['api_on'] is True
            assert all(v is None for v in value['metrics'].values()) and value['error'] is None
        with closing(server.connect()) as db:
            row = db.execute('SELECT * FROM samples ORDER BY ts DESC LIMIT 1').fetchone()
            assert row['error'] == server.SYNC_PENDING and row['power_w'] == 0
        with patch('replay.get_status', return_value={**good, 'on': False}):
            server.sample(now + 1.2)
        assert not server.status()['sync_pending'] and server.status()['on'] is False
        with patch('replay.get_status', return_value=good):
            server.sample(now + 1.3)
        assert server.status()['on'] is True and not server.status()['sync_pending']
        listener = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = Thread(target=listener.serve_forever, daemon=True)
        thread.start()

        def request(method, path, body=None, headers=None):
            conn = http.client.HTTPConnection(*listener.server_address, timeout=3)
            defaults = {'Host': 'dash.huyab.click', 'Origin': 'https://dash.huyab.click', 'Content-Type': 'application/json', 'X-Lumi-Control': '1'}
            defaults.update(headers or {})
            conn.request(method, path, body, defaults)
            response = conn.getresponse()
            value = response.read()
            conn.close()
            return response.status, value

        try:
            with patch('replay.run', return_value={'matching_status': True, 'reported_on': False}) as control:
                for invalid in ('{"on":1}', '{"on":"false"}', '{"on":false,"device":"other"}', '[]'):
                    assert request('POST', '/lumi/api/power', invalid)[0] == 400
                for headers in ({'Host': 'attacker.example'}, {'Origin': 'https://attacker.example'}, {'Origin': ''}, {'X-Lumi-Control': ''}, {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}):
                    assert request('POST', '/lumi/api/power', '{"on":false}', headers)[0] == 403
                assert control.call_count == 0
                status, body = request('POST', '/lumi/api/power', '{"on":false}')
                assert status == 200 and json.loads(body)['confirmed'] is True
                assert server.status()['last_command']['result'] == 'confirmed'
                control.assert_called_once_with(True, on=False, signed=True)
                assert request('POST', '/lumi/api/power', '{"on":true}')[0] == 429
                with closing(server.connect()) as db, db:
                    db.execute("UPDATE events SET ts=ts-4 WHERE kind='command'")
                control.return_value = {'matching_status': False}
                status, body = request('POST', '/lumi/api/power', '{"on":true}')
                assert status == 504 and json.loads(body)['confirmed'] is False
                assert control.call_count == 2
                assert server.status()['last_command']['result'] == 'unconfirmed'
            assert request('GET', '/lumi/state/signing-key.pem')[0] == 404
            assert request('GET', '/lumi/')[0] == 200
            assert request('GET', '/lumi/api/status')[0] == 200
            for query in ('range=bad','range=1h&range=30d','before=-1','before=999999999999999999999999'):
                assert request('GET', '/lumi/api/history?'+query)[0] == 400
            with patch('replay.get_status', side_effect=OSError('private token')):
                server.sample(now + 2)
            with patch('replay.run') as control:
                assert request('POST', '/lumi/api/power', '{"on":true}')[0] == 409
                control.assert_not_called()
        finally:
            listener.shutdown()
            listener.server_close()
            thread.join()
    with TemporaryDirectory() as directory, patch('server.DB', Path(directory) / 'old.sqlite3'):
        now = time.time()
        with closing(server.connect()) as db, db:
            db.executescript('CREATE TABLE samples(ts REAL PRIMARY KEY,on_state INTEGER,online INTEGER,voltage_v REAL,current_a REAL,power_w REAL,error TEXT); CREATE TABLE events(id INTEGER PRIMARY KEY,ts REAL,on_state INTEGER,kind TEXT,result TEXT);')
            db.executemany('INSERT INTO samples VALUES (?,1,1,220,0.2,20,NULL)', [(now - 10,), (now - 5,)])
            db.execute("INSERT INTO events VALUES (1,?,0,'command','confirmed')", (now - 9,))
        server.initialize()
        with closing(server.connect()) as db:
            rows = db.execute('SELECT power_w,error FROM samples ORDER BY ts').fetchall()
            assert tuple(rows[0]) == (20, None) and tuple(rows[1]) == (20, server.SYNC_PENDING)
        assert server.status()['sync_pending'] and server.status()['on'] is False
    with TemporaryDirectory() as directory, patch('server.DB', Path(directory) / 'month.sqlite3'):
        server.initialize()
        now = time.time()
        with closing(server.connect()) as db, db:
            db.executemany('INSERT INTO samples VALUES (?,1,1,220,0.1,20,NULL)',
                           ((now - 31 * 86400 + i * 10,) for i in range(31 * 8640)))
        good = {'on': True, 'online': True, 'metrics': {'voltage_v': 220, 'current_a': 0.1, 'power_w': 20}}
        with patch('replay.get_status', return_value=good):
            server.sample(now)
        for period in server.RANGES:
            value = server.history(period)
            assert len(value['points']) <= 602
            assert any(point['power_w'] == 20 for point in value['points'])
        assert 259199 <= value['sample_count'] <= 259201
    print('PASS: signing, metrics, persistence, retention, gaps, paging, HTTP guards, command outcomes and stale-API migration')


if __name__ == '__main__':
    check()
