'''
updater_apply.py — standalone applier for signed Leiturgia updates.

Compiled to updater.exe. It runs as its own process so it can replace the
running LeiturgiaServer.exe / Leiturgia.exe after they are stopped.

Invoked by the server (from app.py) as:
    updater.exe apply <install_dir> <stage_dir> <manifest>

It:
  1. Verifies every staged file against the manifest (nothing is touched yet).
  2. Stops all Leiturgia processes and waits for them to exit.
  3. Backs up per-machine / user files (license.dat, config.json, data/, media/,
     output/) from the install dir.
  4. Clears everything else (except this applier) and copies in the new bundle.
  5. Restores the backed-up files (so the machine licence and data survive).
  6. Writes data/update/status.json (success/error) and releases the lock.
  7. Relaunches Leiturgia.exe.

Every step is logged to data/update/apply.log. Any failure, including ones
before the main logic runs, is written to status.json so it never stays stuck
on "applying".
'''
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

PRESERVE_FILES = ('license.dat', 'config.json')
PRESERVE_DIRS = ('data', 'media', 'output')

_install_dir = None


def _log(msg):
    line = '[updater] %s' % msg
    print(line, flush=True)
    if _install_dir:
        try:
            d = os.path.join(_install_dir, 'data', 'update')
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'apply.log'), 'a', encoding='utf-8') as f:
                f.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), line))
        except OSError:
            pass


