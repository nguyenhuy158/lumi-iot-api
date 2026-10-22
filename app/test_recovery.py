import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import ssl
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from tools import recover


def check():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        base = root / 'base'
        base.mkdir()
        key = base / 'signing-key.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(key)],
                       capture_output=True, check=True)
        certificate = json.loads((Path(__file__).parent / 'tools/capture.js').read_text().split('const certificate = ', 1)[1].split(';', 1)[0])
        der = bytes.fromhex(certificate['hex'])
        (base / 'mqtt-peer.pem').write_text(ssl.DER_cert_to_PEM_cert(der))
        request = {'cmd': 'set', 'reqid': 'phone-1700000000000-1', 'source': 'phone-1700000000000-1',
                   'control_source': {'id': 'phone-1700000000000-1'},
                   'objects': [{'type': 'devices', 'data': ['synthetic-outlet'],
                                'execution': {'command': 'OnOff', 'params': {'on': True}}}]}
        body = recover.replay.field(b'control') + json.dumps(request).encode()
        previous = recover.replay.SECRETS
        try:
            recover.replay.SECRETS = base
            body, request = recover.replay.signed_command(b'', body, True)
        finally:
            recover.replay.SECRETS = previous
        connect = recover.replay.field(b'MQIsdp') + b'\x03\xc2\x00\x3c' + recover.replay.field(b'fake-client')
        runtime = {'host': 'test.lumi.vn', 'port': 8883, 'device_id': 'synthetic-outlet',
                   'certificate_sha256': hashlib.sha256(der).hexdigest(),
                   **{name: base64.b64encode(value).decode() for name, value in
                      [('connect', connect), ('command', body), ('response_topic', b'status')]}}
        (base / 'runtime.json').write_text(json.dumps(runtime))
        payload = {'homeid': 'synthetic-home', 'payload': {'cmd': 'get', 'reqid': 'phone-1700000000000-1',
                   'objects': [{'type': 'devices', 'data': []}]}}
        status = {'url': f'https://{recover.API_HOST}{recover.API_PATH}',
                  'headers': {'authorization': 'synthetic-token', 'x-lumi-api-key': 'synthetic-api-key', 'content-type': 'application/json'},
                  'payload': payload}
        (base / 'status-request.json').write_text(json.dumps(status))
        assert recover.validate(base)
        content = json.dumps(payload).encode()
        http = (f'POST {recover.API_PATH} HTTP/1.1\r\nHost: {recover.API_HOST}\r\nContent-Length: {len(content)}\r\n'
                'Authorization: synthetic-token\r\nX-Lumi-Api-Key: synthetic-api-key\r\nContent-Type: application/json\r\n\r\n').encode() + content
        reply = {'cmd': 'status', 'reqid': request['reqid'], 'objects': [{'data': [{'devid': 'synthetic-outlet', 'states': {'OnOff': {'on': True}}}]}]}
        streams = {('http', 'out'): http, ('mqtt', 'out'): recover.replay.packet(0x10, connect) * 2 + recover.replay.packet(0x30, body),
                   ('mqtt', 'in'): b'\x20\x02\x00\x00' * 2 + recover.replay.packet(0x30, recover.replay.field(b'status') + json.dumps(reply).encode())}
        events = [{'message': {'payload': {'kind': 'certificate_seen', 'sha256': runtime['certificate_sha256']}}}]
        for (connection, direction), data in streams.items():
            for chunk in (data[:7], data[7:]):
                events.append({'message': {'payload': {'kind': 'tls_plaintext', 'ssl': connection, 'direction': direction}},
                               'data_base64': base64.b64encode(chunk).decode()})
        capture = root / 'events.jsonl'
        capture.write_text('\n'.join(json.dumps(e) for e in events))
        parsed, certs = recover.read_capture(capture)
        assert recover.status_request(parsed) == status
        assert recover.fresh_runtime(runtime, parsed, certs) == runtime
        for full in (False, True):
            args = SimpleNamespace(base=base, capture=capture, output=root / str(full), full=full, key_log=None)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                recover.export(args)
            assert 'synthetic-token' not in output.getvalue()
            assert args.output.stat().st_mode & 0o777 == 0o700
            assert all((args.output / name).stat().st_mode & 0o777 == 0o600 for name in recover.FILES)
            assert recover.validate(args.output)
        for operation in (
            lambda: recover.private_destination(root / 'True'),
            lambda: recover.private_destination(Path(__file__).parent / 'private-test'),
            lambda: recover.status_request({('http', 'out'): http[:-1]}),
            lambda: recover.fresh_runtime(runtime, parsed, set()),
            lambda: recover.fresh_runtime({**runtime, 'device_id': 'wrong'}, parsed, certs),
            lambda: recover.status_request({('http', 'out'): http.replace(recover.API_HOST.encode(), b'evil.invalid')}),
        ):
            try:
                operation()
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid input accepted')
        (base / 'signing-key.pem').write_text('invalid-key')
        try:
            recover.validate(base)
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError('Invalid key accepted')
    print('PASS: fragmented capture, status/full export, repeated CONNECT, permissions, key verification and rejection guards')


if __name__ == '__main__':
    check()
