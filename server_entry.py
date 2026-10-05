"""
server_entry.py — Runs the Leiturgia Flask server as the main entry point.

Used as the PyInstaller entry point for the embedded server executable. Changes
the working directory to this file's folder so runtime state (config.json,
data/, media/, output/) resolves correctly regardless of where the app is
launched from.
"""
import os
import sys
import glob

# Make hard native crashes (access violations in cv2/comtypes/DirectShow)
# dump a Python traceback to the same log file instead of the process silently
# dying. Without this a server that segfaults shows "can't reach this page"
# with no clue in dev5010.log.
try:
    import faulthandler as _fh
    _fh.enable()
except Exception:
    pass

if getattr(sys, 'frozen', False):
    # The windowed build has no attached console: sys.stdout / sys.stderr are
    # None, and the first logging emit (basicConfig uses stderr, flushed on
    # every record) crashes with "'NoneType' object has no attribute 'flush'".
    # Give them silent StringIO sinks so every launch path (operator app,
    # direct/console launch, dev) behaves identically instead of crashing.
    import io as _io
    if sys.stdout is None:
        sys.stdout = _io.StringIO()
    if sys.stderr is None:
        sys.stderr = _io.StringIO()

if getattr(sys, 'frozen', False):
    HERE = os.path.dirname(sys.executable)
    _MEIPASS = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    _MEIPASS = HERE

os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _resolve(name):
    """Return the first existing path under the install dir or the PyInstaller
    _MEIPASS bundle dir, so recovery data resolves in both onefile and onedir."""
    cands = [os.path.join(HERE, name)]
    if _MEIPASS and _MEIPASS != HERE:
        cands.append(os.path.join(_MEIPASS, name))
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]


def _register_pymod():
    """Expose the recovered .pyc project modules (app, roles, ...) on sys.path."""
    pymod = _resolve('pymod')
    if pymod and os.path.isdir(pymod):
        sys.path.insert(0, pymod)


_register_pymod()


def _ensure_config():
    """Create a default config.json on first run so a fresh install can boot
    (and can be activated) without shipping an owner's secrets.  The default
    operator PIN is 1234 (sha256); the operator can change it from Settings."""
    import json as _json
    if os.path.isfile('config.json'):
        return
    import hashlib as _hl
    import secrets as _sec
    _cfg = {
        'pin_hash': _hl.sha256(b'1234').hexdigest(),
        'session_secret': _sec.token_urlsafe(32),
        'session_timeout_hours': 8,
        'max_login_attempts': 5,
        'cloud_enabled': False,
        'cloud_url': '',
        'cloud_token': '',
        'enable_self_update': True,
        'update_url': 'https://github.com/projectlabtest/ecb-app/releases/latest/download',
        'owner_email': '',
        'paymongo_secret': '',
        'paymongo_publishable': '',
        'paymongo_backend': '',
        'paymongo_backend_token': '',
        'license_price_peso': 500,
        'trial_days': 30,
        'log_level': 'INFO',
        'media_video_budget_gb': 2,
        'media_image_budget_gb': 1,
        'media_disk_reserve_gb': 1,
        'media_warn_percent': 80,
        'media_video_dir': os.path.join(HERE, 'media', 'videos'),
        'projection_aspects': {'ch1': 'off'},
    }
    try:
        with open('config.json', 'w') as _f:
            _json.dump(_cfg, _f, indent=2)
    except OSError:
        pass


_ensure_config()

# Dedicated live-video stream daemon mode.
#
# The main server boots with Flask/socketio/eventlet. A long-lived OpenCV capture
# loop running IN THAT SAME eventlet process deadlocks the hub, so live capture
# runs in a SEPARATE process: the same exe re-launched with `--stream`. This
# branch must come BEFORE the licence check (the child is on the same machine)
# and before anything imports eventlet/socketio.
if '--stream' in sys.argv:
    import stream_server
    stream_server.main()
    sys.exit(0)


