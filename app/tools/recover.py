import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import replay

FILES = ('runtime.json', 'status-request.json', 'signing-key.pem', 'mqtt-peer.pem')
API_HOST = 'api.st-smart-home.lumi.vn'
API_PATH = '/device/get-status-devices'


def private_destination(path):
    path = path.resolve()
    if any((parent / '.git').exists() for parent in (path, *path.parents)):
        raise ValueError('Private output must be outside every Git checkout')
    if path.exists():
        raise ValueError('Output already exists; choose a new directory')
    return path


def read_capture(path):
    streams, certificates = {}, set()
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('Capture exceeds 64 MiB; make a shorter capture')
    for line in path.read_text().splitlines():
        event = json.loads(line)
        payload = event.get('message', {}).get('payload', {})
        if payload.get('kind') == 'certificate_seen':
            certificates.add(payload['sha256'])
        if payload.get('kind') == 'tls_plaintext' and 'data_base64' in event:
            key = (payload['ssl'], payload['direction'])
            streams.setdefault(key, bytearray()).extend(base64.b64decode(event['data_base64'], validate=True))
    return streams, certificates


def status_request(streams):
    requests = []
    for (_, direction), raw in streams.items():
        if direction != 'out' or not raw.startswith((b'GET ', b'POST ', b'PUT ', b'DELETE ')):
            continue
        data = bytes(raw)
        while data:
            head, separator, body = data.partition(b'\r\n\r\n')
            if not separator:
                break
            lines = head.decode('iso-8859-1').split('\r\n')
            headers = dict(line.split(':', 1) for line in lines[1:])
            headers = {key.lower().strip(): value.strip() for key, value in headers.items()}
            if 'transfer-encoding' in headers:
                raise ValueError('Chunked request unsupported; recapture a complete Content-Length request')
            size = int(headers.get('content-length', '0'))
            if not 0 <= size <= replay.MAX_PACKET or len(body) < size:
                break
            data = body[size:]
            if lines[0] != f'POST {API_PATH} HTTP/1.1':
                continue
            if headers.get('host') != API_HOST:
                raise ValueError('Unexpected status API host')
            selected = {key: value for key, value in headers.items() if key in (
                'authorization', 'x-lumi-api-key', 'content-type', 'accept', 'user-agent', 'x-lumi-language')}
            if not all(selected.get(key) for key in ('authorization', 'x-lumi-api-key', 'content-type')):
                raise ValueError('Status request is missing authentication headers')
            requests.append({'url': f'https://{API_HOST}{API_PATH}', 'headers': selected,
                             'payload': json.loads(body[:size])})
    if not requests:
        raise ValueError('No complete status request; open device details while capturing')
    if any(request != requests[-1] for request in requests[:-1]):
        identities = {(r['headers']['authorization'], r['payload']['homeid']) for r in requests}
        if len(identities) != 1:
            raise ValueError('Multiple sessions/homes captured; make a focused capture')
    return requests[-1]


def mqtt_records(streams):
    connections = {connection for (connection, direction), data in streams.items()
                   if direction == 'out' and data and data[0] in (0x10, 0x30, 0xc0)}
    records = []
    for (connection, direction), raw in streams.items():
        if connection not in connections:
            continue
        reader = replay.Buffer(bytes(raw))
        while reader.remaining:
            header, body = replay.read_packet(reader)
            records.append((connection, direction, header, body))
    return records


def fresh_runtime(base, streams, certificates):
    if base['certificate_sha256'] not in certificates:
        raise ValueError('Expected broker certificate was not observed in this capture')
    records = mqtt_records(streams)
    accepted = {connection for connection, direction, header, body in records
                if direction == 'in' and header == 0x20 and body == b'\x00\x00'}
    connects = [(connection, body) for connection, direction, header, body in records
                if direction == 'out' and header == 0x10 and connection in accepted]
    candidates = []
    for connection, direction, header, body in records:
        if direction != 'out' or header != 0x30:
            continue
        _, command = replay.publish(body)
        objects = command.get('objects', [])
        if command.get('cmd') != 'set' or len(objects) != 1:
            continue
        obj = objects[0]
        if obj.get('data') != [base['device_id']] or obj.get('type') != 'devices':
            continue
        if obj.get('execution') == {'command': 'OnOff', 'params': {'on': True}}:
            candidates.append((connection, body, command))
    if len(candidates) != 1:
        raise ValueError('Need exactly one ON command for the existing outlet; no command was sent by this tool')
    connection, body, command = candidates[0]
    connect = list({value for cid, value in connects if cid == connection})
    replies = []
    for cid, direction, header, response in records:
        if cid != connection or direction != 'in' or header != 0x30:
            continue
        topic, value = replay.publish(response)
        devices = [item for obj in value.get('objects', []) for item in obj.get('data', []) if isinstance(item, dict)]
        if value.get('cmd') == 'status' and value.get('reqid') == command['reqid'] and any(
                item.get('devid') == base['device_id'] and item.get('states', {}).get('OnOff', {}).get('on') is True for item in devices):
            replies.append(topic)
    if len(connect) != 1 or len(set(replies)) != 1:
        raise ValueError('Missing accepted CONNECT or matching ON confirmation in the same session')
    return {**base, 'connect': base64.b64encode(connect[0]).decode(),
            'command': base64.b64encode(body).decode(), 'response_topic': base64.b64encode(replies[0]).decode()}


