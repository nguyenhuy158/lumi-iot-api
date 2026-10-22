import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import server


def check():
    now = time.time()
    with TemporaryDirectory() as directory, patch.object(server, 'HOST_STATUS', Path(directory) / 'status.json'):
        assert server.host_status()['state'] == 'unknown'
        for state in ('online', 'checking', 'unreachable', 'unknown'):
            server.HOST_STATUS.write_text(json.dumps({'state': state, 'checked_at': now, 'last_seen_at': now - 10}))
            assert server.host_status() == {'state': state, 'checked_at': now, 'last_seen_at': now - 10, 'stale': False}
        for checked in (now - 31, now + 60, float('nan'), float('inf'), True, 'now', None):
            server.HOST_STATUS.write_text(json.dumps({'state': 'online', 'checked_at': checked}))
            assert server.host_status()['state'] == 'unknown'
        for body in ('{', 'null', '[]', '{}', '{"state":"online","checked_at":1,"last_seen_at":{}}'):
            server.HOST_STATUS.write_text(body)
            assert server.host_status()['state'] == 'unknown'
    print('host status: live, stale, missing and invalid data checks passed')


if __name__ == '__main__':
    check()