def _wait_processes_gone(names, timeout=30):
    names = {n.lower() for n in names}
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = subprocess.run(
            ['tasklist', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, timeout=10,
        )
        running = set()
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            # tasklist CSV: "Image Name","PID","Session Name",...
            # Parse the first quoted field robustly.
            if line.startswith('"'):
                end = line.find('"', 1)
                image = line[1:end] if end > 1 else ''
            else:
                image = line.split(',')[0].strip().strip('"')
            running.add(image.lower())
        if not (names & running):
            return True
        time.sleep(0.5)
    return False


def _stop_all():
    for exe in ('Leiturgia.exe', 'LeiturgiaServer.exe'):
        subprocess.run(
            ['taskkill', '/IM', exe, '/T', '/F'],
            capture_output=True, timeout=15,
        )
    if not _wait_processes_gone(['Leiturgia.exe', 'LeiturgiaServer.exe'], timeout=30):
        raise RuntimeError('Leiturgia processes did not exit in time')


def _read_manifest(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _split(rel):
    # Reject path traversal / absolute / drive-letter entries.
    s = str(rel).replace('\\\\', '/')
    if s.startswith('/') or s.startswith('\\\\'):
        raise ValueError('unsafe path in manifest: %r' % rel)
    parts = s.split('/')
    if any(p in ('', '.', '..') for p in parts):
        raise ValueError('unsafe path in manifest: %r' % rel)
    if len(parts[0]) >= 2 and parts[0][1] == ':':
        raise ValueError('unsafe path in manifest: %r' % rel)
    return parts


def _safe_join(base, rel):
    parts = _split(rel)
    base_real = os.path.realpath(base)
    dest = os.path.join(base, *parts)
    dest_real = os.path.realpath(os.path.dirname(dest)) if not os.path.exists(dest) else os.path.realpath(dest)
    # ensure parent containment even before file exists
    parent_real = os.path.realpath(os.path.dirname(dest))
    if parent_real != base_real and not parent_real.startswith(base_real + os.sep):
        raise ValueError('path escapes base directory: %r' % rel)
    return dest, parts


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _verify_stage(manifest, stage_dir):
    from updater import verify
    files = [(f['path'], f['size'], f['sha256']) for f in manifest['files']]
    if not manifest.get('signature') or not verify(files, manifest['version'], manifest['signature']):
        raise RuntimeError('update signature verification FAILED at apply time')
    stage_real = os.path.realpath(stage_dir)
    for entry in manifest['files']:
        src, _parts = _safe_join(stage_dir, entry['path'])
        if not os.path.isfile(src):
            raise RuntimeError('missing staged file: %s' % entry['path'])
        if not os.path.realpath(src).startswith(stage_real + os.sep) and os.path.realpath(src) != stage_real:
            raise RuntimeError('staged path escapes stage dir: %s' % entry['path'])
        if os.path.getsize(src) != entry['size']:
            raise RuntimeError('size mismatch for %s' % entry['path'])
        if _sha256(src) != entry['sha256']:
            raise RuntimeError('sha mismatch for %s' % entry['path'])
    _log('stage verified: %d files' % len(manifest['files']))


def _copy_dir(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


def _clear_install(install_dir, keep):
    self_name = os.path.basename(sys.executable).lower()
    for name in os.listdir(install_dir):
        if name in keep or name.lower() == self_name:
            continue
        full = os.path.join(install_dir, name)
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full)
            else:
                os.unlink(full)
        except OSError as e:
            _log('could not remove %s: %s' % (name, e))


def apply_update(install_dir, stage_dir, manifest_path):
    manifest = _read_manifest(manifest_path)
    _log('applying %s from %s' % (manifest['version'], stage_dir))

    _verify_stage(manifest, stage_dir)

    _log('stopping Leiturgia processes')
    _stop_all()

    backup = os.path.join(os.path.dirname(install_dir), '.leiturgia_backup_%d' % int(time.time()))
    os.makedirs(backup, exist_ok=True)
    for name in PRESERVE_FILES + PRESERVE_DIRS:
        src = os.path.join(install_dir, name)
        if os.path.exists(src):
            _log('saving %s' % name)
            _copy_dir(src, os.path.join(backup, name))

    _log('clearing install folder')
    _clear_install(install_dir, keep=set(PRESERVE_FILES + PRESERVE_DIRS))

    install_real = os.path.realpath(install_dir)
    stage_real = os.path.realpath(stage_dir)
    for entry in manifest['files']:
        src, _parts = _safe_join(stage_dir, entry['path'])
        dst, _parts2 = _safe_join(install_dir, entry['path'])
        if not os.path.realpath(src).startswith(stage_real + os.sep) and os.path.realpath(src) != stage_real:
            raise RuntimeError('staged path escapes stage dir: %s' % entry['path'])
        if not os.path.realpath(os.path.dirname(dst) or install_dir).startswith(install_real + os.sep) and os.path.realpath(os.path.dirname(dst) or install_dir) != install_real:
            raise RuntimeError('install path escapes install dir: %s' % entry['path'])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        _log('installed %s' % entry['path'])

    for name in PRESERVE_FILES + PRESERVE_DIRS:
        src = os.path.join(backup, name)
        if os.path.exists(src):
            _log('restoring %s' % name)
            _copy_dir(src, os.path.join(install_dir, name))

    shutil.rmtree(backup, ignore_errors=True)

    _write_status(install_dir, {
        'status': 'success',
        'target_version': manifest['version'],
        'ts': time.time(),
    })
    _clear_lock(install_dir)

    launcher = os.path.join(install_dir, 'Leiturgia.exe')
    if os.path.exists(launcher):
        _log('relaunching app')
        subprocess.Popen([launcher], cwd=install_dir)
    _log('done')


def _write_status(install_dir, payload):
    d = os.path.join(install_dir, 'data', 'update')
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'status.json'), 'w', encoding='utf-8') as f:
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
    global _install_dir
    if len(sys.argv) < 5 or sys.argv[1] != 'apply':
        print('[updater] usage: updater.exe apply <install_dir> <stage_dir> <manifest_json>', flush=True)
        sys.exit(2)

    install_dir = sys.argv[2]
    stage_dir = sys.argv[3]
    manifest_path = sys.argv[4]
    _install_dir = install_dir

    try:
        apply_update(install_dir, stage_dir, manifest_path)
    except BaseException as e:
        tb = traceback.format_exc()
        _log('UPDATE FAILED: %s\n%s' % (e, tb))
        try:
            _write_status(install_dir, {
                'status': 'error',
                'message': str(e) or type(e).__name__,
                'ts': time.time(),
            })
            _clear_lock(install_dir)
        except OSError:
            pass
        if isinstance(e, SystemExit):
            raise
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
