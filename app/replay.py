import argparse
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
SECRETS = Path(os.environ.get('LUMI_SECRETS', '/run/lumi'))
MAX_PACKET = 1048576


def encode_length(value):
    if not 0 <= value <= MAX_PACKET:
        raise ValueError('MQTT packet exceeds capture limit')
    result = bytearray()
    while True:
        digit = value % 128
        value //= 128
        result.append(digit | (128 if value else 0))
        if not value:
            return bytes(result)


def packet(header, body):
    return bytes([header]) + encode_length(len(body)) + body


def field(value):
    if len(value) > 65535:
        raise ValueError('MQTT string too long')
    return struct.pack('>H', len(value)) + value


def read_field(data, offset):
    if offset + 2 > len(data):
        raise ValueError('Truncated MQTT string length')
    size = int.from_bytes(data[offset:offset + 2], 'big')
    start = offset + 2
    if start + size > len(data):
        raise ValueError('Truncated MQTT string')
    return data[start:start + size], start + size


def read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        chunk = stream.recv(size - len(result))
        if not chunk:
            raise EOFError('MQTT connection closed')
        result.extend(chunk)
    return bytes(result)


def read_packet(stream):
    header = read_exact(stream, 1)[0]
    size = 0
    for index in range(4):
        digit = read_exact(stream, 1)[0]
        size += (digit & 127) << (7 * index)
        if size > MAX_PACKET:
            raise ValueError('MQTT packet exceeds capture limit')
        if not digit & 128:
            return header, read_exact(stream, size)
    raise ValueError('Invalid MQTT remaining length')


class Buffer:
    def __init__(self, value, chunk=MAX_PACKET):
        self.remaining = bytes(value)
        self.chunk = chunk

    def recv(self, size):
        size = min(size, self.chunk)
        value, self.remaining = self.remaining[:size], self.remaining[size:]
        return value


def publish(body):
    topic, offset = read_field(body, 0)
    return topic, json.loads(body[offset:])


def refresh_request(body, now_ms):
    topic, command = publish(body)
    prefix, timestamp, counter = command['reqid'].rsplit('-', 2)
    if len(timestamp) != 13 or not timestamp.isdigit() or not counter.isdigit():
        raise ValueError('Unexpected request ID format')
    command['reqid'] = f'{prefix}-{max(now_ms, int(timestamp) + 1)}-{int(counter) + 1}'
    return field(topic) + json.dumps(command, separators=(',', ':')).encode(), command


def prepare():
    config = json.loads((SECRETS / 'runtime.json').read_text())
    connect = base64.b64decode(config['connect'], validate=True)
    body = base64.b64decode(config['command'], validate=True)
    topic = base64.b64decode(config['response_topic'], validate=True)
    protocol, offset = read_field(connect, 0)
    if protocol != b'MQIsdp' or connect[offset:offset + 2] != b'\x03\xc2':
        raise ValueError('Unexpected MQTT connection')
    _, command = publish(body)
    objects = command.get('objects', [])
    if command.get('cmd') != 'set' or len(objects) != 1:
        raise ValueError('Unexpected command scope')
    obj = objects[0]
    if obj.get('type') != 'devices' or obj.get('data') != [config['device_id']]:
        raise ValueError('Unexpected device scope')
    if obj.get('execution') != {'command': 'OnOff', 'params': {'on': True}}:
        raise ValueError('Unexpected command type')
    cert = SECRETS / 'mqtt-peer.pem'
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    if not hmac.compare_digest(hashlib.sha256(der).hexdigest(), config['certificate_sha256']):
        raise ValueError('Certificate pin mismatch')
    return config['host'], int(config['port']), cert, der, connect, body, topic, command


def signed_command(connect, body, on):
    if type(on) is not bool:
        raise ValueError('on must be a boolean')
    original_topic, original = publish(body)
    body, command = refresh_request(body, time.time_ns() // 1000000)
    for key in ('source',):
        if command.get(key) == original['reqid']:
            command[key] = command['reqid']
    if command['control_source'].get('id') == original['reqid']:
        command['control_source']['id'] = command['reqid']
    command['objects'][0]['execution']['params']['on'] = on
    encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b'=')
    signing_input = encode(b'{"alg":"RS256","typ":"JWT"}') + b'.' + encode(
        json.dumps({'iat': int(time.time())}, separators=(',', ':')).encode())
    signature = subprocess.run(
        ['openssl', 'dgst', '-sha256', '-sign', str(SECRETS / 'signing-key.pem')],
        input=signing_input, capture_output=True, timeout=5, check=True).stdout
    command['jwt'] = (signing_input + b'.' + encode(signature)).decode()
    return field(original_topic) + json.dumps(command, separators=(',', ':')).encode(), command


def get_status():
    config = json.loads((SECRETS / 'status-request.json').read_text())
    payload = config['payload']['payload']
    prefix, _, counter = payload['reqid'].rsplit('-', 2)
    payload['reqid'] = f'{prefix}-{time.time_ns() // 1000000}-{int(counter) + 1}'
    target = prepare()[-1]['objects'][0]['data'][0]
    request = urllib.request.Request(config['url'], headers=config['headers'],
                                     data=json.dumps(config['payload']).encode())
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.load(response)
    if value.get('success') is not True:
        raise RuntimeError('Status API rejected request')
    devices = [d for d in value.get('data', []) if d.get('devid') == target]
    if len(devices) != 1:
        raise RuntimeError('Target device missing from status response')
    device = devices[0]
    states = device.get('states')
    states = states if isinstance(states, dict) else {}
    power_state = states.get('OnOff')
    on = power_state.get('on') if isinstance(power_state, dict) else None
    metrics = {}
    for name, group, key in (('voltage_v', 'Voltage', 'voltage'),
                             ('current_a', 'Ampe', 'ampe'), ('power_w', 'Power', 'power')):
        state = states.get(group)
        value = state.get(key) if isinstance(state, dict) else None
        try:
            metrics[name] = value if type(value) in (int, float) and math.isfinite(value) else None
        except OverflowError:
            metrics[name] = None
    return {'on': on if type(on) is bool else None, 'status_code': device.get('status'),
            'online': True if device.get('status') == 1 else None,
            'metrics': metrics, 'checked_at': time.time(), 'source': 'Lumi status API'}