_ACT_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LIGHT WORSHIP APP &mdash; Device Activation</title>
<style>
  :root{--accent:#14B8A6;--bg:#0f1117;--card:#181b24;--line:#2a2f3d;--txt:#e8e6e3;--muted:#9aa0ae}
  *{box-sizing:border-box} body{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:var(--bg);color:var(--txt);display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:34px 38px;max-width:580px;width:100%;box-shadow:0 10px 40px rgba(0,0,0,.45)}
  h1{font-size:1.35rem;margin:0 0 6px} p{color:var(--muted);font-size:.92rem;line-height:1.55;margin:6px 0;font-weight:400}
  .hwid{background:#0b0e14;border:1px dashed var(--line);border-radius:8px;padding:12px 14px;font-family:Consolas,monospace;font-size:.8rem;word-break:break-all;color:var(--accent);user-select:all;margin-top:4px}
  label{display:block;font-size:.8rem;color:var(--muted);margin:18px 0 6px}
  input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:#0b0e14;color:var(--txt);font-family:Consolas,monospace;font-size:.85rem}
  button{margin-top:14px;width:100%;padding:12px;border:0;border-radius:8px;background:var(--accent);color:#151207;font-weight:700;font-size:.95rem;cursor:pointer}
  button:disabled{opacity:.5;cursor:wait}
  .msg{border-radius:8px;padding:10px 12px;margin-top:16px;font-size:.85rem;display:none}
  .msg.err{background:#3a1b1f;color:#ffb4a8;display:block} .msg.ok{background:#14321f;color:#a8f0c3;display:block}
  .row{display:flex;gap:10px;margin-top:14px;width:100%}
  .row button{flex:1;margin-top:0;background:#1e2430;color:#e8e6e3;border:1px solid var(--line)}
  .divider{text-align:center;color:var(--muted);margin:18px 0 2px;font-size:.74rem;letter-spacing:.16em;text-transform:uppercase}
  .hint{color:var(--muted);font-size:.78rem;margin-top:6px}
  code{color:var(--accent)}
</style></head><body><div class="card">
  <h1>LIGHT WORSHIP APP is not activated</h1>
  {{ trial_notice }}
  <p>This copy needs a license key for <b>this PC</b>. Send your Hardware ID below to your LIGHT WORSHIP APP provider,
     paste the license key you receive, then press <b>Activate device</b>.</p>
  <label>Hardware ID &mdash; send this to your provider</label>
  <div class="hwid">{{ hwid }}</div>
  <label>License key</label>
  <input id="key" autocomplete="off" spellcheck="false" placeholder="Paste license key here">
  <button id="act" onclick="activate()">Activate device</button>
  <div class="row">
    <button id="email" onclick="sendHWID()">Send HWID by email</button>
    <button id="copy" onclick="copyHWID()">Copy HWID</button>
  </div>
  {{ pay_html }}
  <div class="msg" id="msg"></div>
  <div id="done" style="display:none">
    <div class="msg ok">Activated &mdash; loading Leiturgia&hellip;</div>
    <p>Your default sign-in PIN is <code>1234</code> &mdash; change it afterwards.</p>
  </div>
</div>
<script>
  var _hwid = (document.querySelector('.hwid')||{}).textContent ? document.querySelector('.hwid').textContent.trim() : '{{ hwid }}';
  async function copyHWID(){
    try{ await navigator.clipboard.writeText(_hwid); }
    catch(e){
      var ta=document.createElement('textarea'); ta.value=_hwid; document.body.appendChild(ta); ta.select();
      try{ document.execCommand('copy'); }catch(_){}
      document.body.removeChild(ta);
    }
    var m=document.getElementById('msg'); m.className='msg ok'; m.textContent='Hardware ID copied.';
  }
  function sendHWID(){
    var owner='{{ owner_email }}';
    if(!owner){ copyHWID(); return; }
    var sub=encodeURIComponent('Leiturgia License Request');
    var body=encodeURIComponent('Please create a license key for this PC.\n\nHardware ID: ' + _hwid);
    location.href='mailto:'+owner+'?subject='+sub+'&body='+body;
  }
  async function payActivate(){
    var msg=document.getElementById('msg'), btn=document.getElementById('pay');
    msg.className='msg'; msg.textContent='';
    btn.disabled=true; btn.textContent='Creating payment…';
    try{
      var r=await fetch('/api/pay/create',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      var d=await r.json();
      if(!d.ok){ throw new Error(d.message||'create failed'); }
      var hint=document.getElementById('payhint'); if(hint) hint.style.display='none';
      document.getElementById('qr').src=d.qr_image;
      var tc=document.getElementById('testctl');
      if(d.test_url && tc){ tc.style.display='block'; document.getElementById('testframe').src=d.test_url; }
      document.getElementById('paybox').style.display='block';
      btn.textContent='Waiting for payment…';
      var n=0;
      var poll=setInterval(async function(){
        n++;
        try{
          var r2=await fetch('/api/pay/status?id='+encodeURIComponent(d.id));
          var d2=await r2.json();
          if(d2.paid){
            clearInterval(poll);
            if(d2.activated === false){
              msg.className='msg ok';
              msg.textContent='Payment confirmed. Send your Hardware ID (above) to your provider to receive your license key.';
              btn.disabled=false; btn.textContent='Pay &amp; Activate';
            } else {
              document.getElementById('done').style.display='block';
              msg.className='msg ok'; msg.textContent='Payment received. Activating…';
              setTimeout(function(){ location.href='/login'; }, 2000);
            }
          } else if(n>200){
            clearInterval(poll);
            msg.className='msg err'; msg.textContent='Payment has not arrived yet. If already paid, click Pay &amp; Activate again.';
            btn.disabled=false; btn.textContent='Pay &amp; Activate';
          }
        }catch(e2){}
      },3000);
    }catch(e){
      msg.className='msg err'; msg.textContent='Payment error: '+(e.message||'network error');
      btn.disabled=false; btn.textContent='Pay &amp; Activate';
    }
  }
async function activate(){
  const key = document.getElementById('key').value.trim();
  const msg = document.getElementById('msg');
  const btn = document.getElementById('act');
  msg.className='msg'; msg.textContent='';
  if(!key){ msg.className='msg err'; msg.textContent='Paste the license key first.'; return; }
  btn.disabled=true; btn.textContent='Activating…';
  try{
    const r = await fetch('/api/lic/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})});
    const d = await r.json();
    if(d.ok){
      document.getElementById('done').style.display='block';
      msg.className='msg ok'; msg.textContent='Activated. Reloading…';
      setTimeout(function(){ location.href='/login'; }, 2000);
    } else {
      msg.className='msg err'; msg.textContent='Activation failed: '+(d.message||'unknown error');
    }
  }catch(e){ msg.className='msg err'; msg.textContent='Network error — is the app still running?'; }
  btn.disabled=false; btn.textContent='Activate device';
}
</script></body></html>'''


def _write_licence_error(reason):
    os.makedirs('data', exist_ok=True)
    with open(os.path.join('data', 'license_error.log'), 'w') as f:
        f.write('Leiturgia is locked to a specific machine.\n')
        f.write('Reason: %s\n' % reason)
        f.write('This copy is not licensed for this PC. Contact the owner.\n')


from licensing import verify_licence

# Per-machine licence state.  When not licensed the running server only serves
# the activation page (+ /api/lic/* + a locked /api/health); when a valid key is
# submitted the state flips in-process, so no restart / port hand-off is needed.
_lic_state = {'ok': False, 'hwid': ''}

_lic_ok, _lic_reason = verify_licence()
if not _lic_ok:
    _write_licence_error(_lic_reason)
_lic_state['ok'] = _lic_ok


def wire_license_gate():
    """Add the licence gate to the running Flask app (called after the full
    app object exists).  While not licensed, every request other than the
    activation endpoints / a locked /api/health is short-circuited to the
    activation page.  A valid key writes license.dat and flips the state to
    unlocked in-process."""
    import hashlib as _hl
    import hmac as _hm
    from licensing import get_hardware_id, make_licence, rsa_verify
    from flask import request, jsonify, Response

    _lic_state['hwid'] = get_hardware_id()
    _lic_state.setdefault('pay_session', {})
    try:
        import json as _json
        with open('config.json') as _f:
            _c = _json.load(_f) or {}
        _lic_state['owner_email'] = _c.get('owner_email') or ''
        _lic_state['paymongo_secret'] = _c.get('paymongo_secret') or ''
        _lic_state['paymongo_publishable'] = _c.get('paymongo_publishable') or ''
        _lic_state['paymongo_backend'] = _c.get('paymongo_backend') or ''
        _lic_state['paymongo_backend_token'] = _c.get('paymongo_backend_token') or ''
        try:
            _lic_state['license_price_peso'] = int(_c.get('license_price_peso') or 0)
        except Exception:
            _lic_state['license_price_peso'] = 0
    except Exception:
        _lic_state.update({'owner_email': '', 'paymongo_secret': '',
                           'paymongo_publishable': '', 'license_price_peso': 0,
                           'paymongo_backend': '', 'paymongo_backend_token': '',
                           'trial_days': 30})
    try:
        _lic_state['trial_days'] = max(0, int(_c.get('trial_days') or 30))
    except Exception:
        _lic_state['trial_days'] = 30

    import time as _time

    def _trial_state():
        """Return (days_left, total_days) while a free trial is running, or
        (0, total) once it has expired, or None when trials are disabled."""
        _total = int(_lic_state.get('trial_days') or 0)
        if _total <= 0:
            return None
        _tf = os.path.join('data', 'trial.dat')
        _now = _time.time()
        _start = None
        try:
            with open(_tf) as _f:
                _hw, _ts = _f.read().split()
            if _hw == _lic_state['hwid']:
                _start = float(_ts)
        except Exception:
            _start = None
        if _start is None:
            try:
                os.makedirs('data', exist_ok=True)
                with open(_tf, 'w') as _f:
                    _f.write('%s %.3f\n' % (_lic_state['hwid'], _now))
                _start = _now
            except OSError:
                return None
        _left = _total - (_now - _start) / 86400.0
        return (max(0.0, _left), _total)

    try:
        from version import get_version as _gv
        _ver = _gv()
    except Exception:
        _ver = 'unknown'

    def _pm_headers(_secret):
        import base64
        _tok = base64.b64encode((_secret + ':').encode('utf-8')).decode('ascii')
        return {'Authorization': 'Basic ' + _tok}

    def _pay_section_html():
        _secret = (_lic_state.get('paymongo_secret') or '').strip()
        _backend = (_lic_state.get('paymongo_backend') or '').strip()
        _price = int(_lic_state.get('license_price_peso') or 0)
        if (not _secret and not _backend) or _price <= 0:
            return ''
        _is_test = _secret.startswith('sk_test_')
        _test_ctl = (
            '<div id="testctl" style="display:none;margin-top:12px">'
            '<p class="hint" style="margin:0 0 6px"><b>TEST MODE</b> &mdash; simulate the phone approval below:</p>'
            '<iframe id="testframe" title="Authorize test payment" '
            'style="width:100&#37;;height:122px;border:1px dashed var(--line);border-radius:8px;background:#0b0e14"></iframe>'
            '</div>'
        )
        if _is_test:
            _pay_hint = (
                'You are in TEST mode. Scan the QR with any QR Ph / GCash app, '
                'then press <b>Authorize Test Payment</b> inside the box below to finish.'
            )
        elif _backend:
            _pay_hint = (
                'A payment box appears below. On success this page refreshes automatically once paid.'
            )
        else:
            _pay_hint = (
                'Open <b>GCash</b> on your phone, scan this QR and approve. '
                'This page refreshes automatically once paid.'
            )
        return (
            '<div class="divider">or pay online</div>'
            '<label>Instant activation &mdash; &#8369;%d &middot; pay with GCash (scan QR)</label>'
            '<button id="pay" onclick="payActivate()">Pay &amp; Activate &mdash; &#8369;%d</button>'
            '<div id="paybox" style="display:none;margin-top:14px;text-align:center">'
            '<img id="qr" alt="Payment QR" style="width:210px;height:210px;border-radius:10px;'
            'border:1px solid var(--line);background:#fff;padding:8px">'
            '<p class="hint" id="payhint" style="margin:8px 0 0;text-align:center">' + _pay_hint + '</p>'
            + _test_ctl +
            '</div>'
        ) % (_price, _price)

    def _page_html(forced_hwid, trial_notice=''):
        _owner = (_lic_state.get('owner_email') or '').strip()
        _h = forced_hwid or 'UNAVAILABLE'
        return (_ACT_HTML.replace('{{ hwid }}', _h)
                          .replace('{{ owner_email }}', _owner)
                          .replace('{{ pay_html }}', _pay_section_html())
                          .replace('{{ trial_notice }}', trial_notice))

    @app.before_request
    def _gate():
        if _lic_state['ok']:
            return None
        _trial = _trial_state()
        _trial_active = bool(_trial and _trial[0] > 0)
        p = request.path
        if p.startswith('/api/pay/'):
            return None
        if p in ('/api/lic/status', '/api/lic/activate'):
            return None
        if p == '/api/health':
            if _trial_active:
                return None
            return jsonify(status='locked', licensed=False, version=_ver)
        if p.startswith('/static') or p in ('/favicon.ico',):
            return None
        if _trial_active:
            return None
        _notice = ''
        if _trial and _trial[1] > 0:
            _notice = ('<p style="margin:6px 0 0;color:#ff9f6e">Your %d-day free trial '
                       'has ended &mdash; activate this copy to keep using it.</p>' % int(_trial[1]))
        return Response(_page_html(_lic_state['hwid'], _notice), status=200, mimetype='text/html')

    @app.route('/api/lic/status')
    def _lic_status():
        _trial = _trial_state()
        _trial_active = bool(_trial and _trial[0] > 0)
        _j = {'licensed': _lic_state['ok'], 'hwid': _lic_state['hwid'],
              'trial': _trial_active,
              'trial_days_left': (round(_trial[0], 1) if _trial else 0),
              'trial_days_total': (int(_trial[1]) if _trial else 0)}
        if not _lic_state['ok']:
            _j['reason'] = 'Missing or invalid license.dat for this PC'
        return jsonify(**_j)

    @app.route('/api/lic/activate', methods=['POST'])
    def _lic_activate():
        if _lic_state['ok']:
            return jsonify(ok=False, message='Already activated.')
        data = request.get_json(force=True, silent=True) or {}
        key = (data.get('key') or '').strip()
        if not _lic_state['hwid']:
            return jsonify(ok=False, message='Could not read this PC hardware ID.')
        if not key:
            return jsonify(ok=False, message='Enter a license key.')
        # Public-key verify: the pasted key is an RSA signature over this HWID.
        # No private key needed, so activation works on any machine.
        if not rsa_verify(_lic_state['hwid'], key):
            return jsonify(ok=False, message='License key is not valid for this PC.')
        try:
            with open('license.dat', 'w') as f:
                f.write(key)
        except OSError as e:
            return jsonify(ok=False, message='Could not write license.dat: %s' % e)
        _lic_state['ok'] = True
        return jsonify(ok=True, message='Activated.')

    @app.route('/api/pay/create', methods=['POST'])
    def _pay_create():
        if _lic_state['ok']:
            return jsonify(ok=False, message='Already activated.')
        _secret = (_lic_state.get('paymongo_secret') or '').strip()
        _backend = (_lic_state.get('paymongo_backend') or '').strip()
        _price = int(_lic_state.get('license_price_peso') or 0)
        _hw = _lic_state.get('hwid') or ''
        if not _secret and not _backend:
            return jsonify(ok=False, message='Online payment is not configured on this copy.')
        if _price <= 0:
            return jsonify(ok=False, message='License price is not set on this copy.')
        if not _hw:
            return jsonify(ok=False, message='Could not read this PC hardware ID.')
        import requests
        if _backend:
            _tok = (_lic_state.get('paymongo_backend_token') or '').strip()
            _bh = {'Content-Type': 'application/json'}
            if _tok:
                _bh['X-Auth'] = _tok
            try:
                _rb = requests.post(_backend.rstrip('/') + '/api/pay/create',
                                    headers=_bh, json={'hwid': _hw}, timeout=35)
                _jb = _rb.json()
            except Exception as e:
                return jsonify(ok=False, message='Payment backend error: %s' % e)
            if not (isinstance(_jb, dict) and _jb.get('ok')):
                return jsonify(ok=False, message=str((_jb or {}).get('message') or 'Backend refused (check token/hwid).'))
            _pi = _jb.get('id')
            _qr = _jb.get('qr_image')
            _turl = _jb.get('test_url') or ''
            if not _pi or not _qr:
                return jsonify(ok=False, message='Could not start QR payment.')
            _sess = _lic_state.get('pay_session') or {}
            _sess.update({'checkout_id': _pi, 'payment_intent': _pi, 'hwid': _hw})
            _lic_state['pay_session'] = _sess
            return jsonify(ok=True, id=_pi, qr_image=_qr, test_url=_turl)
        _api = 'https://api.paymongo.com/v1'
        _hdr = _pm_headers(_secret)
        try:
            _rpi = requests.post(_api + '/payment_intents', headers=_hdr,
                                 json={'data': {'attributes': {
                                     'amount': _price * 100, 'currency': 'PHP',
                                     'description': 'Leiturgia license for PC-%s' % _hw[:12],
                                     'payment_method_allowed': ['qrph'],
                                     'metadata': {'hwid': _hw},
                                 }}}, timeout=30)
            _jpi = _rpi.json()
            if _rpi.status_code >= 400:
                return jsonify(ok=False, message='PayMongo error: %s'
                               % _json.dumps(_jpi.get('errors'))[:400])
            _pi = (_jpi.get('data') or {}).get('id')
            _ck = ((_jpi.get('data') or {}).get('attributes') or {}).get('client_key')
            _rpm = requests.post(_api + '/payment_methods', headers=_hdr,
                                 json={'data': {'attributes': {'type': 'qrph'}}}, timeout=30)
            if _rpm.status_code >= 400:
                return jsonify(ok=False, message='PayMongo error: %s'
                               % _json.dumps(_rpm.json().get('errors'))[:400])
            _pm = (_rpm.json().get('data') or {}).get('id')
            _rat = requests.post(_api + '/payment_intents/%s/attach' % _pi, headers=_hdr,
                                 json={'data': {'attributes': {'payment_method': _pm,
                                                               'client_key': _ck}}}, timeout=30)
            _jat = _rat.json()
            if _rat.status_code >= 400:
                return jsonify(ok=False, message='PayMongo error: %s'
                               % _json.dumps(_jat.get('errors'))[:400])
            _att = ((_jat.get('data') or {}).get('attributes') or {})
            _code = ((_att.get('next_action') or {}).get('code') or {})
            _qr = _code.get('image_url') or ''
            _turl = _code.get('test_url') or ''
        except Exception as e:
            return jsonify(ok=False, message='PayMongo connection error: %s' % e)
        if not _pi or not _qr:
            return jsonify(ok=False, message='Could not start QR payment.')
        _sess = _lic_state.get('pay_session') or {}
        _sess.update({'checkout_id': _pi, 'payment_intent': _pi, 'hwid': _hw})
        _lic_state['pay_session'] = _sess
        return jsonify(ok=True, id=_pi, qr_image=_qr, test_url=_turl)

    @app.route('/api/pay/status')
    def _pay_status():
        _pid = request.args.get('id') or ''
        if _lic_state['ok']:
            return jsonify(paid=True, activated=True)
        _secret = (_lic_state.get('paymongo_secret') or '').strip()
        _backend = (_lic_state.get('paymongo_backend') or '').strip()
        _sess = _lic_state.get('pay_session') or {}
        if not _pid or _sess.get('checkout_id') != _pid:
            return jsonify(paid=False, message='unknown session')
        _pi = _sess.get('payment_intent')
        if not _pi:
            return jsonify(paid=False, message='not ready')
        if _backend:
            import requests as _req
            _tok = (_lic_state.get('paymongo_backend_token') or '').strip()
            _bh = {'X-Auth': _tok} if _tok else {}
            try:
                _rb = _req.get(_backend.rstrip('/') + '/api/pay/status',
                               params={'id': _pi}, headers=_bh, timeout=30)
                _jb = _rb.json()
            except Exception:
                return jsonify(paid=False, message='backend status check failed')
            if _jb.get('paid'):
                if _sess.get('hwid') == _lic_state.get('hwid'):
                    _lic = (_jb.get('license') or '').strip()
                    if _lic:
                        try:
                            with open('license.dat', 'w') as f:
                                f.write(_lic)
                        except OSError as e:
                            return jsonify(paid=False, message='write failed: %s' % e)
                        _lic_state['ok'] = True
                        return jsonify(paid=True, activated=True)
                    # Backend paid but returned no license (private key missing).
                    return jsonify(paid=True, activated=False,
                                   message='Payment confirmed. Send this HWID to the owner to receive your license key.',
                                   hwid=_lic_state.get('hwid'))
                return jsonify(paid=False, message='hardware mismatch')
            return jsonify(paid=False, status=_jb.get('status'))
        if not _secret:
            return jsonify(paid=False, message='not ready')
        try:
            import requests
            _r = requests.get('https://api.paymongo.com/v1/payment_intents/' + _pi,
                              headers=_pm_headers(_secret), timeout=25)
            _j = _r.json()
            _st = ((_j.get('data') or {}).get('attributes') or {}).get('status', '')
        except Exception:
            return jsonify(paid=False, message='status check failed')
        if _st in ('succeeded', 'paid'):
            if _sess.get('hwid') == _lic_state.get('hwid'):
                try:
                    with open('license.dat', 'w') as f:
                        f.write(make_licence(_lic_state['hwid']))
                except RuntimeError:
                    # Paid, but this machine holds no owner private key, so
                    # it cannot self-issue. The owner issues the key.
                    return jsonify(paid=True, activated=False,
                                   message='Payment confirmed. Send this HWID to the owner to receive your license key.',
                                   hwid=_lic_state.get('hwid'))
                except OSError as e:
                    return jsonify(paid=False, message='write failed: %s' % e)
                _lic_state['ok'] = True
                return jsonify(paid=True, activated=True)
            return jsonify(paid=False, message='hardware mismatch')
        return jsonify(paid=False, status=_st)


# Import live_input BEFORE eventlet.monkey_patch() so its capture threads bind
# to the real (non-green) threading/time modules. See live_input docstring.
import live_input

# Same for audio_input: its level-reader thread does blocking pipe reads and
# must be a real OS thread, or it stalls the whole eventlet hub.
import audio_input

import eventlet
eventlet.monkey_patch()

from app import app, socketio


def _fix_asset_paths(app_obj):
    """Point Flask's template/static search paths at the real asset folders.

    The sourceless app module lives in ``pymod/``, so ``app.root_path`` resolves
    to ``<dir>/pymod`` and its default ``templates``/``static`` folders look in
    ``<dir>/pymod/templates`` (which does not exist). In the packaged build the
    assets are bundled at the top level (``HERE``/``_MEIPASS``), so override the
    Jinja searchpath and the static folder to point there. This keeps dev-mode
    and both onefile / onedir builds working.
    """
    tpl = _resolve('templates')
    sta = _resolve('static')
    if tpl and os.path.isdir(tpl):
        from flask import send_from_directory
        # Bundle first: the packaged templates are always in sync with the
        # running code. The on-disk folder is only a fallback (dev layout /
        # legacy installs) so a stale install dir can never shadow a fresh
        # build again.
        _bundled_tpl = os.path.join(_MEIPASS, 'templates') if _MEIPASS else None
        _search = []
        if _bundled_tpl and os.path.isdir(_bundled_tpl) and _bundled_tpl != tpl:
            _search.append(_bundled_tpl)
        _search.append(tpl)
        app_obj.jinja_loader.searchpath = _search
        app_obj.template_folder = tpl
    if sta and os.path.isdir(sta):
        app_obj.static_folder = sta
    # In a frozen onefile build app.root_path resolves to the temp extract dir
    # (_MEIPASS), so Flask's send_from_directory() resolves relative media dirs
    # (media_manager.videos_dir() -> 'media/videos', media/images) against the
    # temp folder and every /media/* URL 404s. The app's data folders live next
    # to the exe (cwd), so repoint root_path there. Templates/static are already
    # pinned to absolute search paths above, so this only affects media serving.
    app_obj.root_path = os.path.abspath('.')


_fix_asset_paths(app)


class _NoStoreCache:
    """Force every response through no-store so operator/OED/remote pages are
    never served from the browser's stale cache after a redeploy. Without this,
    Flask sends no Cache-Control headers, browsers keep the old HTML heurist
    cached, and the app 'does not follow' edits until a manual hard refresh."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        def _start(status, headers, exc_info=None):
            hs = []
            for k, v in headers:
                if k.lower() == 'cache-control':
                    v = 'no-store, no-cache, must-revalidate, max-age=0'
                hs.append((k, v))
            if not any(k.lower() == 'cache-control' for k, _ in hs):
                hs.append(('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0'))
            return start_response(status, hs, exc_info)

        return self.wsgi_app(environ, _start)


app.wsgi_app = _NoStoreCache(app.wsgi_app)

# Add the licence gate once the full Flask app object exists.
wire_license_gate()

# Register the server-side Live Input endpoints (additive; app.pyc untouched).
live_input.init_app(app)

# Register the Bible verse search endpoints (additive; app.pyc untouched).
import bible
bible.init_app(app)

# Register the Stream Console page route (additive; app.pyc untouched).
import stream_console_routes
stream_console_routes.init_app(app)

# Register the Screen Config endpoints (additive; app.pyc untouched).
import screen_config_routes
screen_config_routes.init_app(app)

# Register the USB/AUX audio input endpoints (additive; app.pyc untouched).
# (Module imported pre-monkey_patch above; init here where app exists.)
audio_input.init_app(app)

# Register the real ffmpeg broadcast encoder (additive; app.pyc untouched).
# Overrides the compiled SIMULATED api_broadcast_{start,stop,status} with a real
# ffmpeg gdigrab encoder pushing the projection monitor output to RTMP.
import broadcast_encoder
broadcast_encoder.init_app(app)


def _install_update_selfexit(flask_app):
    """Wrap the compiled api_update_start so the applier survives its own
    first step. The applier begins with `taskkill /T` on the Leiturgia
    processes, which kills the whole process tree — including the applier
    itself while it is still a child of this server (DETACHED_PROCESS does
    not protect from /T; proven by test). After a successful spawn (202),
    this server exits a few seconds later so the applier is orphaned and
    the tree-kill can no longer reach it. Delegates everything else to the
    original view, so update behavior is otherwise unchanged."""
    try:
        _orig = flask_app.view_functions.get('api_update_start')
        if not _orig:
            return
        import threading as _th
        import time as _t

        def _wrapped(*_a, **_k):
            _r = _orig(*_a, **_k)
            try:
                _code = _r[1] if isinstance(_r, tuple) else getattr(_r, 'status_code', None)
            except Exception:
                _code = None
            if _code == 202:
                def _bye():
                    _t.sleep(5)
                    os._exit(0)
                _th.Thread(target=_bye, daemon=True).start()
            return _r

        _wrapped.__name__ = 'api_update_start'
        flask_app.view_functions['api_update_start'] = _wrapped
    except Exception:
        pass


_install_update_selfexit(app)


def _spawn_stream_child():
    """Launch the dedicated live-stream daemon as a SEPARATE process.

    Live capture can deadlock the eventlet hub when it shares the main process.
    A child process (the same exe re-run with ``--stream``) isolates OpenCV on
    its own native threads + ports, so the operator/projection server can never
    freeze. Best-effort: if the spawn fails, streaming stays unavailable but the
    main server is unaffected.
    """
    import subprocess
    try:
        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, '--stream']
            cwd = os.path.dirname(sys.executable)
        else:
            here = os.path.dirname(os.path.abspath(__file__))
            cmd = [sys.executable, '-m', 'stream_server', '--stream']
            cwd = here
        flag = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        subprocess.Popen(cmd, cwd=cwd, creationflags=flag)
    except Exception:
        pass


_spawn_stream_child()

# Register the build-id endpoint + HTML stamping so long-open output/operator
# tabs auto-reload as soon as a new build is deployed (additive; app.pyc
# untouched). Must run after live_input so the version middleware wraps last.
import server_version
server_version.init_app(app, inject=True)


def _ensure_dirs():
    for p in ('data', 'data/update', 'data/lyrics', 'output', 'media/images', 'media/videos'):
        os.makedirs(p, exist_ok=True)


def _clean_tmp():
    for _p in glob.glob(os.path.join('data', '.tmp-*.json')):
        try:
            os.unlink(_p)
        except OSError:
            continue


def _reconcile_update_lock():
    """Self-heal a stale update lock. The server creates data/update/lock when
    an update starts; older updater.exe builds never released it, so any later
    update is rejected with 'update already in progress'. On boot, drop the
    lock when no update can legitimately be in flight: a terminal status
    (success/error), a stale 'applying' status, or an orphaned lock file."""
    import json as _json
    import time as _time
    _STALE_SECS = 3600
    try:
        _lock = os.path.join('data', 'update', 'lock')
        if not os.path.exists(_lock):
            return
        _now = _time.time()
        _status_path = os.path.join('data', 'update', 'status.json')
        try:
            with open(_status_path) as _f:
                _st = _json.load(_f)
        except (OSError, ValueError):
            _st = {}
        _state = (_st.get('status') or '')
        if _state in ('success', 'error'):
            os.unlink(_lock)
            return
        if _state == 'applying':
            try:
                _age = _now - float(_st.get('ts', 0))
            except (TypeError, ValueError):
                _age = _STALE_SECS + 1
            if _age > _STALE_SECS:
                os.unlink(_lock)
            return
        try:
            _lock_age = _now - os.path.getmtime(_lock)
        except OSError:
            return
        if _lock_age > _STALE_SECS:
            os.unlink(_lock)
    except OSError:
        pass


if __name__ == '__main__':
    _ensure_dirs()
    _clean_tmp()
    _reconcile_update_lock()
    # Allow an alternate port (LEITURGIA_PORT) for dev/test without clobbering
    # the live server on 5001.
    import os as _os
    _port = int(_os.environ.get('LEITURGIA_PORT', '5001'))
    socketio.run(app, host='0.0.0.0', port=_port, debug=False)
