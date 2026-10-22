import base64
import json
import os
from pathlib import Path
import signal
import sys
import time

import frida

root = Path(__file__).resolve().parent
output = Path(sys.argv[1]).resolve()
if any((parent / '.git').exists() for parent in (output, *output.parents)):
    raise SystemExit('Capture output must be outside every Git checkout')
output.mkdir(mode=0o700, exist_ok=False)
os.umask(0o077)
stop = False
count = 0


def shutdown(signum, frame):
    global stop
    stop = True


for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, shutdown)

with (output / 'events.jsonl').open('w') as stream:
    def on_message(message, data):
        global count, stop
        event = {'message': message}
        if data is not None:
            event['data_base64'] = base64.b64encode(data).decode('ascii')
            count += 1
        stream.write(json.dumps(event) + '\n')
        stream.flush()
        if message.get('type') == 'error':
            print('Hook error; see private events file', flush=True)
            stop = True
        elif message.get('payload', {}).get('kind') == 'ready':
            print('Capture ready: runtime instruction checks passed', flush=True)
        elif data is not None:
            print(f"Captured {count}: {message['payload']['direction']} {len(data)} bytes", flush=True)

    device = frida.get_device_manager().add_remote_device('127.0.0.1:27043')
    spawn = not sys.argv[2].isdigit()
    pid = device.spawn(sys.argv[2]) if spawn else int(sys.argv[2])
    session = device.attach(pid)
    try:
        script = session.create_script((root / 'capture.js').read_text())
        script.on('message', on_message)
        script.load()
        if spawn:
            device.resume(pid)
        deadline = time.monotonic() + (int(sys.argv[3]) if len(sys.argv) > 3 else 900)
        while not stop and time.monotonic() < deadline:
            time.sleep(0.2)
    finally:
        session.detach()
        print(f'Detached; captured {count} records', flush=True)