def run(send_once, fresh_request=False, on=True, signed=False):
    if not signed and on is not True:
        raise ValueError('Use a signed command to change the captured power state')
    host, port, cert, der, connect, body, topic, command = prepare()
    if signed:
        body, command = signed_command(connect, body, on)
    elif fresh_request:
        body, command = refresh_request(body, time.time_ns() // 1000000)
    if not send_once:
        return {'mode': 'dry-run', 'captured_command': 'OnOff', 'on': True,
                'device_count': 1, 'app_observed_certificate': True, 'publish_count': 0,
                'fresh_request': fresh_request}
    adb = Path('/Users/huyntq/Library/Android/sdk/platform-tools/adb')
    if adb.exists():
        running = subprocess.run(
            [str(adb), '-s', 'emulator-5554', 'shell', 'pidof', 'com.lumi.thing'],
            capture_output=True, text=True, timeout=10)
        if running.stdout.strip():
            raise RuntimeError('Close Lumi in the emulator before controlling from the web')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert))
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    result = {'mode': 'signed-command' if signed else 'fresh-request' if fresh_request else 'exact-replay',
              'publish_count': 0, 'matching_status': False, 'received_packets': 0}
    try:
        with socket.create_connection((host, port), timeout=8) as raw:
            with context.wrap_socket(raw, server_hostname=host) as stream:
                if not hmac.compare_digest(stream.getpeercert(binary_form=True), der):
                    raise ssl.SSLError('App-observed certificate pin mismatch')
                result['certificate_verified_and_pinned'] = True
                stream.sendall(packet(0x10, connect))
                connack = read_packet(stream)
                if connack[0] == 0x20 and len(connack[1]) == 2:
                    result['connack_code'] = connack[1][1]
                if connack != (0x20, b'\x00\x00'):
                    raise RuntimeError('Broker rejected CONNECT')
                result['broker_connected'] = True
                stream.sendall(packet(0x82, b'\x00\x01' + field(topic) + b'\x00'))
                if read_packet(stream) != (0x90, b'\x00\x01\x00'):
                    raise RuntimeError('Broker rejected subscription')
                result['subscribed'] = True
                result['publish_attempted'] = True
                stream.sendall(packet(0x30, body))
                result['publish_count'] = 1
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    stream.settimeout(max(0.1, deadline - time.monotonic()))
                    header, response = read_packet(stream)
                    result['received_packets'] += 1
                    if header != 0x30:
                        continue
                    response_topic, status = publish(response)
                    if response_topic != topic or status.get('cmd') != 'status' or status.get('reqid') != command['reqid']:
                        continue
                    target = command['objects'][0]['data'][0]
                    states = [d.get('states', {}) for obj in status.get('objects', [])
                              for d in obj.get('data', []) if isinstance(d, dict) and d.get('devid') == target]
                    if any(state.get('OnOff', {}).get('on') is on for state in states):
                        result['matching_status'] = True
                        result['reported_on'] = on
                        break
                stream.sendall(b'\xe0\x00')
        if not result['matching_status']:
            result['error'] = 'No matching device status received; do not automatically resend'
    except (OSError, EOFError, ValueError, RuntimeError) as error:
        result['error_type'] = type(error).__name__
        result['error'] = 'Replay stopped; no automatic retries'
        if isinstance(error, RuntimeError):
            result['reason'] = str(error)
    return result


def self_test():
    for size in (0, 1, 127, 128, 16383, 16384, MAX_PACKET):
        body = b'x' * size
        assert read_packet(Buffer(packet(0x30, body), 13)) == (0x30, body)
    for data in (b'', b'\x30\x80', b'\x30\xff\xff\xff\xff', b'\x30\x02x'):
        try:
            read_packet(Buffer(data, 1))
        except (EOFError, ValueError):
            pass
        else:
            raise AssertionError('Invalid packet accepted')
    assert read_field(field(b'topic'), 0) == (b'topic', 7)
    assert publish(field(b'topic') + b'{"cmd":"status"}') == (b'topic', {'cmd': 'status'})
    original = {'reqid': 'test-phone-1700000000000-1', 'source': 'keep-source',
                'objects': [{'execution': {'command': 'OnOff', 'params': {'on': True}}}]}
    body = field(b'topic') + json.dumps(original).encode()
    refreshed, command = refresh_request(body, 1800000000000)
    assert command['reqid'] == 'test-phone-1800000000000-2'
    assert publish(refreshed)[0] == b'topic'
    assert {k: v for k, v in command.items() if k != 'reqid'} == {k: v for k, v in original.items() if k != 'reqid'}
    print('MQTT framing and truncated-input checks passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--send-once', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--fresh-request', action='store_true')
    parser.add_argument('--signed', action='store_true')
    parser.add_argument('--on', choices=['true', 'false'], default='true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.status:
        print(json.dumps(get_status()))
    else:
        result = run(args.send_once, args.fresh_request, args.on == 'true', args.signed)
        print(json.dumps(result))