def seed_runtime(apk, streams):
    build = json.loads((Path(__file__).parent / 'android-build.json').read_text())
    if hashlib.sha256(apk.read_bytes()).hexdigest() != build['apk_sha256']['com.lumi.thing.apk']:
        raise ValueError('APK differs from the verified build; reverify hooks before using it')
    commands = []
    for _, direction, header, body in mqtt_records(streams):
        if direction == 'out' and header == 0x30:
            _, command = replay.publish(body)
            objects = command.get('objects', [])
            if command.get('cmd') == 'set' and len(objects) == 1 and objects[0].get('type') == 'devices' and objects[0].get('execution') == {'command': 'OnOff', 'params': {'on': True}}:
                commands.append(objects[0]['data'])
    if len(commands) != 1 or len(commands[0]) != 1:
        raise ValueError('Need exactly one outlet in the capture')
    with zipfile.ZipFile(apk) as archive:
        config = dict(line.split('=', 1) for line in archive.read('assets/flutter_assets/.env').decode().splitlines()
                      if '=' in line and not line.lstrip().startswith('#'))
    host = config['MQTT_HOST'].strip().strip('\"\'')
    host = urllib.parse.urlsplit(host if '://' in host else '//' + host).hostname
    if not host or not host.endswith('.lumi.vn'):
        raise ValueError('Unexpected broker host')
    hook = (Path(__file__).parent / 'capture.js').read_text()
    certificate = json.loads(hook.split('const certificate = ', 1)[1].split(';', 1)[0])
    der = bytes.fromhex(certificate['hex'])
    return {'host': host, 'port': int(config['MQTT_PORT']), 'device_id': commands[0][0],
            'certificate_sha256': hashlib.sha256(der).hexdigest()}, ssl.DER_cert_to_PEM_cert(der)


def validate(directory):
    previous = replay.SECRETS
    try:
        replay.SECRETS = directory
        prepared = replay.prepare()
        command = prepared[-1]
        if 'jwt' not in command:
            command = replay.signed_command(prepared[4], prepared[5], True)[1]
    finally:
        replay.SECRETS = previous
    status = json.loads((directory / 'status-request.json').read_text())
    if status['url'] != f'https://{API_HOST}{API_PATH}':
        raise ValueError('Unexpected status URL')
    if not all(status['headers'].get(key) for key in ('authorization', 'x-lumi-api-key', 'content-type')):
        raise ValueError('Missing status credentials')
    if status['payload']['payload']['cmd'] != 'get':
        raise ValueError('Expected read-only status command')
    target = prepared[-1]['objects'][0]['data'][0]
    if not any(obj.get('type') == 'devices' and (obj.get('data') == [] or target in obj.get('data', []))
               for obj in status['payload']['payload']['objects']):
        raise ValueError('Status request does not include the captured outlet')
    header, payload, signature = command['jwt'].split('.')
    decode = lambda text: base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    if json.loads(decode(header)).get('alg') != 'RS256':
        raise ValueError('Unsupported command signature')
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        public = subprocess.run(['openssl', 'pkey', '-in', str(directory / 'signing-key.pem'), '-pubout'],
                                capture_output=True, check=True, timeout=5).stdout
        (root / 'public.pem').write_bytes(public)
        (root / 'signature').write_bytes(decode(signature))
        subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(root / 'public.pem'),
                        '-signature', str(root / 'signature')], input=f'{header}.{payload}'.encode(),
                       capture_output=True, check=True, timeout=5)
    return True


def export(args):
    output = private_destination(args.output)
    streams, certificates = read_capture(args.capture)
    status = status_request(streams)
    certificate = None
    if args.base:
        base = json.loads((args.base / 'runtime.json').read_text())
    else:
        base, certificate = seed_runtime(args.apk, streams)
    runtime = fresh_runtime(base, streams, certificates) if args.full else base
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.lumi-export-', dir=output.parent) as temporary:
        staging = Path(temporary)
        if args.base:
            for name in FILES:
                shutil.copyfile(args.base / name, staging / name)
                (staging / name).chmod(0o600)
        else:
            (staging / 'mqtt-peer.pem').write_text(certificate)
        (staging / 'status-request.json').write_text(json.dumps(status))
        (staging / 'runtime.json').write_text(json.dumps(runtime))
        if args.key_log:
            keys = [line.split('LUMI_PRIVATE_KEY_JSON=', 1)[1] for line in args.key_log.read_text().splitlines()
                    if 'LUMI_PRIVATE_KEY_JSON=' in line]
            if len(keys) != 1:
                raise ValueError('Need exactly one private-key observation')
            key = json.loads(keys[0])
            if not isinstance(key, str):
                raise ValueError('Invalid private-key observation')
            (staging / 'signing-key.pem').write_text(key)
        validate(staging)
        output.mkdir(mode=0o700)
        for name in FILES:
            shutil.copyfile(staging / name, output / name)
            (output / name).chmod(0o600)
    print('Exported four private files; offline signature/schema checks passed. Live authentication is not verified.')


def main():
    parser = argparse.ArgumentParser(description='Offline Lumi credential recovery; never connects or switches the outlet.')
    sub = parser.add_subparsers(dest='action', required=True)
    check = sub.add_parser('validate')
    check.add_argument('directory', type=Path)
    generate = sub.add_parser('export')
    origin = generate.add_mutually_exclusive_group(required=True)
    origin.add_argument('--base', type=Path)
    origin.add_argument('--apk', type=Path)
    generate.add_argument('--capture', type=Path, required=True)
    generate.add_argument('--output', type=Path, required=True)
    generate.add_argument('--full', action='store_true')
    generate.add_argument('--key-log', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == 'validate':
            validate(args.directory)
            print('Offline signature/schema checks passed; live authentication is not verified.')
        else:
            if args.key_log and not args.full:
                parser.error('--key-log requires --full')
            if args.apk and not (args.full and args.key_log):
                parser.error('--apk requires --full and --key-log')
            export(args)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, EOFError, zipfile.BadZipFile, subprocess.SubprocessError):
        print('Recovery failed; no credentials printed. Check input completeness, pinned version and guide.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
