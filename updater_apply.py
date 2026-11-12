'''
updater_apply.py — standalone applier for signed Leiturgia updates.

Compiled to updater.exe. It runs as its own process so it can replace the
running LeiturgiaServer.exe / Leiturgia.exe after they are stopped.

Invoked by the server (from app.py) as:
    updater.exe apply <install_dir> <stage_dir> <manifest>

It:
  1. Stops all Leiturgia processes and waits for them to exit.
  2. Backs up per-machine / user files (license.dat, config.json, data/, media/,
     output/) from the install dir.
  3. Clears everything else and copies in the new signed bundle.
  4. Restores the backed-up files (so the machine licence and data survive).
  5. Writes data/update/status.json (success/error).
  6. Relaunches Leiturgia.exe.
'''
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from updater import canonical, _copy_tree, verify


def _log(msg):
    print('[updater] %s' % msg, flush=True)


def _wait_processes_gone(names, timeout=30):
    names = [n.lower() for n in names]
    deadline = time.time() + timeout
    while time.time() < deadline:
        running = []
        out = subprocess.run(
            ['tasklist', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, timeout=10,
        ).stdout.lower()
        for n in names:
            if n in out:
                running.append(n)
        if not running:
            return
        time.sleep(0.5)


def _stop_all():
    for exe in ('Leiturgia.exe', 'LeiturgiaServer.exe'):
        subprocess.run(
            ['taskkill', '/IM', exe, '/T', '/F'],
            capture_output=True, timeout=15,
        )
    _wait_processes_gone(['Leiturgia.exe', 'LeiturgiaServer.exe'], timeout=30)


def _read_manifest(path):
    with open(path) as f:
        return json.load(f)


def _rel_target(install_dir, rel):
    parts = rel.split('/') if '/' in rel else rel.split('\\')
    return os.path.join(install_dir, *parts)


def apply_update(install_dir, stage_dir, manifest_path):
    _log('stopping Leiturgia processes')
    _stop_all()

    manifest = _read_manifest(manifest_path)
    backup = os.path.join(os.path.dirname(install_dir), '.leiturgia_backup_%d' % int(time.time()))
    os.makedirs(backup, exist_ok=True)

    # Step 2: back up per-machine / user files.
    for name in ('license.dat', 'config.json'):
        src = os.path.join(install_dir, name)
        if not os.path.exists(src):
            continue
        _log('saving %s' % name)
        _copy_tree(src, os.path.join(backup, name))
    for d in ('data', 'media', 'output'):
        src = os.path.join(install_dir, d)
        if not os.path.isdir(src):
            continue
        _log('saving %s/' % d)
        _copy_tree(src, os.path.join(backup, d))

    # Step 3: clear everything in the install dir except the preserved
    # user files, then copy in the new signed bundle.
    try:
        for name in os.listdir(install_dir):
            if name in ('data', 'media', 'output', 'license.dat', 'config.json'):
                continue
            full = os.path.join(install_dir, name)
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                os.unlink(full)
    except OSError as e:
        _log('clear error: %s' % e)

    for entry in manifest['files']:
        rel, size, sha = entry['path'], entry['size'], entry['sha256']
        parts = rel.split('/') if '/' in rel else rel.split('\\')
        src = os.path.join(stage_dir, *parts)
        dst = _rel_target(install_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, 'rb') as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError('sha mismatch for %s at apply time' % rel)
        with open(dst, 'wb') as f:
            f.write(data)
        _log('installed %s' % rel)

    # Step 4: restore backed-up user files.
    for name in ('license.dat', 'config.json'):
        src = os.path.join(backup, name)
        if not os.path.exists(src):
            continue
        _copy_tree(src, os.path.join(install_dir, name))
    for d in ('data', 'media', 'output'):
        src = os.path.join(backup, d)
        if not os.path.isdir(src):
            continue
        _copy_tree(src, os.path.join(install_dir, d))

    shutil.rmtree(backup, ignore_errors=True)

    # Step 5: write success status.
    _write_status(install_dir, {
        'status': 'success',
        'target_version': manifest['version'],
        'ts': time.time(),
    })
    _clear_lock(install_dir)

    # Step 6: relaunch the app.
    launcher = os.path.join(install_dir, 'Leiturgia.exe')
    if os.path.exists(launcher):
        _log('relaunching app')
        subprocess.Popen([launcher], cwd=install_dir)
    _log('done')


def _write_status(install_dir, payload):
    d = os.path.join(install_dir, 'data', 'update')
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'status.json'), 'w') as f:
        json.dump(payload, f)


def _clear_lock(install_dir):
    """Remove the update lock file. The server creates data/update/lock when an
    update starts; the applier must release it once the apply finishes (success
    or failure), otherwise every later update is blocked with
    'update already in progress'."""
    try:
        os.unlink(os.path.join(install_dir, 'data', 'update', 'lock'))
    except OSError:
        pass


def main():
    if len(sys.argv) < 5 or sys.argv[1] != 'apply':
        _log('usage: updater.exe apply <install_dir> <stage_dir> <manifest_json>')
        sys.exit(2)

    install_dir = sys.argv[2]
    stage_dir = sys.argv[3]
    manifest_path = sys.argv[4]

    try:
        apply_update(install_dir, stage_dir, manifest_path)
        sys.exit(0)
    except Exception as e:
        _log('UPDATE FAILED: %s' % e)
        _write_status(install_dir, {
            'status': 'error',
            'message': str(e),
            'ts': time.time(),
        })
        _clear_lock(install_dir)
        sys.exit(1)


if __name__ == '__main__':
    main()
