"""audio_input.py -- Additive USB/AUX audio input endpoints (dev, not compiled).

The compiled app.pyc has no audio-input API: the broadcast encoder only reads
``data/broadcast.json["audio_device"]`` (or auto-detects) and there is no way
to list or choose an AUX/USB source, nor to monitor one on the PC speakers.

This module adds, without touching compiled code:

* ``GET  /api/audio/inputs`` -- DirectShow audio input names (cached ~30s,
  ``?rescan=1`` forces a fresh ffmpeg enumeration), plus the currently
  selected broadcast device and monitor state.
* ``POST /api/audio/select`` -- ``{"device": name}`` persists the broadcast
  audio source into broadcast.json (applies on the NEXT broadcast start;
  a running broadcast is never disturbed).
* ``POST /api/audio/monitor`` -- ``{"on": bool, "device": name}`` plays /
  stops the chosen input on the PC speakers via ffplay (independent of any
  broadcast; shares the device fine with the encoder).
"""

import json
import os
import re
import subprocess
import threading
import time

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_BROADCAST_FILE = os.path.join(_BASE_DIR, "data", "broadcast.json")
_FFMPEG = os.path.join(_BASE_DIR, "tools", "ffmpeg", "bin", "ffmpeg.exe")
_FFPLAY = os.path.join(_BASE_DIR, "tools", "ffmpeg", "bin", "ffplay.exe")

_INPUTS_TTL = 30.0
_inputs_cache = {"at": 0.0, "devices": []}
_inputs_lock = threading.Lock()

_monitor_proc = None
_monitor_device = None
_monitor_lock = threading.Lock()

# ── Realtime input level analyzer (sidecar) ───────────────────────────────
# Levels come from audio_level.py, a SEPARATE stdlib-only process on
# 127.0.0.1:5005 that owns the ffmpeg ebur128 analyzer. This hub process
# never opens pipes or spawns reader threads for analysis: under eventlet,
# any blocking pipe read -- native or green -- wedged the entire server hub
# (proven via py-spy stack dumps during development). Here the hub only does
# a bounded localhost HTTP fetch, so even a dead sidecar costs at most one
# short timeout. Never touches the broadcast encoder or finished code.
_LEVEL_PORT = 5005
_LEVEL_TIMEOUT = 2.5


