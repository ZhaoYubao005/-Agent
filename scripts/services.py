"""Start/check/stop only this project's services. Run from the checkout root."""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
STATE = RUNTIME / 'processes.json'
PYTHON = ROOT / '.venv/bin/python'
WRAPPER = ROOT / 'scripts/run-with-env.py'


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def ready(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=3) as response:
            return response.status == 200
    except (OSError, TimeoutError):
        return False


def main():
    os.chdir(ROOT)
    RUNTIME.mkdir(exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    action = sys.argv[1] if len(sys.argv) > 1 else 'start'
    services = {
        'medusa': (ROOT / 'commerce/apps/backend', ['npm', 'run', 'dev'], 'http://127.0.0.1:9000/health'),
        'agent': (ROOT, [str(PYTHON), '-m', 'uvicorn', 'ecomops.app:app', '--host', '127.0.0.1', '--port', '8100'],
                  'http://127.0.0.1:8100/health'),
    }
    if action == 'stop':
        for name, entry in state.items():
            # Linux: verify this process still owns the saved process-group and
            # its command includes our absolute wrapper path before signaling it.
            proc = Path(f'/proc/{entry["pid"]}/cmdline')
            if proc.exists() and str(WRAPPER).encode() in proc.read_bytes():
                os.killpg(entry['pid'], signal.SIGTERM)
                print(f'Stopped {name}')
        for _ in range(30):
            if not any(ready(services[name][2]) for name in state):
                STATE.unlink(missing_ok=True)
                return
            time.sleep(1)
        raise SystemExit('Services still respond; inspect owned processes before restart')
    if action not in ('start', 'status'):
        raise SystemExit('Use start, status or stop')
    for name, (cwd, command, url) in services.items():
        if action == 'start':
            entry = state.get(name)
            if not entry or not alive(entry['pid']):
                if ready(url):
                    raise SystemExit(f'{name}: existing unmanaged listener; stop it before starting managed services')
                log = (RUNTIME / f'{name}.log').open('ab')
                process = subprocess.Popen([str(PYTHON), str(WRAPPER), *command], cwd=cwd,
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                log.close()
                state[name] = {'pid': process.pid}
                STATE.write_text(json.dumps(state))
        for _ in range(60 if action == 'start' else 1):
            if ready(url):
                print(f'{name}: HTTP 200')
                break
            if action == 'start' and not alive(state[name]['pid']):
                raise SystemExit(f'{name} exited; inspect .runtime/{name}.log')
            time.sleep(1)
        else:
            raise SystemExit(f'{name} not ready; inspect .runtime/{name}.log')
    if not ready('http://127.0.0.1:9000/app'):
        raise SystemExit('Admin HTML did not respond')
    print('Admin HTML: HTTP 200. Run pytest for functional business validation.')


if __name__ == '__main__':
    main()
