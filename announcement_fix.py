"""announcement_fix.py — Push console announcements to announcement-role outputs.

The compiled pymod/app.pyc on_announcement handler only did:

    proj.set_state(channel, state)
    emit('announcement', data, to=channel)

so the payload went only to the console's own channel room. The main-role
output (projection.html) listens for 'announcement' and worked, but the
announcement-role output (announcement.html) only listens for
'announcement:update' / 'announcement:blank' and joins its own room (ch4) —
it never received anything, which is the reported "announcements don't push
to the screen-config output" bug. The compiled handler also never updated
the module-level _announcement_text, so on_join replay and the template's
initial render were always stale.

Console sendAnnouncement() (index.html) emits ONLY the 'announcement' event
(it never calls announcement:push / announcement:blank / /api/announcement),
so replacing that one handler fixes the whole path.

Following the screen_config_routes / bible / live_input pattern: source
app.py edits are ignored at runtime (pymod/*.pyc wins), so this registers at
boot via init_app(). socketio.on('announcement') re-registration overwrites
the compiled handler (python-socketio stores handlers in a dict keyed by
event), and flask-socketio's decorator wraps our function with
_handle_event — same request-context / managed-session behavior the
compiled handlers get, so flask session / emit / disconnect work identically.
"""

def init_app(app):
    import app as _app_mod
    from app import roles, socketio
    from flask import session
    from flask_socketio import emit, disconnect

    proj = _app_mod.proj

    @socketio.on('announcement')
    def _on_announcement(data):
        if not isinstance(data, dict):
            return
        if not session.get('operator'):
            disconnect()
            return
        channel = data.get('channel', 'ch1')
        state = {'type': 'announcement', 'data': data, 'theme_id': 'default'}

        # Same behavior as the compiled handler for the pushed channel
        # (projection.html on main-role outputs listens for 'announcement').
        proj.set_state(channel, state)
        emit('announcement', data, to=channel)

        # Fan out to the pushed channel's role siblings (slide:show pattern)
        # so multi-output roles stay in sync.
        for ch in roles.get_channels(roles.get_role(channel) or 'main'):
            if ch == channel:
                continue
            proj.set_state(ch, state)
            socketio.emit('announcement', data, room=ch)

        # Keep the replay text fresh for compiled on_join and template render.
        _app_mod._announcement_text = data.get('text', '')

        # THE FIX: announcement.html only listens for 'announcement:update'.
        # Full payload (fonts included) so _applyFont() works; the compiled
        # handler's {'text': ...}-only payloads still work on the client
        # (missing fields fall back to defaults).
        for ch in roles.get_channels('announcement'):
            socketio.emit('announcement:update', data, room=ch)
