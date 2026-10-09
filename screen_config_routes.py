"""screen_config_routes.py — Additive Screen Config API endpoints (dev, not compiled).

The deployed app runs from pymod/*.pyc, so source app.py edits are not picked
up. The current pymod/app.pyc predates the Screen Config endpoints (/api/channels,
/api/settings/projection, /api/output/*); following the bible.py / live_input.py
pattern, this regsiters them on the already-existing Flask app at boot via
init_app(), reusing the SAME roles / socketio / jsonio singletons loaded from
pymod so channel assignments and aspect updates stay in sync with the compiled
app.
"""

import ctypes
import json
import os
import re
import subprocess
import tempfile
from ctypes import POINTER, Structure, byref, c_int, c_long, c_ubyte, c_ulong, c_void_p


class _RECT(Structure):
    _fields_ = [
        ("left", c_long),
        ("top", c_long),
        ("right", c_long),
        ("bottom", c_long),
    ]


class _MONITORINFO(Structure):
    _fields_ = [
        ("cbSize", c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", c_ulong),
    ]


_user32 = ctypes.windll.user32
_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    c_int, c_void_p, c_void_p, POINTER(_RECT), c_void_p
)
_user32.EnumDisplayMonitors.argtypes = [
    c_void_p, c_void_p, _MONITORENUMPROC, c_void_p
]
_user32.EnumDisplayMonitors.restype = c_int
_user32.GetMonitorInfoW.argtypes = [c_void_p, POINTER(_MONITORINFO)]
_user32.GetMonitorInfoW.restype = c_int
_MONITORINFOF_PRIMARY = 1


def _get_monitors():
    """Enumerate physical displays and their screen coordinates (pixels)."""
    MONITORINFOF_PRIMARY = _MONITORINFOF_PRIMARY
    out = []

    def _cb(hmon, hdc, lprc, data):
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        _user32.GetMonitorInfoW(hmon, byref(mi))
        r = mi.rcMonitor
        out.append({
            "index": len(out),
            "x": r.left,
            "y": r.top,
            "width": r.right - r.left,
            "height": r.bottom - r.top,
            "primary": bool(mi.dwFlags & MONITORINFOF_PRIMARY),
            "label": "Monitor {}".format(len(out) + 1),
        })
        return True

    _cb = _MONITORENUMPROC(_cb)
    _user32.EnumDisplayMonitors(0, 0, _cb, 0)
    if not out:
        out.append({
            "index": 0, "x": 0, "y": 0,
            "width": 1920, "height": 1080,
            "primary": True, "label": "Monitor 1",
        })
    return out


def _find_edge():
    candidates = [
        os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        + "\\Microsoft\\Edge\\Application\\msedge.exe",
        os.environ.get("ProgramFiles", "C:\\Program Files")
        + "\\Microsoft\\Edge\\Application\\msedge.exe",
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Microsoft", "Edge", "Application", "msedge.exe",
        ),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _edge_profile_dir(channel):
    base = os.path.join(os.environ.get("TEMP", tempfile.gettempdir()), "leitur-edge")
    return os.path.join(base, "ch-" + re.sub("[^A-Za-z0-9]", "", channel).lower())


def _kill_profile_processes(profile):
    if os.name != "nt":
        return False
    marker = os.path.basename(profile)
    script = (
        'Get-CimInstance Win32_Process -Filter "Name=\'msedge.exe\'" '
        "| Where-Object { $_.CommandLine -like '*" + marker + "*' } "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
    )
    try:
        import base64 as _b64
        enc = _b64.b64encode(script.encode("utf-16-le")).decode("ascii")
        p = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        p.communicate(timeout=20)
        return True
    except Exception:
        return False


def _existing_channels(roles):
    chs = list(roles.get_existing_channels())
    for c in ("ch1", "ch2", "ch3", "ch4"):
        if c not in chs:
            chs.append(c)
    return chs


def _sorted_channels(roles):
    return sorted(
        _existing_channels(roles),
        key=lambda c: (not c[2:].isdigit(), int(c[2:]) if c[2:].isdigit() else 0),
    )


def _roles_channel_map(roles):
    result = {}
    for role, channels in roles.to_dict().items():
        for ch in channels:
            result[ch] = role
    return result


def _read_aspects():
    try:
        with open("config.json") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    aspects = (cfg or {}).get("projection_aspects") or {}
    if not isinstance(aspects, dict):
        aspects = {}
    return cfg, aspects


_CUSTOM_SLOTS = 4
_CUSTOM_THEME_FILE = os.path.join("data", "custom_theme.json")