def _level_sidecar_running():
    import socket as _sock
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", _LEVEL_PORT))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _level_sidecar_start():
    import sys as _sys
    try:
        flag = subprocess.CREATE_NO_WINDOW
    except Exception:
        flag = 0
    try:
        subprocess.Popen(
            [_sys.executable, os.path.join(_BASE_DIR, "audio_level.py"),
             str(_LEVEL_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flag, cwd=_BASE_DIR)
        return True
    except Exception:
        return False


def _fetch_level(device):
    import json as _json
    from urllib.parse import quote as _quote
    import urllib.request as _urlreq
    url = "http://127.0.0.1:%d/level?device=%s" % (_LEVEL_PORT,
                                                  _quote(device))
    try:
        with _urlreq.urlopen(url, timeout=_LEVEL_TIMEOUT) as r:
            return _json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def get_device_level(device):
    """Latest level dict for ``device`` via the sidecar (boots it if down)."""
    data = _fetch_level(device) if _level_sidecar_running() else None
    if data is None:
        _level_sidecar_start()
        time.sleep(1.0)
        data = _fetch_level(device)
    return data


def ensure_level_analyzer(device):
    """Sidecar analyzer serving ``device``; True when reachable.

    Kept as a thin wrapper so existing callers keep working: it only checks
    the sidecar is up (booting it on demand). All analysis state lives in
    the sidecar process -- nothing pipe/thread related stays in the hub.
    """
    if not device:
        return False
    if _level_sidecar_running():
        return True
    _level_sidecar_start()
    time.sleep(1.0)
    return _level_sidecar_running()


def _ffmpeg_exe():
    return _FFMPEG if os.path.exists(_FFMPEG) else "ffmpeg"


def _ffplay_exe():
    return _FFPLAY if os.path.exists(_FFPLAY) else "ffplay"


def _enumerate_audio_inputs():
    """DirectShow audio device names via ``ffmpeg -list_devices``."""
    try:
        p = subprocess.Popen(
            [_ffmpeg_exe(), "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out, _ = p.communicate(timeout=15)
    except Exception:
        return []
    try:
        text = out.decode("utf-8", "replace")
    except Exception:
        return []
    devs = []
    for line in text.splitlines():
        m = re.search(r'"(.*)"\s+\(audio\)', line)
        if m and m.group(1) not in devs:
            devs.append(m.group(1))
    return devs


def get_audio_inputs(rescan=False):
    """Cached audio input names; ``rescan=True`` re-runs ffmpeg (~2s)."""
    now = time.monotonic()
    with _inputs_lock:
        if (_inputs_cache["devices"] and not rescan
                and now - _inputs_cache["at"] < _INPUTS_TTL):
            return list(_inputs_cache["devices"])
    devs = _enumerate_audio_inputs()
    with _inputs_lock:
        _inputs_cache["devices"] = devs
        _inputs_cache["at"] = time.monotonic()
    return list(devs)


def _read_broadcast_cfg():
    try:
        with open(_BROADCAST_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
            return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _monitor_stop_locked():
    """Caller must hold ``_monitor_lock``."""
    global _monitor_proc, _monitor_device
    proc, _monitor_proc = _monitor_proc, None
    _monitor_device = None
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def monitor_stop():
    with _monitor_lock:
        _monitor_stop_locked()
        return None


def monitor_start(device):
    """Play ``device`` on the PC speakers. Returns the device or raises."""
    flag = 0
    try:
        import subprocess as _sp
        flag = _sp.CREATE_NO_WINDOW
    except Exception:
        flag = 0
    with _monitor_lock:
        _monitor_stop_locked()
        global _monitor_proc, _monitor_device
        cmd = [_ffplay_exe(), "-hide_banner", "-nodisp", "-vn",
               "-f", "dshow", "-i", "audio=" + device]
        try:
            _monitor_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flag)
        except Exception as e:
            _monitor_proc = None
            _monitor_device = None
            raise e
        time.sleep(0.6)
        if _monitor_proc.poll() is not None:
            _monitor_proc = None
            _monitor_device = None
            raise RuntimeError("could not open audio device")
        _monitor_device = device
        return device


def init_app(app):
    """Register the audio input endpoints on the existing app (additive)."""
    from flask import jsonify, request as _request
    from app import operator_required, atomic_write_json

    @app.route("/api/audio/inputs", methods=["GET"])
    @operator_required
    def api_audio_inputs():
        rescan = bool(_request.args.get("rescan", ""))
        cfg = _read_broadcast_cfg()
        with _monitor_lock:
            monitoring = _monitor_device
            if _monitor_proc is not None and _monitor_proc.poll() is not None:
                monitoring = None
        devs = get_audio_inputs(rescan=rescan)
        act = cfg.get("audio_devices")
        if not isinstance(act, list) or not act:
            single = cfg.get("audio_device")
            act = [single] if single else []
        return jsonify(devices=devs,
                       active=cfg.get("audio_device"),
                       actives=[d for d in act if d in devs],
                       monitoring=monitoring)

    @app.route("/api/audio/select", methods=["POST"])
    @operator_required
    def api_audio_select():
        # Accepts {"devices": [...]} (multi-input mix) or legacy
        # {"device": name} (single). All chosen sources are mixed into the
        # broadcast on the next start; a running broadcast is never touched.
        data = _request.get_json(silent=True) or {}
        raw = data.get("devices")
        if isinstance(raw, list):
            want = [str(x or "").strip() for x in raw]
            want = [w for w in want if w]
        else:
            single = str(data.get("device", "") or "").strip()
            want = [single] if single else []
        if not want:
            return jsonify(status="error",
                           message="No device."), 400
        avail = get_audio_inputs(rescan=True)
        missing = [w for w in want if w not in avail]
        if missing:
            return jsonify(status="error",
                           message="Not found: %s" % ", ".join(missing)), 404
        seen, devices = set(), []
        for w in want:
            if w not in seen:
                seen.add(w)
                devices.append(w)
        cfg = _read_broadcast_cfg()
        cfg["audio_devices"] = devices
        cfg["audio_device"] = devices[0]
        try:
            atomic_write_json(_BROADCAST_FILE, cfg)
        except Exception:
            return jsonify(status="error",
                           message="Could not save."), 500
        return jsonify(status="ok", audio_devices=devices,
                       audio_device=devices[0])

    @app.route("/api/audio/monitor", methods=["POST"])
    @operator_required
    def api_audio_monitor():
        data = _request.get_json(silent=True) or {}
        on = bool(data.get("on", False))
        if not on:
            return jsonify(status="ok", monitoring=monitor_stop())
        device = str(data.get("device", "") or "").strip()
        if not device:
            cfg = _read_broadcast_cfg()
            device = str(cfg.get("audio_device", "") or "").strip()
        if not device:
            devs = get_audio_inputs()
            device = devs[0] if devs else ""
        if not device:
            return jsonify(status="error",
                           message="No audio device."), 404
        try:
            started = monitor_start(device)
        except Exception as e:
            return jsonify(status="error",
                           message="Monitor failed: %s" % e), 500
        return jsonify(status="ok", monitoring=started)

    @app.route("/api/audio/level", methods=["GET"])
    @operator_required
    def api_audio_level():
        # Latest input level for the requested (or selected/first) device,
        # proxied from the sidecar analyzer. The console polls this ~2/sec.
        device = str(_request.args.get("device", "") or "").strip()
        if not device:
            cfg = _read_broadcast_cfg()
            device = str(cfg.get("audio_device", "") or "").strip()
        if not device:
            devs = get_audio_inputs()
            device = devs[0] if devs else ""
        if not device:
            return jsonify(device=None, db=None, peak=None,
                           live=False), 404
        data = get_device_level(device)
        if data is None:
            return jsonify(device=device, db=None, peak=None,
                           live=False), 500
        return jsonify(data)

    return app
