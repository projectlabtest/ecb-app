"""broadcast_encoder.py — Additive REAL ffmpeg encoder for /api/broadcast/* (dev).

The compiled pymod/app.pyc broadcast endpoints (api_broadcast_start/stop/status)
are SIMULATED: they only flip a state field in data/broadcast.json and never
launch a real encoder.  This module makes them drive an actual ffmpeg gdigrab
subprocess that captures the projection monitor output and pushes it to the
saved RTMP destination (Facebook / YouTube).  It reuses the same broadcast.json
config file and returns the same JSON shapes so the compiled {index,studio,
stream_console,custom_output}.html UIs keep working unchanged.

Implementation notes:
- URLs /api/broadcast/{start,stop,status} keep their compiled URL rules; only the
  view_functions dict entries are swapped at boot (additive, app.pyc untouched).
- The capture region comes from config.json["projection_monitor"] (a monitor
  record written by screen_config_routes/api_output_push).  If absent, the
  primary display (0,0) is used.
- Audio: uses data/broadcast.json["audio_device"] when set; otherwise auto-detects
  the first DirectShow audio input via `ffmpeg -list_devices`.  Falls back to a
  video-only stream when no audio input exists.
- State machine lives in data/broadcast.json: state=idle|connecting|live.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_BROADCAST_FILE = os.path.join(_BASE_DIR, "data", "broadcast.json")
_CONF_FILE = os.path.join(_BASE_DIR, "config.json")
_FFMPEG_LOG = os.path.join(_BASE_DIR, "data", "broadcast_ffmpeg.log")

_BROADCAST_DEFAULTS = {
    "destination": "facebook",
    "rtmp_url": "",
    "stream_key": "",
    "title": "Saturday Worship Service",
    "state": "idle",
    "started_at": None,
}

# Dedicated projection-feed child (projection_feed.py) that renders the
# projection channel page headlessly (Edge) and re-serves it as a local MJPEG
# feed on port 5004.  This is what the encoder pushes — NOT a physical monitor
# grab — so a duplicated desktop never gets mirrored into the broadcast.
# Spawned with the encoder, killed on stop, so it only exists while live.
_PREVIEW_PID = None
_PREVIEW_LOCK = threading.Lock()

_PROJ_FEED_PORT = int(os.environ.get("LEITURGIA_PROJ_FEED_PORT", "5004"))
_PROJ_FEED_URL = "http://127.0.0.1:%d/projection/feed.raw" % _PROJ_FEED_PORT
_PROJ_FEED_STATUS = "http://127.0.0.1:%d/projection/status" % _PROJ_FEED_PORT

_LIVE_MARKERS = ("time=", "frame=", " Press [q] to stop")


def _resolve_ffmpeg():
    candidates = []
    try:
        with open(_CONF_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        p = (cfg or {}).get("ffmpeg_path")
        if p:
            candidates.append(p)
    except Exception:
        pass
    candidates.append(os.path.join(_BASE_DIR, "tools", "ffmpeg", "bin", "ffmpeg.exe"))
    candidates.append(os.environ.get("LEITURGIA_FFMPEG"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    import shutil
    return shutil.which("ffmpeg")


def _load_broadcast():
    data = {}
    try:
        with open(_BROADCAST_FILE, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        pass
    merged = dict(_BROADCAST_DEFAULTS)
    merged.update({k: data[k] for k in _BROADCAST_DEFAULTS if k in data})
    return merged


def _safe_broadcast(c):
    return {k: (c.get(k) if k in c else _BROADCAST_DEFAULTS[k])
            for k in _BROADCAST_DEFAULTS}


def _audio_device(cfg, ffmpeg):
    explicit = (cfg or {}).get("audio_device")
    if explicit and isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, timeout=15,
        )
        text = (out.stdout or b"") + (out.stderr or b"")
        text = text.decode("utf-8", "replace")
    except Exception:
        return None
    last_video = False
    for line in text.splitlines():
        line = line.strip()
        m = re.search(r'"(.*)"\s+\((audio)\)', line)
        if m:
            return m.group(1)
        if "(video)" in line:
            last_video = True
    return None


def _capture_monitor():
    try:
        with open(_CONF_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        mon = (cfg or {}).get("projection_monitor")
    except Exception:
        mon = None
    if isinstance(mon, dict):
        x = int(mon.get("x", 0) or 0)
        y = int(mon.get("y", 0) or 0)
        w = int(mon.get("width", 1920) or 1920)
        h = int(mon.get("height", 1080) or 1080)
        return {"x": x, "y": y, "width": w, "height": h,
                "label": mon.get("label", "Monitor")}
    return {"x": 0, "y": 0, "width": 1920, "height": 1080,
            "label": "Monitor 1 (default)"}


class _BroadcastEncoder(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._live = False
        self._err_tail = ""
        self._ffmpeg = None
        self._source = None
        self._use_feed = False
        self._audio = None
        self._started_ts = None

    # ── subprocess helpers ─────────────────────────────────────────────
    def _spawn(self, ffmpeg, cfg, push_url, mon, audio, feed_url=None):
        if feed_url:
            # Server-rendered projection feed (port 5004): the congregation
            # view, no desktop grab, no mirroring even when displays duplicate.
            # The feed renders ~6-10 fps (headless screenshot ceiling), so the
            # input is declared at 15 fps and the output is forced to a steady
            # 30 fps CFR (frame duplication): without this the encoder emits
            # ~6 fps at 0.27x speed and YouTube reports "not receiving enough
            # video" / viewers buffer. Static slides compress trivially, so
            # duplication costs negligible bitrate.
            args = [ffmpeg, "-hide_banner", "-loglevel", "info", "-y",
                    "-f", "mjpeg", "-framerate", "15", "-i", feed_url]
        else:
            # Fallback: direct gdigrab of the projection monitor region.
            args = [ffmpeg, "-hide_banner", "-loglevel", "info", "-y",
                    "-f", "gdigrab", "-framerate", "30",
                    "-offset_x", str(mon["x"]), "-offset_y", str(mon["y"]),
                    "-video_size", "{}x{}".format(mon["width"], mon["height"]),
                    "-draw_mouse", "0", "-i", "desktop"]
        if audio:
            args += ["-f", "dshow", "-i", "audio=" + audio]
        # Multi-input mix: broadcast.json["audio_devices"] (array) captures
        # several sources at once (e.g. USB mic + interface + Stereo Mix) so
        # ALL sounds go live. Extra inputs are appended here and combined
        # with amix below. Single-device behavior is byte-identical to before.
        multi = []
        if isinstance(cfg, dict) and isinstance(cfg.get("audio_devices"), list):
            seen = set()
            for _a in cfg["audio_devices"]:
                _an = str(_a or "").strip()
                if _an and _an not in seen and _an != (audio or ""):
                    seen.add(_an)
                    multi.append(_an)
        for _a in multi:
            args += ["-f", "dshow", "-i", "audio=" + _a]
        n_audio = (1 if audio else 0) + len(multi)
        args += ["-vf",
                 "scale=1920:1080:force_original_aspect_ratio=decrease,"
                 "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                 "-r", "30", "-fps_mode", "cfr",
                 "-c:v", "libx264", "-preset", "veryfast",
                 "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                 "-b:v", "3500k", "-maxrate", "4000k", "-bufsize", "7000k",
                 "-g", "60", "-keyint_min", "60", "-sc_threshold", "0"]
        if n_audio > 1:
            # Mix all audio inputs into one AAC stream. Input 0 is video, so
            # audio inputs are [1:a]..[N:a]. Explicit maps are required here
            # (default selection would keep only one audio).
            _amix = "".join("[%d:a]" % (1 + k) for k in range(n_audio))
            _amix += "amix=inputs=%d:duration=longest:dropout_transition=0[aout]" % n_audio
            args += ["-filter_complex", _amix, "-map", "0:v", "-map", "[aout]"]
            args += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"]
        elif audio:
            args += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"]
        args += ["-f", "flv", push_url]
        try:
            logf = open(_FFMPEG_LOG, "wb")
        except Exception:
            logf = subprocess.DEVNULL
        creation = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        proc = subprocess.Popen(
            args, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=creation, cwd=_BASE_DIR,
        )
        return proc

    def _drain(self, proc):
        def read():
            tail = []
            try:
                with open(_FFMPEG_LOG, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    while True:
                        chunk = f.read(4096)
                        if not chunk:
                            if proc.poll() is not None:
                                break
                            time.sleep(0.4)
                            continue
                        try:
                            text = chunk.decode("utf-8", "replace")
                        except Exception:
                            text = ""
                        if any(m in text for m in _LIVE_MARKERS):
                            with self._lock:
                                self._live = True
                        tail.append(text)
                        if len(tail) > 8:
                            tail.pop(0)
            except Exception:
                pass
            with self._lock:
                self._err_tail = "".join(tail)[-4000:]

        threading.Thread(target=read, daemon=True).start()

    # ── state helpers ───────────────────────────────────────────────────
    def _set_state(self, state, started_at=None):
        c = _load_broadcast()
        c["state"] = state
        c["started_at"] = started_at
        self._write_broadcast(c)
        return c

    def _write_broadcast(self, c):
        from app import atomic_write_json
        atomic_write_json(_BROADCAST_FILE, _safe_broadcast(c))

    def _decide_state(self):
        with self._lock:
            proc = self._proc
            live = self._live
            err = self._err_tail
            started = self._started_ts
        if proc is None:
            return "idle", self._error_only(err)
        rc = proc.poll()
        if rc is not None:
            with self._lock:
                self._proc = None
            if not live:
                err = err or ("encoder exited rc={}".format(rc))
            return "idle", self._error_only(err)
        if not live and started is not None and time.time() - started >= 6:
            return "live", err
        if not live:
            return "connecting", err
        return "live", err

    @staticmethod
    def _error_only(text):
        if not text:
            return ""
        lines = [ln.strip() for ln in text.replace("\r", "\n").split("\n")]
        hits = [
            ln for ln in lines
            if any(w in ln.lower() for w in (
                "error", "could not", "invalid", "failed", "i/o error",
                "server error", "permission denied", "connection refused",
                "unable to", "not found",
            ))
        ]
        return "\n".join(hits)[-2000:]

    # ── public API used by the view functions ───────────────────────────
    def _spawn_preview(self):
        """Launch the projection-feed child (headless Edge render of the
        projection channel). The feed doubles as the PiP source (port 5004)
        and the encoder's video input. Best-effort: the encoder falls back
        to a monitor grab when the feed cannot start.
        """
        if getattr(sys, 'frozen', False):
            # The frozen bundle has no projection_feed.py on disk; launching
            # the exe with a script path would boot a SECOND full server and
            # crash on the taken port (WinError 10048). Skip: the encoder
            # falls back to a direct monitor grab when the feed cannot start.
            return
        global _PREVIEW_PID
        with _PREVIEW_LOCK:
            if _PREVIEW_PID is not None:
                import ctypes
                try:
                    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, _PREVIEW_PID)
                    if handle:
                        ctypes.windll.kernel32.CloseHandle(handle)
                        return  # still alive
                except Exception:
                    return
                _PREVIEW_PID = None
            here = os.path.dirname(os.path.abspath(__file__))
            try:
                cmd = [sys.executable, os.path.join(here, "projection_feed.py")]
                flag = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                proc = subprocess.Popen(cmd, cwd=here, creationflags=flag)
                _PREVIEW_PID = proc.pid
            except Exception as _e:
                _PREVIEW_PID = None
                try:
                    with open(os.path.join(here, "preview-spawn.log"), "a") as _f:
                        _f.write("spawn failed: {}\n".format(repr(_e)))
                except Exception:
                    pass

    def _feed_ready(self, wait=12):
        """True once the projection feed reports it is serving frames."""
        import urllib.request as _urlreq
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                with _urlreq.urlopen(_PROJ_FEED_STATUS, timeout=3) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                if data.get("serving"):
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _kill_preview(self):
        """Terminate the preview child and its ffmpeg via process tree kill."""
        global _PREVIEW_PID
        with _PREVIEW_LOCK:
            pid = _PREVIEW_PID
            _PREVIEW_PID = None
        if pid is None:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=8,
            )
        except Exception:
            try:
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
                if handle:
                    ctypes.windll.kernel32.TerminateProcess(handle, 0)
                    ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass

    def start(self):
        cfg = _load_broadcast()
        rtmp = (cfg.get("rtmp_url") or "").rstrip("/")
        key = cfg.get("stream_key") or ""
        if not rtmp:
            return {"ok": False, "state": "idle", "message": "No RTMP server URL set."}
        if not key:
            return {"ok": False, "state": "idle", "message": "No stream key set."}
        push_url = rtmp + "/" + key.lstrip("/")
        ffmpeg = _resolve_ffmpeg()
        if not ffmpeg:
            return {"ok": False, "state": "idle",
                    "message": "ffmpeg not found (tools\\ffmpeg\\bin\\ffmpeg.exe or PATH)."}
        mon = _capture_monitor()
        audio = _audio_device(cfg, ffmpeg)
        # Start the projection feed first so the encoder can consume it.
        self._spawn_preview()
        feed_url = _PROJ_FEED_URL if self._feed_ready(wait=12) else None
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return {"ok": True, "state": "live",
                        "message": "Encoder already running."}
            self._proc = self._spawn(ffmpeg, cfg, push_url, mon, audio, feed_url)
            self._live = False
            self._err_tail = ""
            self._ffmpeg = ffmpeg
            self._source = mon
            self._use_feed = feed_url is not None
            self._audio = audio
            self._started_ts = time.time()
        c = self._set_state("connecting", time.strftime("%Y-%m-%d %H:%M:%S"))
        self._drain(self._proc)
        return {"ok": True, "state": "connecting",
                "source": "Projection feed" if feed_url else mon.get("label"),
                "video": "1920x1080",
                "audio": audio, "ffmpeg": ffmpeg}

    def stop(self):
        with self._lock:
            proc = self._proc
            self._proc = None
            self._live = False
            self._err_tail = ""
            self._started_ts = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        c = self._set_state("idle")
        self._kill_preview()
        return {"ok": True, "state": "idle"}

    def status(self):
        state, err = self._decide_state()
        c = _load_broadcast()
        if state == "live":
            c = self._set_state("live", c.get("started_at"))
        elif state == "idle" and c.get("state") != "idle":
            c = self._set_state("idle")
        with self._lock:
            source = self._source
            audio = self._audio
            ffmpeg = self._ffmpeg
            use_feed = self._use_feed
        payload = {
            "state": state,
            "destination": c.get("destination") or _BROADCAST_DEFAULTS["destination"],
            "started_at": c.get("started_at"),
            "simulated": False,
            "encoder": "ffmpeg",
            "ffmpeg_path": ffmpeg,
            "source_monitor": (source.get("label") if source else None)
                              if not use_feed else "Projection feed",
            "feed_input": _PROJ_FEED_URL if use_feed else None,
            "audio_device": audio,
        }
        if err and state == "idle":
            payload["last_error"] = err.strip()[-500:]
        if state == "connecting":
            for m in _LIVE_MARKERS:
                if m in err:
                    break
        return payload

    def boot_reset(self):
        c = _load_broadcast()
        if c.get("state") in ("live", "connecting"):
            c["state"] = "idle"
            c["started_at"] = None
            try:
                self._write_broadcast(c)
            except Exception:
                pass


_ENC = _BroadcastEncoder()


def init_app(app):
    """Swap the compiled broadcast control endpoints for the real encoder."""
    views = app.view_functions

    def api_broadcast_start():
        return jsonify_result(_ENC.start())

    def api_broadcast_stop():
        return jsonify_result(_ENC.stop())

    def api_broadcast_status():
        return jsonify_result(_ENC.status())

    from flask import jsonify

    def jsonify_result(obj):
        status = 200
        if isinstance(obj, dict) and obj.get("ok") is False:
            status = 400
        return jsonify(obj), status

    for name in ("api_broadcast_start", "api_broadcast_stop", "api_broadcast_status"):
        if name in views:
            if name == "api_broadcast_start":
                views[name] = api_broadcast_start
            elif name == "api_broadcast_stop":
                views[name] = api_broadcast_stop
            else:
                views[name] = api_broadcast_status

    _ENC.boot_reset()
    return app