def _custom_theme_state():
    """Mirror the deployed app's custom-theme state (active slot + slot images)."""
    _default = {"active": 0, "slots": [None] * _CUSTOM_SLOTS}
    try:
        with open(_CUSTOM_THEME_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _default
    if not isinstance(data, dict):
        return _default
    slots = data.get("slots", _default["slots"])
    if not isinstance(slots, list) or len(slots) != _CUSTOM_SLOTS:
        slots = [None] * _CUSTOM_SLOTS
    active = data.get("active", _default["active"])
    try:
        active = int(active)
    except (TypeError, ValueError):
        active = _default["active"]
    if active < 0 or active >= _CUSTOM_SLOTS:
        active = 0
    return {"active": active, "slots": slots}


def _get_projection_aspect(channel):
    try:
        with open("config.json") as f:
            cfg = json.load(f)
        return str((cfg or {}).get("projection_aspects", {})
                   .get(channel, "off") or "off")
    except Exception:
        return "off"


def init_app(app):
    """Register the Screen Config endpoints on the existing app (additive)."""
    from flask import jsonify, request as _request
    import app as _app_mod
    from app import roles, socketio, operator_required, atomic_write_json
    from roles import _VALID_ROLES

    # The compiled 2nd Project app.pyc predates the CUSTOM role, so its
    # in-memory _VALID_ROLES lacks 'custom' while roles.pyc (and the deployed
    # main build) include it.  Patch the module global at boot so the compiled
    # socket handler roles:assign (which checks app._VALID_ROLES) accepts the
    # CUSTOM button rendered by the console.  This mirrors the main build.
    if "custom" not in tuple(getattr(_app_mod, "_VALID_ROLES", ())):
        _app_mod._VALID_ROLES = tuple(getattr(_app_mod, "_VALID_ROLES", ())) + ("custom",)

    # The compiled 2nd Project app.pyc predates projection custom themes:
    # projection_channel / custom_output_route render projection.html /
    # custom_output.html WITHOUT the custom_theme and aspect vars that the
    # newer templates require (a newer main build passes both).  Inject them
    # via a context processor so those templates render instead of crashing
    # with UndefinedError.  Uses the per-channel aspect from config.json and
    # the data/custom_theme.json state, mirroring the deployed app.
    @app.context_processor
    def _projection_context_extras():
        aspect = "off"
        try:
            n = _request.view_args.get("n")
            if n is not None:
                aspect = _get_projection_aspect("ch" + str(n))
        except Exception:
            pass
        return {"aspect": aspect, "custom_theme": _custom_theme_state()}

    @app.route("/api/channels", methods=["GET"])
    @operator_required
    def api_channels_get():
        ch_map = _roles_channel_map(roles)
        channels = _sorted_channels(roles)
        _, aspects = _read_aspects()
        aspects_map = {
            ch: str(aspects.get(ch, "off") or "off") for ch in channels
        }
        return jsonify(channels=channels, assignments=ch_map, aspects=aspects_map)

    @app.route("/api/channels", methods=["POST"])
    @operator_required
    def api_channels_post():
        data = _request.get_json(silent=True) or {}
        role = str(data.get("role", "") or "") or "main"
        if role not in _VALID_ROLES:
            role = "main"
        new_ch = roles.add_channel(role)
        ch_map = _roles_channel_map(roles)
        socketio.emit("roles:updated", {"assignments": ch_map})
        channels = _sorted_channels(roles)
        return jsonify(status="ok", channel=new_ch, channels=channels,
                       assignments=ch_map)

    @app.route("/api/channels/<channel>", methods=["DELETE"])
    @operator_required
    def api_channel_delete(channel):
        if channel == "ch1":
            return jsonify(status="error",
                           message="Cannot delete the main console channel"), 400
        if channel not in _existing_channels(roles):
            return jsonify(status="error", message="Invalid channel"), 404
        roles.remove_channel(channel)
        ch_map = _roles_channel_map(roles)
        socketio.emit("roles:updated", {"assignments": ch_map})
        channels = _sorted_channels(roles)
        return jsonify(status="ok", channel=channel, channels=channels,
                       assignments=ch_map)

    @app.route("/api/settings/projection", methods=["GET"])
    @operator_required
    def api_settings_projection_get():
        channels = list(roles.get_existing_channels())
        _, aspects = _read_aspects()
        return jsonify(channels=[{
            "channel": ch,
            "role": roles.get_role(ch) or "main",
            "aspect": str(aspects.get(ch, "off") or "off"),
        } for ch in channels])

    @app.route("/api/settings/projection", methods=["POST"])
    @operator_required
    def api_settings_projection_post():
        data = _request.get_json(silent=True) or {}
        channel = str(data.get("channel", "") or "").strip()
        aspect = str(data.get("projection_aspect", "off") or "off").strip()
        existing = roles.get_existing_channels()
        if channel not in existing:
            return jsonify(status="error", message="Unknown channel."), 400
        if aspect not in ("off", "auto"):
            m = re.match(r"^\s*(\d+)\s*[:xX]\s*(\d+)\s*$", aspect)
            if not m:
                return jsonify(status="error",
                               message='Aspect must be "off", "auto", or "W:H" '
                                       "(e.g. 16:9, 4:3)."), 400
            aspect = "%d:%d" % (int(m.group(1)), int(m.group(2)))
        cfg, aspects = _read_aspects()
        aspects[channel] = aspect
        if isinstance(cfg, dict):
            cfg["projection_aspects"] = aspects
            atomic_write_json("config.json", cfg)
        socketio.emit("aspect:update",
                      {"aspect": aspect, "channel": channel}, room="room")
        return jsonify(status="ok", channel=channel, projection_aspect=aspect)

    @app.route("/api/output/monitors", methods=["GET"])
    @operator_required
    def api_output_monitors():
        return jsonify(monitors=_get_monitors(), edge=bool(_find_edge()))

    @app.route("/api/output/push", methods=["POST"])
    @operator_required
    def api_output_push():
        data = _request.get_json(silent=True) or {}
        channel = str(data.get("channel", "") or "").strip().lower()
        monitor_i = int(data.get("monitor", 0) or 0)
        if not channel or channel not in _existing_channels(roles):
            return jsonify(status="error", message="Unknown channel."), 400
        edge = _find_edge()
        if not edge:
            return jsonify(status="error",
                           message="Microsoft Edge not found."), 500
        mons = _get_monitors()
        if not mons:
            return jsonify(status="error",
                           message="No display detected."), 500
        mon = next((m for m in mons if m["index"] == monitor_i), mons[0])
        # Remember which display the output was pushed to so the broadcast
        # encoder can capture the projection monitor (mirrors main app; the
        # compiled app.pyc has no such record).
        cfg, _ = _read_aspects()
        if isinstance(cfg, dict):
            cfg["projection_monitor"] = mon
            atomic_write_json("config.json", cfg)
        url = "http://%s/%s?output=1" % (_request.host, channel)
        profile = _edge_profile_dir(channel)
        args = [
            edge,
            "--app=%s" % url,
            "--kiosk",
            "--window-position=%s,%s" % (mon["x"], mon["y"]),
            "--window-size=%s,%s" % (mon["width"], mon["height"]),
            "--user-data-dir=%s" % profile,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
        ]
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:
            return jsonify(status="error",
                           message="Failed to launch Edge."), 500
        return jsonify(status="ok", channel=channel, monitor=mon["index"],
                       url=url, position=[mon["x"], mon["y"]],
                       size=[mon["width"], mon["height"]])

    @app.route("/api/output/close", methods=["POST"])
    @operator_required
    def api_output_close():
        data = _request.get_json(silent=True) or _request.form or {}
        channel = str(data.get("channel", "") or "").strip().lower()
        if not channel:
            return jsonify(status="error", message="No channel."), 400
        profile = _edge_profile_dir(channel)
        killed = _kill_profile_processes(profile)
        return jsonify(status="ok" if killed else "noop",
                       channel=channel, killed=killed)

    # ── Custom theme backgrounds ──────────────────────────────────────────
    # The console's Custom theme panel (slots + upload) calls these; the
    # compiled app.pyc predates them, so without these routes every call
    # 404s and no picture can ever be uploaded. Additive only.
    from flask import send_file as _send_file

    _CUSTOM_BG_DIR = os.path.join("data", "custom_theme")
    _CUSTOM_BG_EXTS = {"png": "image/png", "jpg": "image/jpeg",
                       "jpeg": "image/jpeg", "gif": "image/gif",
                       "webp": "image/webp", "bmp": "image/bmp"}
    _CUSTOM_BG_MAX = 8 * 1024 * 1024

    def _custom_bg_find(idx):
        """On-disk file for slot ``idx``, or None."""
        try:
            names = os.listdir(_CUSTOM_BG_DIR)
        except OSError:
            return None
        for n in names:
            stem, dot, ext = n.partition(".")
            if stem == ("slot%d" % idx) and dot and ext.lower() in _CUSTOM_BG_EXTS:
                return os.path.join(_CUSTOM_BG_DIR, n)
        return None

    def _custom_bg_url(idx):
        return "/api/themes/custom/file/%d" % idx

    def _custom_theme_save(active, slots):
        state = {"active": active, "slots": slots}
        atomic_write_json(_CUSTOM_THEME_FILE, state)
        return state

    @app.route("/api/themes/custom", methods=["GET"])
    @operator_required
    def api_custom_theme_get():
        st = _custom_theme_state()
        return jsonify(active=st["active"], slots=st["slots"],
                       count=_CUSTOM_SLOTS)

    @app.route("/api/themes/custom/upload", methods=["POST"])
    @operator_required
    def api_custom_theme_upload():
        try:
            idx = int((_request.form.get("slot", "") or "").strip())
        except (TypeError, ValueError):
            return jsonify(status="error",
                           message="Invalid slot."), 400
        if idx < 0 or idx >= _CUSTOM_SLOTS:
            return jsonify(status="error",
                           message="Invalid slot."), 400
        f = _request.files.get("file")
        if f is None or not (f.filename or "").strip():
            return jsonify(status="error",
                           message="No file selected."), 400
        ext = (f.filename or "").rsplit(".", 1)
        ext = ext[-1].lower() if len(ext) > 1 else ""
        if ext not in _CUSTOM_BG_EXTS:
            return jsonify(status="error",
                           message="Use PNG, JPG, GIF, WEBP or BMP."), 400
        try:
            data = f.read(_CUSTOM_BG_MAX + 1)
        except Exception:
            return jsonify(status="error",
                           message="Could not read file."), 400
        if not data or len(data) > _CUSTOM_BG_MAX:
            return jsonify(status="error",
                           message="File is empty or over 8 MB."), 400
        try:
            os.makedirs(_CUSTOM_BG_DIR, exist_ok=True)
            old = _custom_bg_find(idx)
            if old:
                try:
                    os.remove(old)
                except OSError:
                    pass
            with open(os.path.join(_CUSTOM_BG_DIR,
                                   "slot%d.%s" % (idx, ext)), "wb") as fh:
                fh.write(data)
        except OSError:
            return jsonify(status="error",
                           message="Could not save file."), 500
        st = _custom_theme_state()
        slots = list(st["slots"])
        slots[idx] = _custom_bg_url(idx)
        saved = _custom_theme_save(st["active"], slots)
        return jsonify(status="ok", slots=saved["slots"],
                       active=saved["active"])

    @app.route("/api/themes/custom/active", methods=["POST"])
    @operator_required
    def api_custom_theme_active():
        data = _request.get_json(silent=True) or {}
        try:
            idx = int(data.get("slot"))
        except (TypeError, ValueError):
            return jsonify(status="error",
                           message="Invalid slot."), 400
        if idx < 0 or idx >= _CUSTOM_SLOTS:
            return jsonify(status="error",
                           message="Invalid slot."), 400
        st = _custom_theme_state()
        saved = _custom_theme_save(idx, list(st["slots"]))
        return jsonify(status="ok", active=saved["active"])

    @app.route("/api/themes/custom/<int:idx>", methods=["DELETE"])
    @operator_required
    def api_custom_theme_delete(idx):
        if idx < 0 or idx >= _CUSTOM_SLOTS:
            return jsonify(status="error",
                           message="Invalid slot."), 404
        old = _custom_bg_find(idx)
        if old:
            try:
                os.remove(old)
            except OSError:
                pass
        st = _custom_theme_state()
        slots = list(st["slots"])
        slots[idx] = None
        active = st["active"]
        if active == idx:
            active = 0
        saved = _custom_theme_save(active, slots)
        return jsonify(status="ok", slots=saved["slots"],
                       active=saved["active"])

    @app.route("/api/themes/custom/file/<int:idx>", methods=["GET"])
    def api_custom_theme_file(idx):
        # PUBLIC (no operator login): the projection page and the broadcast
        # headless render load this URL as a CSS background / <img>.
        if idx < 0 or idx >= _CUSTOM_SLOTS:
            return jsonify(status="error",
                           message="Invalid slot."), 404
        path = _custom_bg_find(idx)
        if not path:
            return jsonify(status="error",
                           message="Empty slot."), 404
        ext = path.rsplit(".", 1)[-1].lower()
        return _send_file(path, mimetype=_CUSTOM_BG_EXTS.get(ext,
                           "application/octet-stream"))

    # ── Current server LAN IP ─────────────────────────────────────────────
    # The console's QR (mobile remote) and Direct Output (IP) modals used a
    # template IP rendered once at page load, so after a network change they
    # kept showing the stale address and receivers could not connect. This
    # endpoint recomputes the address on every call; the modals fetch it
    # each time they open. Same detection as the main app's _server_ip.
    @app.route("/api/server-ip", methods=["GET"])
    @operator_required
    def api_server_ip():
        import socket as _sock
        ip = "127.0.0.1"
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0] or ip
            s.close()
        except Exception:
            pass
        return jsonify(ip=ip)

    return app