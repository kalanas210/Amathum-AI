#!/usr/bin/env python3
"""Naxter PBX Monitor — full admin panel."""
import ipaddress
import os, csv, json, subprocess, datetime, re, time, socket, uuid, struct, hashlib, wave, threading, secrets, hmac
from pathlib import Path
from functools import wraps
from collections import defaultdict
from flask import (Flask, render_template, jsonify, request, abort, Response,
                   session, redirect, url_for, flash, send_file)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
try:
    import boto3
    from botocore.exceptions import ClientError, BotoCoreError
except ImportError:
    boto3 = None

# Install root. Overridable so the app can be imported against a throwaway tree
# (the restaurant transcript tests do exactly that) without touching /opt.
BASE = Path(os.environ.get('PBX_MONITOR_BASE', '/opt/pbx-monitor'))
AUTH_FILE = BASE / 'instance' / 'auth.json'
AMI_FILE = BASE / 'instance' / 'ami.json'
AWS_FILE = BASE / 'instance' / 'aws.json'

# ============================================================================
# SITE CONFIG — one codebase, several boxes
# ============================================================================
# This app started life on one machine (the Sri Lankan home box: raw Asterisk +
# the sampath-ai Node bridge) and every path, service name and feature was a
# constant. It now also runs on remote FreePBX boxes with their own voice agent,
# so everything that DIFFERS between boxes is collected into one small JSON file
# read once, here, at boot.
#
#   NO FILE PRESENT -> the defaults below, which are exactly the values this app
#                      has always used on the home box. That is the contract:
#                      adding site support must not change home behaviour.
#
# The full documented schema is config/site-example.json; the live remote one is
# config/site-linode.json. Anything not mentioned in the file keeps its default.
SITE_FILE = Path(os.environ.get('PBX_MONITOR_SITE', '/etc/pbx-monitor/site.json'))

# Feature switches. Every one defaults ON so a box with no site.json — i.e. home
# — behaves exactly as before. A remote box turns OFF what is meaningless there
# rather than having the code deleted; see PORTING.md for the reasoning.
FEATURE_DEFAULTS = {
    'trunk_recovery': True,   # /trunk, soft-recover, SIP capture, snapshots — Dialog SLT trunk
    'broadcast': True,        # AWS Polly TTS broadcast
    'webphone': True,         # browser softphone bound to ext 1010
    'survey': True,           # dial-pad NPS survey ([nps-survey] context)
    'flows': True,            # multi-agent flow editor + Gemini flow generation
    'agent_mode': True,       # which persona the home agent answers as
    'ai_agent': True,         # /ai-agent — config editor for the home bridge
    'ai_sessions': True,      # /calls, /customers, session transcripts & recordings
    'mesh': True,             # inter-PBX trunks + generated IVR
    'auto_confirm': True,     # background sales order confirm-call watcher
}

SITE_DEFAULTS = {
    'site_id': 'home',
    'site_name': 'Naxter PBX',
    'site_subtitle': 'DID 0114794050',
    # 'raw-asterisk': /etc/asterisk/*.conf are ours to edit.
    # 'freepbx':      they are GENERATED and `fwconsole reload` overwrites them,
    #                 so we must edit the *_custom.conf includes instead.
    'flavour': 'raw-asterisk',
    'bind': {'host': '127.0.0.1', 'port': 5051},
    'dashboards': ['reservations', 'hospital', 'sales', 'restaurant'],
    'features': {},
    'agent': {
        'service': 'sampath-ai',
        'install_dir': '/opt/sampath-ai',
        'data_dir': '/var/lib/sampath-ai',
        'sessions_dir': None,          # default <data_dir>/sessions
        'customers_dir': None,         # default <data_dir>/customers
        'channels_dir': None,          # default <data_dir>/channels
        'config_file': None,           # default <install_dir>/agent-config.json
        'env_file': None,              # default <install_dir>/.env
        'voice_sample_script': None,   # default <install_dir>/voice-sample.cjs
    },
    'paths': {
        'asterisk_bin': 'asterisk',
        'extensions_conf': None,       # default depends on flavour (see above)
        'pjsip_conf': None,            # ditto
        'asterisk_log': '/var/log/asterisk/messages.log',
        'recordings_dir': '/var/spool/asterisk/recordings',
        'voicemail_root': '/var/spool/asterisk/voicemail/default',
        'ivr_audio_dir': '/usr/share/asterisk/sounds/en/custom',
    },
    'services': ['asterisk', 'cloudflared', 'sip-trunk-route', 'tailscaled', 'pbx-monitor'],
    'cdr': {
        'backend': 'csv',              # 'csv' | 'mysql' | 'auto' (csv, then mysql)
        'csv_file': '/var/log/asterisk/cdr-csv/Master.csv',
        'freepbx_conf': '/etc/freepbx.conf',
        'mysql_bin': '/usr/bin/mysql',
        'mysql_db': 'asteriskcdrdb',
    },
    # Per-vertical record/refdata location overrides. A vertical whose records
    # are written by a DIFFERENT agent (petcare-ai) does not live under the home
    # agent's bookings tree, so it needs an absolute path.
    #   "record_dirs":   {"petcare": {"appointments": "/var/lib/petcare-ai/bookings/appointments"}}
    #   "refdata_files": {"petcare": "/var/lib/petcare-ai/refdata/petcare.json"}
    'record_dirs': {},
    'refdata_files': {},
}


def _site_merge(base, over):
    """Recursive dict merge — a site file only states what it changes."""
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _site_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _load_site():
    try:
        with SITE_FILE.open() as f:
            raw = json.load(f)
    except FileNotFoundError:
        return dict(SITE_DEFAULTS), None
    except Exception as e:
        # Never fall back to the home defaults on a malformed file: on a remote
        # box that would silently point the app at the wrong data directory,
        # edit the WRONG (generated) Asterisk configs and switch home-only
        # features back on. Refuse to start instead.
        raise SystemExit('pbx-monitor: %s is unreadable (%s) — refusing to start '
                         'rather than fall back to the home defaults' % (SITE_FILE, e))
    if not isinstance(raw, dict):
        raise SystemExit('pbx-monitor: %s must contain a JSON object' % SITE_FILE)
    return _site_merge(SITE_DEFAULTS, raw), str(SITE_FILE)


SITE, SITE_PATH = _load_site()
FLAVOUR = 'freepbx' if str(SITE.get('flavour', '')).lower() == 'freepbx' else 'raw-asterisk'
IS_FREEPBX = (FLAVOUR == 'freepbx')

FEATURES = dict(FEATURE_DEFAULTS)
for _k, _v in (SITE.get('features') or {}).items():
    if _k in FEATURE_DEFAULTS:
        FEATURES[_k] = bool(_v)
    else:
        print('[site] ignoring unknown feature flag %r in %s' % (_k, SITE_PATH), flush=True)

_SITE_PATHS = SITE.get('paths') or {}
_SITE_AGENT = SITE.get('agent') or {}


def _site_path(section, key, default):
    v = section.get(key)
    return Path(v) if v else Path(default)


def feature(name):
    """Is this feature switched on for this box?"""
    return bool(FEATURES.get(name, True))


AGENT_SERVICE = _SITE_AGENT.get('service') or 'sampath-ai'
AGENT_INSTALL_DIR = _site_path(_SITE_AGENT, 'install_dir', '/opt/sampath-ai')
AGENT_DATA_DIR = _site_path(_SITE_AGENT, 'data_dir', '/var/lib/sampath-ai')

ASTERISK_BIN = _SITE_PATHS.get('asterisk_bin') or 'asterisk'
TTS_CACHE = Path('/var/lib/asterisk/sounds/custom/tts-cache')
TTS_CACHE_SHARED = Path('/usr/share/asterisk/sounds/custom/tts-cache')
CDR_PATH = _site_path(SITE.get('cdr') or {}, 'csv_file', '/var/log/asterisk/cdr-csv/Master.csv')
LOG_PATH = _site_path(_SITE_PATHS, 'asterisk_log', '/var/log/asterisk/messages.log')
# On FreePBX, extensions.conf and pjsip.conf are GENERATED — `fwconsole reload`
# rewrites them wholesale, so anything this app writes there is lost on the next
# admin action (and can break the dialplan in the meantime). FreePBX includes
# *_custom.conf for exactly this purpose; that is what the editor pages and the
# mesh IVR writer must target.
EXTENSIONS_CONF = _site_path(_SITE_PATHS, 'extensions_conf',
                             '/etc/asterisk/extensions_custom.conf' if IS_FREEPBX
                             else '/etc/asterisk/extensions.conf')
PJSIP_CONF = _site_path(_SITE_PATHS, 'pjsip_conf',
                        '/etc/asterisk/pjsip_custom.conf' if IS_FREEPBX
                        else '/etc/asterisk/pjsip.conf')
SIP_CAPTURE_DIR = Path('/var/log/sip-capture')
PCAP_PATH = Path('/tmp/sip-monitor.pcap')  # legacy fallback; real capture is in SIP_CAPTURE_DIR

# Shared data tree with the voice-agent bridge. Dashboards whose records are REAL
# (captured from live calls or entered by staff) read/write here. The env var
# still wins so the restaurant transcript tests can point it at a temp tree.
SAMPATH_DATA = Path(os.environ.get('SAMPATH_DATA_DIR') or str(AGENT_DATA_DIR))
BOOKINGS_DIR = SAMPATH_DATA / 'bookings'   # BOOKINGS_DIR/<vertical>/<collection>/<id>.json
REFDATA_DIR = SAMPATH_DATA / 'refdata'     # REFDATA_DIR/<vertical>.json (dummy reference catalog)
ACTIVE_FLOW_FILE = SAMPATH_DATA / 'active-flow.json'   # which flow the voice agent answers as
FLOWS_DIR_PATH = SAMPATH_DATA / 'flows'
OUTBOUND_DIR = SAMPATH_DATA / 'outbound'   # per-call order context for outbound AI confirm-calls
SURVEY_RESULTS_DIR = SAMPATH_DATA / 'survey'          # DTMF feedback/NPS responses (one JSON/response)
INBOUND_MODE_FILE = SAMPATH_DATA / 'inbound-mode'     # "survey" -> DID diverts to [nps-survey]; else AI agent
SURVEY_ID = 'hemas-nps'                               # the active dial-pad survey
SURVEY_CONTEXT = 'nps-survey'                          # dialplan context that plays it + reads DTMF


def _current_pcap():
    """Return the newest sip.pcap* file from the rolling capture, or None."""
    if SIP_CAPTURE_DIR.exists():
        pcaps = list(SIP_CAPTURE_DIR.glob('sip.pcap*'))
        if pcaps:
            return max(pcaps, key=lambda p: p.stat().st_mtime)
    if PCAP_PATH.exists():
        return PCAP_PATH
    return None
SOUNDS_DIRS = [Path('/var/lib/asterisk/sounds/custom'),
               Path('/usr/share/asterisk/sounds/custom')]
VM_ROOT = _site_path(_SITE_PATHS, 'voicemail_root', '/var/spool/asterisk/voicemail/default')
RECORDINGS_DIR = _site_path(_SITE_PATHS, 'recordings_dir', '/var/spool/asterisk/recordings')
GREETING_NAME = 'naxter-test-greeting'

# Roles
ROLES = ['admin', 'operator', 'viewer']
ROLE_PERMS = {
    'admin':    {'read', 'call', 'admin', 'config'},
    'operator': {'read', 'call'},
    'viewer':   {'read'},
}

# ==================== INDUSTRY DASHBOARDS ====================
# Industry-specific dashboards that can be attached to a user account *in addition*
# to the core PBX panel. Adding a new vertical = one entry here + a
# static/data/<id>.js dummy-data file. Access is granted per-user (see auth.json
# users[].dashboards); admins implicitly see every dashboard.
#
# This is the CATALOGUE of everything the codebase can render. Which of them a
# given box actually serves is site["dashboards"] — a vertical that is not
# enabled here is not in DASHBOARDS at all, so _dash_guard 404s it, the nav does
# not show it and user_dashboards() cannot grant it.
DASHBOARD_REGISTRY = {
    'reservations': {
        'label': 'Reservations',
        'icon': 'calendar-check',
        'desc': 'Bookings, confirmation calls, deposits & guest CRM',
    },
    'hospital': {
        'label': 'Hospital',
        'icon': 'stethoscope',
        'desc': 'Appointments, doctors, lab services & patient billing',
    },
    'sales': {
        'label': 'Sales',
        'icon': 'shopping-cart',
        'desc': 'Leads, orders, order-confirmation calls & payments',
    },
    'restaurant': {
        'label': 'Restaurant',
        'icon': 'utensils-crossed',
        'desc': 'Live order board, menu, promotions, riders & customer CRM',
    },
    'petcare': {
        'label': 'Pet care',
        'icon': 'paw-print',
        'desc': 'Vet appointments taken by the phone agent — owners, pets, vets & branches',
    },
    'computershop': {
        'label': 'Computer Shop',
        'icon': 'laptop',
        'desc': 'Product orders & repair jobs taken by the phone agent — customers, stock & branches',
    },
}

DASHBOARDS = {k: DASHBOARD_REGISTRY[k]
              for k in (SITE.get('dashboards') or [])
              if k in DASHBOARD_REGISTRY}
for _d in (SITE.get('dashboards') or []):
    if _d not in DASHBOARD_REGISTRY:
        print('[site] ignoring unknown dashboard %r in %s' % (_d, SITE_PATH), flush=True)

# Voice-agent "modes": which industry persona the agent answers calls as. Each
# maps to a flow file under SAMPATH_DATA/flows/. Switching mode just repoints
# active-flow.json — the bridge loads it on the next call (no restart). One
# active mode at a time.
AGENT_MODES = [
    {'key': 'hospital',     'label': 'Hospital',     'icon': 'stethoscope',     'flow': 'durdans',
     'desc': 'Greets as the hospital, books appointments, finds doctors & lab services.'},
    {'key': 'reservations', 'label': 'Reservations', 'icon': 'calendar-check',  'flow': 'reservations',
     'desc': 'Greets as the restaurant host and books tables.'},
    {'key': 'sales',        'label': 'Sales',        'icon': 'shopping-cart',   'flow': 'sales',
     'desc': 'Greets as the store assistant, checks stock & takes product orders.'},
    {'key': 'restaurant',   'label': 'Restaurant',   'icon': 'utensils-crossed', 'flow': 'restaurant',
     'desc': 'Greets as the fried-chicken shop, takes food orders, quotes delivery times & handles order status.'},
]

with AUTH_FILE.open() as f:
    AUTH = json.load(f)
with AMI_FILE.open() as f:
    AMI_CFG = json.load(f)
try:
    with AWS_FILE.open() as f:
        AWS_CFG = json.load(f)
except Exception:
    AWS_CFG = None

app = Flask(__name__)
app.secret_key = AUTH['secret_key']
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024

# Session hardening. Previously only secret_key + MAX_CONTENT_LENGTH were set, so
# session.permanent = True inherited Flask's 31-day default and a stolen cookie
# stayed valid for a month. cookie_secure defaults on because production reaches
# this app over HTTPS via cloudflared; set "cookie_secure": false in auth.json if
# you ever need to sign in over plain http://127.0.0.1:5050 directly.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(AUTH.get('cookie_secure', True)),
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=int(AUTH.get('session_hours', 12))),
)


# ==================== CSRF ====================
# Every mutator used request.get_json(force=True), which parses a body whatever
# its Content-Type — so the usual "JSON APIs are preflight-protected" argument
# did not hold and a cross-site <form enctype="text/plain"> POST could reach
# /api/users (create an admin), /api/restart-asterisk, /api/make-call, etc.
# Token is per-session; the browser side attaches it automatically (base.html).
CSRF_HEADER = 'X-CSRF-Token'
# The Node bridge authenticates with a shared secret and has no session or
# cookies, so it cannot carry a CSRF token — and CSRF is meaningless there.
CSRF_EXEMPT_PREFIXES = ('/api/agent/',)


def csrf_token():
    t = session.get('_csrf')
    if not t:
        t = secrets.token_urlsafe(32)
        session['_csrf'] = t
    return t


@app.before_request
def _csrf_protect():
    if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
        return
    if request.path.startswith(CSRF_EXEMPT_PREFIXES):
        return
    sent = request.headers.get(CSRF_HEADER, '')
    if not sent and request.mimetype in ('application/x-www-form-urlencoded', 'multipart/form-data'):
        sent = request.form.get('_csrf', '')
    expected = session.get('_csrf', '')
    if not expected or not sent or not hmac.compare_digest(str(sent), str(expected)):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'csrf', 'detail': 'missing or invalid CSRF token'}), 403
        flash('Your session expired — please try again.', 'error')
        return redirect(url_for('login'))


@app.context_processor
def _inject_csrf():
    return {'csrf_token': csrf_token()}


# Pre-computed hash used to keep unknown-user logins on the same code path as
# real ones (see login()). Value is irrelevant; it never matches.
_DUMMY_HASH = generate_password_hash('not-a-real-password', 'pbkdf2:sha256', salt_length=16)


WEBPHONE_FILE = BASE / 'instance' / 'webphone.json'
_WEBPHONE_LEGACY = {'extension': '1010', 'password': 'WebRtc1010!', 'realm': '192.168.1.132'}


def _webphone_cfg():
    """Softphone credentials from instance/webphone.json.

    Seeded once from the value that used to be hardcoded in this file, so the
    webphone keeps working across this change; rotate it afterwards."""
    try:
        return json.loads(WEBPHONE_FILE.read_text())
    except Exception:
        try:
            WEBPHONE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = WEBPHONE_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(_WEBPHONE_LEGACY, indent=2))
            os.chmod(tmp, 0o640)
            tmp.replace(WEBPHONE_FILE)
        except Exception:
            pass
        return dict(_WEBPHONE_LEGACY)


def save_auth():
    tmp = AUTH_FILE.with_suffix('.tmp')
    with tmp.open('w') as f: json.dump(AUTH, f, indent=2)
    tmp.chmod(0o640); tmp.replace(AUTH_FILE)


# ==================== AMI CLIENT ====================
class AMI:
    def __init__(self, host='127.0.0.1', port=5038):
        self.host, self.port = host, port
        self.user, self.secret = AMI_CFG['user'], AMI_CFG['secret']
        self.sock = None

    def _recv_one_message(self, deadline):
        """Read until next \\r\\n\\r\\n. Buffer leftover for subsequent calls."""
        if not hasattr(self, '_buf'): self._buf = b''
        while b'\r\n\r\n' not in self._buf:
            remaining = max(0.5, deadline - time.time())
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(4096)
                if not chunk: break
                self._buf += chunk
            except socket.timeout:
                raise
        if b'\r\n\r\n' in self._buf:
            msg, self._buf = self._buf.split(b'\r\n\r\n', 1)
            return msg.decode(errors='replace')
        return ''

    def _parse(self, raw):
        out = {}
        for line in raw.splitlines():
            if ':' in line:
                k, _, v = line.partition(':')
                out[k.strip()] = v.strip()
        return out

    def _send(self, fields):
        """Send action and return the first message whose top key is 'Response'.
        Skips intervening Event messages (FullyBooted, etc.)."""
        msg = '\r\n'.join(f'{k}: {v}' for k, v in fields.items() if v is not None) + '\r\n\r\n'
        self.sock.sendall(msg.encode())
        deadline = time.time() + 12
        while time.time() < deadline:
            raw = self._recv_one_message(deadline)
            if not raw: break
            parsed = self._parse(raw)
            if 'Response' in parsed:
                return parsed
        raise socket.timeout('No Response from AMI within 12s')

    def __enter__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(8)
        self.sock.connect((self.host, self.port))
        self._buf = b''
        # banner
        try:
            self.sock.settimeout(2)
            self._buf += self.sock.recv(4096)
        except socket.timeout: pass
        # Login
        r = self._send({'Action': 'Login', 'Username': self.user, 'Secret': self.secret})
        if r.get('Response') != 'Success':
            raise RuntimeError(f"AMI login failed: {r}")
        return self

    def __exit__(self, *a):
        try: self._send({'Action': 'Logoff'})
        except Exception: pass
        try: self.sock.close()
        except Exception: pass

    def originate(self, channel, context, exten='s', priority=1, callerid='',
                  timeout_ms=30000, async_=True, variables=None):
        fields = {
            'Action': 'Originate', 'ActionID': str(uuid.uuid4()),
            'Channel': channel, 'Context': context, 'Exten': exten,
            'Priority': str(priority), 'CallerID': callerid,
            'Timeout': str(timeout_ms), 'Async': 'true' if async_ else 'false',
        }
        if variables:
            # AMI Variable: header takes comma-separated K=V pairs
            fields['Variable'] = ','.join(f'{k}={v}' for k, v in variables.items())
        return self._send(fields)

    def originate_wait(self, channel, context, exten='s', priority=1, callerid='',
                       timeout_ms=45000, variables=None, wait_s=15):
        """Async-originate, then wait up to wait_s for the OriginateResponse event so
        we can report the REAL outcome (channel unavailable / congestion / no answer)
        instead of only the immediate 'queued' Response. Returns
        {'accepted': bool, 'result': <event dict or None>}."""
        action_id = str(uuid.uuid4())
        fields = {
            'Action': 'Originate', 'ActionID': action_id,
            'Channel': channel, 'Context': context, 'Exten': exten,
            'Priority': str(priority), 'CallerID': callerid,
            'Timeout': str(timeout_ms), 'Async': 'true',
        }
        if variables:
            fields['Variable'] = ','.join(f'{k}={v}' for k, v in variables.items())
        accept = self._send(fields)
        accepted = accept.get('Response') == 'Success'
        result = None
        if accepted:
            deadline = time.time() + wait_s
            while time.time() < deadline:
                try:
                    raw = self._recv_one_message(deadline)
                except socket.timeout:
                    break
                if not raw:
                    break
                ev = self._parse(raw)
                if ev.get('Event') == 'OriginateResponse' and ev.get('ActionID') == action_id:
                    result = ev
                    break
        return {'accepted': accepted, 'result': result}


# ==================== POLLY TTS ====================
_polly_client = None
def get_polly():
    global _polly_client
    if _polly_client is None and boto3 and AWS_CFG:
        _polly_client = boto3.client(
            'polly',
            region_name=AWS_CFG['region'],
            aws_access_key_id=AWS_CFG['access_key_id'],
            aws_secret_access_key=AWS_CFG['secret_access_key'],
        )
    return _polly_client

def tts_synthesize(text, voice=None, engine=None):
    """Synthesize text → cached audio files. Returns dict with sound 'name' for Asterisk.
    Cached by hash of (voice, engine, text). Polly only called on cache miss.
    Files written as .wav, .ulaw, .alaw in both Asterisk sounds directories.
    """
    if not boto3 or not AWS_CFG:
        raise RuntimeError("AWS Polly is not configured")
    voice = voice or AWS_CFG.get('default_voice', 'Matthew')
    engine = engine or AWS_CFG.get('default_engine', 'neural')
    text = (text or '').strip()
    if not text:
        raise ValueError("text is empty")
    if len(text) > 3000:
        raise ValueError("text too long (max 3000 chars)")

    h = hashlib.sha256(f"{voice}|{engine}|{text}".encode()).hexdigest()[:16]
    TTS_CACHE.mkdir(parents=True, exist_ok=True)
    TTS_CACHE_SHARED.mkdir(parents=True, exist_ok=True)
    wav_path = TTS_CACHE / f"{h}.wav"

    if not wav_path.exists():
        client = get_polly()
        resp = client.synthesize_speech(
            Text=text, OutputFormat='pcm', VoiceId=voice,
            Engine=engine, SampleRate='8000',
        )
        raw = resp['AudioStream'].read()
        # Polly raw PCM = signed 16-bit little-endian mono at requested rate
        with wave.open(str(wav_path), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(raw)
        # Generate ulaw + alaw via sox
        for ext, t in (('ulaw', 'ul'), ('alaw', 'al')):
            out = TTS_CACHE / f"{h}.{ext}"
            subprocess.run(['sox', str(wav_path), '-r', '8000', '-c', '1', '-t', t, str(out)],
                           check=True, capture_output=True, timeout=20)
        # Mirror into /usr/share path too (Asterisk looks at both)
        for ext in ('wav', 'ulaw', 'alaw'):
            src = TTS_CACHE / f"{h}.{ext}"
            dst = TTS_CACHE_SHARED / f"{h}.{ext}"
            dst.write_bytes(src.read_bytes())
            try:
                os.chmod(src, 0o644); os.chmod(dst, 0o644)
            except Exception: pass

    duration_s = wav_path.stat().st_size / 16000  # 8000 samples/sec * 2 bytes/sample
    return {
        'name':  f"custom/tts-cache/{h}",   # what Asterisk Playback() expects
        'hash':  h,
        'voice': voice, 'engine': engine,
        'chars': len(text),
        'duration_s': round(duration_s, 2),
        'cached': False,  # caller can check wav_path mtime if needed
        'wav_url': f"/api/tts/audio/{h}",
    }


# ==================== AUTH ====================
def login_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if 'user' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*a, **kw)
    return wrapped

def perm_required(perm):
    def decorator(f):
        @wraps(f)
        def wrapped(*a, **kw):
            if 'user' not in session:
                return redirect(url_for('login'))
            role = AUTH['users'].get(session['user'], {}).get('role', 'viewer')
            if perm not in ROLE_PERMS.get(role, set()):
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'error': 'forbidden'}), 403
                abort(403)
            return f(*a, **kw)
        return wrapped
    return decorator

def feature_required(name):
    """Gate a route on a site feature switch.

    A disabled feature is *absent*, not forbidden: it 404s exactly like a route
    that was never registered, because on a box where it does not apply the
    difference is invisible to a caller and 404 leaks nothing about the box.
    The route object still exists, so url_for() in shared templates keeps
    resolving — the nav simply never renders the link (see base.html).
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*a, **kw):
            if not feature(name):
                abort(404)
            return f(*a, **kw)
        return wrapped
    return decorator


def _may_write():
    """May the signed-in user MUTATE dashboard records? Viewers may not — they
    could otherwise create, patch and delete records through the generic
    collection routes, which no permission previously guarded."""
    role = AUTH['users'].get(session.get('user'), {}).get('role', 'viewer')
    return 'call' in ROLE_PERMS.get(role, set())


def user_dashboards(email=None):
    """Industry dashboards a user may access. Admins implicitly get all of them."""
    rec = AUTH['users'].get(email or session.get('user'), {})
    role = rec.get('role', 'viewer')
    if 'admin' in ROLE_PERMS.get(role, set()):
        return list(DASHBOARDS.keys())
    return [d for d in rec.get('dashboards', []) if d in DASHBOARDS]

@app.context_processor
def inject_user():
    user = session.get('user')
    role = AUTH['users'].get(user, {}).get('role', 'viewer') if user else None
    return {'current_user': user, 'current_role': role,
            'role_perms': ROLE_PERMS.get(role, set()) if role else set(),
            'user_dashboards': user_dashboards() if user else [],
            'dashboards_registry': DASHBOARDS,
            # Per-box chrome + nav gating. Templates must never hard-code a site
            # name, a DID or a config filename again.
            'features': FEATURES, 'site': SITE, 'flavour': FLAVOUR,
            'extensions_conf': str(EXTENSIONS_CONF), 'pjsip_conf': str(PJSIP_CONF)}


# ==================== HELPERS ====================
def asterisk_cli(cmd, timeout=8):
    try:
        r = subprocess.run([ASTERISK_BIN, '-rx', cmd], capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception as e:
        return f"<error: {e}>"

def tcpdump_tail(filt='host 10.10.10.89', n=20):
    pcap = _current_pcap()
    if pcap is None: return []
    try:
        r = subprocess.run(['tcpdump', '-r', str(pcap), '-nn', filt],
                           capture_output=True, text=True, timeout=10)
        return [l for l in r.stdout.splitlines() if l][-n:]
    except Exception: return []

def _read_cdr_csv(n=200):
    if not CDR_PATH.exists(): return []
    rows = []
    try:
        with CDR_PATH.open() as f:
            for line in f.readlines()[-n:]:
                try:
                    p = next(csv.reader([line]))
                    if len(p) < 17: continue
                    rows.append({
                        'src': p[1] or '-', 'dst': p[2] or '-',
                        'dcontext': p[3], 'channel': p[5], 'app': p[7],
                        'start': p[9], 'answer': p[10], 'end': p[11],
                        'duration': int(p[12] or 0), 'billsec': int(p[13] or 0),
                        'disposition': p[14],
                    })
                except Exception: continue
    except PermissionError: pass
    return list(reversed(rows))


def _freepbx_db_creds():
    """AMPDBUSER / AMPDBPASS / AMPDBHOST out of /etc/freepbx.conf."""
    try:
        text = Path((SITE.get('cdr') or {}).get('freepbx_conf') or '/etc/freepbx.conf').read_text(
            encoding='utf-8', errors='replace')
    except Exception:
        return None
    out = {}
    for key, field in (('AMPDBUSER', 'user'), ('AMPDBPASS', 'password'), ('AMPDBHOST', 'host')):
        m = re.search(r"\$amp_conf\[['\"]" + key + r"['\"]\]\s*=\s*['\"](.*?)['\"]", text)
        if m:
            out[field] = m.group(1)
    if 'user' not in out:
        return None
    out.setdefault('host', 'localhost')
    out.setdefault('password', '')
    return out


def _read_cdr_mysql(n=200):
    """FreePBX keeps CDR in MySQL (asteriskcdrdb.cdr), not cdr-csv — the CSV
    backend is off by default on a FreePBX install, so /var/log/asterisk/cdr-csv
    is empty and the CSV reader above silently returns nothing.

    Rows are shaped exactly like the CSV ones so every caller (overview, charts,
    /api/cdr, the calls page) is unchanged. FreePBX has no separate answer/end
    columns, so those come back empty rather than invented.
    """
    creds = _freepbx_db_creds()
    if not creds:
        return []
    cdrcfg = SITE.get('cdr') or {}
    n = max(1, min(int(n), 2000))
    # Fixed SQL; `n` is an int clamped just above and formatted with %d, and no
    # other value ever reaches the query.
    sql = ('SELECT src,dst,dcontext,channel,lastapp,calldate,duration,billsec,disposition '
           'FROM cdr ORDER BY calldate DESC LIMIT %d' % n)
    env = dict(os.environ)
    # MYSQL_PWD keeps the password out of argv, where any local user could read
    # it from /proc/<pid>/cmdline.
    env['MYSQL_PWD'] = creds['password']
    argv = [cdrcfg.get('mysql_bin') or '/usr/bin/mysql',
            '-h', creds['host'], '-u', creds['user'],
            '--batch', '--raw', '--skip-column-names',
            '-D', cdrcfg.get('mysql_db') or 'asteriskcdrdb', '-e', sql]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=env)
    except Exception:
        return []
    if p.returncode != 0:
        return []
    rows = []
    for line in p.stdout.splitlines():
        f = line.split('\t')
        if len(f) != 9:
            continue
        try:
            dur, bill = int(f[6] or 0), int(f[7] or 0)
        except ValueError:
            dur, bill = 0, 0
        rows.append({'src': f[0] or '-', 'dst': f[1] or '-', 'dcontext': f[2],
                     'channel': f[3], 'app': f[4], 'start': f[5], 'answer': '', 'end': '',
                     'duration': dur, 'billsec': bill, 'disposition': f[8]})
    return rows


def read_cdr(n=200):
    backend = (SITE.get('cdr') or {}).get('backend') or 'csv'
    if backend in ('csv', 'auto'):
        rows = _read_cdr_csv(n)
        if rows or backend == 'csv':
            return rows
    if backend in ('mysql', 'auto'):
        return _read_cdr_mysql(n)
    return []

def services_status():
    out = {}
    for svc in (SITE.get('services') or []):
        try:
            r = subprocess.run(['systemctl', 'is-active', svc], capture_output=True, text=True, timeout=3)
            out[svc] = r.stdout.strip()
        except Exception: out[svc] = 'unknown'
    return out

def system_info():
    try:
        load = open('/proc/loadavg').read().split()[:3]
        mem = {}
        for ln in open('/proc/meminfo'):
            k, *rest = ln.split(':')
            v = rest[0].strip().split()[0]
            mem[k] = int(v)
        used = mem['MemTotal'] - mem['MemAvailable']
        uptime_s = float(open('/proc/uptime').read().split()[0])
        df = subprocess.run(['df', '-B1', '/'], capture_output=True, text=True, timeout=3)
        df_parts = df.stdout.splitlines()[-1].split()
        return {
            'load': load,
            'mem_total_gb': round(mem['MemTotal']/1024/1024, 1),
            'mem_used_gb': round(used/1024/1024, 1),
            'mem_pct': round(100*used/mem['MemTotal'], 1),
            'host_uptime_h': f"{int(uptime_s//3600)}h {int((uptime_s%3600)//60)}m",
            'disk_total_gb': round(int(df_parts[1])/1024/1024/1024, 1),
            'disk_used_gb': round(int(df_parts[2])/1024/1024/1024, 1),
            'disk_pct': df_parts[4],
        }
    except Exception as e:
        return {'error': str(e)}

def trunk_health():
    last = tcpdump_tail('host 10.10.10.89 and port 5060', 30)
    in_count = sum(1 for l in last if ' In  ' in l)
    out_count = sum(1 for l in last if ' Out ' in l)
    ok200 = sum(1 for l in last if '200 OK' in l)
    last_options = ''
    for l in reversed(last):
        if 'OPTIONS' in l:
            m = re.match(r'(\d\d:\d\d:\d\d\.\d+)', l)
            if m: last_options = m.group(1); break
    return {'alive': ok200 > 0, 'in_count': in_count, 'out_count': out_count,
            'ok_count': ok200, 'last_options': last_options}

def endpoints_parsed():
    raw = asterisk_cli('pjsip show endpoints')
    eps = []
    for line in raw.splitlines():
        m = re.match(r'\s*Endpoint:\s+(\S+)\s+(\S+(?:\s+\S+)?)\s+(\d+ of \S+)', line)
        if m:
            eps.append({'name': m.group(1), 'state': m.group(2).strip(), 'channels': m.group(3)})
    return eps


# ==================== VOICEMAIL HELPERS ====================
def parse_vm_meta(txt_path):
    info = {}
    try:
        for line in txt_path.read_text().splitlines():
            if '=' in line:
                k, _, v = line.partition('=')
                info[k.strip()] = v.strip()
    except Exception: pass
    return info

def list_vm_messages(box, folder='INBOX'):
    folder_path = VM_ROOT / box / folder
    if not folder_path.exists(): return []
    msgs = []
    for txt in sorted(folder_path.glob('msg*.txt')):
        info = parse_vm_meta(txt)
        wav = txt.with_suffix('.wav')
        try:
            origtime = int(info.get('origtime', '0'))
            when = datetime.datetime.fromtimestamp(origtime).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            when = info.get('origtime', '')
        msgs.append({
            'num': txt.stem.replace('msg', ''),
            'folder': folder,
            'callerid': info.get('callerid', '?'),
            'origmailbox': info.get('origmailbox', ''),
            'context': info.get('context', ''),
            'duration': int(info.get('duration', 0)),
            'origtime': when,
            'wav_exists': wav.exists(),
            'wav_size': wav.stat().st_size if wav.exists() else 0,
        })
    return msgs

def list_vm_boxes():
    if not VM_ROOT.exists(): return []
    boxes = []
    for d in sorted(VM_ROOT.iterdir()):
        if d.is_dir():
            inbox = list_vm_messages(d.name, 'INBOX')
            old = list_vm_messages(d.name, 'Old')
            boxes.append({
                'box': d.name, 'new_count': len(inbox),
                'old_count': len(old), 'inbox': inbox, 'old': old,
            })
    return boxes


# ==================== ROUTES — pages ====================
# Failed-login throttle. In-process only, which is fine because this app is
# single-worker by construction (AUTH is a mutable global written back to disk).
_LOGIN_FAILS = defaultdict(list)      # key -> [monotonic timestamps]
_LOGIN_WINDOW_S = 15 * 60
_LOGIN_MAX_FAILS = 8
_LOGIN_LOCK = threading.Lock()


def _login_key():
    """Throttle bucket = (real client IP, email).

    remote_addr alone is WRONG here: cloudflared terminates the public hostname
    on this same host and proxies to 127.0.0.1, so every internet user shares one
    address and a single attacker would lock out the whole team. Trusting the
    proxy headers is safe in this direction because the app binds 127.0.0.1 and
    is only reachable through nginx/cloudflared, which set them.

    Keying on the email too means hammering one account never locks another.
    """
    ip = (request.headers.get('CF-Connecting-IP')
          or (request.headers.get('X-Forwarded-For', '').split(',')[0].strip())
          or request.remote_addr or '?')
    email = (request.form.get('email', '') or '').strip().lower()
    return (ip, email)


def _login_blocked():
    now = time.monotonic()
    with _LOGIN_LOCK:
        hits = [t for t in _LOGIN_FAILS[_login_key()] if now - t < _LOGIN_WINDOW_S]
        _LOGIN_FAILS[_login_key()] = hits
        return len(hits) >= _LOGIN_MAX_FAILS


def _login_record_fail():
    with _LOGIN_LOCK:
        _LOGIN_FAILS[_login_key()].append(time.monotonic())


def _login_clear():
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(_login_key(), None)


def _safe_next(target):
    """Only ever redirect to a path on this site.

    request.args['next'] was previously passed straight to redirect(), so a
    crafted /login?next=https://evil.example link turned the login page into an
    open redirect."""
    if not target:
        return None
    if target.startswith('//') or '://' in target:
        return None
    return target if target.startswith('/') else None


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if _login_blocked():
            flash('Too many failed attempts. Try again in 15 minutes.', 'error')
            return render_template('login.html'), 429
        email = request.form.get('email', '').strip().lower()
        pw = request.form.get('password', '')
        user = AUTH['users'].get(email)
        # Always hash-compare, even for an unknown user, so response time does not
        # reveal whether the account exists.
        stored = user.get('password_hash', '') if isinstance(user, dict) else ''
        ok = check_password_hash(stored, pw) if stored else (check_password_hash(_DUMMY_HASH, pw) and False)
        if ok:
            _login_clear()
            session.clear()                 # new session id on privilege change
            session['user'] = email
            session.permanent = True
            csrf_token()                    # mint a token for the new session
            return redirect(_safe_next(request.args.get('next')) or url_for('dashboard'))
        _login_record_fail()
        flash('Invalid credentials', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html', nav='dashboard')

@app.route('/make-call')
@login_required
@perm_required('call')
def make_call_page():
    return render_template('make_call.html', nav='make-call')

@app.route('/webphone')
@login_required
@perm_required('call')
@feature_required('webphone')
def webphone_page():
    return render_template('webphone.html', nav='webphone')

@app.route('/calls')
@login_required
@feature_required('ai_sessions')
def calls():
    return render_template('calls.html', nav='calls')

@app.route('/voicemail')
@login_required
def voicemail_page():
    return render_template('voicemail.html', nav='voicemail')

@app.route('/broadcast')
@login_required
@perm_required('call')
@feature_required('broadcast')
def broadcast_page():
    return render_template('broadcast.html', nav='broadcast')

@app.route('/recordings')
@login_required
def recordings_page():
    return render_template('recordings.html', nav='recordings')

@app.route('/endpoints')
@login_required
def endpoints():
    return render_template('endpoints.html', nav='endpoints')

@app.route('/trunk')
@login_required
@feature_required('trunk_recovery')
def trunk():
    return render_template('trunk.html', nav='trunk')

@app.route('/dialplan')
@login_required
@perm_required('config')
def dialplan():
    return render_template('dialplan.html', nav='dialplan')

@app.route('/pjsip')
@login_required
@perm_required('config')
def pjsip():
    return render_template('pjsip.html', nav='pjsip')

@app.route('/sounds')
@login_required
@perm_required('config')
def sounds():
    return render_template('sounds.html', nav='sounds')

@app.route('/system')
@login_required
def system_page():
    return render_template('system.html', nav='system')

@app.route('/logs')
@login_required
def logs():
    return render_template('logs.html', nav='logs')

@app.route('/users')
@login_required
@perm_required('admin')
def users():
    return render_template('users.html', nav='users')

@app.route('/settings')
@login_required
def settings():
    return render_template('settings.html', nav='settings', user=session['user'])


@app.route('/dashboard/<industry>')
@login_required
def industry_dashboard(industry):
    """Render an industry vertical dashboard (reservations / hospital / sales).
    Data is dummy/local (see static/data/<industry>.js); access is per-user."""
    if industry not in DASHBOARDS:
        abort(404)
    if industry not in user_dashboards():
        abort(403)
    return render_template('industry.html', nav='dash-' + industry,
                           industry=industry, meta=DASHBOARDS[industry])


# ==================== API — live dashboard data (real, server-backed) ====================
# Verticals whose transactional records are REAL — captured from live AI calls
# (the sampath-ai bridge writes them when the voice agent calls book_appointment)
# or entered by staff — instead of browser-only dummy data. Reference catalogs
# (doctors/branches/specialties) live in REFDATA_DIR/<vertical>.json (dummy until
# a real client supplies them); captured records are one JSON file per record
# under BOOKINGS_DIR/<vertical>/<collection>/.
DASH_COLLECTIONS_ALL = {
    'hospital': {'appointments', 'labs', 'patients'},
    'reservations': {'reservations'},
    'sales': {'orders', 'leads'},
    'restaurant': {'orders', 'customers'},
    # petcare records are written by a SEPARATE agent (petcare-ai) into its own
    # data tree; see site["record_dirs"] and _coll_dir().
    'petcare': {'appointments'},
    # computershop likewise: computershop-ai on the Germany box writes into
    # /var/lib/computershop-ai/bookings/{orders,repairs}.
    'computershop': {'orders', 'repairs'},
}
DASH_COLLECTIONS = {k: v for k, v in DASH_COLLECTIONS_ALL.items() if k in DASHBOARDS}
_REC_PREFIX = {'appointments': 'AP', 'labs': 'LAB', 'reservations': 'RS', 'orders': 'ORD', 'leads': 'LEAD'}
# Per-vertical override of the generic prefix above. petcare references are
# minted by the Node agent as HP-AP-######; a staff-created appointment has to
# be indistinguishable from one the phone agent booked, so it uses the same
# format rather than the generic AP-<date>-<rand>. computershop is the same
# arrangement with two collections (BH-OR-###### / BH-RP-######).
_REC_PREFIX_BY_VERTICAL = {
    'petcare': {'appointments': 'HP-AP'},
    'computershop': {'orders': 'BH-OR', 'repairs': 'BH-RP'},
}

# Per-vertical record directory / refdata file overrides from the site config.
DASH_RECORD_DIRS = {v: {c: Path(p) for c, p in (colls or {}).items()}
                    for v, colls in (SITE.get('record_dirs') or {}).items()}
DASH_REFDATA_FILES = {v: Path(p) for v, p in (SITE.get('refdata_files') or {}).items()}
# Server-derived fields a client may never set at creation time. Kept as a
# blocklist rather than a whitelist because create legitimately accepts a wide,
# vertical-specific set of caller fields.
_REC_CREATE_BLOCKED = frozenset({
    'id', 'created', 'created_by', 'updated', 'updated_by',
    'results', 'critical', 'queue_no', 'session_time', 'accession', 'mrn',
    'fee', 'cost', 'deposit', 'total', 'discount_total', 'lines',
    'collected_by', 'received_by', 'verified_by', 'delivered_by',
    'auto_call_at', 'auto_call_by', 'auto_call_uuid', 'auto_call_ok',
})
_REC_PATCHABLE = {'status', 'paid', 'notes', 'result', 'time', 'date', 'doctor',
                  'branch', 'type', 'priority', 'area', 'party'}


def _dash_guard(vertical, collection=None):
    """None if the caller may touch this vertical/collection, else an error response."""
    if vertical not in DASHBOARDS:
        return jsonify({'error': 'unknown dashboard'}), 404
    if vertical not in user_dashboards():
        return jsonify({'error': 'forbidden'}), 403
    if collection is not None and collection not in DASH_COLLECTIONS.get(vertical, set()):
        return jsonify({'error': 'unknown collection'}), 404
    return None


def _coll_dir(vertical, collection):
    """Where this vertical's records live.

    Default: the home agent's own bookings tree. A vertical fed by a different
    agent on a different box (petcare-ai writes /var/lib/petcare-ai/bookings/
    appointments) declares an absolute path in site["record_dirs"], because its
    records must land where THAT agent reads and writes them.
    """
    over = DASH_RECORD_DIRS.get(vertical, {}).get(collection)
    return over if over is not None else BOOKINGS_DIR / vertical / collection


def _new_rec_id(collection):
    import random, string
    stamp = datetime.datetime.now().strftime('%y%m%d')
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{_REC_PREFIX.get(collection, 'REC')}-{stamp}-{rand}"


def _iso_z():
    """UTC ISO-8601 with a Z suffix — the format the Node agents write."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _gen_petcare_ref():
    """Happy Paws booking reference (HP-AP-482917), unique in the appointments dir."""
    import random
    pfx = _REC_PREFIX_BY_VERTICAL['petcare']['appointments']
    d = _coll_dir('petcare', 'appointments')
    existing = set()
    try:
        for f in d.glob('*.json'):
            existing.add(f.stem)
    except Exception:
        pass
    for _ in range(50):
        ref = f'{pfx}-{random.randint(100000, 999999)}'
        if ref not in existing and not (d / f'{ref}.json').exists():
            return ref
    return f'{pfx}-{random.randint(100000, 999999)}'


def _gen_computershop_ref(collection):
    """ByteHub reference (BH-OR-482917 / BH-RP-482917), unique in that collection.

    Mirrors _gen_petcare_ref(): the computershop-ai agent mints these on the
    phone, so a staff-created record has to carry the same format rather than
    the generic ORD-<date>-<rand>, or the two would be trivially told apart.
    Records are stored under <id>.json (ord-/rep-<uuid>), so the reference has
    to be checked against the records themselves, not just the filenames.
    """
    import random
    pfx = _REC_PREFIX_BY_VERTICAL['computershop'][collection]
    d = _coll_dir('computershop', collection)
    existing = set()
    try:
        for f in d.glob('*.json'):
            existing.add(f.stem)
    except Exception:
        pass
    for r in _read_records('computershop', collection):
        if r.get('reference'):
            existing.add(str(r['reference']))
    for _ in range(50):
        ref = f'{pfx}-{random.randint(100000, 999999)}'
        if ref not in existing and not (d / f'{ref}.json').exists():
            return ref
    return f'{pfx}-{random.randint(100000, 999999)}'


def _gen_appt_id():
    """Holton appointment reference (HOL-AP-482917) — same brand/format the voice
    agent uses, so manual and AI-booked appointments look identical."""
    import random
    d = _coll_dir('hospital', 'appointments')
    for _ in range(50):
        rid = f"HOL-AP-{random.randint(100000, 999999)}"
        if not (d / f"{rid}.json").exists():
            return rid
    return f"HOL-AP-{random.randint(100000, 999999)}"


def _next_queue_no(doctor, branch, date):
    """Channelling queue position for this doctor's clinic at this branch on this
    date (1-based) — mirrors the bridge's nextQueueNo so both write paths agree."""
    dn = str(doctor or '').lower().strip()
    bn = str(branch or '').lower().strip()
    n = 0
    for r in _read_records('hospital', 'appointments'):
        if r.get('status') in ('cancelled', 'no_show'):
            continue
        if (str(r.get('doctor', '')).lower().strip() == dn
                and str(r.get('branch', '')).lower().strip() == bn
                and str(r.get('date', '')) == str(date or '')):
            n += 1
    return n + 1


def _session_start(slots, time):
    """Doctor's channelling session start in the same day-part as `time`
    (morning <12:00, afternoon 12:00–15:59, evening >=16:00) — mirrors the
    bridge's sessionStart."""
    import re
    def to_min(t):
        m = re.search(r'(\d{1,2}):(\d{2})', str(t or ''))
        return int(m.group(1)) * 60 + int(m.group(2)) if m else -1
    part = lambda m: 0 if m < 720 else (1 if m < 960 else 2)
    srt = sorted([s for s in (slots or [])], key=to_min)
    bm = to_min(time)
    if bm < 0 or not srt:
        return time or (srt[0] if srt else '')
    in_part = [s for s in srt if part(to_min(s)) == part(bm)]
    return in_part[0] if in_part else srt[0]


# Parsed-record cache keyed by file path, invalidated on (mtime_ns, size). The
# restaurant board polls once a second, so re-parsing every order JSON on every
# poll would be wasteful; a stat() per file is not. Records written by the voice
# bridge (a separate process) still invalidate correctly because the key is the
# file's own mtime.
# NOTE: the returned dicts are SHARED — treat _read_records() output as
# read-only. Anything that mutates a record loads it separately (see
# _rest_load_order / api_dash_record) and writes it back atomically.
_REC_CACHE = {}


def _read_records(vertical, collection):
    d = _coll_dir(vertical, collection)
    out = []
    if d.exists():
        for f in d.glob('*.json'):
            key = str(f)
            try:
                st = f.stat()
                hit = _REC_CACHE.get(key)
                if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
                    out.append(hit[2])
                    continue
                with f.open() as fh:
                    rec = json.load(fh)
                _REC_CACHE[key] = (st.st_mtime_ns, st.st_size, rec)
                out.append(rec)
            except Exception:
                _REC_CACHE.pop(key, None)
                continue
    # `created` is the home agents' field; `created_at` is what the petcare
    # agent writes. The `or` chain leaves home records on exactly the key they
    # have always sorted by.
    out.sort(key=lambda r: str(r.get('created') or r.get('created_at') or ''), reverse=True)
    return out


def _write_record(vertical, collection, rec):
    d = _coll_dir(vertical, collection)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{rec['id']}.json"
    tmp = f.with_suffix('.json.tmp')
    with tmp.open('w') as fh:
        json.dump(rec, fh, indent=2)
    tmp.replace(f)
    # Drop the cached parse rather than relying on (mtime, size) to differ — an
    # update that only touches `updated` leaves the size identical, and a
    # coarse-granularity filesystem could otherwise serve the pre-write copy.
    _REC_CACHE.pop(str(f), None)
    return rec


def _load_refdata(vertical):
    path = DASH_REFDATA_FILES.get(vertical) or (REFDATA_DIR / f'{vertical}.json')
    try:
        with path.open() as f:
            return json.load(f)
    except Exception:
        return {}


# ---- LIS helpers (lab catalogue, result flagging, accession, patient registry) ----
def _load_test(code_or_name):
    if not code_or_name:
        return None
    q = str(code_or_name).strip().lower()
    tests = _load_refdata('hospital').get('tests', [])
    for t in tests:
        if str(t.get('code', '')).lower() == q or str(t.get('name', '')).lower() == q:
            return t
    for t in tests:
        if q in str(t.get('name', '')).lower():
            return t
    return None


def _compute_lab_results(test_code, entered):
    """Return (results[], critical_bool). Units/ranges/flags are resolved from the
    catalogue server-side so flags can't be spoofed by the client. Flags: H / L / critical."""
    t = _load_test(test_code) or {}
    spec = {a.get('name'): a for a in t.get('analytes', [])}
    out, critical = [], False
    for e in (entered or []):
        name = e.get('analyte')
        a = spec.get(name, {})
        val = e.get('value')
        flag = ''
        if a.get('type') != 'text':
            try:
                v = float(val)
                lo, hi, cl, ch = a.get('low'), a.get('high'), a.get('crit_low'), a.get('crit_high')
                if (cl is not None and v <= cl) or (ch is not None and v >= ch):
                    flag = 'critical'; critical = True
                elif lo is not None and v < lo:
                    flag = 'L'
                elif hi is not None and v > hi:
                    flag = 'H'
            except (TypeError, ValueError):
                pass
        out.append({'analyte': name, 'value': val, 'units': a.get('units', ''),
                    'low': a.get('low'), 'high': a.get('high'), 'flag': flag,
                    'type': a.get('type', 'numeric')})
    return out, critical


def _gen_accession():
    import random, string
    return 'AC-' + datetime.datetime.now().strftime('%y%m%d') + '-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))


def _upsert_patient(name, phone, extra=None):
    """Real patient registry, deduped by phone. Returns the MRN."""
    d = _coll_dir('hospital', 'patients')
    pn = re.sub(r'[^0-9+]', '', str(phone or ''))
    if d.exists() and pn:
        for f in d.glob('*.json'):
            try:
                p = json.load(f.open())
            except Exception:
                continue
            if re.sub(r'[^0-9+]', '', str(p.get('phone') or '')) == pn:
                return p.get('id') or p.get('mrn')
    import random
    mrn = 'MRN' + str(random.randint(10000, 99999))
    rec = {'id': mrn, 'mrn': mrn, 'name': name or 'Unknown', 'phone': phone,
           'created': datetime.datetime.now().isoformat(timespec='seconds'), 'source': 'Manual'}
    if extra:
        rec.update(extra)
    _write_record('hospital', 'patients', rec)
    return mrn


# ==================== RESTAURANT (QSR phone ordering) ====================
# Single source of truth for the EX Chicken vertical. EVERYTHING that prices,
# creates or mutates an order goes through the helpers below — the voice agent
# (via /api/agent/restaurant/<tool>, proxied by bridge.ts) and the dashboard
# both call into the same functions, so a total can never be computed two
# different ways. Menu prices, promotions, branches, timings and riders are read
# from REFDATA_DIR/restaurant.json on every call; nothing is hardcoded here.

try:
    from zoneinfo import ZoneInfo
except ImportError:                                     # pragma: no cover
    ZoneInfo = None

REST_STATUS_FLOW = ['pending', 'preparing', 'out_for_delivery', 'delivered']
REST_STATUS_LABELS = {
    'pending': 'Pending', 'preparing': 'Preparing',
    'out_for_delivery': 'Out for Delivery', 'delivered': 'Delivered',
    'cancelled': 'Cancelled',
}
AGENT_API_FILE = SAMPATH_DATA / 'agent-api.json'   # shared secret for the voice bridge


def _rest_ref():
    return _load_refdata('restaurant')


def _rest_tz():
    name = (_rest_ref().get('timezone') or 'Asia/Colombo')
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.timezone(datetime.timedelta(hours=5, minutes=30), 'Asia/Colombo')


def _rest_now():
    """Current local (Asia/Colombo) time. RESTAURANT_FAKE_NOW is a test seam —
    set it to an ISO timestamp to pin the clock; never set in production."""
    fake = os.environ.get('RESTAURANT_FAKE_NOW')
    if fake:
        try:
            d = datetime.datetime.fromisoformat(fake)
            return d if d.tzinfo else d.replace(tzinfo=_rest_tz())
        except Exception:
            pass
    return datetime.datetime.now(_rest_tz())


def _rest_stamp():
    """Timestamp for a stored restaurant record.

    Offset-aware Asia/Colombo, NOT naive datetime.now(). The rest of the vertical
    (opening hours, promotion date ranges, ETAs) reasons in Asia/Colombo while
    this host runs UTC, so naive stamps made the dashboard's "late" badges and
    "today" filters wrong by the UTC offset. An explicit offset also means the
    browser parses these correctly wherever staff are.
    """
    return _rest_now().isoformat(timespec='seconds')


def _rest_money(n):
    return 'LKR {:,}'.format(int(round(n or 0)))


def _rest_norm(s):
    """Lowercase, punctuation-stripped, whitespace-collapsed — used for all menu
    and address matching so 'Colombo 04' == 'colombo-04'."""
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9\s]+', ' ', str(s or '').lower())).strip()


def _rest_norm_loose(s):
    """_rest_norm plus zero-stripped numbers, so 'colombo 04' == 'colombo 4'."""
    return re.sub(r'\b0+(\d)', r'\1', _rest_norm(s))


def _rest_phone(p):
    """Canonical Sri Lankan form 0XXXXXXXXX (matches the bridge's normalizeLkPhone)."""
    d = re.sub(r'[^0-9]', '', str(p or ''))
    if d.startswith('94') and len(d) == 11:
        d = '0' + d[2:]
    elif len(d) == 9 and d[0] == '7':
        d = '0' + d
    return d


def _rest_valid_phone(p):
    d = _rest_phone(p)
    return len(d) >= 9 and len(d) <= 12 and d.isdigit()


# ---------------------------------------------------------------- menu

def _rest_menu():
    return _rest_ref().get('menu') or []


def _rest_categories(spoken_only=False):
    cats = sorted(_rest_ref().get('categories') or [], key=lambda c: c.get('order', 99))
    return [c for c in cats if c.get('spoken')] if spoken_only else cats


def _rest_category(q):
    """Resolve a spoken category word to a category record ('biriyani', 'Rice Meals')."""
    n = _rest_norm(q)
    if not n:
        return None
    for c in _rest_categories():
        if _rest_norm(c['id']) == n or _rest_norm(c['label']) == n:
            return c
    for c in _rest_categories():
        cl, ci = _rest_norm(c['label']), _rest_norm(c['id'])
        if n in cl or cl in n or n in ci or ci in n:
            return c
    return None


def _rest_item(q):
    """Resolve ONE menu item from a SKU, an exact name, or a caller's phrasing.
    Returns None rather than guessing when nothing is a clear match."""
    n = _rest_norm(q)
    if not n:
        return None
    menu = _rest_menu()
    for m in menu:
        if _rest_norm(m['sku']) == n or _rest_norm(m['name']) == n:
            return m
    # caller phrasings like "the 12 piece bucket please" / "large pepsi"
    best, best_len = None, 0
    for m in menu:
        mn = _rest_norm(m['name'])
        if mn in n and len(mn) > best_len:
            best, best_len = m, len(mn)
    if best:
        return best
    for m in menu:
        if n in _rest_norm(m['name']):
            return m
    for m in menu:
        for k in (m.get('keywords') or []):
            if _rest_norm(k) == n:
                return m
    return None


def _rest_category_exact(q):
    """Category match for a caller's WORD ('burgers', 'biriyani'). Deliberately
    strict — fuzzy matching here would send 'chicken burger' to the chicken
    bucket category instead of the burgers."""
    n = _rest_norm(q)
    if not n:
        return None
    variants = {n, n.rstrip('s'), n + 's'}
    for c in _rest_categories():
        for target in (_rest_norm(c['id']).replace('_', ' '), _rest_norm(c['label'])):
            if target in variants or target.rstrip('s') in variants:
                return c
    return None


def _rest_category_in(query):
    """Find a category word inside a caller's sentence ('what burgers do you
    have?' -> Burgers). Returns None when the sentence names two categories
    ('chicken burger'), so those fall through to item search instead."""
    tokens = [t for t in _rest_norm(query).split() if t not in _REST_STOPWORDS]
    found, spans = [], []
    for size in (2, 1):
        for i in range(len(tokens) - size + 1):
            if any(i < e and s < i + size for s, e in spans):
                continue
            c = _rest_category_exact(' '.join(tokens[i:i + size]))
            if c:
                found.append(c)
                spans.append((i, i + size))
    uniq = {c['id']: c for c in found}
    if len(uniq) != 1:
        return None, tokens
    cat = list(uniq.values())[0]
    rest = [t for i, t in enumerate(tokens) if not any(s <= i < e for s, e in spans)]
    return cat, rest


def _rest_search(query=None, category=None, limit=None):
    """Menu search behind get_menu. Handles an explicit category, a caller's
    category word inside a sentence ('what burgers do you have?'), or a fuzzy
    item phrase ('spicy rice' -> the three rice meals)."""
    menu = _rest_menu()
    cat = _rest_category(category) if category else None
    leftover = None
    if not cat and query:
        cat, leftover = _rest_category_in(query)
    rows = [m for m in menu if m['category'] == cat['id']] if cat else menu
    if query:
        if cat is None:
            rows = _rest_score(rows, query)
        elif leftover:
            rows = _rest_score(rows, ' '.join(leftover))
    return (rows[:limit] if limit else rows), cat


_REST_STOPWORDS = {'a', 'an', 'the', 'some', 'me', 'you', 'we', 'have', 'has', 'do', 'does',
                   'what', 'which', 'is', 'are', 'how', 'much', 'many', 'and', 'or', 'got',
                   'your', 'please', 'want', 'like', 'for', 'of', 'any'}


def _rest_score(rows, query):
    """Rank by how well a caller's words match the item name/keywords. EVERY
    token must appear somewhere, so 'spicy rice' can never return a bucket.
    Ties break on menu order, so a category always reads out in menu sequence."""
    n = _rest_norm(query)
    if not n:
        return list(rows)
    tokens = [t for t in n.split() if t not in _REST_STOPWORDS]
    if not tokens:
        return list(rows)
    order = {m['sku']: i for i, m in enumerate(_rest_menu())}
    scored = []
    for m in rows:
        hay = _rest_norm(m['name'] + ' ' + ' '.join(m.get('keywords') or []) + ' ' + (m.get('description') or ''))
        name = _rest_norm(m['name'])
        if any(t not in hay for t in tokens):
            continue
        score = len(tokens) * 10 + (5 if all(t in name for t in tokens) else 0)
        scored.append((score, order.get(m['sku'], 999), m))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [m for _, _, m in scored]


# ---------------------------------------------------------------- pricing

def _rest_build_lines(items, min_qty=1):
    """[{item|sku, quantity}] -> (lines, unknown[]). Prices come from the menu,
    never from the caller or the model.

    min_qty=1 for adding (a missing quantity means "one"). Removal passes
    min_qty=0, so "remove 0 of these" is the no-op it reads as rather than
    quietly deleting a unit."""
    lines, unknown = [], []
    for raw in (items or []):
        if isinstance(raw, str):
            raw = {'item': raw}
        if not isinstance(raw, dict):
            continue
        want = raw.get('sku') or raw.get('item') or raw.get('name')
        raw_qty = raw.get('quantity')
        if raw_qty is None:
            raw_qty = raw.get('qty')
        # An OMITTED quantity means "one" on both paths — "take the burger off"
        # is a removal of one. Only an EXPLICIT 0 or negative is honoured as a
        # no-op, and only when removing (min_qty=0).
        try:
            qty = 1 if raw_qty is None else int(raw_qty)
        except (TypeError, ValueError):
            qty = 1
        qty = max(min_qty, min(qty, 99))
        m = _rest_item(want)
        if not m:
            unknown.append(str(want or ''))
            continue
        ex = next((l for l in lines if l['sku'] == m['sku'] and not l.get('free')), None)
        if ex:
            ex['qty'] += qty
            ex['line_total'] = ex['qty'] * ex['unit_price']
        else:
            lines.append({'sku': m['sku'], 'name': m['name'], 'category': m['category'],
                          'qty': qty, 'unit_price': m['price'], 'line_total': m['price'] * qty})
    return lines, unknown


def _rest_active_promotions(on=None):
    day = (on or _rest_now()).date().isoformat()
    out = []
    for p in (_rest_ref().get('promotions') or []):
        if not p.get('active', True):
            continue
        if p.get('starts') and day < str(p['starts']):
            continue
        if p.get('ends') and day > str(p['ends']):
            continue
        out.append(p)
    return out


def _rest_units(lines):
    """Explode lines into individual priced units so promos can pick specific ones."""
    units = []
    for l in lines:
        if l.get('free'):
            continue
        for _ in range(l['qty']):
            units.append({'sku': l['sku'], 'category': l['category'], 'price': l['unit_price'], 'name': l['name']})
    return units


def _rest_apply_promotions(lines, promo_id=None, on=None):
    """Return (extra_free_lines, discounts[]). Promotions are data-driven rows in
    refdata; each type below maps to one row shape. All applicable promos stack,
    and the caller-facing total is clamped so it can never go below zero."""
    promos = _rest_active_promotions(on)
    units = _rest_units(lines)
    subtotal = sum(u['price'] for u in units)
    discounts, free_lines = [], []

    for p in promos:
        kind = p.get('type')

        if kind == 'bogo':
            n = sum(1 for u in units if u['sku'] == p.get('sku'))
            free = n // 2
            if free:
                item = _rest_item(p['sku'])
                amount = free * (item['price'] if item else 0)
                if amount > 0:
                    discounts.append({'id': p['id'], 'label': p['label'], 'amount': amount,
                                      'detail': '{} x {} free'.format(free, item['name'] if item else p['sku'])})

        elif kind == 'bundle':
            times = None
            for req in (p.get('requires') or []):
                pool = [u for u in units if (u['sku'] == req.get('sku') if req.get('sku') else u['category'] == req.get('category'))]
                have = len(pool) // max(1, int(req.get('qty') or 1))
                times = have if times is None else min(times, have)
            times = times or 0
            if times > 0:
                # Consume the CHEAPEST qualifying units so the discount is never
                # overstated against the restaurant.
                pool_value, taken = 0, []
                for req in (p.get('requires') or []):
                    cands = sorted([u for u in units if u not in taken and
                                    (u['sku'] == req.get('sku') if req.get('sku') else u['category'] == req.get('category'))],
                                   key=lambda u: u['price'])
                    need = int(req.get('qty') or 1) * times
                    for u in cands[:need]:
                        taken.append(u)
                        pool_value += u['price']
                amount = pool_value - int(p.get('bundle_price') or 0) * times
                if amount > 0:
                    discounts.append({'id': p['id'], 'label': p['label'], 'amount': amount,
                                      'detail': '{}x bundle price {}'.format(times, _rest_money(p.get('bundle_price')))})

        elif kind == 'percent_off_combo':
            groups = []
            for req in (p.get('requires') or []):
                pool = sorted([u for u in units if (u['sku'] == req.get('sku') if req.get('sku') else u['category'] == req.get('category'))],
                              key=lambda u: u['price'])
                groups.append((int(req.get('qty') or 1), pool))
            combos = min([len(pool) // qty for qty, pool in groups] or [0])
            if combos > 0:
                value = 0
                for qty, pool in groups:
                    value += sum(u['price'] for u in pool[:qty * combos])
                amount = int(round(value * float(p.get('percent') or 0) / 100.0))
                if amount > 0:
                    discounts.append({'id': p['id'], 'label': p['label'], 'amount': amount,
                                      'detail': '{}% off {} combo(s)'.format(p.get('percent'), combos)})

        elif kind == 'free_item_over':
            if subtotal > int(p.get('threshold') or 0):
                item = _rest_item(p.get('free_sku'))
                if not item:
                    continue
                if p.get('auto_add', True):
                    if not any(l['sku'] == item['sku'] and l.get('free') for l in lines):
                        free_lines.append({'sku': item['sku'], 'name': item['name'], 'category': item['category'],
                                           'qty': 1, 'unit_price': 0, 'line_total': 0,
                                           'free': True, 'promo_id': p['id']})
                        discounts.append({'id': p['id'], 'label': p['label'], 'amount': 0,
                                          'detail': 'free {} added'.format(item['name'])})
                else:
                    # auto_add:false -> the caller has EARNED it but must be
                    # offered it; nothing is added to the basket here.
                    discounts.append({'id': p['id'], 'label': p['label'], 'amount': 0,
                                      'eligible': True, 'free_item': item['name'],
                                      'detail': 'eligible for a free {} — offer it'.format(item['name'])})
    return free_lines, discounts


def _rest_price(lines, fulfilment='delivery', promo_id=None, on=None):
    """THE pricing function. Every order total in the system comes from here."""
    ref = _rest_ref()
    priced = [dict(l) for l in lines if not l.get('free')]
    for l in priced:
        l['line_total'] = l['qty'] * l['unit_price']
    free_lines, discounts = _rest_apply_promotions(priced, promo_id=promo_id, on=on)
    all_lines = priced + free_lines
    subtotal = sum(l['line_total'] for l in priced)
    discount_total = min(sum(d['amount'] for d in discounts), subtotal)
    delivery_fee = int((ref.get('delivery') or {}).get('fee') or 0) if fulfilment == 'delivery' else 0
    total = max(0, subtotal - discount_total + delivery_fee)
    return {
        'lines': all_lines, 'subtotal': subtotal, 'discounts': discounts,
        'discount_total': discount_total, 'delivery_fee': delivery_fee, 'total': total,
        'items': _rest_items_text(all_lines), 'currency': ref.get('currency') or 'LKR',
    }


def _rest_items_text(lines):
    """The human 'Items' string shown on the dashboard row (spec §7)."""
    parts = []
    for l in lines:
        parts.append(l['name'] if l['qty'] == 1 else '{} x {}'.format(l['qty'], l['name']))
        if l.get('free'):
            parts[-1] += ' (free)'
    return ', '.join(parts)


# ---------------------------------------------------------------- branches

def _rest_branch(q=None):
    branches = _rest_ref().get('branches') or []
    if not branches:
        return None
    n = _rest_norm(q)
    if n:
        for b in branches:
            if _rest_norm(b['id']) == n or _rest_norm(b['name']) == n:
                return b
        for b in branches:
            if _rest_norm(b['name']) in n or n in _rest_norm(b['name']):
                return b
        return None
    default = _rest_ref().get('default_branch')
    return next((b for b in branches if b['id'] == default), branches[0])


# Street-type words. "Galle Road, Ratmalana" names a Colombo street, not the town
# of Galle — matching the bare word would ship the order 120 km down the coast.
_REST_STREET_WORDS = r'(?:road|rd|street|st|mawatha|mw|lane|ln|avenue|ave|place|pl|highway|junction|circular)'


def _rest_area_branch(address):
    """Which branch covers this address? (branch, matched_area) or (None, None)
    when the address is outside every delivery area we serve.

    Matched per comma-separated segment, on whole tokens, ignoring area words
    that are really street names. A bare substring test is not good enough here:
    it accepts "Galle Road, Ratmalana" as Galle, and "Colombo 01" matches inside
    "Colombo 12" because the loose form zero-strips to "colombo 1"."""
    segments = [_rest_norm_loose(s) for s in re.split(r'[,\n/]+', str(address or ''))]
    segments = [s for s in segments if s]
    if not segments:
        return None, None
    best = (None, None, 0)
    for b in (_rest_ref().get('branches') or []):
        for area in (b.get('delivery_areas') or []):
            na = _rest_norm_loose(area)
            if not na or len(na) <= best[2]:
                continue
            for seg in segments:
                if not re.search(r'(?<![a-z0-9])' + re.escape(na) + r'(?![a-z0-9])', seg):
                    continue
                if re.search(re.escape(na) + r'\s+' + _REST_STREET_WORDS + r'(?![a-z])', seg):
                    continue                      # "<area> Road" is a street name
                best = (b, area, len(na))
                break
    return best[0], best[1]


def _rest_all_areas():
    out = []
    for b in (_rest_ref().get('branches') or []):
        out.extend(b.get('delivery_areas') or [])
    return out


def _rest_is_open(now=None, branch=None):
    now = now or _rest_now()
    ref = _rest_ref()
    hours = (branch or {}).get('hours') or ref.get('hours') or {}
    o, c = str(hours.get('open') or '10:00'), str(hours.get('close') or '23:00')

    def mins(t):
        m = re.match(r'(\d{1,2}):(\d{2})', t)
        return int(m.group(1)) * 60 + int(m.group(2)) if m else 0
    cur = now.hour * 60 + now.minute
    om, cm = mins(o), mins(c)
    is_open = (om <= cur < cm) if cm > om else (cur >= om or cur < cm)   # tolerate past-midnight close
    return is_open, {'open': o, 'close': c,
                     'spoken': (ref.get('hours') or {}).get('spoken') or '{} to {}'.format(o, c)}


# ---------------------------------------------------------------- CRM

def _rest_customer_file(phone):
    return _coll_dir('restaurant', 'customers') / '{}.json'.format(_rest_customer_id(phone))


def _rest_customer_id(phone):
    return 'CUS-' + _rest_phone(phone)


def _rest_get_customer(phone):
    f = _rest_customer_file(phone)
    if not f.exists():
        return None
    try:
        with f.open() as fh:
            return json.load(fh)
    except Exception:
        return None


def _rest_upsert_customer(phone, name=None, address=None, source='AI call'):
    """CRM write. Addresses are kept most-recently-used first so the agent can
    confirm 'deliver to <saved address>?' instead of asking again."""
    phone = _rest_phone(phone)
    if not phone:
        return None
    rec = _rest_get_customer(phone) or {
        'id': _rest_customer_id(phone), 'phone': phone, 'name': name or '',
        'addresses': [], 'created': _rest_stamp(),
        'source': source, 'orders': 0, 'total_spend': 0,
    }
    if name and (not rec.get('name') or rec['name'].lower() in ('', 'unknown')):
        rec['name'] = name
    if address:
        a = str(address).strip()
        rec['addresses'] = [x for x in (rec.get('addresses') or []) if _rest_norm(x.get('address')) != _rest_norm(a)]
        rec['addresses'].insert(0, {'address': a, 'last_used': _rest_stamp()})
        rec['addresses'] = rec['addresses'][:5]
    rec['updated'] = _rest_stamp()
    _write_record('restaurant', 'customers', rec)
    return rec


def _rest_default_address(cust):
    addrs = (cust or {}).get('addresses') or []
    return addrs[0]['address'] if addrs else ''


# ---------------------------------------------------------------- orders

def _rest_order_no():
    import random
    cfg = _rest_ref().get('order_number') or {}
    lo, hi = int(cfg.get('min', 10000)), int(cfg.get('max', 99999))
    d = _coll_dir('restaurant', 'orders')
    for _ in range(500):
        n = str(random.randint(lo, hi))
        if not (d / '{}.json'.format(n)).exists():
            return n
    raise RuntimeError('order number space exhausted')


_REST_ORDER_LOCKS = {}
_REST_LOCKS_GUARD = threading.Lock()


def _rest_order_lock(order_no):
    """One lock per order id. Every load -> mutate -> write on an order runs
    inside it, because Flask serves the bridge's tool calls and the dashboard's
    status clicks on different threads of the SAME process: without this, a
    modify_order and a "start preparing" interleave and the last writer silently
    reverts the other's fields, including status."""
    key = re.sub(r'[^0-9]', '', str(order_no or '')) or '-'
    with _REST_LOCKS_GUARD:
        lock = _REST_ORDER_LOCKS.get(key)
        if lock is None:
            lock = _REST_ORDER_LOCKS[key] = threading.Lock()
        return lock


def _rest_load_order(order_no):
    """Fresh (uncached) load for mutation. Accepts '#54873' or '54873'."""
    n = re.sub(r'[^0-9]', '', str(order_no or ''))
    if not n:
        return None
    f = _coll_dir('restaurant', 'orders') / '{}.json'.format(n)
    if not f.exists():
        return None
    try:
        with f.open() as fh:
            return json.load(fh)
    except Exception:
        return None


def _rest_find_order(order_no=None, phone=None, caller_phone=None):
    """Resolve the order a caller is asking about. Returns (record, error).

    Order numbers are short and numeric, so knowing one must NOT by itself grant
    control of somebody else's order — the caller has to be on, or able to name,
    the number the order was placed with. A caller ringing from the number they
    ordered on therefore just says the order number and it works; anyone else is
    asked for the phone the order was placed under.

    A supplied order number that does not exist is a hard miss: it must never
    fall through to 'their most recent order', or a caller correcting a digit
    would silently cancel a different order.
    """
    known = {p for p in (_rest_phone(phone), _rest_phone(caller_phone)) if p}

    if order_no:
        o = _rest_load_order(order_no)
        if not o:
            return None, 'not_found'
        if known and _rest_phone(o.get('phone')) in known:
            return o, None
        return None, 'not_authorised'

    for p in (known - {''}):
        mine = [r for r in _read_records('restaurant', 'orders') if _rest_phone(r.get('phone')) == p]
        if mine:
            live = [r for r in mine if r.get('status') in ('pending', 'preparing', 'out_for_delivery')]
            return _rest_load_order((live or mine)[0]['id']), None
    return None, 'not_found'


_REST_LOOKUP_ERRORS = {
    'not_found': {
        'error': 'not_found',
        'note': ('No order matches that. Ask the caller to repeat the order number digit by digit, or to give '
                 'the phone number they placed the order with. Do NOT invent a status and do NOT act on a '
                 'different order.'),
    },
    'not_authorised': {
        'error': 'not_authorised',
        'note': ('That order exists but it was not placed on this phone number, so you may not read it out or '
                 'change it. Ask the caller for the phone number the order was placed with. If they cannot give '
                 'it, apologise and offer to connect them to the branch.'),
    },
}


def _rest_eta(fulfilment, returning):
    """Spoken delivery/pickup quote + the 'Estimated Delivery' dashboard field (§6/§7)."""
    t = _rest_ref().get('timing') or {}
    if fulfilment == 'pickup':
        r = t.get('pickup') or {'min': 20, 'max': 20, 'spoken': 'approximately 20 minutes'}
    elif returning:
        r = t.get('existing_customer_delivery') or {'min': 30, 'max': 30, 'spoken': '30 minutes'}
    else:
        r = t.get('new_customer_delivery') or {'min': 35, 'max': 45, 'spoken': '35 to 45 minutes'}
    mid = int(round((int(r.get('min', 30)) + int(r.get('max', 30))) / 2.0))
    return r.get('spoken', ''), mid, '{} mins'.format(mid)


def _rest_place_order(payload, source='AI call', call_uuid=None):
    """Create an order. Returns (record, error_dict). The ONLY order-creation path
    — the dashboard's "New order" button and the voice agent both land here."""
    ref = _rest_ref()
    name = str(payload.get('customer_name') or payload.get('customer') or '').strip()
    phone = _rest_phone(payload.get('phone'))
    address = str(payload.get('address') or '').strip()
    fulfilment = 'pickup' if str(payload.get('fulfilment') or 'delivery').lower().startswith('pick') else 'delivery'
    notes = str(payload.get('notes') or '').strip()
    promo_id = payload.get('promo_id') or None
    payment = str(payload.get('payment') or 'Cash on delivery').strip()

    if not _rest_valid_phone(phone):
        return None, {'error': 'need_phone',
                      'message': 'Do NOT place the order yet. Ask the caller for a contact phone number, read it back digit by digit, then call place_food_order again.'}
    if not name:
        return None, {'error': 'need_name',
                      'message': 'Do NOT place the order yet. Ask the caller for their name, then call place_food_order again.'}

    items = payload.get('items') or []
    # A caller can ask for a promotion by name ("I'll take the bucket promotion")
    # — pull that promo's components into the basket instead of guessing items.
    if promo_id:
        promo = next((p for p in _rest_active_promotions() if p['id'] == promo_id), None)
        if promo and promo.get('adds'):
            # Count what the caller already chose against the promotion's own
            # REQUIREMENTS. Matching on exact SKU alone would ignore a drink they
            # already picked (the bundle requires 'any 2 drinks' but adds Pepsi),
            # and tip two extra Pepsis into the basket.
            have_lines = _rest_build_lines(items)[0]
            by_sku = {l['sku']: l['qty'] for l in have_lines}
            by_cat = {}
            for l in have_lines:
                by_cat[l['category']] = by_cat.get(l['category'], 0) + l['qty']
            reqs = promo.get('requires') or []
            topups = []
            for add in promo['adds']:
                want = int(add.get('qty') or 1)
                item = _rest_item(add.get('sku'))
                req = next((r for r in reqs if r.get('sku') == add.get('sku')), None)
                if req is None and item:
                    req = next((r for r in reqs if r.get('category') == item['category']), None)
                if req and req.get('category') and item:
                    already = by_cat.get(item['category'], 0)
                    want = int(req.get('qty') or want)
                else:
                    already = by_sku.get(add['sku'], 0)
                short = want - already
                if short > 0:
                    topups.append({'sku': add['sku'], 'quantity': short})
            items = list(items) + topups

    lines, unknown = _rest_build_lines(items)
    if unknown:
        return None, {'error': 'unknown_item', 'unknown': unknown,
                      'message': "That item is not on the menu. Do NOT invent it — use get_menu to read the caller the real options, then call place_food_order again with an item from the menu."}
    if not lines:
        return None, {'error': 'no_items', 'message': 'The order has no items yet. Ask the caller what they would like.'}

    branch = _rest_branch(payload.get('branch'))
    if fulfilment == 'pickup' and not str(payload.get('branch') or '').strip():
        # _rest_branch(None) falls back to the default branch, which would book a
        # collection order at Colombo without anyone ever naming a branch.
        return None, {'error': 'need_branch', 'branches': [b['name'] for b in (ref.get('branches') or [])],
                      'message': 'Ask the caller which branch they want to collect from, then call place_food_order again.'}
    if fulfilment == 'delivery':
        if not address:
            return None, {'error': 'need_address',
                          'message': 'Do NOT place the order yet. Ask the caller for the delivery address (or confirm their saved one), then call place_food_order again.'}
        area_branch, area = _rest_area_branch(address)
        if not area_branch:
            return None, {'error': 'out_of_delivery_area', 'areas': _rest_all_areas()[:14],
                          'message': 'We do not deliver to that address. Apologise, tell the caller which areas we cover, and offer store pickup from the nearest branch instead.'}
        branch = area_branch
        branch_area = area
    else:
        branch_area = None
        if not branch:
            return None, {'error': 'unknown_branch', 'branches': [b['name'] for b in (ref.get('branches') or [])],
                          'message': 'Ask the caller which branch they want to collect from.'}

    is_open, hours = _rest_is_open(branch=branch)
    if not is_open:
        return None, {'error': 'closed', 'hours': hours,
                      'message': 'The restaurant is CLOSED right now. Tell the caller our hours ({}) and that we cannot take the order at the moment. Do not place it.'.format(hours['spoken'])}

    existing = _rest_get_customer(phone)
    returning = bool(existing and (existing.get('orders') or 0) > 0)
    priced = _rest_price(lines, fulfilment=fulfilment, promo_id=promo_id)
    eta_spoken, eta_min, eta_field = _rest_eta(fulfilment, returning)

    oid = _rest_order_no()
    now = _rest_stamp()
    rec = {
        'id': oid, 'ref': '#' + oid, 'order_no': oid,
        'customer': name, 'phone': phone,
        'address': address if fulfilment == 'delivery' else '',
        'area': branch_area or '', 'branch': branch['name'] if branch else '',
        'branch_id': branch['id'] if branch else '',
        'fulfilment': fulfilment,
        'items': priced['items'], 'lines': priced['lines'],
        'subtotal': priced['subtotal'], 'discounts': priced['discounts'],
        'discount_total': priced['discount_total'], 'delivery_fee': priced['delivery_fee'],
        'total': priced['total'], 'currency': priced['currency'],
        'promo_id': promo_id or '',
        'payment': payment, 'paid': False,
        'status': 'pending', 'priority': False,
        'rider_name': '', 'rider_phone': '',
        'eta_spoken': eta_spoken, 'eta_minutes': eta_min, 'estimated_delivery': eta_field,
        'new_customer': not returning,
        'notes': notes, 'source': source, 'channel': 'Phone',
        'customer_id': _rest_customer_id(phone),
        'call_uuid': call_uuid or '', 'created': now, 'placed_at': now,
    }
    _write_record('restaurant', 'orders', rec)

    cust = _rest_upsert_customer(phone, name, address if fulfilment == 'delivery' else None, source=source)
    if cust:
        cust['orders'] = int(cust.get('orders') or 0) + 1
        cust['total_spend'] = int(cust.get('total_spend') or 0) + int(priced['total'])
        cust['last_order_at'] = now
        cust['last_order_ref'] = rec['ref']
        _write_record('restaurant', 'customers', cust)
    return rec, None


def _rest_reprice(rec):
    """Recompute totals from the record's own line items. Called after every
    modification so a total is never carried forward or guessed."""
    base = [l for l in (rec.get('lines') or []) if not l.get('free')]
    priced = _rest_price(base, fulfilment=rec.get('fulfilment') or 'delivery', promo_id=rec.get('promo_id') or None)
    rec['lines'] = priced['lines']
    rec['items'] = priced['items']
    rec['subtotal'] = priced['subtotal']
    rec['discounts'] = priced['discounts']
    rec['discount_total'] = priced['discount_total']
    rec['delivery_fee'] = priced['delivery_fee']
    rec['total'] = priced['total']
    return rec


def _rest_set_status(rec, status, actor='Staff', rider=None):
    """Advance an order. Returns (rec, error). Enforces the legal transitions so a
    delivered or cancelled order can't be quietly reopened.

    Callers must hold _rest_order_lock(rec['id']) and must have loaded `rec`
    inside it — the transition check and the write have to be one atomic step."""
    status = str(status or '').strip().lower().replace(' ', '_')
    if status not in REST_STATUS_LABELS:
        return None, {'error': 'bad_status', 'message': 'Unknown status.'}
    cur = rec.get('status') or 'pending'
    if cur in ('delivered', 'cancelled'):
        return None, {'error': 'closed_order',
                      'message': 'Order {} is already {} and cannot be changed.'.format(rec.get('ref'), REST_STATUS_LABELS[cur])}
    if status == 'cancelled':
        if cur not in (_rest_ref().get('cancellable_statuses') or ['pending', 'preparing']):
            return None, {'error': 'not_cancellable', 'status': cur,
                          'message': 'This order has already left the kitchen, so it cannot be cancelled over the phone. Offer to connect the branch.'}
    elif status != cur:
        try:
            if REST_STATUS_FLOW.index(status) < REST_STATUS_FLOW.index(cur):
                return None, {'error': 'bad_transition',
                              'message': 'Cannot move an order backwards from {} to {}.'.format(cur, status)}
        except ValueError:
            pass
    now = _rest_stamp()
    rec['status'] = status
    rec['updated'] = now
    rec['updated_by'] = actor
    stamp = {'preparing': 'preparing_at', 'out_for_delivery': 'dispatched_at',
             'delivered': 'delivered_at', 'cancelled': 'cancelled_at'}.get(status)
    if stamp:
        rec.setdefault(stamp, now)
    if status == 'out_for_delivery' and rider:
        rec['rider_name'] = rider.get('name') or rec.get('rider_name') or ''
        rec['rider_phone'] = rider.get('phone') or rec.get('rider_phone') or ''
        rec['rider_eta_minutes'] = int((_rest_ref().get('timing') or {}).get('out_for_delivery_eta', {}).get('minutes') or 8)
    if status == 'delivered':
        rec['paid'] = True
    _write_record('restaurant', 'orders', rec)
    return rec, None


def _rest_status_report(rec):
    """The spoken status payload (spec Flow 5): kitchen vs. on-the-road."""
    t = _rest_ref().get('timing') or {}
    st = rec.get('status') or 'pending'
    out = {'order_number': rec.get('ref'), 'status': st, 'status_label': REST_STATUS_LABELS.get(st, st),
           'customer': rec.get('customer'), 'items': rec.get('items'), 'total': rec.get('total'),
           'fulfilment': rec.get('fulfilment'), 'branch': rec.get('branch'), 'priority': bool(rec.get('priority'))}
    if st in ('pending', 'preparing'):
        k = t.get('in_kitchen') or {}
        out['stage'] = 'in_kitchen'
        out['ready_in_minutes'] = int(k.get('ready_minutes') or 8)
        if rec.get('fulfilment') == 'pickup':
            out['note'] = ('The order is still in the kitchen. Tell the caller the current status and that it will be ready for '
                           'COLLECTION at the {} branch in about {} minutes. This is a pickup order — do NOT mention delivery. '
                           'Then offer to ask the restaurant to prioritise it.'
                           ).format(rec.get('branch') or 'collection', out['ready_in_minutes'])
        else:
            out['delivery_in_minutes'] = [int(k.get('delivery_min') or 15), int(k.get('delivery_max') or 20)]
            out['note'] = ('The order is still in the kitchen. Tell the caller the current status, that it will be ready in about {} minutes '
                           'and delivery is another {} to {} minutes. Then offer to ask the restaurant to prioritise it.'
                           ).format(out['ready_in_minutes'], out['delivery_in_minutes'][0], out['delivery_in_minutes'][1])
    elif st == 'out_for_delivery':
        out['stage'] = 'out_for_delivery'
        out['rider_name'] = rec.get('rider_name') or ''
        out['rider_phone'] = rec.get('rider_phone') or ''
        out['eta_minutes'] = int(rec.get('rider_eta_minutes') or (t.get('out_for_delivery_eta') or {}).get('minutes') or 8)
        out['note'] = ('The order is already out for delivery. Give the caller the rider name, the rider phone number (read digits one at a time) '
                       'and the estimated arrival exactly as returned.')
    elif st == 'delivered':
        out['stage'] = 'delivered'
        out['note'] = 'This order has already been delivered. If the caller says they did not receive it, offer to connect them to the branch.'
    else:
        out['stage'] = 'cancelled'
        out['note'] = 'This order was cancelled. Offer to place a new one.'
    return out


# ---------------------------------------------------------------- agent tools

def _rest_menu_payload(items, cat=None):
    ref = _rest_ref()
    return {
        'currency': ref.get('currency') or 'LKR',
        'category': (cat or {}).get('label') if cat else None,
        'count': len(items),
        'items': [{'sku': m['sku'], 'name': m['name'], 'price': m['price'],
                   'category': m['category'], 'spoken_price': _rest_money(m['price'])} for m in items],
    }


def _tool_lookup_customer(a):
    """Caller identification (spec §2). Returns everything needed to greet a
    returning caller by name and confirm their saved address.

    Keyed on the CALLER ID only (`_caller_phone`, set by the bridge), never on a
    number the model passes in. Otherwise this tool would hand out any
    customer's name, home address and order history to whoever asked for their
    number — the model is not a trustworthy source of "whose record to read"."""
    phone = _rest_phone(a.get('_caller_phone'))
    if not _rest_valid_phone(phone):
        return {'known': False, 'reason': 'no_caller_id',
                'note': ("There is no usable caller ID for this call. Greet the caller normally and, when you get to the order, "
                         "ask for their name, phone number (digit by digit) and delivery address as a new customer.")}
    cust = _rest_get_customer(phone)
    if not cust:
        return {'known': False, 'phone': phone,
                'note': ("This is a NEW customer — we have no record of {}. Take their name and delivery address during the order. "
                         "Do not claim to recognise them.").format(phone)}
    orders = [r for r in _read_records('restaurant', 'orders') if _rest_phone(r.get('phone')) == phone]
    recent = [{'order_number': r.get('ref'), 'items': r.get('items'), 'total': r.get('total'),
               'status': r.get('status'), 'date': str(r.get('created', ''))[:10]} for r in orders[:3]]
    addr = _rest_default_address(cust)
    return {
        'known': True, 'phone': phone, 'name': cust.get('name') or '',
        'saved_address': addr,
        'addresses': [x.get('address') for x in (cust.get('addresses') or [])],
        'order_count': int(cust.get('orders') or 0), 'total_spend': int(cust.get('total_spend') or 0),
        'recent_orders': recent,
        'note': ("Returning customer. Greet them BY NAME. Do NOT ask for their name, phone number or address again — "
                 + ('confirm the saved address instead: "Would you like it delivered to {}?"'.format(addr) if addr
                    else 'they have no saved address yet, so ask for one when the order is delivery.')),
    }


def _tool_get_menu(a):
    items, cat = _rest_search(query=a.get('query'), category=a.get('category'), limit=int(a.get('limit') or 12))
    if not items:
        cats = [c['label'] for c in _rest_categories(spoken_only=True)]
        empty = _rest_category(a.get('category') or a.get('query') or '')
        if empty:
            return {'count': 0, 'category': empty['label'], 'categories': cats,
                    'note': ("We have no items listed under {} yet. Do NOT invent any. Tell the caller you will check that with the branch, "
                             "and offer another category.").format(empty['label'])}
        return {'count': 0, 'categories': cats,
                'note': ("Nothing on the menu matches that. Do NOT invent an item. Ask the caller to say it another way, "
                         "or read them the categories: " + ', '.join(cats) + '.')}
    p = _rest_menu_payload(items, cat)
    p['note'] = ('Read these out with the price for each as "LKR <amount>", one per line, then ask which one they would like. '
                 'These prices are live from the menu database — never quote a price that is not here.')
    return p


def _tool_get_promotions(a):
    promos = _rest_active_promotions()
    if not promos:
        return {'count': 0, 'note': 'There are no promotions running today. Say so plainly and offer to take an order.'}
    return {
        'count': len(promos), 'currency': _rest_ref().get('currency') or 'LKR',
        'promotions': [{'id': p['id'], 'label': p['label'], 'spoken': p.get('spoken') or p['label'], 'type': p.get('type')} for p in promos],
        'note': ("Read the offers out, one per line, then ask if they would like to order any of them. If they pick one, pass its 'id' "
                 "as promo_id to place_food_order — the discount is applied server-side, so never calculate one yourself."),
    }


def _tool_get_branch_info(a):
    ref = _rest_ref()
    branches = ref.get('branches') or []
    topic = _rest_norm(a.get('topic'))
    q = a.get('branch')
    b = _rest_branch(q) if q else None
    now = _rest_now()
    is_open, hours = _rest_is_open(now, b)
    base = {
        'brand': ref.get('brand'), 'now_local': now.strftime('%Y-%m-%d %H:%M'),
        'open_now': is_open, 'hours': hours,
        'payment_methods': ref.get('payment_methods') or [],
        'hotline': ref.get('hotline') or '',
    }
    if not q:
        base.update({
            'branches': [x['name'] for x in branches],
            'note': ('We have branches at ' + ', '.join(x['name'] for x in branches) +
                     '. Ask which branch they want details for. '
                     + ('We are OPEN right now.' if is_open else 'We are CLOSED right now — say so if they ask.')),
        })
        return base
    if not b:
        return {'error': 'unknown_branch', 'branches': [x['name'] for x in branches],
                'note': 'We have no branch by that name. Read out the branches we do have: ' + ', '.join(x['name'] for x in branches) + '.'}
    base.update({
        'branch': b['name'], 'address': b.get('address'), 'phone': b.get('phone'),
        'delivery_areas': b.get('delivery_areas') or [],
        'note': ('Our {} branch is at {}. We are open every day from {}. '.format(b['name'], b.get('address'), hours['spoken'])
                 + ('We are open right now.' if is_open else 'We are closed right now.')),
    })
    return base


def _tool_place_food_order(a, call_uuid=None):
    rec, err = _rest_place_order(a, source=a.get('_source') or 'AI call', call_uuid=call_uuid)
    if err:
        out = dict(err)
        out['ok'] = False
        return out
    return {
        'ok': True, 'order_number': rec['ref'], 'order_no': rec['id'],
        'customer': rec['customer'], 'phone': rec['phone'], 'address': rec['address'],
        'branch': rec['branch'], 'fulfilment': rec['fulfilment'],
        'items': rec['items'], 'lines': rec['lines'],
        'subtotal': rec['subtotal'], 'discounts': rec['discounts'], 'total': rec['total'],
        'spoken_total': _rest_money(rec['total']), 'currency': rec['currency'],
        'eta_spoken': rec['eta_spoken'], 'estimated_delivery': rec['estimated_delivery'],
        'note': ('Order placed. Tell the caller it has been placed successfully, that the estimated {} time is {}, '
                 'and read out the order number {} exactly as returned. Then thank them. Never invent an order number or a total.'
                 ).format('collection' if rec['fulfilment'] == 'pickup' else 'delivery', rec['eta_spoken'], rec['ref']),
    }


def _tool_check_order_status(a):
    rec, err = _rest_find_order(a.get('order_number'), a.get('phone'), a.get('_caller_phone'))
    if err:
        return dict(_REST_LOOKUP_ERRORS[err], ok=False)
    out = _rest_status_report(rec)
    out['ok'] = True
    return out


def _tool_cancel_order(a):
    rec, err = _rest_find_order(a.get('order_number'), a.get('phone'), a.get('_caller_phone'))
    if err:
        return dict(_REST_LOOKUP_ERRORS[err], ok=False)
    if not a.get('confirm'):
        st = rec.get('status')
        if st not in (_rest_ref().get('cancellable_statuses') or ['pending', 'preparing']):
            return {'ok': False, 'error': 'not_cancellable', 'status': st, 'order_number': rec.get('ref'),
                    'note': ('Order {} is already {} and cannot be cancelled over the phone. Apologise, explain that, and offer to '
                             'connect them to the {} branch.').format(rec.get('ref'), REST_STATUS_LABELS.get(st, st), rec.get('branch'))}
        return {'ok': True, 'cancellable': True, 'confirmed': False, 'order_number': rec.get('ref'),
                'status': st, 'items': rec.get('items'), 'total': rec.get('total'),
                'note': ('Order {} is still {} so it CAN be cancelled. Tell the caller that and ask them to confirm they want to go ahead. '
                         'Only when they say yes, call cancel_order again with confirm set to true.').format(rec.get('ref'), REST_STATUS_LABELS.get(st, st))}
    with _rest_order_lock(rec['id']):
        fresh = _rest_load_order(rec['id'])
        if not fresh:
            return dict(_REST_LOOKUP_ERRORS['not_found'], ok=False)
        rec2, err = _rest_set_status(fresh, 'cancelled', actor='AI call')
    if err:
        out = dict(err)
        out['ok'] = False
        out['order_number'] = rec.get('ref')
        return out
    return {'ok': True, 'cancelled': True, 'order_number': rec2.get('ref'), 'status': 'cancelled',
            'note': 'Order {} is cancelled. Confirm that to the caller and ask if there is anything else.'.format(rec2.get('ref'))}


def _tool_modify_order(a):
    if a.get('_staff'):
        rec, err = _rest_load_order(a.get('order_number')), None
        if not rec:
            err = 'not_found'
    else:
        rec, err = _rest_find_order(a.get('order_number'), a.get('phone'), a.get('_caller_phone'))
    if err:
        return dict(_REST_LOOKUP_ERRORS[err], ok=False)
    with _rest_order_lock(rec['id']):
        return _rest_modify_locked(rec['id'], a)


def _rest_modify_locked(order_id, a):
    # Re-read inside the lock: the status we validated a moment ago may already
    # have been advanced by the kitchen clicking "start preparing".
    rec = _rest_load_order(order_id)
    if not rec:
        return dict(_REST_LOOKUP_ERRORS['not_found'], ok=False)
    st = rec.get('status')
    allowed = _rest_ref().get('modifiable_statuses') or ['pending', 'preparing']
    if st not in allowed:
        return {'ok': False, 'error': 'not_modifiable', 'status': st, 'order_number': rec.get('ref'),
                'note': ('Order {} has already left the kitchen, so it cannot be changed. Apologise and offer to connect the {} branch.'
                         ).format(rec.get('ref'), rec.get('branch'))}

    changed, save_address = [], None
    if a.get('prioritise') is not None:
        rec['priority'] = bool(a.get('prioritise'))
        changed.append('priority flag set' if rec['priority'] else 'priority flag cleared')

    if a.get('address'):
        if rec.get('fulfilment') != 'delivery':
            return {'ok': False, 'error': 'pickup_order', 'order_number': rec.get('ref'),
                    'note': 'This is a store-pickup order, so there is no delivery address to change.'}
        b, area = _rest_area_branch(a['address'])
        if not b:
            return {'ok': False, 'error': 'out_of_delivery_area', 'areas': _rest_all_areas()[:14],
                    'note': 'We do not deliver to that new address. Tell the caller which areas we cover.'}
        rec['address'] = str(a['address']).strip()
        rec['area'] = area or ''
        rec['branch'] = b['name']
        rec['branch_id'] = b['id']
        save_address = rec['address']        # written to the CRM only once the WHOLE edit succeeds
        changed.append('address updated')

    base = [dict(l) for l in (rec.get('lines') or []) if not l.get('free')]
    if a.get('add_items'):
        add, unknown = _rest_build_lines(a['add_items'])
        if unknown:
            return {'ok': False, 'error': 'unknown_item', 'unknown': unknown,
                    'note': 'That item is not on the menu — do NOT invent it. Use get_menu and offer a real one.'}
        for l in add:
            ex = next((x for x in base if x['sku'] == l['sku']), None)
            if ex:
                ex['qty'] += l['qty']
            else:
                base.append(l)
        changed.append('added ' + _rest_items_text(add))
    if a.get('remove_items'):
        rm, unknown = _rest_build_lines(a['remove_items'], min_qty=0)
        if unknown:
            return {'ok': False, 'error': 'unknown_item', 'unknown': unknown,
                    'note': 'That item is not on the order. Read the caller what the order currently contains.'}
        rm = [l for l in rm if l['qty'] > 0]
        if not rm:
            return {'ok': False, 'error': 'nothing_to_remove',
                    'note': 'No quantity was given to remove. Ask the caller how many they want taken off.'}
        for l in rm:
            ex = next((x for x in base if x['sku'] == l['sku']), None)
            if not ex:
                return {'ok': False, 'error': 'not_on_order', 'item': l['name'], 'items': rec.get('items'),
                        'note': '{} is not on this order. Read out what the order actually contains.'.format(l['name'])}
            ex['qty'] -= l['qty']
        base = [x for x in base if x['qty'] > 0]
        if not base:
            return {'ok': False, 'error': 'would_empty_order', 'order_number': rec.get('ref'),
                    'note': 'Removing that would leave the order empty. Ask the caller whether they want to cancel the whole order instead.'}
        changed.append('removed ' + _rest_items_text(rm))

    if a.get('add_items') or a.get('remove_items'):
        rec['lines'] = base
    _rest_reprice(rec)
    rec['updated'] = _rest_stamp()
    rec['updated_by'] = 'AI call'
    _write_record('restaurant', 'orders', rec)
    if save_address:
        _rest_upsert_customer(rec.get('phone'), rec.get('customer'), save_address, source='AI call')
    return {
        'ok': True, 'order_number': rec.get('ref'), 'status': rec.get('status'),
        'changed': changed, 'items': rec.get('items'), 'lines': rec.get('lines'),
        'subtotal': rec.get('subtotal'), 'discounts': rec.get('discounts'),
        'total': rec.get('total'), 'spoken_total': _rest_money(rec.get('total')),
        'priority': bool(rec.get('priority')),
        'note': ('Order updated: {}. Tell the caller what changed and read out the updated total, {}, exactly as returned — '
                 'it has been recomputed from the real line items. Then ask them to confirm the updated order.'
                 ).format('; '.join(changed) or 'no change', _rest_money(rec.get('total'))),
    }


REST_AGENT_TOOLS = {
    'lookup_customer': _tool_lookup_customer,
    'get_menu': _tool_get_menu,
    'get_promotions': _tool_get_promotions,
    'get_branch_info': _tool_get_branch_info,
    'place_food_order': _tool_place_food_order,
    'check_order_status': _tool_check_order_status,
    'cancel_order': _tool_cancel_order,
    'modify_order': _tool_modify_order,
}


def _agent_token():
    """Shared secret for the voice bridge. Auto-created on first use so the
    installer does not have to provision it."""
    try:
        with AGENT_API_FILE.open() as f:
            tok = (json.load(f) or {}).get('token')
        if tok:
            return str(tok)
    except Exception:
        pass
    tok = uuid.uuid4().hex + uuid.uuid4().hex
    try:
        AGENT_API_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = AGENT_API_FILE.with_suffix('.tmp')
        with tmp.open('w') as f:
            json.dump({'token': tok, '_note': 'Shared secret used by bridge.ts to call /api/agent/<vertical>/<tool>.'}, f, indent=2)
        os.chmod(tmp, 0o600)
        tmp.replace(AGENT_API_FILE)
    except Exception as e:
        print('[agent-api] could not persist token:', e, flush=True)
    return tok


@app.route('/api/agent/<vertical>/<tool>', methods=['POST'])
def api_agent_tool(vertical, tool):
    """Tool endpoint for the voice agent. bridge.ts proxies every restaurant
    function call here so that pricing, promotions and the order lifecycle have
    exactly ONE implementation. Localhost + shared-secret only — no user session."""
    # cloudflared terminates monitor.easmoney.me on this same host and proxies to
    # 127.0.0.1:5051, so remote_addr is 127.0.0.1 for INTERNET traffic too — it
    # is not a network boundary on its own. The shared secret below is the real
    # control; the proxy headers cloudflared adds are what actually distinguish
    # a tunnelled request from the bridge's direct loopback call.
    if request.remote_addr not in ('127.0.0.1', '::1', 'localhost'):
        return jsonify({'error': 'forbidden'}), 403
    if any(request.headers.get(h) for h in ('X-Forwarded-For', 'X-Forwarded-Proto',
                                            'CF-Connecting-IP', 'CF-Ray', 'Forwarded')):
        return jsonify({'error': 'forbidden'}), 403
    if not hashlib.sha256((request.headers.get('X-Agent-Token') or '').encode()).digest() == \
            hashlib.sha256(_agent_token().encode()).digest():
        return jsonify({'error': 'unauthorized'}), 401
    if vertical != 'restaurant' or tool not in REST_AGENT_TOOLS:
        return jsonify({'error': 'unknown tool'}), 404
    raw = request.get_json(force=True, silent=True) or {}
    trusted = {k: raw.get(k) for k in ('_caller_phone', '_call_uuid')}
    args = {k: v for k, v in raw.items() if not k.startswith('_')}
    args['_caller_phone'] = trusted['_caller_phone'] or ''
    call_uuid = trusted['_call_uuid']
    try:
        fn = REST_AGENT_TOOLS[tool]
        out = fn(args, call_uuid) if tool == 'place_food_order' else fn(args)
    except Exception as e:
        print('[agent-api] {} failed: {}'.format(tool, e), flush=True)
        return jsonify({'ok': False, 'error': 'internal',
                        'note': 'The ordering system is temporarily unavailable. Apologise and offer to connect the caller to the branch.'}), 500
    return jsonify(out)


# ---------------------------------------------------------------- dashboard routes

def _rest_guard():
    g = _dash_guard('restaurant', 'orders')
    return g


@app.route('/api/dash/restaurant/orders/<rid>/status', methods=['POST'])
@login_required
@perm_required('call')
def api_rest_status(rid):
    g = _rest_guard()
    if g:
        return g
    j = request.get_json(force=True) or {}
    rider = None
    if j.get('rider_id') or j.get('rider_name'):
        rider = _rest_rider(j.get('rider_id')) or {'name': j.get('rider_name'), 'phone': j.get('rider_phone')}
    with _rest_order_lock(rid):
        rec = _rest_load_order(rid)
        if not rec:
            return jsonify({'error': 'not found'}), 404
        rec2, err = _rest_set_status(rec, j.get('status'), actor=session.get('user') or 'Staff', rider=rider)
    if err:
        return jsonify(err), 400
    return jsonify({'ok': True, 'record': rec2})


def _rest_rider(rider_id):
    if not rider_id:
        return None
    return next((r for r in (_rest_ref().get('riders') or []) if r.get('id') == rider_id or _rest_norm(r.get('name')) == _rest_norm(rider_id)), None)


@app.route('/api/dash/restaurant/orders/<rid>/rider', methods=['POST'])
@login_required
@perm_required('call')
def api_rest_rider(rid):
    g = _rest_guard()
    if g:
        return g
    j = request.get_json(force=True) or {}
    with _rest_order_lock(rid):
        rec = _rest_load_order(rid)
        if not rec:
            return jsonify({'error': 'not found'}), 404
        r = _rest_rider(j.get('rider_id')) or ({'name': j.get('name'), 'phone': j.get('phone')} if j.get('name') else None)
        if not r or not r.get('name'):
            return jsonify({'error': 'unknown rider'}), 400
        rec['rider_name'] = r['name']
        rec['rider_phone'] = r.get('phone') or ''
        rec['rider_eta_minutes'] = int((_rest_ref().get('timing') or {}).get('out_for_delivery_eta', {}).get('minutes') or 8)
        rec['updated'] = _rest_stamp()
        rec['updated_by'] = session.get('user') or 'Staff'
        _write_record('restaurant', 'orders', rec)
    return jsonify({'ok': True, 'record': rec})


@app.route('/api/dash/restaurant/orders/<rid>/priority', methods=['POST'])
@login_required
@perm_required('call')
def api_rest_priority(rid):
    g = _rest_guard()
    if g:
        return g
    j = request.get_json(force=True) or {}
    with _rest_order_lock(rid):
        rec = _rest_load_order(rid)
        if not rec:
            return jsonify({'error': 'not found'}), 404
        rec['priority'] = bool(j.get('priority', True))
        rec['updated'] = _rest_stamp()
        rec['updated_by'] = session.get('user') or 'Staff'
        _write_record('restaurant', 'orders', rec)
    return jsonify({'ok': True, 'record': rec})


@app.route('/api/dash/restaurant/orders/<rid>/items', methods=['POST'])
@login_required
@perm_required('call')
def api_rest_items(rid):
    """Staff-side order modification — same reprice path the voice agent uses."""
    g = _rest_guard()
    if g:
        return g
    rec = _rest_load_order(rid)
    if not rec:
        return jsonify({'error': 'not found'}), 404
    j = request.get_json(force=True) or {}
    out = _tool_modify_order({'order_number': rid, '_staff': True,
                              'add_items': j.get('add'), 'remove_items': j.get('remove')})
    if not out.get('ok'):
        return jsonify(out), 400
    return jsonify({'ok': True, 'record': _rest_load_order(rid)})


@app.route('/api/dash/<vertical>/refdata')
@login_required
def api_dash_refdata(vertical):
    g = _dash_guard(vertical)
    if g:
        return g
    return jsonify(_load_refdata(vertical))


@app.route('/api/dash/<vertical>/<collection>', methods=['GET', 'POST'])
@login_required
def api_dash_collection(vertical, collection):
    g = _dash_guard(vertical, collection)
    if g:
        return g
    if request.method == 'GET':
        return jsonify(_read_records(vertical, collection))
    if not _may_write():
        return jsonify({'error': 'forbidden'}), 403
    # POST = staff manually creates a record from the dashboard
    j = request.get_json(force=True) or {}
    if vertical == 'restaurant':
        # Restaurant orders/customers go through the same engine the voice agent
        # uses, so a phone order and a counter order are priced identically.
        if collection == 'orders':
            rec, err = _rest_place_order(j, source=j.get('source') or 'Counter')
            if err:
                return jsonify(err), 400
            return jsonify({'ok': True, 'id': rec['id'], 'record': rec})
        rec = _rest_upsert_customer(j.get('phone'), j.get('name'), j.get('address'),
                                    source=j.get('source') or 'Manual')
        if not rec:
            return jsonify({'error': 'need_phone'}), 400
        return jsonify({'ok': True, 'id': rec['id'], 'record': rec})
    if vertical == 'petcare' and collection == 'appointments':
        # Written into the petcare-ai agent's own bookings tree, so the record
        # must be shaped exactly like one the phone agent books: `appt-<uuid>`
        # id, HP-AP-###### reference, `created_at` (not `created`). Everything
        # the server owns is stripped from the client payload first — the
        # generic blocklist does not know about these field names.
        rec = {k: v for k, v in j.items()
               if k not in _REC_CREATE_BLOCKED
               and k not in ('reference', 'created_at', 'created_by',
                             'cancelled_at', 'cancelled_by', 'call_uuid')}
        rec['id'] = 'appt-' + str(uuid.uuid4())
        rec['reference'] = _gen_petcare_ref()
        rec.setdefault('status', 'booked')
        rec.setdefault('source', 'Manual')
        rec['created_at'] = _iso_z()
        rec['created_by'] = session.get('user')
        _write_record(vertical, collection, rec)
        return jsonify({'ok': True, 'id': rec['id'], 'record': rec})
    if vertical == 'computershop':
        # Written into the computershop-ai agent's own bookings tree, so the
        # record has to be shaped exactly like one the phone agent writes:
        # `ord-<uuid>`/`rep-<uuid>` id, BH-OR/BH-RP-###### reference, a `kind`
        # discriminator and `created_at` (not `created`). Everything the server
        # owns is stripped from the client payload first — the generic
        # blocklist does not know these field names.
        rec = {k: v for k, v in j.items()
               if k not in _REC_CREATE_BLOCKED
               and k not in ('reference', 'created_at', 'created_by', 'kind',
                             'cancelled_at', 'cancelled_by', 'call_uuid')}
        ref = _load_refdata('computershop')
        # Branch name is the shop's, never the caller's — look it up rather than
        # trust whatever came in on the payload.
        branch = next((b for b in (ref.get('branches') or [])
                       if str(b.get('id', '')).lower() == str(rec.get('branch') or '').lower()), None)
        if branch:
            rec['branch_name'] = branch.get('name')
        if collection == 'orders':
            # Price is the catalogue's, and the line total is derived here —
            # `total` is in _REC_CREATE_BLOCKED precisely so a client cannot
            # dictate what an order is worth.
            p = next((x for x in (ref.get('products') or [])
                      if str(x.get('sku', '')).lower() == str(rec.get('sku') or '').lower()), None)
            if p:
                rec['product'] = p.get('name')
                rec['unit_price'] = p.get('price', 0)
            try:
                qty = int(rec.get('qty') or 1)
            except Exception:
                qty = 1
            rec['qty'] = max(1, qty)
            rec['total'] = round(float(rec.get('unit_price') or 0) * rec['qty'], 2)
            rec['id'] = 'ord-' + str(uuid.uuid4())
            rec['kind'] = 'order'
            rec.setdefault('status', 'reserved')
        else:
            # Hamburg has no workshop. That is a business rule, so the server
            # decides it — refuse rather than record a job nobody can do. Only
            # enforced when the branch is actually in the catalogue, so an
            # unreadable refdata file cannot block every booking.
            if branch and branch.get('has_workshop') is False:
                return jsonify({'error': 'branch_has_no_workshop',
                                'branch': branch.get('name')}), 400
            s = next((x for x in (ref.get('repair_services') or [])
                      if str(x.get('name', '')).lower() == str(rec.get('service') or '').lower()), None)
            if s:
                rec['price'] = s.get('price', 0)
                rec['turnaround'] = s.get('turnaround')
            rec['id'] = 'rep-' + str(uuid.uuid4())
            rec['kind'] = 'repair'
            rec.setdefault('status', 'booked')
        rec['reference'] = _gen_computershop_ref(collection)
        rec.setdefault('source', 'Manual')
        rec['created_at'] = _iso_z()
        rec['created_by'] = session.get('user')
        _write_record(vertical, collection, rec)
        return jsonify({'ok': True, 'id': rec['id'], 'record': rec})
    if vertical == 'hospital' and collection == 'patients':
        mrn = _upsert_patient(j.get('name'), j.get('phone'),
                              {k: v for k, v in j.items() if k in ('age', 'gender', 'allergies', 'address', 'nic')})
        try:
            return jsonify({'ok': True, 'id': mrn, 'record': json.load((_coll_dir('hospital', 'patients') / f'{mrn}.json').open())})
        except Exception:
            return jsonify({'ok': True, 'id': mrn})
    rid = _gen_appt_id() if (vertical == 'hospital' and collection == 'appointments') else _new_rec_id(collection)
    # Client fields are copied wholesale, so anything the SERVER is supposed to
    # derive must be refused here — otherwise a caller could POST a record with
    # its own critical flag, lab results, queue number or fee and the dashboard
    # would show them as if the server had computed them. (Patch already has a
    # whitelist, _REC_PATCHABLE; create had nothing.)
    rec = {k: v for k, v in j.items() if k not in _REC_CREATE_BLOCKED}
    rec['id'] = rid
    rec.setdefault('ref', rid)
    rec.setdefault('status', 'booked')
    rec.setdefault('source', 'Manual')
    rec.setdefault('paid', False)
    # Enrich a hospital appointment from the doctor directory (fee/specialty/branch).
    if vertical == 'hospital' and collection == 'appointments' and rec.get('doctor'):
        ref = _load_refdata('hospital')
        d = next((x for x in ref.get('doctors', [])
                  if x.get('name', '').lower() == str(rec['doctor']).lower()), None)
        if d:
            rec.setdefault('specialty', d.get('specialty'))
            rec.setdefault('fee', d.get('fee', 0))
            if not rec.get('branch') and d.get('branches'):
                rec['branch'] = d['branches'][0]
        rec.setdefault('type', 'Consultation')
        # Channelling queue number + doctor session start — assigned once the
        # doctor/branch are known.
        rec['queue_no'] = _next_queue_no(rec.get('doctor'), rec.get('branch'), rec.get('date'))
        if d:
            rec['session_time'] = _session_start(d.get('slots') or [], rec.get('time'))
    if vertical == 'reservations' and collection == 'reservations':
        ref = _load_refdata('reservations')
        try:
            party = int(rec.get('party') or 0)
        except Exception:
            party = 0
        if party and not rec.get('deposit'):
            rec['deposit'] = party * int(ref.get('depositPerGuest', 0) or 0)
        rec.setdefault('channel', rec.get('source', 'Manual'))
    if vertical == 'hospital' and collection == 'labs':
        t = _load_test(rec.get('test_code') or rec.get('test') or rec.get('test_name'))
        if t:
            rec['test_code'] = t['code']
            rec.setdefault('test_name', t['name'])
            rec.setdefault('department', t.get('department'))
            rec.setdefault('specimen', t.get('specimen'))
            rec.setdefault('cost', t.get('price', 0))
            rec['panel'] = len(t.get('analytes', [])) > 1
        rec.setdefault('accession', _gen_accession())
        rec.setdefault('priority', 'Routine')
        rec['status'] = 'ordered'
        rec.setdefault('results', [])
        rec.setdefault('critical', False)
        rec['ordered_at'] = datetime.datetime.now().isoformat(timespec='seconds')
        rec.setdefault('ordered_by', session.get('user') or 'Staff')
    if vertical == 'hospital' and collection in ('appointments', 'labs') and rec.get('phone'):
        rec['mrn'] = _upsert_patient(rec.get('patient'), rec.get('phone'))
    rec['created'] = datetime.datetime.now().isoformat(timespec='seconds')
    rec['created_by'] = session.get('user')
    _write_record(vertical, collection, rec)
    return jsonify({'ok': True, 'id': rid, 'record': rec})


@app.route('/api/dash/<vertical>/<collection>/<rid>', methods=['POST', 'DELETE'])
@login_required
def api_dash_record(vertical, collection, rid):
    g = _dash_guard(vertical, collection)
    if g:
        return g
    if not _may_write():
        return jsonify({'error': 'forbidden'}), 403
    if '/' in rid or '..' in rid:
        return jsonify({'error': 'bad id'}), 400
    f = _coll_dir(vertical, collection) / f'{rid}.json'
    if not f.exists():
        return jsonify({'error': 'not found'}), 404
    if request.method == 'DELETE':
        try:
            f.unlink()
        except Exception:
            pass
        return jsonify({'ok': True, 'deleted': rid})
    try:
        rec = json.load(f.open())
    except Exception:
        return jsonify({'error': 'unreadable'}), 500
    patch = request.get_json(force=True) or {}
    was = rec.get('status')
    for k, v in patch.items():
        if k in _REC_PATCHABLE:
            rec[k] = v
    # petcare / computershop cancellations carry who/when, matching what the
    # phone agent writes when a caller cancels — the dashboard detail view reads
    # these back.
    if vertical in ('petcare', 'computershop') and rec.get('status') == 'cancelled' and was != 'cancelled':
        rec['cancelled_at'] = _iso_z()
        rec['cancelled_by'] = session.get('user') or 'Staff'
    rec['updated'] = datetime.datetime.now().isoformat(timespec='seconds')
    rec['updated_by'] = session.get('user')
    _write_record(vertical, collection, rec)
    return jsonify({'ok': True, 'record': rec})


def _originate_outbound(vertical, collection, rid, kind, by='auto'):
    """Core of an outbound AI call — write the call context to OUTBOUND_DIR/<uuid>.json
    then AMI-originate into the [ai-outbound] dialplan, which bridges to the agent on
    9092 with the right persona (by 'kind'), which records the outcome on the record.
    No session/guard, so it is callable from the auto-confirm watcher as well as the
    dashboard routes. Returns a plain dict; error dicts carry a 'status' for the HTTP
    wrapper.
    SAFETY: refdata[vertical].confirm_test_number, if set, overrides the dialled
    number so every call rings that test number instead of the real contact."""
    if '/' in rid or '..' in rid:
        return {'error': 'bad id', 'status': 400}
    f = _coll_dir(vertical, collection) / f'{rid}.json'
    if not f.exists():
        return {'error': 'record not found', 'status': 404}
    try:
        o = json.load(f.open())
    except Exception:
        return {'error': 'unreadable record', 'status': 500}
    ref = _load_refdata(vertical)
    phone = re.sub(r'[^0-9+]', '', str(o.get('phone') or ''))
    test_num = re.sub(r'[^0-9+]', '', str(ref.get('confirm_test_number') or ''))
    dial = test_num or phone
    if len(re.sub(r'\D', '', dial)) < 7:
        return {'error': 'no valid phone number on this record', 'status': 400}
    if collection == 'appointments':
        customer = o.get('patient')
        summary = ('%s (%s) at %s on %s %s' % (o.get('doctor', ''), o.get('specialty', ''),
                   o.get('branch', ''), o.get('date', ''), o.get('time', ''))).strip()
    elif collection == 'labs':
        customer = o.get('patient')
        summary = o.get('test_name') or 'your lab test'
    else:
        customer = o.get('customer')
        summary = o.get('items')
    u = str(uuid.uuid4())
    ctx = {
        'kind': kind, 'vertical': vertical, 'collection': collection, 'ref': o.get('ref') or rid,
        'customer': customer, 'phone': phone, 'address': o.get('address'), 'summary': summary,
        'total': o.get('total'), 'currency': ref.get('currency', 'Rs'), 'payment': o.get('payment'),
        # Outbound call speaks the language the patient used inbound (defaults to
        # Sinhala in the bridge if absent on older records).
        'language': o.get('language'),
        'created': datetime.datetime.now().isoformat(timespec='seconds'), 'by': by,
    }
    try:
        OUTBOUND_DIR.mkdir(parents=True, exist_ok=True)
        tmp = OUTBOUND_DIR / (u + '.json.tmp')
        with tmp.open('w') as fh:
            json.dump(ctx, fh, indent=2)
        tmp.replace(OUTBOUND_DIR / (u + '.json'))
    except Exception as e:
        return {'error': 'context write failed: %s' % e, 'status': 500}
    try:
        with AMI() as a:
            r = a.originate(channel='PJSIP/%s@pabx' % dial, context='ai-outbound',
                            exten='s', priority=1, callerid='Naxter <0114794050>',
                            timeout_ms=45000, async_=True, variables={'AI_UUID': u})
        ok = r.get('Response') == 'Success'
    except Exception as e:
        try:
            (OUTBOUND_DIR / (u + '.json')).unlink()
        except Exception:
            pass
        return {'error': 'originate failed: %s' % e, 'status': 502}
    return {'ok': ok, 'uuid': u, 'dialled': dial, 'test_mode': bool(test_num), 'kind': kind, 'response': r}


def _place_outbound_call(vertical, collection, rid, kind):
    """HTTP wrapper around _originate_outbound for a dashboard-initiated (manual) call:
    enforces the per-user dashboard guard + 'call' permission, then maps the core
    result dict to a JSON response."""
    g = _dash_guard(vertical, collection)
    if g:
        return g
    role = AUTH['users'].get(session['user'], {}).get('role', 'viewer')
    if 'call' not in ROLE_PERMS.get(role, set()):
        return jsonify({'error': 'forbidden — needs call permission'}), 403
    res = _originate_outbound(vertical, collection, rid, kind, by=session.get('user') or 'staff')
    status = res.pop('status', None)
    if 'error' in res:
        return jsonify(res), status or 400
    return jsonify(res)


@app.route('/api/dash/<vertical>/orders/<rid>/confirm-call', methods=['POST'])
@login_required
def api_dash_confirm_call(vertical, rid):
    return _place_outbound_call(vertical, 'orders', rid, 'order_confirm')


@app.route('/api/dash/<vertical>/orders/<rid>/ship-call', methods=['POST'])
@login_required
def api_dash_ship_call(vertical, rid):
    """Manual: AI-call the buyer to tell them their (shipped) order is on the way."""
    return _place_outbound_call(vertical, 'orders', rid, 'order_shipped')


@app.route('/api/dash/hospital/appointments/<rid>/call', methods=['POST'])
@login_required
def api_hosp_appt_call(rid):
    return _place_outbound_call('hospital', 'appointments', rid, 'appt_confirm')


@app.route('/api/dash/hospital/labs/<rid>/call', methods=['POST'])
@login_required
def api_hosp_lab_call(rid):
    # Validate BEFORE the pre-read below joins rid into a path. The sibling
    # routes already do this; this one read <rid>.json first, which let a
    # crafted rid probe for arbitrary .json files (one bit: exists + truthy
    # 'critical'). _originate_outbound re-checks, but that was too late.
    if '/' in rid or '..' in rid:
        return jsonify({'error': 'bad id'}), 400
    kind = 'lab_ready'
    try:
        if json.load((_coll_dir('hospital', 'labs') / f'{rid}.json').open()).get('critical'):
            kind = 'lab_critical'
    except Exception:
        pass
    return _place_outbound_call('hospital', 'labs', rid, kind)


@app.route('/api/dash/hospital/labs/<rid>/lab-action', methods=['POST'])
@login_required
def api_lab_action(rid):
    """LIS state machine + result entry. Actions: collect (assigns accession),
    receive, process, result (server computes flags + critical from the catalogue),
    verify (only from resulted), deliver, reject."""
    g = _dash_guard('hospital', 'labs')
    if g:
        return g
    # This route writes lab results and drives the LIS state machine, so it needs
    # the same write gate as the generic record mutator. Without it a *viewer*
    # with the hospital dashboard could enter results and mark specimens verified.
    if not _may_write():
        return jsonify({'error': 'forbidden'}), 403
    if '/' in rid or '..' in rid:
        return jsonify({'error': 'bad id'}), 400
    f = _coll_dir('hospital', 'labs') / f'{rid}.json'
    if not f.exists():
        return jsonify({'error': 'not found'}), 404
    try:
        o = json.load(f.open())
    except Exception:
        return jsonify({'error': 'unreadable'}), 500
    j = request.get_json(force=True) or {}
    action = j.get('action')
    now = datetime.datetime.now().isoformat(timespec='seconds')
    user = session.get('user')
    if action == 'collect':
        o['status'] = 'collected'; o.setdefault('accession', _gen_accession()); o['collected_at'] = now; o['collected_by'] = user
    elif action == 'receive':
        o['status'] = 'received'; o['received_at'] = now
    elif action == 'process':
        o['status'] = 'in_process'
    elif action == 'result':
        results, critical = _compute_lab_results(o.get('test_code'), j.get('results') or [])
        o['results'] = results; o['critical'] = critical; o['status'] = 'resulted'; o['resulted_at'] = now; o['resulted_by'] = user
    elif action == 'verify':
        if o.get('status') != 'resulted':
            return jsonify({'error': 'lab must be resulted before it can be verified'}), 400
        o['status'] = 'verified'; o['verified_at'] = now; o['verified_by'] = user
    elif action == 'deliver':
        o['status'] = 'delivered'; o['delivered_at'] = now; o.setdefault('delivered_via', 'Counter')
    elif action == 'reject':
        o['status'] = 'rejected'; o['reject_reason'] = j.get('reason', ''); o['rejected_at'] = now
    else:
        return jsonify({'error': 'unknown action'}), 400
    o['updated'] = now; o['updated_by'] = user
    _write_record('hospital', 'labs', o)
    return jsonify({'ok': True, 'record': o})


# ==================== Agent mode (which vertical the voice agent answers as) ====================
def _active_flow_id():
    try:
        return json.load(ACTIVE_FLOW_FILE.open()).get('active_id')
    except Exception:
        return None


@app.route('/agent-mode')
@login_required
@perm_required('admin')
@feature_required('agent_mode')
def agent_mode_page():
    return render_template('agent_mode.html', nav='agent-mode')


@app.route('/api/agent-mode')
@login_required
@perm_required('admin')
@feature_required('agent_mode')
def api_agent_mode_get():
    active = _active_flow_id()
    modes = [{**m, 'available': (FLOWS_DIR_PATH / (m['flow'] + '.json')).exists(),
              'active': m['flow'] == active} for m in AGENT_MODES]
    return jsonify({'active_flow': active, 'modes': modes})


@app.route('/api/agent-mode', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('agent_mode')
def api_agent_mode_set():
    j = request.get_json(force=True) or {}
    m = next((x for x in AGENT_MODES if x['key'] == j.get('mode')), None)
    if not m:
        return jsonify({'error': 'unknown mode'}), 400
    if not (FLOWS_DIR_PATH / (m['flow'] + '.json')).exists():
        return jsonify({'error': "flow '%s' is not installed yet" % m['flow']}), 400
    try:
        payload = {'active_id': m['flow'],
                   'activated_at': datetime.datetime.now().isoformat(timespec='seconds'),
                   'activated_by': session.get('user')}
        tmp = ACTIVE_FLOW_FILE.with_suffix('.json.tmp')
        with tmp.open('w') as f:
            json.dump(payload, f, indent=2)
        tmp.replace(ACTIVE_FLOW_FILE)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'mode': m['key'], 'active_flow': m['flow']})


# ==================== Feedback / NPS survey (dial-pad, no AI) ====================
# The [nps-survey] dialplan context plays a recorded Sinhala prompt and collects a
# 0-10 keypad score with NO Gemini involvement. When INBOUND_MODE_FILE contains
# "survey", inbound DID calls divert there instead of the AI agent; otherwise the
# AI agent answers as before. Responses land as JSON files in SURVEY_RESULTS_DIR
# (written by /usr/local/bin/nps-record.sh from the dialplan).
def _inbound_mode():
    """'survey' if inbound calls should hit the dial-pad survey, else 'agent'."""
    try:
        return 'survey' if INBOUND_MODE_FILE.read_text().strip().lower() == 'survey' else 'agent'
    except Exception:
        return 'agent'


def _read_survey_results(limit=1000):
    rows = []
    if SURVEY_RESULTS_DIR.exists():
        files = sorted(SURVEY_RESULTS_DIR.glob('*.json'), reverse=True)[:limit]
        for p in files:
            try:
                d = json.load(p.open())
            except Exception:
                continue
            d['_file'] = p.name
            rows.append(d)
    return rows


def _survey_summary(rows):
    scores = [int(r['score']) for r in rows
              if str(r.get('score', '')).strip().lstrip('-').isdigit() and 0 <= int(r['score']) <= 10]
    n = len(scores)
    promoters = sum(1 for s in scores if s >= 9)
    passives = sum(1 for s in scores if 7 <= s <= 8)
    detractors = sum(1 for s in scores if s <= 6)
    return {
        'responses': n,
        'avg': round(sum(scores) / n, 2) if n else None,
        'nps': round(100 * (promoters - detractors) / n, 1) if n else None,
        'promoters': promoters, 'passives': passives, 'detractors': detractors,
        'distribution': {str(i): sum(1 for s in scores if s == i) for i in range(11)},
    }


@app.route('/survey')
@login_required
@perm_required('admin')
@feature_required('survey')
def survey_page():
    return render_template('survey.html', nav='survey')


@app.route('/api/survey/mode')
@login_required
@perm_required('admin')
@feature_required('survey')
def api_survey_mode_get():
    return jsonify({'mode': _inbound_mode(), 'survey_id': SURVEY_ID})


@app.route('/api/survey/mode', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('survey')
def api_survey_mode_set():
    j = request.get_json(force=True) or {}
    mode = 'survey' if j.get('mode') == 'survey' else 'agent'
    try:
        SAMPATH_DATA.mkdir(parents=True, exist_ok=True)
        tmp = INBOUND_MODE_FILE.with_suffix('.tmp')
        tmp.write_text(mode)   # no trailing newline: dialplan SHELL() compares the raw bytes
        tmp.replace(INBOUND_MODE_FILE)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'mode': mode})


@app.route('/api/survey/results')
@login_required
@perm_required('admin')
@feature_required('survey')
def api_survey_results():
    rows = _read_survey_results()
    return jsonify({
        'survey_id': SURVEY_ID,
        'mode': _inbound_mode(),
        'summary': _survey_summary(rows),
        'responses': rows,
    })


@app.route('/api/survey/testcall', methods=['POST'])
@login_required
@perm_required('call')
@feature_required('survey')
def api_survey_testcall():
    """Originate a single outbound survey call (test): dials the number straight
    into the [nps-survey] context. Marked direction=outbound in the record."""
    j = request.get_json(force=True) or {}
    to = re.sub(r'[^0-9+]', '', j.get('to', '') or '')
    if not to or len(to) < 4 or len(to) > 18:
        return jsonify({'error': 'invalid TO number'}), 400
    # Same caller-ID identity the proven outbound path (_originate_outbound) uses:
    # the DID number, which the upstream PABX accepts (anonymous callers are dropped).
    callerid = 'Sonant <0114794050>'
    chan = f'PJSIP/{to}@pabx'
    try:
        with AMI() as a:
            r = a.originate_wait(
                channel=chan, context=SURVEY_CONTEXT,
                exten='s', priority=1, callerid=callerid,
                timeout_ms=45000, variables={'__SURVEY_DIR': 'outbound'},
            )
    except Exception as e:
        return jsonify({'error': 'AMI error: %s' % e, 'channel': chan}), 502
    if not r['accepted']:
        return jsonify({'error': 'AMI did not accept the Originate', 'channel': chan}), 502
    ev = r.get('result')
    if ev is None:
        # Accepted but no OriginateResponse within the wait — usually still ringing.
        return jsonify({'ok': True, 'queued': True, 'channel': chan,
                        'detail': 'Call placed (still ringing after 15s / no result yet). '
                                  'Check the phone; if it never rings, the trunk/PABX is likely the issue.'})
    ok = ev.get('Response') == 'Success'
    reason = str(ev.get('Reason', ''))
    hint = {'0': 'could not create the outbound channel (PJSIP endpoint / route)',
            '1': 'hangup / rejected by the network',
            '3': 'no answer / timeout',
            '4': 'answered',
            '5': 'busy',
            '8': 'congestion — channel unavailable at the trunk'}.get(reason, '')
    detail = ('Answered — survey is playing on the phone.' if ok
              else 'Call failed (Asterisk reason code %s%s).' % (reason, ': ' + hint if hint else ''))
    return jsonify({'ok': ok, 'queued': False, 'reason': reason, 'hint': hint,
                    'detail': detail, 'channel': chan, 'event': ev})


# ==================== API — overview/data ====================
@app.route('/api/overview')
@login_required
def api_overview():
    cdr = read_cdr(300)
    today = datetime.date.today().isoformat()
    today_calls = [c for c in cdr if c['start'].startswith(today)]
    answered = [c for c in today_calls if c['disposition'] == 'ANSWERED']
    return jsonify({
        'time': datetime.datetime.now().isoformat(timespec='seconds'),
        'trunk': trunk_health(),
        'services': services_status(),
        'system': system_info(),
        'endpoints': endpoints_parsed(),
        'asterisk_uptime': asterisk_cli('core show uptime').strip(),
        'channels_text': asterisk_cli('core show channels').strip(),
        'today': {
            'total': len(today_calls), 'answered': len(answered),
            'success_rate': round(100*len(answered)/max(1,len(today_calls)),1),
        },
        'recent_calls': cdr[:10],
    })

@app.route('/api/charts/today')
@login_required
def api_chart_today():
    cdr = read_cdr(500)
    today = datetime.date.today().isoformat()
    by_hour = defaultdict(lambda: {'total': 0, 'answered': 0, 'duration': 0})
    for c in cdr:
        if not c['start'].startswith(today): continue
        try:
            hour = int(c['start'][11:13])
        except Exception: continue
        by_hour[hour]['total'] += 1
        by_hour[hour]['duration'] += c['billsec']
        if c['disposition'] == 'ANSWERED':
            by_hour[hour]['answered'] += 1
    return jsonify({
        'labels': [f'{h:02d}:00' for h in range(24)],
        'total':    [by_hour[h]['total']    for h in range(24)],
        'answered': [by_hour[h]['answered'] for h in range(24)],
        'duration_min': [round(by_hour[h]['duration']/60, 1) for h in range(24)],
    })

@app.route('/api/charts/status-breakdown')
@login_required
def api_chart_status():
    cdr = read_cdr(500)
    by_status = defaultdict(int)
    for c in cdr: by_status[c['disposition']] += 1
    return jsonify({'labels': list(by_status.keys()), 'data': list(by_status.values())})

@app.route('/api/cdr')
@login_required
def api_cdr():
    return jsonify(read_cdr(int(request.args.get('limit', 200))))

@app.route('/api/channels')
@login_required
def api_channels():
    return jsonify({'text': asterisk_cli('core show channels verbose')})

@app.route('/api/endpoints/full')
@login_required
def api_endpoints_full():
    return jsonify({
        'endpoints': asterisk_cli('pjsip show endpoints'),
        'contacts': asterisk_cli('pjsip show contacts'),
        'aors': asterisk_cli('pjsip show aors'),
        'identifies': asterisk_cli('pjsip show identifies'),
    })

@app.route('/api/trunk')
@login_required
@feature_required('trunk_recovery')
def api_trunk():
    return jsonify({
        'health': trunk_health(),
        'sip_tail': tcpdump_tail('host 10.10.10.89 and port 5060', 60),
        'rtp_tail': tcpdump_tail('host 10.10.10.89 and not port 5060', 30),
    })

@app.route('/api/log')
@login_required
def api_log():
    if not LOG_PATH.exists(): return jsonify({'text': '(no log file yet)'})
    try:
        n = int(request.args.get('lines', 200))
        with LOG_PATH.open() as f: return jsonify({'text': ''.join(f.readlines()[-n:])})
    except Exception as e: return jsonify({'text': f'error: {e}'})


# ==================== API — config ====================
@app.route('/api/config/<name>', methods=['GET', 'POST'])
@login_required
@perm_required('config')
def api_config(name):
    # BOTH verbs need 'config'. The check used to be POST-only, so any logged-in
    # viewer could GET /api/config/pjsip and read the trunk secrets and endpoint
    # passwords verbatim — while the /pjsip and /dialplan PAGES were already
    # config-gated. That was a UI-vs-API gap, not an intended read grant.
    # On FreePBX these resolve to extensions_custom.conf / pjsip_custom.conf.
    # The generated extensions.conf / pjsip.conf are rewritten by every
    # `fwconsole reload`, so editing them here would lose the change and could
    # break the dialplan in between. See the site-config block at the top.
    files = {'extensions': EXTENSIONS_CONF, 'pjsip': PJSIP_CONF}
    if name not in files: abort(404)
    path = files[name]
    if request.method == 'POST':
        new = request.json.get('content', '')
        if len(new) < 50: return jsonify({'error': 'content too short'}), 400
        backup = path.with_name(path.name + '.bak-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
        existed = path.exists()
        try:
            # A *_custom.conf include that does not exist yet is not an error —
            # it just has nothing to back up. When it DOES exist the copy must
            # still succeed before we overwrite it, hence check=existed.
            if existed:
                subprocess.run(['cp', str(path), str(backup)], check=True, timeout=5)
            path.write_text(new)
            return jsonify({'ok': True, 'backup': backup.name if existed else None})
        except Exception as e: return jsonify({'error': str(e)}), 500
    try: return Response(path.read_text(), mimetype='text/plain')
    except FileNotFoundError:
        return Response('; %s does not exist yet — saving this page creates it.\n' % path,
                        mimetype='text/plain')
    except PermissionError: return Response('(permission denied)', mimetype='text/plain')


# ==================== API — actions ====================
@app.route('/api/reload/<what>', methods=['POST'])
@login_required
@perm_required('config')
def api_reload(what):
    cmds = {'dialplan': 'dialplan reload', 'pjsip': 'module reload res_pjsip.so', 'all': 'core reload'}
    if what not in cmds: abort(404)
    return jsonify({'output': asterisk_cli(cmds[what])})

@app.route('/api/restart-asterisk', methods=['POST'])
@login_required
@perm_required('admin')
def api_restart_asterisk():
    try:
        r = subprocess.run(['sudo', '-n', 'systemctl', 'restart', 'asterisk'],
                           capture_output=True, text=True, timeout=20)
        return jsonify({'output': (r.stdout + r.stderr) or 'OK', 'rc': r.returncode})
    except Exception as e: return jsonify({'output': f'error: {e}'})

@app.route('/api/hangup-all', methods=['POST'])
@login_required
@perm_required('admin')   # drops EVERY channel, including live AI calls — bigger
                          # than the rest of 'call' (which places a single call)
def api_hangup_all():
    return jsonify({'output': asterisk_cli('channel request hangup all')})


# ==================== API — make call ====================
@app.route('/api/make-call', methods=['POST'])
@login_required
@perm_required('call')
def api_make_call():
    j = request.get_json(force=True)
    to = re.sub(r'[^0-9+]', '', j.get('to', ''))
    cid_from = re.sub(r'[^0-9+]', '', j.get('from', ''))
    action = j.get('action', 'playback')
    timeout = min(int(j.get('timeout', 30)), 90)
    manager = re.sub(r'[^0-9+]', '', j.get('manager_number', '') or '0779190005')
    greeting_text = (j.get('greeting_text') or '').strip()
    connecting_text = (j.get('connecting_text') or '').strip()
    voice = j.get('voice') or (AWS_CFG.get('default_voice', 'Matthew') if AWS_CFG else 'Matthew')
    engine = j.get('engine') or (AWS_CFG.get('default_engine', 'neural') if AWS_CFG else 'neural')

    valid = {
        'playback':    'outbound-on-answer-playback',
        'echo':        'outbound-on-answer-echo',
        'hello':       'outbound-on-answer-hello',
        'softphone':   'outbound-on-answer-softphone',
        'ivr_forward': 'outbound-on-answer-ivr-forward',
        'play_custom': 'outbound-on-answer-play-custom',
    }
    if not to or len(to) < 4 or len(to) > 18:
        return jsonify({'error': 'invalid TO number'}), 400
    if action not in valid:
        return jsonify({'error': 'invalid action'}), 400
    if action == 'ivr_forward' and (not manager or len(manager) < 4):
        return jsonify({'error': 'invalid manager_number'}), 400

    callerid = f'"{cid_from or "0114794050"}" <{cid_from or "0114794050"}>'
    variables = {}
    tts_info = {}

    # Use double-underscore prefix on Variable: so values inherit to spawned channels.
    # Without __, the variable is set on the originating dialing channel only and
    # PJSIP can lose it when transitioning to the answered channel that runs Context/Exten.
    if action == 'play_custom':
        audio_file = (j.get('audio_file') or '').strip()
        broadcast_text = (j.get('broadcast_text') or '').strip()
        if broadcast_text:
            try:
                a = tts_synthesize(broadcast_text, voice, engine)
                variables['__AUDIO_FILE'] = a['name']
                tts_info['broadcast'] = a
            except Exception as e:
                return jsonify({'error': f'broadcast TTS failed: {e}'}), 400
        elif audio_file:
            safe = re.sub(r'[^A-Za-z0-9_/.-]', '', audio_file)
            sounds_dirs = ['/var/lib/asterisk/sounds/', '/usr/share/asterisk/sounds/']
            found = False
            for d in sounds_dirs:
                for ext in ('wav', 'ulaw', 'alaw', 'gsm', ''):
                    f = Path(d) / (f"{safe}.{ext}" if ext else safe)
                    if f.exists():
                        found = True; break
                if found: break
            if not found:
                return jsonify({'error': f'audio_file not found: {safe}'}), 400
            variables['__AUDIO_FILE'] = safe
        else:
            return jsonify({'error': 'provide either audio_file or broadcast_text'}), 400

    if action == 'ivr_forward':
        variables['__MANAGER_NUMBER'] = manager
        if greeting_text:
            try:
                g = tts_synthesize(greeting_text, voice, engine)
                variables['__GREETING_FILE'] = g['name']
                tts_info['greeting'] = g
            except Exception as e:
                return jsonify({'error': f'greeting TTS failed: {e}'}), 400
        if connecting_text:
            try:
                c = tts_synthesize(connecting_text, voice, engine)
                variables['__CONNECTING_FILE'] = c['name']
                tts_info['connecting'] = c
            except Exception as e:
                return jsonify({'error': f'connecting TTS failed: {e}'}), 400

    # Dial PJSIP directly — no Local channel layer. Simpler bridge, fixes one-way audio.
    try:
        with AMI() as a:
            r = a.originate(
                channel=f'PJSIP/{to}@pabx',
                context=valid[action],
                exten='s', priority=1, callerid=callerid,
                timeout_ms=timeout * 1000, async_=True,
                variables=variables or None,
            )
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({
        'ok': r.get('Response') == 'Success',
        'response': r,
        'channel': f'PJSIP/{to}@pabx',
        'callerid': callerid,
        'manager_number': manager if action == 'ivr_forward' else None,
        'tts': tts_info,
    })


@app.route('/api/tts/preview', methods=['POST'])
@login_required
@perm_required('call')
@feature_required('broadcast')
def api_tts_preview():
    """Synthesize text → return JSON with the audio URL for in-browser playback."""
    j = request.get_json(force=True)
    text = j.get('text', '')
    voice = j.get('voice') or (AWS_CFG.get('default_voice', 'Matthew') if AWS_CFG else 'Matthew')
    engine = j.get('engine') or (AWS_CFG.get('default_engine', 'neural') if AWS_CFG else 'neural')
    try:
        info = tts_synthesize(text, voice, engine)
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(info)


@app.route('/api/tts/audio/<h>')
@login_required
@perm_required('call')
@feature_required('broadcast')
def api_tts_audio(h):
    """Serve a cached TTS WAV file by hash."""
    safe = re.sub(r'[^0-9a-f]', '', h)[:16]
    p = TTS_CACHE / f"{safe}.wav"
    if not p.exists():
        abort(404)
    return send_file(str(p), mimetype='audio/wav')


@app.route('/api/tts/voices')
@login_required
@perm_required('call')
@feature_required('broadcast')
def api_tts_voices():
    """Curated list of high-quality Polly voices (neural)."""
    return jsonify([
        {'id': 'Matthew',  'gender': 'Male',   'language': 'en-US', 'engine': 'neural'},
        {'id': 'Joanna',   'gender': 'Female', 'language': 'en-US', 'engine': 'neural'},
        {'id': 'Stephen',  'gender': 'Male',   'language': 'en-US', 'engine': 'neural'},
        {'id': 'Ruth',     'gender': 'Female', 'language': 'en-US', 'engine': 'neural'},
        {'id': 'Brian',    'gender': 'Male',   'language': 'en-GB', 'engine': 'neural'},
        {'id': 'Amy',      'gender': 'Female', 'language': 'en-GB', 'engine': 'neural'},
        {'id': 'Olivia',   'gender': 'Female', 'language': 'en-AU', 'engine': 'neural'},
        {'id': 'Kajal',    'gender': 'Female', 'language': 'en-IN', 'engine': 'neural'},
    ])


# ==================== API — sounds ====================
@app.route('/api/sounds')
@login_required
def api_sounds():
    out = []
    for d in SOUNDS_DIRS:
        if d.exists():
            for f in sorted(d.iterdir()):
                if f.is_file():
                    out.append({'name': f.name, 'dir': str(d),
                                'size': f.stat().st_size,
                                'mtime': datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')})
    return jsonify(out)

@app.route('/api/sounds/upload', methods=['POST'])
@login_required
@perm_required('call')
def api_sound_upload():
    """Upload an arbitrary audio file. User-provided name (optional).
    Converted to 8kHz mono PCM + ulaw + alaw and installed in both sounds dirs.
    Returns the Asterisk Playback() name for immediate use.
    """
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'no filename'}), 400

    # Friendly name → sanitized basename, no extension
    raw_name = (request.form.get('name') or '').strip()
    if not raw_name:
        raw_name = Path(f.filename).stem
    base = re.sub(r'[^A-Za-z0-9_-]+', '-', raw_name).strip('-').lower()[:64]
    if not base:
        return jsonify({'error': 'name produces empty result after sanitisation'}), 400

    # Use distinct names for INPUT (original suffix preserved) and OUTPUT (different basename)
    # to avoid sox reading + writing the same file when upload is already a .wav.
    ts = int(time.time())
    tmp_in = Path('/tmp') / f'upload-in-{ts}-{secure_filename(f.filename)}'
    f.save(tmp_in)
    out_wav  = Path('/tmp') / f'upload-out-{ts}.wav'
    out_ulaw = Path('/tmp') / f'upload-out-{ts}.ulaw'
    out_alaw = Path('/tmp') / f'upload-out-{ts}.alaw'
    try:
        subprocess.run(['sox', str(tmp_in), '-r','8000','-c','1','-b','16','-e','signed-integer', str(out_wav)],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(['sox', str(out_wav), '-r','8000','-c','1','-t','ul', str(out_ulaw)],
                       check=True, capture_output=True, timeout=20)
        subprocess.run(['sox', str(out_wav), '-r','8000','-c','1','-t','al', str(out_alaw)],
                       check=True, capture_output=True, timeout=20)
        for src, ext in ((out_wav, 'wav'), (out_ulaw, 'ulaw'), (out_alaw, 'alaw')):
            for d in SOUNDS_DIRS:
                d.mkdir(parents=True, exist_ok=True)
                dest = d / f'{base}.{ext}'
                dest.write_bytes(src.read_bytes())
        for p in (tmp_in, out_wav, out_ulaw, out_alaw):
            try: p.unlink()
            except Exception: pass
    except subprocess.CalledProcessError as e:
        for p in (tmp_in, out_wav, out_ulaw, out_alaw):
            try: p.unlink()
            except Exception: pass
        return jsonify({'error': f'audio conversion failed: {e.stderr.decode()[:200] if e.stderr else e}'}), 400

    final_wav = SOUNDS_DIRS[0] / f'{base}.wav'
    return jsonify({
        'ok': True,
        'name': base,                    # human-friendly name
        'play_name': f'custom/{base}',   # what to pass to Asterisk Playback()
        'filename': f'{base}.wav',
        'size': final_wav.stat().st_size if final_wav.exists() else 0,
    })


@app.route('/api/sounds/upload-greeting', methods=['POST'])
@login_required
@perm_required('config')
def api_upload_greeting():
    if 'file' not in request.files: return jsonify({'error': 'no file'}), 400
    f = request.files['file']
    if not f.filename: return jsonify({'error': 'no filename'}), 400
    ts = int(time.time())
    tmp_in = Path('/tmp') / f'greet-in-{ts}-{secure_filename(f.filename)}'
    f.save(tmp_in)
    out_wav = Path('/tmp') / f'greet-out-{ts}.wav'
    out_u   = Path('/tmp') / f'greet-out-{ts}.ulaw'
    out_a   = Path('/tmp') / f'greet-out-{ts}.alaw'
    try:
        subprocess.run(['sox', str(tmp_in), '-r','8000','-c','1','-b','16','-e','signed-integer', str(out_wav)],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(['sox', str(out_wav), '-r','8000','-c','1','-t','ul', str(out_u)],
                       check=True, capture_output=True, timeout=20)
        subprocess.run(['sox', str(out_wav), '-r','8000','-c','1','-t','al', str(out_a)],
                       check=True, capture_output=True, timeout=20)
        for src, ext in ((out_wav, 'wav'), (out_u, 'ulaw'), (out_a, 'alaw')):
            for d in SOUNDS_DIRS:
                d.mkdir(parents=True, exist_ok=True)
                (d / f'{GREETING_NAME}.{ext}').write_bytes(src.read_bytes())
        for p in (tmp_in, out_wav, out_u, out_a):
            try: p.unlink()
            except Exception: pass
        return jsonify({'ok': True, 'name': GREETING_NAME})
    except subprocess.CalledProcessError as e:
        for p in (tmp_in, out_wav, out_u, out_a):
            try: p.unlink()
            except Exception: pass
        return jsonify({'error': f'audio conversion failed: {e.stderr.decode() if e.stderr else e}'}), 400

@app.route('/api/sounds/download/<name>')
@login_required
def api_sound_download(name):
    safe = secure_filename(name)
    for d in SOUNDS_DIRS:
        p = d / safe
        if p.exists(): return send_file(str(p), as_attachment=False)
    abort(404)


# ==================== API — voicemail ====================
@app.route('/api/voicemail')
@login_required
def api_vm_list():
    return jsonify(list_vm_boxes())

# ==================== RECORDINGS API ====================
def parse_recording_meta(name):
    """Parse our recording filename: YYYY-MM-DD_HH-MM-SS_<from>_<to>_<uniqueid>.wav"""
    m = re.match(r'^(\d{4}-\d\d-\d\d)_(\d\d-\d\d-\d\d)_([^_]*)_([^_]*)_(.+)\.wav$', name)
    if m:
        return {
            'date': m.group(1),
            'time': m.group(2).replace('-', ':'),
            'from': m.group(3) or '-',
            'to':   m.group(4) or '-',
            'uniqueid': m.group(5),
        }
    return {'date': '-', 'time': '-', 'from': '-', 'to': '-', 'uniqueid': name}

@app.route('/api/recordings')
@login_required
def api_recordings():
    if not RECORDINGS_DIR.exists():
        return jsonify([])
    out = []
    for f in sorted(RECORDINGS_DIR.glob('*.wav'), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = parse_recording_meta(f.name)
        sz = f.stat().st_size
        # WAV at 8kHz mono 16-bit = 16000 bytes/sec; subtract 44 bytes header
        dur = max(0, (sz - 44) / 16000)
        meta.update({
            'name': f.name,
            'size': sz,
            'duration_s': round(dur, 1),
            'mtime': datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
        })
        out.append(meta)
    return jsonify(out)

@app.route('/api/recordings/<name>/audio')
@login_required
def api_recording_audio(name):
    safe = secure_filename(name)
    p = RECORDINGS_DIR / safe
    if not p.exists() or not safe.endswith('.wav'):
        abort(404)
    return send_file(str(p), mimetype='audio/wav', as_attachment=False)

@app.route('/api/recordings/<name>/download')
@login_required
def api_recording_download(name):
    safe = secure_filename(name)
    p = RECORDINGS_DIR / safe
    if not p.exists() or not safe.endswith('.wav'):
        abort(404)
    return send_file(str(p), mimetype='audio/wav', as_attachment=True, download_name=safe)

@app.route('/api/recordings/<name>', methods=['DELETE'])
@login_required
@perm_required('admin')
def api_recording_delete(name):
    safe = secure_filename(name)
    p = RECORDINGS_DIR / safe
    if not p.exists() or not safe.endswith('.wav'):
        abort(404)
    p.unlink()
    return jsonify({'ok': True})

@app.route('/api/voicemail/<box>/<folder>/<num>/audio')
@login_required
def api_vm_audio(box, folder, num):
    safe_box = re.sub(r'[^0-9]', '', box)
    safe_folder = re.sub(r'[^A-Za-z]', '', folder)
    safe_num = re.sub(r'[^0-9]', '', num)
    wav = VM_ROOT / safe_box / safe_folder / f'msg{safe_num.zfill(4)}.wav'
    if not wav.exists(): abort(404)
    return send_file(str(wav), mimetype='audio/wav')

@app.route('/api/voicemail/<box>/<folder>/<num>', methods=['DELETE'])
@login_required
@perm_required('call')
def api_vm_delete(box, folder, num):
    safe_box = re.sub(r'[^0-9]', '', box)
    safe_folder = re.sub(r'[^A-Za-z]', '', folder)
    safe_num = re.sub(r'[^0-9]', '', num)
    folder_path = VM_ROOT / safe_box / safe_folder
    if not folder_path.exists(): abort(404)
    deleted = 0
    for ext in ('wav', 'wav49', 'gsm', 'txt'):
        f = folder_path / f'msg{safe_num.zfill(4)}.{ext}'
        if f.exists():
            try:
                f.unlink()
                deleted += 1
            except Exception: pass
    return jsonify({'ok': True, 'deleted': deleted})


# ==================== API — users ====================
@app.route('/api/users', methods=['GET', 'POST'])
@login_required
@perm_required('admin')
def api_users():
    if request.method == 'POST':
        j = request.get_json(force=True)
        email = j.get('email', '').strip().lower()
        pw = j.get('password', '')
        role = j.get('role', 'viewer')
        if not email or '@' not in email: return jsonify({'error': 'invalid email'}), 400
        if len(pw) < 8: return jsonify({'error': 'password too short'}), 400
        if role not in ROLES: return jsonify({'error': 'invalid role'}), 400
        dashboards = [d for d in (j.get('dashboards') or []) if d in DASHBOARDS]
        AUTH['users'][email] = {
            'password_hash': generate_password_hash(pw, method='pbkdf2:sha256', salt_length=16),
            'role': role,
            'dashboards': dashboards,
            'created': datetime.datetime.now().isoformat(timespec='seconds'),
        }
        save_auth()
        return jsonify({'ok': True})
    return jsonify([{'email': e, 'role': u['role'], 'created': u.get('created','-'),
                     'dashboards': u.get('dashboards', [])}
                    for e, u in AUTH['users'].items()])

@app.route('/api/users/<email>', methods=['DELETE', 'PATCH'])
@login_required
@perm_required('admin')
def api_user_modify(email):
    email = email.lower()
    if email == session['user']:
        return jsonify({'error': 'cannot modify self'}), 400
    if email not in AUTH['users']:
        return jsonify({'error': 'not found'}), 404
    if request.method == 'DELETE':
        del AUTH['users'][email]
        save_auth()
        return jsonify({'ok': True})
    if request.method == 'PATCH':
        j = request.get_json(force=True)
        if 'role' in j and j['role'] in ROLES:
            AUTH['users'][email]['role'] = j['role']
        if 'password' in j and len(j['password']) >= 8:
            AUTH['users'][email]['password_hash'] = generate_password_hash(j['password'], method='pbkdf2:sha256', salt_length=16)
        if 'dashboards' in j and isinstance(j['dashboards'], list):
            AUTH['users'][email]['dashboards'] = [d for d in j['dashboards'] if d in DASHBOARDS]
        save_auth()
        return jsonify({'ok': True})


@app.route('/api/settings/password', methods=['POST'])
@login_required
def api_change_password():
    j = request.json
    old, new = j.get('old', ''), j.get('new', '')
    user = session['user']
    if not check_password_hash(AUTH['users'][user]['password_hash'], old):
        return jsonify({'error': 'old password incorrect'}), 401
    if len(new) < 8: return jsonify({'error': 'new password too short (8+ chars)'}), 400
    AUTH['users'][user]['password_hash'] = generate_password_hash(new, method='pbkdf2:sha256', salt_length=16)
    save_auth()
    return jsonify({'ok': True})


# ==================== API — webphone (returns config for browser SIP) ====================
@app.route('/api/webphone-config')
@login_required
@perm_required('call')
@feature_required('webphone')
def api_webphone_config():
    host = request.host.split(':')[0]
    # Prefer wss:// if the page itself was loaded over HTTPS; mixed-content blocked otherwise
    scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
    ws_scheme = 'wss' if scheme == 'https' else 'ws'
    # Softphone credentials come from instance/webphone.json, NOT from source.
    # They used to be a literal here, which meant the SIP password was readable
    # by anyone with 'call' (and sat in every backup and diff of this file).
    # Anyone holding it can register a softphone straight against Asterisk and
    # place trunk calls outside this app's roles, logging and recording — so
    # rotate it in pjsip.conf and here, and give operators their own extensions.
    wp = _webphone_cfg()
    sip_realm = wp.get('realm', '192.168.1.132')
    return jsonify({
        'extension': wp.get('extension', '1010'),
        'password': wp.get('password', ''),
        'sip_uri': f"sip:{wp.get('extension', '1010')}@{sip_realm}",
        'realm': sip_realm,
        'ws_urls': [
            {'url': f'{ws_scheme}://{host}/ws',
             'label': f'Cloudflare Tunnel ({host})',
             'note': 'Works from anywhere — nginx routes /ws to Asterisk via the same hostname.'},
            {'url': 'ws://i7-4790.taild13621.ts.net:8088/ws',
             'label': 'Tailscale (direct)',
             'note': 'Only works if your device is signed into your tailnet.'},
            {'url': 'ws://192.168.1.132:8088/ws',
             'label': 'LAN (192.168.1.132)',
             'note': 'Only works from devices on the local network.'},
        ],
    })


# ==================== AI AGENT (Samali) ====================
# All paths are absolute; the bridge process and Flask both read these files.

# Derived from site["agent"]; the defaults are the home bridge's own paths, so a
# box with no site.json lands on exactly the constants that used to be literals.
AI_CONFIG_PATH = _site_path(_SITE_AGENT, 'config_file', AGENT_INSTALL_DIR / 'agent-config.json')
AI_SESSIONS_DIR = _site_path(_SITE_AGENT, 'sessions_dir', AGENT_DATA_DIR / 'sessions')
AI_CUSTOMERS_DIR = _site_path(_SITE_AGENT, 'customers_dir', AGENT_DATA_DIR / 'customers')
AI_VOICE_SAMPLE_DIR = AGENT_DATA_DIR / 'voice-samples'
AI_VOICE_SAMPLE_SCRIPT = str(_site_path(_SITE_AGENT, 'voice_sample_script',
                                        AGENT_INSTALL_DIR / 'voice-sample.cjs'))

# Standard Gemini Live prebuilt voices as of May 2026 — verified working.
AI_AVAILABLE_VOICES = [
    'Zephyr', 'Kore', 'Aoede', 'Leda', 'Callirrhoe', 'Autonoe',
    'Despina', 'Erinome', 'Laomedeia', 'Achernar', 'Gacrux',
    'Pulcherrima', 'Vindemiatrix', 'Sulafat',
]

AI_DEFAULT_CONFIG = {
    'model': 'gemini-3.1-flash-live-preview',
    'voice': 'Aoede',
    'greeting_trigger': 'The customer has just connected. Please greet them now.',
    'retry_greeting_trigger': 'The customer was brought back to you because the manager was unavailable. Apologise warmly and offer to help.',
    'manager_number': '0779190005',
    'test_mode': True,  # Locks escalation dial target to 0779190005 in the dialplan
    'escalation_timeout_sec': 60,
    'hold_music_class': 'default',
    'escalation_announcement': 'Please hold on a moment while I connect you to our support team.',
    'system_prompt': '',
    'custom_instructions': '',
    'voices': AI_AVAILABLE_VOICES,
}

# Hardcoded test-mode escalation target — also enforced by the dialplan.
AI_TEST_MODE_TARGET = '0779190005'


def ai_load_config():
    try:
        with AI_CONFIG_PATH.open() as f:
            data = json.load(f)
        return {**AI_DEFAULT_CONFIG, **data}
    except FileNotFoundError:
        return AI_DEFAULT_CONFIG.copy()
    except Exception as e:
        return {**AI_DEFAULT_CONFIG, '_load_error': str(e)}


def ai_save_config(data):
    AI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Always preserve the immutable voices list and merge with current.
    current = ai_load_config()
    merged = {**current, **data, 'voices': AI_AVAILABLE_VOICES}
    tmp = AI_CONFIG_PATH.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    tmp.chmod(0o644)
    tmp.replace(AI_CONFIG_PATH)
    return merged


def ai_iter_session_files():
    if not AI_SESSIONS_DIR.exists():
        return []
    files = sorted(
        AI_SESSIONS_DIR.glob('*.jsonl'),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files


# Romanized Sinhala markers — Gemini Live often transcribes spoken Sinhala as
# Latin text ("puluwanda", "karanna", "kohomada"), which a pure-script check
# miscounts as English. These tokens almost never occur in genuine English, so
# their presence marks a turn as Sinhala. Mirrors ROMANIZED_SI in bridge.ts so
# the dashboard's per-call mark matches the language the agent actually used.
_ROMANIZED_SI = re.compile(
    r'\b(puluwan\w*|karann\w*|thiyen\w*|tiyen\w*|kohomada|monaw\w*|kaw?uda|oyal\w*|'
    r'mage|kenek|tikak|naadda|sinhalen|singlen|sinhala|denna|gann\w*|balann\w*|kiyann\w*)\b',
    re.I)


def _utterance_lang(text):
    """Language of one transcript turn (mirrors detectUtteranceLang in bridge.ts):
    Sinhala script is decisive, then Tamil script, then romanized-Sinhala markers,
    else English."""
    has_si = has_ta = False
    for ch in (text or ''):
        o = ord(ch)
        if 0x0D80 <= o <= 0x0DFF:
            has_si = True
        elif 0x0B80 <= o <= 0x0BFF:
            has_ta = True
    if has_si:
        return 'si'
    if has_ta:
        return 'ta'
    return 'si' if _ROMANIZED_SI.search(text or '') else 'en'


def _detect_call_language(user_turns, all_turns):
    """Best-effort language a call was conducted in, from its transcripts. Tallies
    each turn's language (so loanword-padded Sinhala still reads as Sinhala) and
    picks the call language with the same rule the agent uses for outbound
    follow-ups (callLanguage in bridge.ts): ANY real Sinhala/Tamil presence beats
    a plurality of (noisy) English turns; a call is English only if no turn was
    Sinhala or Tamil. Prefers the CALLER's (user) turns and falls back to the
    whole transcript. Returns 'si' / 'ta' / 'en' / None (no transcript)."""
    def tally(turns):
        c = {'si': 0, 'ta': 0, 'en': 0}
        for t in turns or []:
            if t and t.strip():
                c[_utterance_lang(t)] += 1
        return c
    c = tally(user_turns)
    if not any(c.values()):
        c = tally(all_turns)
    if c['si'] > 0 and c['si'] >= c['ta']:
        return 'si'
    if c['ta'] > 0:
        return 'ta'
    return 'en' if c['en'] > 0 else None


def ai_summarise_session(jsonl_path):
    """Read a JSONL session log and produce a summary dict suitable for the list view."""
    out = {
        'id': jsonl_path.stem,
        'started_at': None,
        'ended_at': None,
        'duration_s': None,
        'channel': None,
        'mode': 'primary',
        'turns': 0,
        'extracted': {},
        'escalated': False,
        'ended_via_tool': None,
        'recording': None,
        'last_user_text': '',
        'last_agent_text': '',
        'caller_num': None,
        'language': None,
    }
    _user_text = []
    _all_text = []
    try:
        with jsonl_path.open() as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                ts = ev.get('ts')
                if not out['started_at'] and ts:
                    out['started_at'] = ts
                if ts:
                    out['ended_at'] = ts
                t = ev.get('type')
                if t == 'session_open':
                    out['mode'] = ev.get('mode', 'primary')
                    out['channel'] = ev.get('channel')
                elif t == 'transcript':
                    out['turns'] += 1
                    _txt = ev.get('text', '') or ''
                    _all_text.append(_txt)
                    if ev.get('role') == 'user':
                        out['last_user_text'] = _txt
                        _user_text.append(_txt)
                    elif ev.get('role') == 'agent':
                        out['last_agent_text'] = _txt
                elif t == 'extracted':
                    out['extracted'][ev.get('field', '?')] = ev.get('value', '')
                elif t == 'escalation_requested':
                    out['escalated'] = True
                elif t == 'end_call_requested':
                    out['ended_via_tool'] = 'end_call'
                elif t == 'tool_call':
                    if ev.get('name') == 'end_call':
                        out['ended_via_tool'] = 'end_call'
    except PermissionError:
        return out
    # Try to find the matching MixMonitor recording (filename ends with _<uuid>.wav)
    if RECORDINGS_DIR.exists():
        suffix = f"_{out['id']}.wav"
        for rec in RECORDINGS_DIR.glob('*.wav'):
            if rec.name.endswith(suffix):
                out['recording'] = rec.name
                # derive caller number from filename: <date>_<time>_<from>_<to>...
                parts = rec.name.split('_')
                if len(parts) >= 3:
                    out['caller_num'] = parts[2] or None
                break
    # Compute duration
    try:
        if out['started_at'] and out['ended_at']:
            s = datetime.datetime.fromisoformat(out['started_at'].replace('Z', '+00:00'))
            e = datetime.datetime.fromisoformat(out['ended_at'].replace('Z', '+00:00'))
            out['duration_s'] = round((e - s).total_seconds(), 1)
    except Exception:
        pass
    out['language'] = _detect_call_language(_user_text, _all_text)
    return out


_TERMINAL_EVENT_TYPES = {'session_close', 'gemini_closed', 'ami_hangup'}

def ai_session_is_active(jsonl_path, summary=None):
    """Active = the JSONL has no terminal event AND was modified recently."""
    try:
        age = time.time() - jsonl_path.stat().st_mtime
        # File untouched for 90s with no terminal event → consider dead anyway
        # (bridge crashed mid-call, or this is the activity gap before hangup).
        if age > 90:
            return False
        # Read the LAST KILOBYTE and scan all events in it for terminal types
        with jsonl_path.open('rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode(errors='replace').splitlines()
            for line in reversed(tail):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get('type') in _TERMINAL_EVENT_TYPES:
                    return False
                # First parseable non-terminal line means session is mid-flight
                return True
        return False
    except Exception:
        return False


@app.route('/ai-agent')
@login_required
@perm_required('config')
@feature_required('ai_agent')
def ai_agent_page():
    return render_template('ai_agent.html', nav='ai-agent')


@app.route('/api/ai-agent/config', methods=['GET', 'PUT'])
@login_required
@perm_required('config')   # GET exposes the full system prompt + manager number;
                           # the /ai-agent PAGE was already config-gated.
@feature_required('ai_agent')
def api_ai_config():
    if request.method == 'PUT':
        if 'config' not in ROLE_PERMS.get(AUTH['users'].get(session.get('user'), {}).get('role'), set()):
            return jsonify({'error': 'forbidden'}), 403
        j = request.get_json(force=True)
        current = ai_load_config()
        in_test_mode = bool(current.get('test_mode', True))
        # Validate
        if 'voice' in j and j['voice'] not in AI_AVAILABLE_VOICES:
            return jsonify({'error': f'unknown voice: {j["voice"]}'}), 400
        if 'manager_number' in j:
            mgr = re.sub(r'[^0-9+]', '', j.get('manager_number', ''))
            if len(mgr) < 4:
                return jsonify({'error': 'manager_number too short'}), 400
            # While test_mode is true the configured manager_number is ignored
            # by the dialplan anyway, but we forbid edits to it so the admin
            # UI can't quietly drift away from the hardcoded test target.
            if in_test_mode and mgr != AI_TEST_MODE_TARGET:
                return jsonify({
                    'error': f'test_mode is ON — manager_number is locked to {AI_TEST_MODE_TARGET}. '
                             f'Disable test_mode first if you intend to route to a different number.',
                }), 400
            j['manager_number'] = mgr
        if 'escalation_timeout_sec' in j:
            try:
                t = int(j['escalation_timeout_sec'])
                if t < 5 or t > 300:
                    return jsonify({'error': 'escalation_timeout_sec must be 5-300'}), 400
                j['escalation_timeout_sec'] = t
            except (ValueError, TypeError):
                return jsonify({'error': 'invalid escalation_timeout_sec'}), 400
        if 'test_mode' in j:
            j['test_mode'] = bool(j['test_mode'])
        merged = ai_save_config(j)
        # If turning test_mode ON, also snap manager_number back to the safe target
        if merged.get('test_mode') and merged.get('manager_number') != AI_TEST_MODE_TARGET:
            ai_save_config({'manager_number': AI_TEST_MODE_TARGET})
            merged = ai_load_config()
        return jsonify({'ok': True, 'config': merged})
    return jsonify(ai_load_config())


@app.route('/api/ai-agent/voices')
@login_required
@feature_required('ai_agent')
def api_ai_voices():
    return jsonify(AI_AVAILABLE_VOICES)


@app.route('/api/ai-agent/voice-test', methods=['POST'])
@login_required
@perm_required('config')
@feature_required('ai_agent')
def api_ai_voice_test():
    """Generate a WAV sample of `text` in `voice` via Gemini Live."""
    j = request.get_json(force=True)
    voice = j.get('voice', 'Aoede')
    text = (j.get('text', '') or '').strip()
    if voice not in AI_AVAILABLE_VOICES:
        return jsonify({'error': f'unknown voice: {voice}'}), 400
    if not text:
        return jsonify({'error': 'text required'}), 400
    if len(text) > 500:
        return jsonify({'error': 'text too long (max 500 chars)'}), 400

    AI_VOICE_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(f"{voice}|{text}".encode()).hexdigest()[:16]
    out_path = AI_VOICE_SAMPLE_DIR / f"{h}.wav"

    if not out_path.exists():
        try:
            r = subprocess.run(
                ['node', AI_VOICE_SAMPLE_SCRIPT, voice, text, str(out_path)],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode != 0:
                return jsonify({
                    'error': 'voice-sample failed',
                    'detail': (r.stderr or r.stdout)[-500:],
                }), 500
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'voice-sample timed out'}), 504
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return jsonify({
        'ok': True,
        'voice': voice,
        'hash': h,
        'url': f'/api/ai-agent/voice-test/{h}',
        'size': out_path.stat().st_size,
    })


@app.route('/api/ai-agent/voice-test/<h>')
@login_required
@feature_required('ai_agent')
def api_ai_voice_test_audio(h):
    safe = re.sub(r'[^0-9a-f]', '', h)[:16]
    p = AI_VOICE_SAMPLE_DIR / f"{safe}.wav"
    if not p.exists():
        abort(404)
    return send_file(str(p), mimetype='audio/wav')


@app.route('/api/ai-agent/sessions')
@login_required
@feature_required('ai_sessions')
def api_ai_sessions():
    """List sessions, newest first."""
    limit = int(request.args.get('limit', 50))
    out = []
    for f in ai_iter_session_files()[:limit]:
        s = ai_summarise_session(f)
        s['active'] = ai_session_is_active(f, s)
        out.append(s)
    return jsonify(out)


@app.route('/api/ai-agent/sessions/<sid>')
@login_required
@feature_required('ai_sessions')
def api_ai_session_detail(sid):
    safe = re.sub(r'[^0-9a-f-]', '', sid)[:36]
    f = AI_SESSIONS_DIR / f"{safe}.jsonl"
    if not f.exists():
        abort(404)
    events = []
    try:
        with f.open() as fp:
            for line in fp:
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except PermissionError:
        return jsonify({'error': 'permission denied'}), 403
    summary = ai_summarise_session(f)
    summary['active'] = ai_session_is_active(f, summary)
    return jsonify({'summary': summary, 'events': events})


@app.route('/api/ai-agent/sessions/<sid>/stream')
@login_required
@feature_required('ai_sessions')
def api_ai_session_stream(sid):
    """SSE stream of events for a single session (tail -f), with Last-Event-ID
    support so that browser auto-reconnects don't re-deliver events the client
    has already seen. Each event's id is the byte offset of the START of the
    next line — sending that back as Last-Event-ID lets us resume cleanly."""
    safe = re.sub(r'[^0-9a-f-]', '', sid)[:36]
    f = AI_SESSIONS_DIR / f"{safe}.jsonl"
    if not f.exists():
        abort(404)

    # Browser sends Last-Event-ID on reconnect. Treat as start offset.
    try:
        start_pos = int(request.headers.get('Last-Event-ID', '0'))
    except (TypeError, ValueError):
        start_pos = 0

    def gen():
        # Replay from start_pos to current EOF
        try:
            with f.open('rb') as fp:
                fp.seek(start_pos)
                while True:
                    line = fp.readline()
                    if not line:
                        break
                    pos = fp.tell()
                    text = line.decode(errors='replace').strip()
                    if text:
                        yield f"id: {pos}\ndata: {text}\n\n"
                tail_pos = fp.tell()
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
            return

        # Then tail for new content. Keep alive much longer than before (15 min)
        # but reset the deadline whenever anything new arrives.
        deadline_idle_sec = 900
        last_event = time.time()
        with f.open('rb') as fp:
            fp.seek(tail_pos)
            while time.time() - last_event < deadline_idle_sec:
                line = fp.readline()
                if line:
                    last_event = time.time()
                    tail_pos = fp.tell()
                    text = line.decode(errors='replace').strip()
                    if text:
                        yield f"id: {tail_pos}\ndata: {text}\n\n"
                else:
                    time.sleep(1.5)
                    yield ": keepalive\n\n"

    return Response(gen(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
    })


@app.route('/api/ai-agent/sessions/<sid>/recording')
@login_required
@feature_required('ai_sessions')
def api_ai_session_recording(sid):
    safe = re.sub(r'[^0-9a-f-]', '', sid)[:36]
    if not RECORDINGS_DIR.exists():
        abort(404)
    suffix = f"_{safe}.wav"
    for rec in RECORDINGS_DIR.glob('*.wav'):
        if rec.name.endswith(suffix):
            return send_file(str(rec), mimetype='audio/wav')
    abort(404)


@app.route('/api/ai-agent/customers')
@login_required
@feature_required('ai_sessions')
def api_ai_customers():
    if not AI_CUSTOMERS_DIR.exists():
        return jsonify([])
    out = []
    for f in sorted(AI_CUSTOMERS_DIR.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with f.open() as fp:
                data = json.load(fp)
            out.append({
                'key': f.stem,
                'fields': data.get('fields', {}),
                'last_updated': data.get('last_updated', ''),
            })
        except Exception:
            continue
    return jsonify(out)


@app.route('/api/ai-agent/restart-bridge', methods=['POST'])
@login_required
@perm_required('config')
@feature_required('ai_agent')
def api_ai_restart_bridge():
    try:
        r = subprocess.run(
            ['sudo', '-n', 'systemctl', 'restart', AGENT_SERVICE],
            capture_output=True, text=True, timeout=15,
        )
        return jsonify({'output': (r.stdout + r.stderr) or 'OK', 'rc': r.returncode})
    except Exception as e:
        return jsonify({'output': f'error: {e}'})


# ==================== SOFT RECOVERY + DIAGNOSTICS ====================
SNAPSHOTS_ROOT = Path('/var/log/pbx-monitor/snapshots')
SNAPSHOT_SH = '/opt/pbx-monitor/snapshot.sh'
DIALOG_TRUNK_NAME = 'NAXTER3029'


def _run(cmd, timeout=20):
    """Run a shell command list; return (rc, combined_output)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f'timeout after {timeout}s'
    except Exception as e:
        return 1, f'error: {e}'


def _detect_sip_module():
    """Return 'pjsip' or 'chan_sip' depending on which Asterisk module is loaded."""
    out = asterisk_cli('module show like chan_pjsip', timeout=4)
    if 'chan_pjsip.so' in out and 'Running' in out:
        return 'pjsip'
    out = asterisk_cli('module show like chan_sip', timeout=4)
    if 'chan_sip.so' in out and 'Running' in out:
        return 'chan_sip'
    return 'pjsip'  # default — modern Asterisk


@app.route('/api/soft-recover', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('trunk_recovery')
def api_soft_recover():
    """Recover the Dialog trunk without rebooting the server.

    Order: snapshot -> route -> bridge -> qualify -> recheck -> (reload asterisk).
    Each step's output is returned so the operator can see which step fixed it.
    """
    steps = []

    rc, out = _run(['sudo', '-n', SNAPSHOT_SH, 'soft-recover'], timeout=30)
    snap_dir = out.splitlines()[-1] if rc == 0 and out else ''
    steps.append({'step': 'snapshot', 'rc': rc, 'output': out, 'snapshot_dir': snap_dir})

    rc, out = _run(['sudo', '-n', 'systemctl', 'restart', 'sip-trunk-route'], timeout=15)
    steps.append({'step': 'sip-trunk-route', 'rc': rc, 'output': out or 'OK'})

    rc, out = _run(['sudo', '-n', 'systemctl', 'restart', AGENT_SERVICE], timeout=15)
    steps.append({'step': AGENT_SERVICE, 'rc': rc, 'output': out or 'OK'})

    mod = _detect_sip_module()
    if mod == 'pjsip':
        qcmd = f'pjsip qualify aor {DIALOG_TRUNK_NAME}'
    else:
        qcmd = f'sip qualify peer {DIALOG_TRUNK_NAME}'
    qout = asterisk_cli(qcmd, timeout=8)
    steps.append({'step': f'asterisk-qualify ({mod})', 'rc': 0, 'output': qout or 'OK'})

    time.sleep(5)
    h = trunk_health()
    steps.append({'step': 'health-after-5s', 'rc': 0 if h['alive'] else 1,
                  'output': json.dumps(h)})
    if not h['alive']:
        rc, out = _run(['sudo', '-n', 'systemctl', 'reload', 'asterisk'], timeout=20)
        steps.append({'step': 'asterisk-reload', 'rc': rc, 'output': out or 'OK'})
        time.sleep(3)
        h = trunk_health()
        steps.append({'step': 'health-after-reload', 'rc': 0 if h['alive'] else 1,
                      'output': json.dumps(h)})

    return jsonify({'ok': h['alive'], 'final_health': h, 'steps': steps})


@app.route('/api/snapshots')
@login_required
@perm_required('admin')
@feature_required('trunk_recovery')
def api_snapshots_list():
    """List the last 20 incident snapshots."""
    if not SNAPSHOTS_ROOT.exists():
        return jsonify([])
    dirs = sorted(
        (d for d in SNAPSHOTS_ROOT.iterdir() if d.is_dir()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:20]
    out = []
    for d in dirs:
        try:
            files = sorted(f.name for f in d.iterdir() if f.is_file())
        except Exception:
            files = []
        out.append({
            'name': d.name,
            'mtime': datetime.datetime.fromtimestamp(d.stat().st_mtime).isoformat(timespec='seconds'),
            'files': files,
        })
    return jsonify(out)


@app.route('/api/snapshots/<name>/<path:fname>')
@login_required
@perm_required('admin')
@feature_required('trunk_recovery')
def api_snapshot_file(name, fname):
    """Serve an individual file from a snapshot directory.

    Resolved paths are confined to SNAPSHOTS_ROOT to block traversal.
    """
    try:
        root = SNAPSHOTS_ROOT.resolve()
        target = (SNAPSHOTS_ROOT / name / fname).resolve()
        target.relative_to(root)
    except Exception:
        abort(404)
    if not target.is_file():
        abort(404)
    mt = 'application/vnd.tcpdump.pcap' if fname.endswith('.pcap') else 'text/plain'
    return send_file(str(target), mimetype=mt, as_attachment=fname.endswith('.pcap'))


# ==================== /flows — multi-agent voice configurations ====================
#
# Data model lives at /var/lib/sampath-ai/flows/*.json with /active-flow.json
# as the pointer to whichever flow is currently live. See migrate-flows.py for
# the canonical schema and seed values. The bridge (Node.js) reads these files
# directly on every new call — switching flows requires no service restart.

FLOWS_DIR = AGENT_DATA_DIR / 'flows'
ACTIVE_FLOW_POINTER = AGENT_DATA_DIR / 'active-flow.json'

FLOW_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,62}$')
PHONE_RE = re.compile(r'^[0-9+]{4,18}$')
SAMPATH_ENV_PATH = _site_path(_SITE_AGENT, 'env_file', AGENT_INSTALL_DIR / '.env')


def _read_env_var(name):
    """Read NAME from the voice agent's .env (site agent.env_file); fall back to process env."""
    try:
        for line in SAMPATH_ENV_PATH.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith('#') or '=' not in s: continue
            k, v = s.split('=', 1)
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return os.environ.get(name, '')


def _read_gemini_key():
    return _read_env_var('GEMINI_API_KEY')


def _gemini_model_name():
    """Resolve effective model: env > .env > module default."""
    return _read_env_var('GEMINI_GEN_MODEL') or GEMINI_GEN_MODEL

# Available tools — must stay in sync with TOOL_REGISTRY in /opt/sampath-ai/src/lib/gemini-live.ts.
# This catalog drives the Tools tab of the editor.
FLOW_TOOL_CATALOG = [
    {
        'id': 'save_customer_info',
        'name': 'Save customer info',
        'description': 'Agent writes facts about the caller (name, NIC, complaint, etc.) into a structured record.',
        'config_schema': {
            'fields': {
                'type': 'string-list',
                'label': 'Recommended fields',
                'help': 'snake_case field keys you want the agent to use (one per line).',
                'default': ['name', 'phone', 'email'],
            },
        },
    },
    {
        'id': 'request_human_transfer',
        'name': 'Transfer to human',
        'description': 'Agent escalates the call. Bridge picks the manager number from this flow\'s transfer_rules based on the chosen category.',
        'config_schema': {},
    },
    {
        'id': 'end_call',
        'name': 'End call',
        'description': 'Agent hangs up gracefully after saying goodbye. (No config.)',
        'config_schema': {},
    },
    {
        'id': 'find_sampath_branch',
        'name': 'Find Sampath branch',
        'description': 'Sampath Bank only — look up branch by area/town/name from the bank\'s live database.',
        'config_schema': {},
    },
    {
        'id': 'get_exchange_rates',
        'name': 'Get exchange rates',
        'description': 'Sampath Bank only — fetch live forex rates.',
        'config_schema': {},
    },
]

# Generated flows tracked as "draft" until saved with `is_preset=False`.
FLOW_DEFAULT_TEMPLATE = {
    'voice': 'Aoede',
    'model': 'gemini-3.1-flash-live-preview',
    'language_hint': 'en',
    'greeting_trigger': 'The customer has just connected. Greet them and ask how you can help.',
    'retry_greeting_trigger': 'The customer was just brought back to you because the manager was unavailable. Apologise warmly and offer to help instead.',
    'test_mode': True,
    'test_mode_number': '0779190005',
    'escalation_timeout_sec': 60,
    'transfer_rules': [{'category': 'default', 'manager_number': '0779190005', 'description': 'Default escalation target'}],
    'tools_enabled': ['save_customer_info', 'request_human_transfer', 'end_call'],
    'tools_config': {},
    'system_prompt': 'You are a helpful voice assistant. Greet the caller and ask how you can help.',
    'custom_instructions': '',
    'working_hours': {
        'enabled': False,
        'timezone': 'Asia/Colombo',
        'schedule': {'0': '', '1': '09:00-17:00', '2': '09:00-17:00', '3': '09:00-17:00',
                     '4': '09:00-17:00', '5': '09:00-17:00', '6': ''},
        'out_of_hours_action': 'greet',
        'out_of_hours_greeting': '',
        'out_of_hours_transfer_category': 'default',
        'out_of_hours_hangup_message': '',
    },
    'record_calls': False,
    'flow': {
        'nodes': [
            {'id': 'start', 'type': 'start', 'position': {'x': 50, 'y': 200},
             'data': {'label': 'Start', 'greeting_text': 'Greet caller and ask how to help.'}},
            {'id': 'end', 'type': 'end', 'position': {'x': 400, 'y': 200},
             'data': {'label': 'End call', 'farewell_text': 'Thanks for calling.'}},
        ],
        'edges': [{'id': 'e1', 'source': 'start', 'target': 'end'}],
        'viewport': {'x': 0, 'y': 0, 'zoom': 1},
    },
}


def _flow_path(flow_id: str) -> Path:
    if not FLOW_ID_RE.match(flow_id):
        abort(400, description='invalid flow id')
    return FLOWS_DIR / f'{flow_id}.json'


def _read_active_id():
    try:
        with ACTIVE_FLOW_POINTER.open() as f:
            return json.load(f).get('active_id')
    except Exception:
        return None


def _flows_list():
    if not FLOWS_DIR.exists():
        return []
    active = _read_active_id()
    out = []
    for p in sorted(FLOWS_DIR.glob('*.json')):
        try:
            with p.open() as f:
                data = json.load(f)
            out.append({
                'id': data.get('id') or p.stem,
                'name': data.get('name') or p.stem,
                'description': data.get('description', ''),
                'is_preset': bool(data.get('is_preset')),
                'is_active': (data.get('id') or p.stem) == active,
                'voice': data.get('voice'),
                'model': data.get('model'),
                'updated_at': data.get('updated_at', ''),
                'transfer_rules_count': len(data.get('transfer_rules') or []),
                'tools_count': len(data.get('tools_enabled') or []),
            })
        except Exception as e:
            out.append({'id': p.stem, 'name': p.stem, 'error': str(e)})
    return out


def _flow_load(flow_id: str) -> dict:
    p = _flow_path(flow_id)
    if not p.exists():
        abort(404)
    with p.open() as f:
        return json.load(f)


def _flow_validate(data: dict) -> list:
    """Return list of error strings; empty list = valid."""
    errors = []
    name = data.get('name', '').strip()
    if not name or len(name) > 80:
        errors.append('name must be 1–80 chars')
    voice = data.get('voice', '')
    if voice not in AI_AVAILABLE_VOICES:
        errors.append(f'voice must be one of {AI_AVAILABLE_VOICES}')
    rules = data.get('transfer_rules') or []
    if not isinstance(rules, list) or not rules:
        errors.append('at least one transfer_rule required')
    else:
        for i, r in enumerate(rules):
            if not isinstance(r, dict):
                errors.append(f'transfer_rules[{i}] must be object')
                continue
            cat = (r.get('category') or '').strip()
            num = (r.get('manager_number') or '').strip()
            if not cat:
                errors.append(f'transfer_rules[{i}].category required')
            if not PHONE_RE.match(num):
                errors.append(f'transfer_rules[{i}].manager_number invalid (digits/+, 4–18 chars)')
        if not any((r.get('category') or '').strip() == 'default' for r in rules):
            errors.append('transfer_rules must include a "default" category')
    test_mode_number = (data.get('test_mode_number') or '').strip()
    if test_mode_number and not PHONE_RE.match(test_mode_number):
        errors.append('test_mode_number invalid')
    tools = data.get('tools_enabled') or []
    if not isinstance(tools, list):
        errors.append('tools_enabled must be array')
    else:
        valid_ids = {t['id'] for t in FLOW_TOOL_CATALOG}
        for t in tools:
            if t not in valid_ids:
                errors.append(f'unknown tool: {t}')
    return errors


def _flow_save(flow_id: str, data: dict, actor: str):
    """Atomic write with .tmp → rename pattern."""
    p = _flow_path(flow_id)
    FLOWS_DIR.mkdir(parents=True, exist_ok=True)
    data['id'] = flow_id
    data['updated_at'] = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
    data['updated_by'] = actor
    if not data.get('created_at'):
        data['created_at'] = data['updated_at']
        data['created_by'] = actor
    # Auto-regenerate the flow-compiled prompt section.
    data['system_prompt'] = _compile_flow_into_prompt(
        data.get('system_prompt', ''), data.get('flow') or {}, data.get('transfer_rules') or []
    )
    tmp = p.with_suffix('.json.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.chmod(0o640)
    tmp.replace(p)


def _compile_flow_into_prompt(current_prompt: str, flow: dict, transfer_rules: list) -> str:
    """
    Strip any prior 'FROM FLOW' generated section, then re-append a fresh one
    based on the current graph. Users who edit system_prompt manually keep their
    edits; only the FROM-FLOW block is rewritten.
    """
    MARK = '\n\n--- GENERATED FROM FLOW (do not edit by hand; regenerated on save) ---\n'
    head = current_prompt.split(MARK)[0].rstrip()
    nodes = {n['id']: n for n in (flow.get('nodes') or [])}
    edges = flow.get('edges') or []
    if not nodes:
        return head
    lines = ['', '', '--- GENERATED FROM FLOW (do not edit by hand; regenerated on save) ---', '', 'CONVERSATION FLOW:']
    # Walk edges grouped by source node
    by_source = {}
    for e in edges:
        by_source.setdefault(e.get('source'), []).append(e)
    for nid, node in nodes.items():
        ntype = node.get('type', '')
        d = node.get('data', {}) or {}
        label = d.get('label', nid)
        out_edges = by_source.get(nid, [])
        out_labels = [e.get('label') or nodes.get(e.get('target'), {}).get('data', {}).get('label', e.get('target', '?')) for e in out_edges]
        if ntype == 'start':
            lines.append(f"- START: open the conversation. {d.get('greeting_text', '')}")
        elif ntype == 'intent':
            lines.append(f"- LISTEN for intent ({label}): {d.get('description', '')}")
            if out_labels:
                lines.append(f"  Branches: {', '.join(out_labels)}")
        elif ntype == 'response':
            lines.append(f"- SAY: {d.get('message_text', label)}")
        elif ntype == 'tool':
            lines.append(f"- TOOL: call {d.get('tool_id', '?')} ({d.get('arg_template', '')})")
        elif ntype == 'transfer':
            cat = d.get('category', 'default')
            mgr = next((r['manager_number'] for r in transfer_rules if r.get('category') == cat), '?')
            lines.append(f"- TRANSFER on category '{cat}' (→ {mgr}) via request_human_transfer.")
        elif ntype == 'end':
            lines.append(f"- END CALL: say '{d.get('farewell_text', 'Goodbye')}' then call end_call.")
        else:
            lines.append(f"- {ntype.upper() or 'NODE'}: {label}")
    lines.append('')
    lines.append('Use this flow as a guide — adapt phrasing naturally to the caller.')
    return head + '\n'.join(lines)


# ---------- routes ----------

@app.route('/flows')
@login_required
@perm_required('admin')
@feature_required('flows')
def flows_page():
    return render_template('flows.html', nav='flows')


@app.route('/api/flows')
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flows_list():
    return jsonify({
        'flows': _flows_list(),
        'active_id': _read_active_id(),
    })


@app.route('/api/flows/_tool-catalog')
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flows_tool_catalog():
    return jsonify(FLOW_TOOL_CATALOG)


@app.route('/api/flows/_voices')
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flows_voices():
    return jsonify(AI_AVAILABLE_VOICES)


@app.route('/api/flows/<flow_id>', methods=['GET'])
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flow_get(flow_id):
    return jsonify(_flow_load(flow_id))


@app.route('/api/flows/<flow_id>', methods=['PUT'])
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flow_put(flow_id):
    existing = _flow_load(flow_id)
    if existing.get('is_preset'):
        return jsonify({'error': 'preset flows cannot be edited — clone first'}), 403
    data = request.get_json(force=True) or {}
    # Merge with defaults to forgive partial PUTs
    merged = {**FLOW_DEFAULT_TEMPLATE, **existing, **data}
    merged['is_preset'] = False  # never elevate to preset via API
    merged['id'] = flow_id
    errs = _flow_validate(merged)
    if errs:
        return jsonify({'error': 'validation_failed', 'details': errs}), 400
    _flow_save(flow_id, merged, actor=session.get('user', 'unknown'))
    return jsonify({'ok': True, 'flow': _flow_load(flow_id)})


@app.route('/api/flows', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flow_create():
    body = request.get_json(force=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    flow_id = (body.get('id') or re.sub(r'[^a-z0-9-]', '-', name.lower())).strip('-')[:60]
    if not FLOW_ID_RE.match(flow_id):
        return jsonify({'error': 'invalid id'}), 400
    if _flow_path(flow_id).exists():
        return jsonify({'error': 'id already exists'}), 409
    clone_from = body.get('clone_from')
    if clone_from:
        try:
            base = _flow_load(clone_from)
        except Exception:
            return jsonify({'error': f'clone_from {clone_from} not found'}), 404
        new = {**base, 'id': flow_id, 'name': name, 'description': body.get('description', ''),
               'is_preset': False, 'created_at': '', 'updated_at': ''}
    else:
        new = {**FLOW_DEFAULT_TEMPLATE, 'id': flow_id, 'name': name,
               'description': body.get('description', ''), 'is_preset': False}
    errs = _flow_validate(new)
    if errs:
        return jsonify({'error': 'validation_failed', 'details': errs}), 400
    _flow_save(flow_id, new, actor=session.get('user', 'unknown'))
    return jsonify({'ok': True, 'id': flow_id})


@app.route('/api/flows/<flow_id>/clone', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flow_clone(flow_id):
    base = _flow_load(flow_id)
    body = request.get_json(force=True) or {}
    new_name = (body.get('name') or f"Copy of {base.get('name', flow_id)}").strip()
    return api_flow_create_inner(new_name, base)


def api_flow_create_inner(new_name, base):
    new_id = re.sub(r'[^a-z0-9-]', '-', new_name.lower()).strip('-')[:60]
    # Avoid collisions
    candidate = new_id
    i = 1
    while _flow_path(candidate).exists():
        i += 1
        candidate = f"{new_id}-{i}"
    new = {**base, 'id': candidate, 'name': new_name, 'is_preset': False,
           'created_at': '', 'updated_at': ''}
    _flow_save(candidate, new, actor=session.get('user', 'unknown'))
    return jsonify({'ok': True, 'id': candidate})


@app.route('/api/flows/<flow_id>', methods=['DELETE'])
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flow_delete(flow_id):
    data = _flow_load(flow_id)
    if data.get('is_preset'):
        return jsonify({'error': 'cannot delete preset flows'}), 403
    if _read_active_id() == flow_id:
        return jsonify({'error': 'cannot delete the active flow — activate another first'}), 403
    _flow_path(flow_id).unlink()
    return jsonify({'ok': True})


@app.route('/api/flows/<flow_id>/activate', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flow_activate(flow_id):
    # Confirm exists
    _flow_load(flow_id)
    pointer = {
        'active_id': flow_id,
        'activated_at': datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
        'activated_by': session.get('user', 'unknown'),
    }
    ACTIVE_FLOW_POINTER.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVE_FLOW_POINTER.with_suffix('.json.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(pointer, f, indent=2)
    tmp.chmod(0o644)
    tmp.replace(ACTIVE_FLOW_POINTER)
    return jsonify({'ok': True, 'active_id': flow_id})


# ==================== /customers — live cross-call customer-info table ====================
#
# Aggregates `save_customer_info` events from every session JSONL into a flat
# row-per-event view that the panel can search/filter/sort/export. Source files
# are under /var/lib/sampath-ai/sessions/*.jsonl, written by the bridge in real
# time. We treat session mtime as a watermark so polling clients can ask for
# only what's new since their last fetch.

CUSTOMER_FIELD_PRESET_ORDER = ['name', 'nic', 'phone', 'email', 'complaint',
                                'account_number', 'preferred_branch', 'language']


def _iter_customer_rows(since_iso=None, limit=2000):
    """Walk session JSONLs, extract save_customer_info events as rows.
    `since_iso`: only return rows from sessions modified after this ISO ts.
    """
    if not AI_SESSIONS_DIR.exists(): return []
    cutoff = 0.0
    if since_iso:
        try:
            cutoff = datetime.datetime.fromisoformat(since_iso.replace('Z', '+00:00')).timestamp()
        except Exception:
            cutoff = 0.0
    rows = []
    # Walk newest sessions first so the natural cap returns most recent.
    files = sorted(AI_SESSIONS_DIR.glob('*.jsonl'),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            mt = path.stat().st_mtime
            if cutoff and mt <= cutoff:
                # Whole session unchanged since cutoff — fine to skip entirely.
                continue
            sess_id = path.stem
            caller = None
            channel = None
            flow_id = None
            with path.open() as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    t = ev.get('type')
                    if t == 'session_open':
                        channel = ev.get('channel')
                    elif t == 'caller_id':
                        caller = ev.get('value') or caller
                    elif t == 'flow_active':
                        flow_id = ev.get('flow_id')
                    elif t == 'tool_call' and ev.get('name') == 'save_customer_info':
                        a = ev.get('args') or {}
                        rows.append({
                            'ts': ev.get('ts'),
                            'session_id': sess_id,
                            'channel': channel,
                            'caller': caller,
                            'flow_id': flow_id,
                            'field': str(a.get('field') or ''),
                            'value': str(a.get('value') or ''),
                        })
        except Exception:
            continue
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: r.get('ts') or '', reverse=True)
    return rows[:limit]


@app.route('/customers')
@login_required
@perm_required('read')
@feature_required('ai_sessions')
def customers_page():
    return render_template('customers.html', nav='customers')


@app.route('/api/customers')
@login_required
@perm_required('read')
@feature_required('ai_sessions')
def api_customers():
    since = request.args.get('since')
    q = (request.args.get('q') or '').strip().lower()
    flow_filter = (request.args.get('flow') or '').strip()
    field_filter = (request.args.get('field') or '').strip()
    limit = min(int(request.args.get('limit', 500)), 2000)
    rows = _iter_customer_rows(since_iso=since, limit=limit * 2)  # over-fetch then filter
    if q:
        rows = [r for r in rows if any(q in str(v).lower() for v in r.values())]
    if flow_filter:
        rows = [r for r in rows if r.get('flow_id') == flow_filter]
    if field_filter:
        rows = [r for r in rows if r.get('field') == field_filter]
    rows = rows[:limit]
    fields = sorted({r['field'] for r in rows if r.get('field')})
    flows_seen = sorted({r['flow_id'] for r in rows if r.get('flow_id')})
    return jsonify({
        'rows': rows,
        'count': len(rows),
        'available_fields': fields,
        'available_flows': flows_seen,
        'server_time': datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
    })


def _session_summary(path: Path):
    """Walk one .jsonl file, return a summary dict suitable for the list view."""
    sid = path.stem
    started_at = None
    last_ts = None
    caller = None
    channel = None
    flow_id = None
    captured = {}
    transcript_count = 0
    closed = False
    end_reason = None
    voice = None
    try:
        with path.open() as f:
            for line in f:
                try: ev = json.loads(line)
                except Exception: continue
                t = ev.get('type')
                ts = ev.get('ts')
                if ts: last_ts = ts
                if t == 'session_open':
                    started_at = ts
                    channel = ev.get('channel')
                elif t == 'gemini_ready':
                    voice = ev.get('voice')
                elif t == 'caller_id':
                    caller = ev.get('value') or caller
                elif t == 'flow_active':
                    flow_id = ev.get('flow_id')
                elif t == 'tool_call' and ev.get('name') == 'save_customer_info':
                    a = ev.get('args') or {}
                    fld = str(a.get('field') or '').strip()
                    val = str(a.get('value') or '').strip()
                    if fld:
                        captured[fld] = val
                elif t == 'transcript':
                    transcript_count += 1
                elif t in ('hangup_from_asterisk', 'gemini_closed', 'ami_hangup', 'end_call_requested'):
                    closed = True
                    end_reason = end_reason or ev.get('reason') or t
    except Exception:
        pass

    mtime = path.stat().st_mtime
    age = time.time() - mtime
    is_live = (not closed) and (age < 60)

    # Duration: seconds between started_at and last_ts (best effort)
    duration = None
    try:
        if started_at and last_ts:
            sa = datetime.datetime.fromisoformat(started_at.replace('Z', '+00:00'))
            la = datetime.datetime.fromisoformat(last_ts.replace('Z', '+00:00'))
            duration = max(0, int((la - sa).total_seconds()))
    except Exception:
        pass

    # Recording lookup — naming convention from existing endpoint: *_<sid>.wav
    has_recording = False
    try:
        if RECORDINGS_DIR.exists():
            for rec in RECORDINGS_DIR.glob(f'*_{sid}.wav'):
                has_recording = True
                break
    except Exception:
        pass

    return {
        'id': sid,
        'started_at': started_at,
        'last_event_at': last_ts,
        'duration_sec': duration,
        'caller': caller,
        'channel': channel,
        'flow_id': flow_id,
        'voice': voice,
        'captured': captured,
        'captured_count': len(captured),
        'transcript_count': transcript_count,
        'has_recording': has_recording,
        'is_live': is_live,
        'closed': closed,
        'end_reason': end_reason,
    }


@app.route('/api/sessions')
@login_required
@perm_required('read')
@feature_required('ai_sessions')
def api_sessions():
    q = (request.args.get('q') or '').strip().lower()
    flow_filter = (request.args.get('flow') or '').strip()
    live_only = request.args.get('live_only') == '1'
    limit = min(int(request.args.get('limit', 100)), 500)
    if not AI_SESSIONS_DIR.exists():
        return jsonify({'sessions': [], 'server_time': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z'})

    files = sorted(AI_SESSIONS_DIR.glob('*.jsonl'),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit * 3]
    out = []
    flows_seen = set()
    for p in files:
        s = _session_summary(p)
        if s.get('flow_id'): flows_seen.add(s['flow_id'])
        if live_only and not s['is_live']: continue
        if flow_filter and s.get('flow_id') != flow_filter: continue
        if q:
            hay = ' '.join([str(s.get('caller') or ''), str(s.get('flow_id') or ''),
                            ' '.join(f'{k}={v}' for k, v in (s.get('captured') or {}).items()),
                            s.get('id', '')]).lower()
            if q not in hay: continue
        out.append(s)
        if len(out) >= limit: break
    return jsonify({
        'sessions': out,
        'count': len(out),
        'available_flows': sorted(flows_seen),
        'server_time': datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
    })


@app.route('/api/sessions/<sid>')
@login_required
@perm_required('read')
@feature_required('ai_sessions')
def api_session_detail(sid):
    """Wraps api_ai_session_detail with a richer summary (uses _session_summary)
    plus filtered transcript / event lists ready for the UI."""
    safe = re.sub(r'[^0-9a-f-]', '', sid)[:36]
    f = AI_SESSIONS_DIR / f"{safe}.jsonl"
    if not f.exists(): abort(404)
    summary = _session_summary(f)
    events = []
    transcripts = []
    tool_calls = []
    try:
        with f.open() as fp:
            for line in fp:
                try: ev = json.loads(line)
                except Exception: continue
                events.append(ev)
                if ev.get('type') == 'transcript':
                    transcripts.append({
                        'ts': ev.get('ts'),
                        'role': ev.get('role'),
                        'text': ev.get('text', ''),
                    })
                elif ev.get('type') == 'tool_call':
                    tool_calls.append({
                        'ts': ev.get('ts'),
                        'name': ev.get('name'),
                        'args': ev.get('args') or {},
                    })
    except PermissionError:
        return jsonify({'error': 'permission denied'}), 403
    return jsonify({
        'summary': summary,
        'transcripts': transcripts,
        'tool_calls': tool_calls,
        'events': events,
        'recording_url': f'/api/ai-agent/sessions/{safe}/recording' if summary['has_recording'] else None,
    })


@app.route('/api/customers/export.csv')
@login_required
@perm_required('read')
@feature_required('ai_sessions')
def api_customers_export():
    rows = _iter_customer_rows(limit=10000)
    out = ['ts,session_id,channel,caller,flow_id,field,value']
    for r in rows:
        out.append(','.join(
            '"' + str(r.get(k, '')).replace('"', '""').replace('\n', ' ') + '"'
            for k in ('ts', 'session_id', 'channel', 'caller', 'flow_id', 'field', 'value')
        ))
    csv = '\n'.join(out)
    return Response(csv, mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=customers.csv'})


# ==================== /api/calls — live call list + transcript stream ====================

AI_CHANNELS_DIR = _site_path(_SITE_AGENT, 'channels_dir', AGENT_DATA_DIR / 'channels')


def _list_live_calls():
    """A 'live' call = a session whose JSONL was touched in the last 60 s
    AND which has not logged a closing event."""
    out = []
    if not AI_SESSIONS_DIR.exists(): return out
    now = time.time()
    for p in sorted(AI_SESSIONS_DIR.glob('*.jsonl'),
                    key=lambda x: x.stat().st_mtime, reverse=True)[:30]:
        age = now - p.stat().st_mtime
        if age > 60: break  # files are mtime-sorted, no point continuing
        try:
            opened_at = None
            channel = None
            closed = False
            caller = None
            with p.open() as f:
                for line in f:
                    try: ev = json.loads(line)
                    except Exception: continue
                    t = ev.get('type')
                    if t == 'session_open':
                        opened_at = ev.get('ts')
                        channel = ev.get('channel')
                    elif t in ('hangup_from_asterisk', 'gemini_closed', 'ami_hangup'):
                        closed = True
                    elif t == 'caller_id':
                        caller = ev.get('value')
            if closed: continue
            out.append({
                'id': p.stem,
                'opened_at': opened_at,
                'channel': channel,
                'caller': caller,
                'last_event_age_sec': round(age, 1),
            })
        except Exception: continue
    return out


@app.route('/api/calls/live')
@login_required
@perm_required('read')
@feature_required('ai_sessions')
def api_calls_live():
    return jsonify({'calls': _list_live_calls()})


# Live transcript stream is already provided by /api/ai-agent/sessions/<sid>/stream.
# We expose a thin alias at /api/calls/<id>/stream so the new Live Calls page
# uses a stable path.
@app.route('/api/calls/<sid>/stream')
@login_required
@perm_required('read')
@feature_required('ai_sessions')
def api_calls_stream(sid):
    return api_ai_session_stream(sid)  # re-uses existing SSE implementation


# ==================== /api/flows — playground + AI generation ====================

# gemini-2.5-flash has a free-tier quota; -pro is limit:0 on free tier.
# Override via GEMINI_GEN_MODEL env var or /opt/sampath-ai/.env if you want.
GEMINI_GEN_MODEL = os.environ.get('GEMINI_GEN_MODEL', 'gemini-2.5-flash')


def _gemini_rest(payload: dict, model: str = None, timeout: int = 45):
    if not model:
        model = _gemini_model_name()
    """Call Gemini's generateContent REST endpoint. Returns parsed JSON or raises."""
    import urllib.request
    import urllib.error
    key = _read_gemini_key()
    if not key:
        raise RuntimeError('GEMINI_API_KEY not configured (see /opt/sampath-ai/.env)')
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:500]
        raise RuntimeError(f'gemini HTTP {e.code}: {body}')


FLOW_GENERATE_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'name': {'type': 'STRING'},
        'description': {'type': 'STRING'},
        'voice': {'type': 'STRING'},
        'language_hint': {'type': 'STRING'},
        'greeting_trigger': {'type': 'STRING'},
        'retry_greeting_trigger': {'type': 'STRING'},
        'system_prompt': {'type': 'STRING'},
        'transfer_rules': {
            'type': 'ARRAY',
            'items': {
                'type': 'OBJECT',
                'properties': {
                    'category': {'type': 'STRING'},
                    'manager_number': {'type': 'STRING'},
                    'description': {'type': 'STRING'},
                },
            },
        },
        'tools_enabled': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'flow_nodes': {
            'type': 'ARRAY',
            'items': {
                'type': 'OBJECT',
                'properties': {
                    'id': {'type': 'STRING'},
                    'type': {'type': 'STRING'},
                    'label': {'type': 'STRING'},
                    'detail': {'type': 'STRING'},
                    'x': {'type': 'NUMBER'},
                    'y': {'type': 'NUMBER'},
                },
            },
        },
        'flow_edges': {
            'type': 'ARRAY',
            'items': {
                'type': 'OBJECT',
                'properties': {
                    'source': {'type': 'STRING'},
                    'target': {'type': 'STRING'},
                    'label': {'type': 'STRING'},
                },
            },
        },
    },
}


@app.route('/api/flows/_generate', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flow_generate():
    body = request.get_json(force=True) or {}
    idea = (body.get('idea') or '').strip()
    if not idea or len(idea) > 2000:
        return jsonify({'error': 'idea required (1-2000 chars)'}), 400
    meta = (
        "You design VOICE AI AGENTS for a Sri Lankan telecom platform. The user describes a use case in plain English. "
        "Produce a complete agent configuration. Constraints:\n"
        "- name: short business-y name (<=60 chars)\n"
        "- description: one short sentence (<=200 chars)\n"
        "- voice: choose from: " + ', '.join(AI_AVAILABLE_VOICES) + "\n"
        "- language_hint: BCP-47 like 'en', 'si-LK', 'en-LK'\n"
        "- greeting_trigger: 1-2 sentences instructing the agent how to greet (NOT the greeting itself)\n"
        "- retry_greeting_trigger: same but for when a previous transfer failed and caller is back\n"
        "- system_prompt: 200-600 words covering persona, scope, tone, what info to gather, when to transfer/end\n"
        "- transfer_rules: 2-5 items. MUST include a category='default'. Manager numbers may be PLACEHOLDERS like '0779190001' that the admin will edit\n"
        "- tools_enabled: subset of ['save_customer_info','request_human_transfer','end_call']\n"
        "  (the other catalog tools 'find_sampath_branch' and 'get_exchange_rates' are Sampath-Bank specific; only include if the use case is literally Sampath Bank)\n"
        "- flow_nodes: 8-14 nodes laying out the conversation. types: 'start','intent','response','tool','transfer','end'. exactly ONE 'start' and ONE or more 'end'. x/y positions on a 1000x500 grid; lay them out left-to-right by conversation order.\n"
        "- flow_edges: connect nodes; label edges with the branch condition (e.g. 'wants to book', 'billing question')\n"
        "Output strict JSON conforming to the response schema. No commentary, no markdown.\n\n"
        "User's idea:\n" + idea
    )
    try:
        resp = _gemini_rest({
            'contents': [{'role': 'user', 'parts': [{'text': meta}]}],
            'generationConfig': {
                'responseMimeType': 'application/json',
                'responseSchema': FLOW_GENERATE_SCHEMA,
                'temperature': 0.7,
            },
        })
    except Exception as e:
        # Return 200 with ok:false so Cloudflare doesn't replace the body with
        # its own HTML 5xx page. The client checks `error` regardless of status.
        return jsonify({'ok': False, 'error': str(e)})

    try:
        text = resp['candidates'][0]['content']['parts'][0]['text']
        parsed = json.loads(text)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'unparseable Gemini response: {e}', 'raw': resp})

    # Coerce to the canonical flow JSON shape
    nodes = []
    for n in parsed.get('flow_nodes', []):
        nid = (n.get('id') or '').strip() or f'n-{len(nodes)}'
        ntype = (n.get('type') or 'response').strip()
        data = {'label': n.get('label') or nid}
        if 'detail' in n: data['description'] = n['detail']
        # type-specific data shape
        if ntype == 'start': data['greeting_text'] = n.get('detail') or ''
        elif ntype == 'response': data['message_text'] = n.get('detail') or ''
        elif ntype == 'transfer': data['category'] = n.get('detail') or 'default'
        elif ntype == 'tool': data['tool_id'] = n.get('detail') or 'save_customer_info'
        elif ntype == 'end': data['farewell_text'] = n.get('detail') or 'Thank you, goodbye.'
        nodes.append({'id': nid, 'type': ntype,
                      'position': {'x': float(n.get('x', 100 + 80 * len(nodes))),
                                   'y': float(n.get('y', 200))},
                      'data': data})
    edges = []
    for i, e in enumerate(parsed.get('flow_edges', [])):
        edges.append({'id': f'e{i}',
                      'source': e.get('source'), 'target': e.get('target'),
                      'label': e.get('label') or ''})

    out = {
        'name': parsed.get('name') or 'Generated Agent',
        'description': parsed.get('description') or '',
        'voice': parsed.get('voice') if parsed.get('voice') in AI_AVAILABLE_VOICES else 'Aoede',
        'language_hint': parsed.get('language_hint') or 'en',
        'greeting_trigger': parsed.get('greeting_trigger') or FLOW_DEFAULT_TEMPLATE['greeting_trigger'],
        'retry_greeting_trigger': parsed.get('retry_greeting_trigger') or FLOW_DEFAULT_TEMPLATE['retry_greeting_trigger'],
        'system_prompt': parsed.get('system_prompt') or '',
        'transfer_rules': parsed.get('transfer_rules') or FLOW_DEFAULT_TEMPLATE['transfer_rules'],
        'tools_enabled': parsed.get('tools_enabled') or ['save_customer_info', 'request_human_transfer', 'end_call'],
        'flow': {'nodes': nodes, 'edges': edges, 'viewport': {'x': 0, 'y': 0, 'zoom': 1}},
    }
    return jsonify({'ok': True, 'proposed': out})


FLOW_REGEN_PROMPTS_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'system_prompt':           {'type': 'STRING'},
        'greeting_trigger':        {'type': 'STRING'},
        'retry_greeting_trigger':  {'type': 'STRING'},
        'custom_instructions':     {'type': 'STRING'},
    },
}


@app.route('/api/flows/_gemini-health')
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flows_gemini_health():
    """Diagnostic: is the Gemini key configured and does generateContent reach the API?"""
    key = _read_gemini_key()
    out = {
        'env_path': str(SAMPATH_ENV_PATH),
        'env_readable': False,
        'key_present': bool(key),
        'key_length': len(key) if key else 0,
        'key_prefix': key[:6] + '…' if key else None,
        'model': _gemini_model_name(),
        'test_call_ok': False,
        'test_call_error': None,
        'test_call_response_keys': None,
    }
    try:
        SAMPATH_ENV_PATH.open().close()
        out['env_readable'] = True
    except Exception as e:
        out['test_call_error'] = f'cannot read env: {e}'
        return jsonify(out)
    if not key:
        out['test_call_error'] = 'GEMINI_API_KEY not found in env file or process env'
        return jsonify(out)
    try:
        resp = _gemini_rest({
            'contents': [{'role': 'user', 'parts': [{'text': 'Reply with exactly: pong'}]}],
            'generationConfig': {'temperature': 0.0},
        }, timeout=15)
        out['test_call_ok'] = True
        out['test_call_response_keys'] = list(resp.keys())
        try:
            out['test_call_text'] = resp['candidates'][0]['content']['parts'][0]['text']
        except Exception:
            out['test_call_text'] = '(no text in response — see response_keys)'
    except Exception as e:
        out['test_call_error'] = str(e)
    return jsonify(out)


@app.route('/api/flows/<flow_id>/regenerate-prompts', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flow_regenerate_prompts(flow_id):
    """Generate all prompts for an EXISTING flow from a free-text use case.
    Body: {idea: "..."}  →  {proposed: {system_prompt, greeting_trigger, retry_greeting_trigger, custom_instructions}}
    Caller can preview and apply (the apply step is a normal PUT)."""
    flow = _flow_load(flow_id)
    body = request.get_json(force=True) or {}
    idea = (body.get('idea') or '').strip()
    if not idea or len(idea) > 2000:
        return jsonify({'error': 'idea required (1-2000 chars)'}), 400
    rules_summary = ', '.join(f"{r.get('category')}({r.get('description','')})" for r in (flow.get('transfer_rules') or []))
    tools_summary = ', '.join(flow.get('tools_enabled') or [])
    meta = (
        "You write prompts for a Sri Lankan voice AI agent on a telephone helpdesk platform. "
        "The user describes their use case; you produce four pieces:\n"
        "- system_prompt: 200-600 words covering persona, scope, tone, what info to gather, when to transfer/end. Reference the actual tools available below by name when relevant. Use markdown-ish '## headings' for clarity.\n"
        "- greeting_trigger: 1-2 sentences INSTRUCTING the agent how to greet (NOT the greeting itself — Gemini will speak the greeting based on this instruction).\n"
        "- retry_greeting_trigger: same but for when a previous transfer failed and the caller is back with you.\n"
        "- custom_instructions: 1-3 short overrides for tone/scope that admins commonly tweak. Can be empty.\n\n"
        f"Existing agent name: {flow.get('name', flow_id)}\n"
        f"Voice: {flow.get('voice', 'Aoede')}, language hint: {flow.get('language_hint', 'en')}\n"
        f"Tools the agent CAN call: {tools_summary or '(none)'}\n"
        f"Transfer categories available: {rules_summary or '(only default)'}\n\n"
        "Use case the user wants the agent to handle:\n" + idea + "\n\n"
        "Output strict JSON conforming to the response schema. No markdown fences, no commentary."
    )
    try:
        resp = _gemini_rest({
            'contents': [{'role': 'user', 'parts': [{'text': meta}]}],
            'generationConfig': {
                'responseMimeType': 'application/json',
                'responseSchema': FLOW_REGEN_PROMPTS_SCHEMA,
                'temperature': 0.5,
            },
        })
    except Exception as e:
        # Return 200 with ok:false so Cloudflare doesn't replace the body with
        # its own HTML 5xx page. The client checks `error` regardless of status.
        return jsonify({'ok': False, 'error': str(e)})
    try:
        text = resp['candidates'][0]['content']['parts'][0]['text']
        parsed = json.loads(text)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'unparseable Gemini response: {e}', 'raw': resp})
    return jsonify({
        'ok': True,
        'proposed': {
            'system_prompt':          parsed.get('system_prompt') or '',
            'greeting_trigger':       parsed.get('greeting_trigger') or flow.get('greeting_trigger', ''),
            'retry_greeting_trigger': parsed.get('retry_greeting_trigger') or flow.get('retry_greeting_trigger', ''),
            'custom_instructions':    parsed.get('custom_instructions') or '',
        },
    })


FLOW_REGEN_DIAGRAM_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'flow_nodes': {
            'type': 'ARRAY',
            'items': {'type': 'OBJECT', 'properties': {
                'id': {'type': 'STRING'}, 'type': {'type': 'STRING'},
                'label': {'type': 'STRING'}, 'detail': {'type': 'STRING'},
                'x': {'type': 'NUMBER'}, 'y': {'type': 'NUMBER'},
            }},
        },
        'flow_edges': {
            'type': 'ARRAY',
            'items': {'type': 'OBJECT', 'properties': {
                'source': {'type': 'STRING'}, 'target': {'type': 'STRING'}, 'label': {'type': 'STRING'},
            }},
        },
    },
}


@app.route('/api/flows/<flow_id>/regenerate-flow', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flow_regenerate_diagram(flow_id):
    """Generate just the visual flow diagram (nodes + edges) for an existing flow,
    using its current system_prompt + transfer_rules + tools_enabled as grounding."""
    flow = _flow_load(flow_id)
    body = request.get_json(force=True) or {}
    extra = (body.get('idea') or '').strip()
    rules_summary = ', '.join(f"{r.get('category')}({r.get('description','')})" for r in (flow.get('transfer_rules') or []))
    tools_summary = ', '.join(flow.get('tools_enabled') or [])
    meta = (
        "Design the visual conversation flow for this voice AI agent. "
        "Produce 8-14 nodes and edges that visualize how a call typically goes.\n"
        "Node types: 'start' (exactly one), 'intent', 'response', 'tool', 'transfer', 'end' (one or more).\n"
        "Positions x/y are on a 1100x500 grid; lay out left-to-right by conversation order, branches stacked vertically.\n"
        "Edges connect node IDs by id; LABEL edges with the branch condition (e.g. 'wants booking', 'billing question').\n"
        f"Agent name: {flow.get('name', flow_id)}\n"
        f"Tools available: {tools_summary or '(none)'}\n"
        f"Transfer categories: {rules_summary or '(only default)'}\n"
        f"System prompt context (first 600 chars):\n{(flow.get('system_prompt') or '')[:600]}\n"
        + (f"\nExtra user guidance:\n{extra}\n" if extra else "") +
        "\nOutput strict JSON conforming to the response schema. No commentary."
    )
    try:
        resp = _gemini_rest({
            'contents': [{'role': 'user', 'parts': [{'text': meta}]}],
            'generationConfig': {
                'responseMimeType': 'application/json',
                'responseSchema': FLOW_REGEN_DIAGRAM_SCHEMA,
                'temperature': 0.6,
            },
        })
    except Exception as e:
        # Return 200 with ok:false so Cloudflare doesn't replace the body with
        # its own HTML 5xx page. The client checks `error` regardless of status.
        return jsonify({'ok': False, 'error': str(e)})
    try:
        text = resp['candidates'][0]['content']['parts'][0]['text']
        parsed = json.loads(text)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'unparseable Gemini response: {e}', 'raw': resp})
    nodes = []
    for i, n in enumerate(parsed.get('flow_nodes') or []):
        nid = (n.get('id') or '').strip() or f'n-{i}'
        ntype = (n.get('type') or 'response').strip()
        data = {'label': n.get('label') or nid}
        if 'detail' in n: data['description'] = n['detail']
        if ntype == 'start': data['greeting_text'] = n.get('detail') or ''
        elif ntype == 'response': data['message_text'] = n.get('detail') or ''
        elif ntype == 'transfer': data['category'] = n.get('detail') or 'default'
        elif ntype == 'tool': data['tool_id'] = n.get('detail') or 'save_customer_info'
        elif ntype == 'end': data['farewell_text'] = n.get('detail') or 'Thank you, goodbye.'
        nodes.append({'id': nid, 'type': ntype,
                      'position': {'x': float(n.get('x', 100 + 90 * i)),
                                   'y': float(n.get('y', 240))},
                      'data': data})
    edges = []
    for i, e in enumerate(parsed.get('flow_edges') or []):
        edges.append({'id': f'e{i}',
                      'source': e.get('source'), 'target': e.get('target'),
                      'label': e.get('label') or ''})
    return jsonify({
        'ok': True,
        'proposed': {
            'nodes': nodes, 'edges': edges,
            'viewport': {'x': 0, 'y': 0, 'zoom': 1},
        },
    })


@app.route('/api/flows/<flow_id>/playground', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('flows')
def api_flow_playground(flow_id):
    """Text chat against the flow's current configuration — without phoning anyone.
    Body: {messages: [{role:'user'|'model', text}, ...]}
    Returns: {reply: '...', tool_calls: [...]}
    """
    flow = _flow_load(flow_id)
    body = request.get_json(force=True) or {}
    msgs = body.get('messages') or []
    if not isinstance(msgs, list) or not msgs:
        return jsonify({'error': 'messages array required'}), 400
    contents = []
    for m in msgs[-20:]:
        role = 'user' if m.get('role') == 'user' else 'model'
        contents.append({'role': role, 'parts': [{'text': str(m.get('text', ''))[:4000]}]})
    payload = {
        'contents': contents,
        'systemInstruction': {'parts': [{'text': flow.get('system_prompt', '')}]},
        'generationConfig': {'temperature': 0.5},
    }
    try:
        resp = _gemini_rest(payload, timeout=30)
    except Exception as e:
        # Return 200 with ok:false so Cloudflare doesn't replace the body with
        # its own HTML 5xx page. The client checks `error` regardless of status.
        return jsonify({'ok': False, 'error': str(e)})
    reply = ''
    try:
        parts = resp['candidates'][0]['content']['parts']
        reply = '\n'.join(p.get('text', '') for p in parts if p.get('text'))
    except Exception:
        reply = '(no reply)'
    return jsonify({'ok': True, 'reply': reply, 'tool_calls': []})


# ============================================================================
# Auto-confirmation watcher — places the order-confirm AI call automatically the
# moment a NEW order lands (website checkout or staff-entered), so nobody has to
# click "Call to confirm". A single daemon thread does it; an flock keeps it to
# ONE runner even if several processes import this module. Orders taken on an
# inbound AI call (source "AI call") are skipped — that buyer was just on the
# phone. Each order is CLAIMED (stamped auto_call_at) before dialling so an
# overlapping sweep or a flapping AMI can never ring the same buyer twice; staff
# can still retry a no-answer with the manual button (status stays "pending").
# refdata/sales.json config:
#   "auto_confirm_call": false      -> disable (require the manual button instead)
#   "auto_call_hours": [from, to]   -> only dial in this local-time window (default 8–21)
#   "confirm_test_number": "07..."  -> SAFETY: every auto-call rings this instead of the buyer
# ============================================================================
AUTO_CONFIRM_LOCK = SAMPATH_DATA / 'auto-confirm.lock'


def _auto_call_within_hours(ref):
    """Is it inside the configured auto-call window, in LOCAL (Asia/Colombo) time?

    This host runs UTC. A naive datetime.now().hour turned the intended
    08:00-21:00 window into 13:30-02:30 Colombo, i.e. the watcher robo-called
    real buyers at 2am and never called them in the morning. Use the same
    offset-aware clock the restaurant vertical already reasons in (_rest_now).
    """
    hrs = ref.get('auto_call_hours') or [8, 21]
    try:
        lo, hi = int(hrs[0]), int(hrs[1])
    except Exception:
        lo, hi = 8, 21
    return lo <= _rest_now().hour < hi


def _auto_confirm_scan():
    """One sweep of sales/orders: auto-place a confirm call for each fresh order."""
    ref = _load_refdata('sales')
    if ref.get('auto_confirm_call', True) is False:
        return
    d = _coll_dir('sales', 'orders')
    if not d.exists() or not _auto_call_within_hours(ref):
        return
    for f in sorted(d.glob('*.json')):
        try:
            o = json.load(f.open())
        except Exception:
            continue
        if o.get('status') != 'pending' or o.get('auto_call_at'):
            continue
        if str(o.get('source') or '') == 'AI call':   # buyer was just on the phone
            continue
        if len(re.sub(r'\D', '', str(o.get('phone') or ''))) < 7:
            continue
        rid = o.get('id') or f.stem
        # Claim first (stamp before dialling) so this order is called exactly once.
        o['auto_call_at'] = datetime.datetime.now().isoformat(timespec='seconds')
        o['auto_call_by'] = 'auto'
        try:
            _write_record('sales', 'orders', o)
        except Exception:
            continue
        res = _originate_outbound('sales', 'orders', rid, 'order_confirm', by='auto')
        print('[auto-confirm] %s -> %s' % (
            rid, res.get('dialled') if res.get('ok') else ('FAILED: ' + str(res.get('error')))),
            flush=True)
        # Stamp the call result without clobbering anything the bridge may have just
        # written (re-read before the merge-write).
        try:
            cur = json.load((d / ('%s.json' % rid)).open())
            cur['auto_call_uuid'] = res.get('uuid')
            cur['auto_call_ok'] = bool(res.get('ok'))
            _write_record('sales', 'orders', cur)
        except Exception:
            pass


def _auto_confirm_loop(interval=6):
    import fcntl
    try:
        lf = open(AUTO_CONFIRM_LOCK, 'w')
    except Exception as e:
        print('[auto-confirm] cannot open lock %s: %s — watcher disabled' % (AUTO_CONFIRM_LOCK, e), flush=True)
        return
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        return  # another process already holds it — that one runs the watcher
    globals()['_auto_confirm_lockfile'] = lf  # hold the fd for the process lifetime
    print('[auto-confirm] watcher started (interval %ss)' % interval, flush=True)
    while True:
        try:
            _auto_confirm_scan()
        except Exception as e:
            print('[auto-confirm] scan error:', e, flush=True)
        time.sleep(interval)


_auto_confirm_started = False


def _start_auto_confirm():
    global _auto_confirm_started
    if _auto_confirm_started:
        return
    # Escape hatch for importing app.py as a library (the restaurant transcript
    # tests do this) — no background caller should be started in that case.
    if os.environ.get('PBX_MONITOR_NO_WATCHERS'):
        print('[auto-confirm] disabled via PBX_MONITOR_NO_WATCHERS', flush=True)
        return
    # A box without the sales vertical has nothing for this to watch, and a
    # background thread that dials people is not something to leave running by
    # accident on a client's PBX.
    if not feature('auto_confirm') or 'sales' not in DASHBOARDS:
        print('[auto-confirm] disabled for this site', flush=True)
        return
    _auto_confirm_started = True
    import threading
    threading.Thread(target=_auto_confirm_loop, name='auto-confirm', daemon=True).start()


_start_auto_confirm()

# Provision the voice bridge's shared secret at BOOT, not on the first
# authenticated call. bridge.ts refuses to send a request when the token file is
# missing, and this file used to be created only by a request reaching
# api_agent_tool — so on a clean box neither side could go first and every
# restaurant tool call failed with no_token, permanently.
try:
    _agent_token()
except Exception as e:                                      # pragma: no cover
    print('[agent-api] could not provision token at boot:', e, flush=True)

# ==================== PBX MESH — inter-PBX trunks + IVR routing ====================
# This is the piece the console was missing entirely: everything about how the
# three PBXs reach each other, and how an inbound caller picks between them, used
# to live in hand-edited pjsip.conf / extensions.conf plus two shell scripts.
#
# Model: /var/lib/sampath-ai/mesh.json is the source of truth for the IVR menu.
# The dialplan block and the TTS prompt text are GENERATED from it, so the UI and
# what callers actually hear can never drift apart.

MESH_FILE = SAMPATH_DATA / 'mesh.json'
IVR_MARKER_BEGIN = '; ===== NAXTER-IVR-V3 (generated by pbx-monitor — do not hand-edit) ====='
IVR_MARKER_END = '; ===== END NAXTER-IVR-V3 ====='
IVR_AUDIO_DIR = _site_path(_SITE_PATHS, 'ivr_audio_dir', '/usr/share/asterisk/sounds/en/custom')   # astdatadir + language

MESH_DEFAULT = {
    'greeting': 'Thank you for calling.',
    'retries': 3,
    'digit_timeout': 5,
    'response_timeout': 10,
    'fallback': 'local_agent',
    'options': [],
}

# ---------------------------------------------------------------------------
# Optional language selection in front of the menu.
#
# A mesh.json with NO "languages" block must behave exactly as it did before
# this existed: no [ai-lang] context, un-suffixed prompt file names, and a
# byte-identical [ai-ivr] block. _mesh_langs() returning None is that contract —
# every consumer below treats None as "the way it has always been".
#
# When it IS enabled the caller first hears a selector in which each option is
# spoken IN ITS OWN LANGUAGE, so a caller who only speaks Sinhala can still
# understand option 2. The choice is stashed in the inheritable __AI_LANG, which
# selects the per-language menu recording here and rides a SIP header out to the
# sibling PBXs so they never ask again.
# ---------------------------------------------------------------------------
LANG_CODE_RE = re.compile(r'^[a-z]{2,3}$')
LANG_NAMES = {'en': 'English', 'si': 'Sinhala', 'ta': 'Tamil',
              'de': 'German', 'ms': 'Malay', 'zh': 'Chinese', 'hi': 'Hindi'}
LANG_HEADER = 'X-AI-Lang'                 # carries the choice over an inter-PBX trunk
LANG_HDR_CONTEXT = 'ai-lang-hdr'          # Dial() pre-dial handler that stamps it
LANG_CONTEXT = 'ai-lang'

# Defaults are full, punctuated sentences on purpose: the Gemini TTS endpoint
# answers 200-with-no-audio (finishReason=OTHER) noticeably more often for short
# clipped fragments than for natural sentences, and Sinhala is the worst of the
# three for it.
LANG_SELECT_DEFAULT = {
    'en': 'For English, press 1.',
    'si': 'සිංහල භාෂාව සඳහා, කරුණාකර දෙක ඔබන්න.',
    'ta': 'தமிழ் மொழியில் தொடர, தயவுசெய்து மூன்றை அழுத்தவும்.',
}
LANG_INVALID_DEFAULT = {
    'en': 'Sorry, I did not get that. For English, press 1.',
    'si': 'සමාවන්න, එය තේරුම් ගත නොහැකි විය. සිංහල භාෂාව සඳහා, කරුණාකර දෙක ඔබන්න.',
    'ta': 'மன்னிக்கவும், அது புரியவில்லை. தமிழ் மொழியில் தொடர, தயவுசெய்து மூன்றை அழுத்தவும்.',
}
# Lead-in for the menu retry prompt, per language. 'en' is byte-for-byte what the
# single-language build has always said.
IVR_RETRY_LEAD = {
    'en': 'Sorry, I did not get that.',
    'si': 'සමාවන්න, එය තේරුම් ගත නොහැකි විය.',
    'ta': 'மன்னிக்கவும், அது புரியவில்லை.',
}
IVR_MIN_RMS = 200.0        # below this a "recording" is silence, not speech
IVR_MIN_SECONDS = 0.4


def _mesh_langs(cfg):
    """Normalised language config, or None when this site is single-language.

    None is the backward-compatibility signal — see the block comment above.
    """
    lc = cfg.get('languages')
    if not isinstance(lc, dict):
        return None
    if lc.get('enabled') is False:                       # explicit off switch
        return None
    enabled, seen = [], set()
    for c in (lc.get('enabled') or []):
        c = str(c).strip().lower()
        if LANG_CODE_RE.match(c) and c not in seen:
            seen.add(c)
            enabled.append(c)
    if not enabled:
        return None

    default = str(lc.get('default') or '').strip().lower()
    if default not in enabled:
        default = enabled[0]

    # digit -> language. An explicit map wins; otherwise 1..N in listed order.
    dmap, raw = {}, lc.get('map')
    if isinstance(raw, dict):
        for d, code in raw.items():
            d, code = str(d).strip(), str(code).strip().lower()
            if len(d) == 1 and d in '0123456789' and code in enabled and d not in dmap:
                dmap[d] = code
    if not dmap:
        dmap = {str(i + 1): c for i, c in enumerate(enabled[:9])}
    # A language nobody can press is not offered — keep the two views consistent.
    order = [dmap[d] for d in sorted(dmap)]
    digit_of = {c: d for d, c in sorted(dmap.items(), reverse=True)}

    def _texts(key, fallback):
        raw = lc.get(key)
        out = {}
        for code in order:
            t = str((raw or {}).get(code) or '').strip() if isinstance(raw, dict) else ''
            if not t:
                t = fallback.get(code) or ('For %s, press %s.' % (
                    LANG_NAMES.get(code, code.upper()), digit_of.get(code, '')))
            out[code] = t
        return out

    return {
        'enabled': order,
        'default': default if default in order else order[0],
        'map': dict(sorted(dmap.items())),
        'order': order,
        'digit_of': digit_of,
        'select': _texts('select', LANG_SELECT_DEFAULT),
        'invalid': _texts('invalid', LANG_INVALID_DEFAULT),
        'retries': max(1, min(9, int(lc.get('retries') or 2))),
        'digit_timeout': max(1, min(30, int(lc.get('digit_timeout') or 5))),
        'response_timeout': max(1, min(60, int(lc.get('response_timeout') or 10))),
    }


def _ivr_lang_text(val, lang, default_lang):
    """A greeting/phrase/short value: a plain string (every language) OR {lang: text}."""
    if isinstance(val, dict):
        for k in (lang, default_lang, 'en'):
            if k and str(val.get(k) or '').strip():
                return str(val[k]).strip()
        for v in val.values():
            if str(v or '').strip():
                return str(v).strip()
        return ''
    return str(val or '').strip()


def _mesh_load():
    try:
        d = json.loads(MESH_FILE.read_text())
        if isinstance(d, dict):
            out = dict(MESH_DEFAULT)
            out.update(d)
            return out
    except Exception:
        pass
    return dict(MESH_DEFAULT)


def _mesh_save(cfg):
    MESH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MESH_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(MESH_FILE)


def _mesh_trunks():
    """Inter-PBX trunks parsed from pjsip.conf, joined to their live state.

    An 'inter-PBX trunk' here is any endpoint bound to the Tailscale transport —
    that is what distinguishes a sibling PBX from the carrier trunk.
    """
    trunks = {}
    try:
        txt = PJSIP_CONF.read_text()
    except Exception:
        return []
    section, cur = None, {}
    for raw in txt.splitlines():
        line = raw.strip()
        if line.startswith(';') or not line:
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            cur = {}
            continue
        if '=' not in line or section is None:
            continue
        k, v = line.split('=', 1)
        k, v = k.strip(), v.strip()
        t = trunks.setdefault(section, {'name': section, 'contact': '', 'match': '',
                                        'transport': '', 'context': '', 'is_mesh': False})
        if k == 'contact':
            t['contact'] = v
        elif k == 'match':
            t['match'] = v
        elif k == 'transport':
            t['transport'] = v
            if 'tailscale' in v:
                t['is_mesh'] = True
        elif k == 'context':
            t['context'] = v

    # live status from Asterisk
    live = {}
    try:
        for ln in asterisk_cli('pjsip show endpoints').splitlines():
            m = re.search(r'Contact:\s+(\S+?)/\S+\s+\S+\s+(\S+)\s+(\S+)', ln)
            if m:
                live[m.group(1)] = {'status': m.group(2), 'rtt_ms': m.group(3)}
            m2 = re.match(r'\s*Endpoint:\s+(\S+)\s+(\S.*?)\s{2,}', ln)
            if m2:
                live.setdefault(m2.group(1), {})['state'] = m2.group(2).strip()
    except Exception:
        pass

    out = []
    for name, t in trunks.items():
        if not t['is_mesh']:
            continue
        st = live.get(name, {})
        out.append({**t,
                    'state': st.get('state', 'unknown'),
                    'status': st.get('status', '-'),
                    'rtt_ms': st.get('rtt_ms', '-')})
    return sorted(out, key=lambda x: x['name'])


def _ivr_prompt_text(cfg, lang=None):
    """The exact words the menu and the retry prompt should say.

    With no languages block this is the original single-language code path,
    character for character. With one, every string-or-{lang: text} field is
    resolved for `lang` (default: the configured default language).
    """
    lc = _mesh_langs(cfg)
    dflt = lc['default'] if lc else None
    lang = lang or dflt

    def T(v):
        if lc or isinstance(v, dict):
            return _ivr_lang_text(v, lang, dflt)
        return v or ''

    parts = [T(cfg.get('greeting'))]
    for o in cfg.get('options', []):
        phrase = T(o.get('phrase'))
        if phrase:
            parts.append(phrase.rstrip('.') + '.')
    menu = ' '.join(p for p in parts if p).strip()

    lead = 'Sorry, I did not get that.'
    if lc:
        lead = _ivr_lang_text(cfg.get('retry_lead'), lang, dflt) or \
            IVR_RETRY_LEAD.get(lang) or lead
    retry = lead + ' ' + ' '.join(
        (T(o.get('short')) or T(o.get('phrase'))).rstrip('.') + '.'
        for o in cfg.get('options', []) if (T(o.get('short')) or T(o.get('phrase'))))
    return menu, retry.strip()


def _ivr_lang_prompt_text(cfg):
    """The trilingual selector wording: (select, invalid). ('','') when disabled.

    Each option is spoken in its own language and they are simply concatenated in
    digit order — one recording, so the caller never has to wait through a
    language they cannot understand before the one they can.
    """
    lc = _mesh_langs(cfg)
    if not lc:
        return '', ''
    sel = ' '.join(lc['select'][c] for c in lc['order'] if lc['select'].get(c)).strip()
    inv = ' '.join(lc['invalid'][c] for c in lc['order'] if lc['invalid'].get(c)).strip()
    return sel, inv


def _ivr_sound(base, lc):
    """Prompt file name: per-language when languages are on, bare name when not."""
    return 'custom/%s-${AI_LANG}' % base if lc else 'custom/%s' % base


def _ivr_lang_dialplan(cfg, lc):
    """[ai-lang] — the language picker that runs in front of [ai-ivr].

    Sets __AI_LANG (double underscore = inherited by every channel spawned from
    this one, which is what lets the Dial pre-dial handler stamp the header on
    the outbound leg) and falls through to the existing menu.

    Deliberately does NOT touch CHANNEL(language): Asterisk resolves
    "custom/<name>" under sounds/<language>/, and every prompt this system owns
    lives in .../sounds/en/custom. Switching the channel language would send
    Asterisk looking in sounds/si/custom and every prompt would go missing. The
    language is carried in the FILE NAME instead.
    """
    dflt = lc['default']
    L = ['', '[%s]' % LANG_CONTEXT,
         ';  Language selection. Runs on every inbound call; sets __AI_LANG and',
         ';  hands over to [ai-ivr], which plays the menu in the chosen language.',
         'exten => s,1,NoOp(*** Language select *** caller=${CALLERID(num)})',
         ' same => n,Answer()',
         ' same => n,Wait(1)',
         ' same => n,Set(TIMEOUT(digit)=%d)' % lc['digit_timeout'],
         ' same => n,Set(TIMEOUT(response)=%d)' % lc['response_timeout'],
         ' same => n,Set(LANGTRIES=0)',
         ' same => n,Read(LANGCHOICE,custom/naxter-lang-select,1,,1,10)',
         " same => n(langvalidate),NoOp(language choice='${LANGCHOICE}' tries=${LANGTRIES})"]
    for d in sorted(lc['map']):
        L.append(' same => n,GotoIf($["${LANGCHOICE}"="%s"]?lang_%s)' % (d, lc['map'][d]))
    L += [' ; invalid or no input -> retry, then fall back to the default language',
          ' same => n,Set(LANGTRIES=$[${LANGTRIES}+1])',
          ' same => n,GotoIf($[${LANGTRIES}>=%d]?lang_default)' % lc['retries'],
          ' same => n,Read(LANGCHOICE,custom/naxter-lang-invalid,1,,1,10)',
          ' same => n,Goto(langvalidate)',
          '']
    for d in sorted(lc['map']):
        code = lc['map'][d]
        L += [' same => n(lang_%s),Set(__AI_LANG=%s)' % (code, code),
              ' same => n,Goto(chosen)',
              '']
    L += [' same => n(lang_default),NoOp(no language chosen after ${LANGTRIES} tries -> %s)' % dflt,
          ' same => n,Set(__AI_LANG=%s)' % dflt,
          '',
          ' same => n(chosen),NoOp(caller language=${AI_LANG})',
          ' same => n,Goto(ai-ivr,s,1)',
          '',
          'exten => h,1,NoOp(language select ended lang=${AI_LANG})',
          '',
          '[%s]' % LANG_HDR_CONTEXT,
          ';  Dial() pre-dial handler. PJSIP_HEADER(add) applies to the channel it',
          ';  runs on, so the header has to be set HERE — on the outbound leg —',
          ';  not on the caller channel before Dial(). ${AI_LANG} is visible because',
          ';  [ai-lang] set it with the inheritable __ prefix.',
          'exten => s,1,Set(PJSIP_HEADER(add,%s)=${AI_LANG})' % LANG_HEADER,
          ' same => n,Return()']
    return L


def _ivr_dialplan(cfg):
    """Render the [ai-ivr] context (and, if enabled, [ai-lang]) from mesh.json."""
    lc = _mesh_langs(cfg)
    L = [IVR_MARKER_BEGIN,
         ';  Generated from /var/lib/sampath-ai/mesh.json by the PBX Mesh page.',
         ';  Edit it there — anything written here by hand is overwritten on save.',
         '[ai-ivr]',
         'exten => s,1,NoOp(*** IVR menu *** caller=${CALLERID(num)})',
         ' same => n,Answer()',
         ' same => n,Wait(1)',
         ' same => n,Set(TIMEOUT(digit)=%d)' % int(cfg.get('digit_timeout', 5)),
         ' same => n,Set(TIMEOUT(response)=%d)' % int(cfg.get('response_timeout', 12)),
         ' same => n,Set(TRIES=0)']
    if lc:
        # Reached directly (test call, survey hand-back) rather than through
        # [ai-lang]: pick the default rather than looking for a "-" sound file.
        L.append(' same => n,ExecIf($["${AI_LANG}"=""]?Set(__AI_LANG=%s))' % lc['default'])
    L += [' same => n,Read(IVRCHOICE,%s,1,,1,10)' % _ivr_sound('naxter-ivr-menu', lc),
          " same => n(validate),NoOp(IVR choice='${IVRCHOICE}' tries=${TRIES})"]
    for o in cfg.get('options', []):
        L.append(' same => n,GotoIf($["${IVRCHOICE}"="%s"]?opt%s)' % (o['key'], o['key']))
    L += [' ; invalid or no input -> retry, then fall back',
          ' same => n,Set(TRIES=$[${TRIES}+1])',
          ' same => n,GotoIf($[${TRIES}>=%d]?giveup)' % int(cfg.get('retries', 3)),
          ' same => n,Read(IVRCHOICE,%s,1,,1,10)' % _ivr_sound('naxter-ivr-invalid', lc),
          ' same => n,Goto(validate)',
          '']
    for o in cfg.get('options', []):
        tgt = o.get('target') or {}
        L.append(' same => n(opt%s),NoOp(IVR -> %s)' % (o['key'], o.get('label', '')))
        if tgt.get('type') == 'trunk':
            L += [' same => n,Dial(PJSIP/%s@%s,%d%s)' % (
                      tgt.get('exten', ''), tgt.get('trunk', ''),
                      int(tgt.get('timeout', 60)),
                      ',b(%s^s^1)' % LANG_HDR_CONTEXT if lc else ''),
                  ' same => n,NoOp(leg ended status=${DIALSTATUS})',
                  ' same => n,GotoIf($["${DIALSTATUS}"="ANSWER"]?done)',
                  ' ; remote PBX down/busy — keep the caller, hand to the local agent',
                  ' same => n,Goto(ai-agent,s,1)']
        elif tgt.get('type') == 'extension':
            L += [' same => n,Dial(PJSIP/%s,%d)' % (tgt.get('exten', ''), int(tgt.get('timeout', 45))),
                  ' same => n,Goto(ai-agent,s,1)']
        elif tgt.get('type') == 'context':
            L.append(' same => n,Goto(%s,s,1)' % tgt.get('context', 'ai-agent'))
        else:                                   # local_agent
            L.append(' same => n,Goto(ai-agent,s,1)')
        L.append('')
    L += [' same => n(done),Hangup()',
          '',
          ' same => n(giveup),NoOp(IVR no valid input after ${TRIES} tries)',
          ' same => n,Goto(%s,s,1)' % ('ai-agent' if cfg.get('fallback', 'local_agent') == 'local_agent'
                                       else cfg.get('fallback')),
          '',
          'exten => h,1,NoOp(IVR call ended choice=${IVRCHOICE})']
    if lc:
        L += _ivr_lang_dialplan(cfg, lc)
    L.append(IVR_MARKER_END)
    return '\n'.join(L) + '\n'


# The inbound DID's final Goto, immediately after the survey toggle. The toggle
# must keep winning, so only this one line is ever rewritten.
_DID_ROUTE_RE = re.compile(r'(SURVEY-TOGGLE-V1\n[ \t]*same => n,)Goto\(ai-(?:ivr|lang),s,1\)')


def _ivr_route_did(txt, target):
    """Point the inbound DID at [ai-lang] or [ai-ivr], keeping the survey toggle first.

    On a single-language box target is 'ai-ivr', which is what is already there —
    the substitution is a no-op and the file comes out byte-identical. That is
    also how a box reverts cleanly if languages are switched back off.
    """
    return _DID_ROUTE_RE.sub(lambda m: m.group(1) + 'Goto(%s,s,1)' % target, txt, count=1)


def _ivr_write_dialplan(cfg):
    """Replace the generated block in the dialplan file, backing up first.

    EXTENSIONS_CONF is extensions.conf on a raw-Asterisk box and
    extensions_custom.conf on FreePBX — writing the generated file there would
    be undone by the next `fwconsole reload`. See the site-config block at the
    top of this file.
    """
    try:
        txt = EXTENSIONS_CONF.read_text()
    except FileNotFoundError:
        # A FreePBX box that has never had a custom dialplan yet: the include
        # target exists in the config but the file may not. Start it empty.
        txt = ''
    block = _ivr_dialplan(cfg)
    if IVR_MARKER_BEGIN in txt and IVR_MARKER_END in txt:
        head = txt.split(IVR_MARKER_BEGIN)[0]
        # The block already ends in a newline and the tail begins with the one
        # that followed the end marker, so a naive head+block+tail grew the file
        # by a blank line on every single apply. Normalise it instead: exactly
        # one blank line before whatever follows, or nothing if it was last.
        rest = txt.split(IVR_MARKER_END, 1)[1].lstrip('\n')
        new = head + block + ('\n' + rest if rest else '')
    else:
        # First run: retire any older hand-installed IVR block, which by
        # convention ran from its marker to EOF.
        m = re.search(r'^; ===== NAXTER-IVR-V[12] =====\s*$', txt, re.M)
        new = (txt[:m.start()] if m else txt.rstrip() + '\n\n') + block
    new = _ivr_route_did(new, LANG_CONTEXT if _mesh_langs(cfg) else 'ai-ivr')
    bak = EXTENSIONS_CONF.with_name(
        EXTENSIONS_CONF.name + '.bak-mesh-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    subprocess.run(['cp', '-a', str(EXTENSIONS_CONF), str(bak)], check=False)
    tmp = EXTENSIONS_CONF.with_suffix('.conf.tmp')
    tmp.write_text(new)
    tmp.replace(EXTENSIONS_CONF)
    return str(bak)


def _mesh_validate_langs(lc):
    """Return an error string for a bad languages block, or None if it is fine."""
    if not isinstance(lc, dict):
        return 'languages must be an object'
    codes = lc.get('enabled')
    if not isinstance(codes, list) or not codes:
        return 'languages.enabled must be a non-empty list of language codes'
    if len(codes) > 9:
        return 'at most 9 languages (one per dial-pad digit)'
    seen = set()
    for c in codes:
        c = str(c).strip().lower()
        if not LANG_CODE_RE.match(c):
            return f'{c!r} is not a language code (two or three lower-case letters)'
        if c in seen:
            return f'duplicate language {c}'
        seen.add(c)
    if lc.get('default') and str(lc['default']).strip().lower() not in seen:
        return f'default language {lc["default"]!r} is not in the enabled list'
    m = lc.get('map')
    if m is not None:
        if not isinstance(m, dict):
            return 'languages.map must be an object of digit -> language'
        used = set()
        for d, c in m.items():
            d, c = str(d).strip(), str(c).strip().lower()
            if len(d) != 1 or d not in '0123456789':
                return f'{d!r} is not a single dial-pad digit'
            if d in used:
                return f'duplicate digit {d} in the language map'
            used.add(d)
            if c not in seen:
                return f'digit {d} maps to {c!r}, which is not enabled'
    for key in ('select', 'invalid'):
        v = lc.get(key)
        if v is None:
            continue
        if not isinstance(v, dict):
            return f'languages.{key} must be an object of language -> text'
        for c, t in v.items():
            if not LANG_CODE_RE.match(str(c).strip().lower()):
                return f'languages.{key}: {c!r} is not a language code'
            if not isinstance(t, str) or len(t) > 2000:
                return f'languages.{key}: text for {c} must be a string under 2000 chars'
    for key in ('retries', 'digit_timeout', 'response_timeout'):
        if key in lc and lc[key] is not None:
            try:
                int(lc[key])
            except (TypeError, ValueError):
                return f'languages.{key} must be a number'
    return None


@app.route('/mesh')
@login_required
@perm_required('admin')
@feature_required('mesh')
def mesh_page():
    return render_template('mesh.html', nav='mesh')


@app.route('/api/mesh', methods=['GET'])
@login_required
@perm_required('admin')
@feature_required('mesh')
def api_mesh():
    cfg = _mesh_load()
    lc = _mesh_langs(cfg)
    menu, retry = _ivr_prompt_text(cfg)
    sel, sel_invalid = _ivr_lang_prompt_text(cfg)
    clips = _ivr_audio_clips(cfg)
    return jsonify({
        'ivr': cfg,
        'trunks': _mesh_trunks(),
        'prompt_menu': menu,
        'prompt_retry': retry,
        'dialplan_preview': _ivr_dialplan(cfg),
        # Legacy field, unchanged for a single-language box; on a multilingual
        # one it means "every clip the current config needs is on disk".
        'audio_present': (all((IVR_AUDIO_DIR / (c[0] + '.wav')).exists() for c in clips)
                          if lc else (IVR_AUDIO_DIR / 'naxter-ivr-menu.wav').exists()),
        'audio_files': {c[0]: (IVR_AUDIO_DIR / (c[0] + '.wav')).exists() for c in clips},
        'languages': lc,                       # null when the site is single-language
        'language_names': LANG_NAMES,
        'prompt_lang_select': sel,
        'prompt_lang_invalid': sel_invalid,
        'prompt_by_lang': ({c: dict(zip(('menu', 'retry'), _ivr_prompt_text(cfg, c)))
                            for c in lc['order']} if lc else {}),
        'local_contexts': ['ai-agent', 'nps-survey'],
    })


@app.route('/api/mesh/ivr', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('mesh')
def api_mesh_ivr_save():
    j = request.get_json(silent=True) or {}
    opts = j.get('options')
    if not isinstance(opts, list) or not opts:
        return jsonify({'error': 'at least one option is required'}), 400

    # The language block is optional and stays optional: absent/null means the
    # site keeps behaving exactly as a single-language site always has.
    if 'languages' in j and j['languages'] not in (None, {}, False):
        err = _mesh_validate_langs(j['languages'])
        if err:
            return jsonify({'error': err}), 400

    def _bad_text(v, what):
        """Accept a plain string (all languages) or a {lang: text} object."""
        if isinstance(v, dict):
            for k, t in v.items():
                if not LANG_CODE_RE.match(str(k).strip().lower()):
                    return f'{what}: {k!r} is not a language code'
                if not isinstance(t, str) or len(t) > 2000:
                    return f'{what}: text for {k} must be a string under 2000 chars'
            return None
        if v is None or isinstance(v, str) and len(v) <= 2000:
            return None
        return f'{what} must be text, or an object of language -> text'

    e = _bad_text(j.get('greeting'), 'greeting')
    if e:
        return jsonify({'error': e}), 400

    seen = set()
    for o in opts:
        k = str(o.get('key', '')).strip()
        if k not in list('0123456789'):
            return jsonify({'error': f'bad key {k!r} — must be a single digit'}), 400
        if k in seen:
            return jsonify({'error': f'duplicate key {k}'}), 400
        seen.add(k)
        t = o.get('target') or {}
        if t.get('type') == 'trunk':
            if not t.get('trunk') or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(t.get('trunk'))):
                return jsonify({'error': f'option {k}: invalid trunk name'}), 400
            if not re.fullmatch(r'[0-9A-Za-z*#+]{1,20}', str(t.get('exten', ''))):
                return jsonify({'error': f'option {k}: invalid extension'}), 400
        elif t.get('type') == 'context':
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(t.get('context', ''))):
                return jsonify({'error': f'option {k}: invalid context'}), 400
        for fld in ('phrase', 'short'):
            e = _bad_text(o.get(fld), f'option {k}: {fld}')
            if e:
                return jsonify({'error': e}), 400
    cfg = _mesh_load()
    cfg.update({k: v for k, v in j.items()
                if k in ('greeting', 'options', 'retries', 'digit_timeout',
                         'response_timeout', 'fallback', 'retry_lead')})
    if 'languages' in j:
        # null / {} / false all mean "turn it off and go back to the
        # single-language dialplan" — drop the key rather than storing a husk.
        if j['languages'] in (None, {}, False):
            cfg.pop('languages', None)
        else:
            cfg['languages'] = j['languages']
    _mesh_save(cfg)
    menu, retry = _ivr_prompt_text(cfg)
    sel, sel_invalid = _ivr_lang_prompt_text(cfg)
    return jsonify({'ok': True, 'prompt_menu': menu, 'prompt_retry': retry,
                    'prompt_lang_select': sel, 'prompt_lang_invalid': sel_invalid,
                    'languages': _mesh_langs(cfg),
                    'dialplan_preview': _ivr_dialplan(cfg),
                    'note': 'Saved. Apply to write the dialplan; regenerate audio if the wording changed.'})


@app.route('/api/mesh/apply', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('mesh')
def api_mesh_apply():
    """Write the generated dialplan and reload it. Audio is a separate step."""
    cfg = _mesh_load()
    try:
        bak = _ivr_write_dialplan(cfg)
    except Exception as e:
        return jsonify({'error': f'could not write dialplan: {e}'}), 500
    out = asterisk_cli('dialplan reload')
    ok = 'reload' in (out or '').lower() or out is not None
    return jsonify({'ok': bool(ok), 'backup': bak, 'output': out,
                    'rollback': f'sudo cp -a {bak} {EXTENSIONS_CONF} && sudo asterisk -rx "dialplan reload"'})


@app.route('/api/mesh/audio', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('mesh')
def api_mesh_audio():
    """Re-synthesise every prompt the current menu wording needs.

    Uses the same Gemini TTS voice as the live agent so the menu and the agent
    do not sound like two different systems. On a multilingual site that is the
    trilingual selector plus a menu/retry pair per language.
    """
    cfg = _mesh_load()
    clips = _ivr_audio_clips(cfg)
    if not clips or not clips[0][1]:
        return jsonify({'error': 'menu text is empty'}), 400
    try:
        made, sizes = [], {}
        for i, (name, text, lang) in enumerate(clips):
            if not text:
                continue
            if i:
                # The API key is shared with all three live voice agents and 429s
                # readily when clips are generated back to back.
                time.sleep(IVR_TTS_GAP_SECONDS)
            r = _ivr_synth(name, text, lang)
            made.append(r)
            sizes.setdefault(r['samples'], []).append(name)
        _ivr_assert_distinct(clips, sizes)
        menu, retry = _ivr_prompt_text(cfg)
        return jsonify({'ok': True, 'files': made, 'menu': menu, 'retry': retry})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


IVR_TTS_GAP_SECONDS = 4          # pacing between synths — the key is shared with the agents


def _ivr_audio_clips(cfg):
    """[(file base name, text, language)] that this config needs on disk."""
    lc = _mesh_langs(cfg)
    if not lc:
        menu, retry = _ivr_prompt_text(cfg)
        return [('naxter-ivr-menu', menu, ''), ('naxter-ivr-invalid', retry, '')]
    sel, inv = _ivr_lang_prompt_text(cfg)
    out = [('naxter-lang-select', sel, 'multi'), ('naxter-lang-invalid', inv, 'multi')]
    for code in lc['order']:
        menu, retry = _ivr_prompt_text(cfg, code)
        out += [('naxter-ivr-menu-%s' % code, menu, code),
                ('naxter-ivr-invalid-%s' % code, retry, code)]
    return out


def _ivr_assert_distinct(clips, sizes):
    """Two languages that produced the SAME number of samples are the same audio.

    That is the failure this guards: Gemini quietly reading the English text and
    it getting installed as naxter-ivr-menu-si. Fail the whole regeneration
    rather than ship a Sinhala file that speaks English.
    """
    langs = {name: lang for name, _t, lang in clips}
    for names in sizes.values():
        distinct = {langs.get(n) for n in names if langs.get(n) not in ('', 'multi')}
        if len(names) > 1 and len(distinct) > 1:
            raise RuntimeError(
                'TTS returned byte-identical audio for different languages (%s) — '
                'refusing to install it; retry the regeneration' % ', '.join(sorted(names)))


def _pcm_rms(pcm):
    """RMS of signed 16-bit little-endian PCM. Silence -> ~0."""
    import array as _array
    import sys as _sys
    a = _array.array('h')
    a.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
    if _sys.byteorder == 'big':
        a.byteswap()
    if not len(a):
        return 0.0
    return (sum(float(s) * s for s in a) / len(a)) ** 0.5


def _gemini_tts(text, voice, key, attempts=4):
    """Gemini TTS -> (pcm16, sample rate), retrying the two ways it flakes.

    1. A 200 response whose candidate has finishReason=OTHER and NO content key
       at all. Seen repeatedly on Sinhala. Naive code does
       data['candidates'][0]['content'] and dies with KeyError.
    2. HTTP 429 — the key is shared with the live voice agents, so TTS competes
       with call traffic for quota.

    Both are retried with backoff. Anything else, and exhaustion, raises.
    """
    import urllib.request
    import urllib.error
    import base64 as _b64
    url = ('https://generativelanguage.googleapis.com/v1beta/models/'
           'gemini-2.5-flash-preview-tts:generateContent?key=' + key)
    body = {'contents': [{'parts': [{'text': text}]}],
            'generationConfig': {'responseModalities': ['AUDIO'],
                                 'speechConfig': {'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': voice}}}}}
    last = 'no attempt made'
    for i in range(attempts):
        if i:
            time.sleep(min(45, 5 * (2 ** (i - 1))))
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8', 'replace')[:300]
            except Exception:
                pass
            last = 'HTTP %s %s' % (e.code, detail)
            if e.code in (429, 500, 502, 503, 504):
                continue
            raise RuntimeError(last)
        except Exception as e:
            last = str(e)
            continue
        cand = (data.get('candidates') or [{}])[0]
        parts = ((cand.get('content') or {}).get('parts')) or []
        inline = next((p['inlineData'] for p in parts if 'inlineData' in p), None)
        if not inline:
            last = 'response carried no audio (finishReason=%s)' % cand.get('finishReason')
            continue
        pcm = _b64.b64decode(inline['data'])
        m = re.search(r'rate=(\d+)', inline.get('mimeType', ''))
        return pcm, (int(m.group(1)) if m else 24000)
    raise RuntimeError('Gemini TTS gave up after %d attempts: %s' % (attempts, last))


def _ivr_synth(name, text, lang=''):
    """Gemini TTS -> 8 kHz wav/ulaw/alaw installed where Asterisk looks.

    Asterisk resolves "custom/<n>" under astdatadir + language, i.e.
    /usr/share/asterisk/sounds/en/custom — NOT /var/lib/asterisk/sounds. The
    caller's language is encoded in the FILE NAME, not in the channel language,
    so every clip stays in the en/ tree.

    Nothing is written until the returned audio has been checked for length and
    loudness: a silent clip installed under a Sinhala name is worse than a
    failed regeneration, because nobody hears it until a caller does.
    """
    if not re.fullmatch(r'[a-z0-9-]{1,64}', name):
        raise RuntimeError('bad prompt name %r' % name)
    key = _read_env_var('GEMINI_API_KEY')
    if not key:
        raise RuntimeError('GEMINI_API_KEY not available')
    voice = (ai_load_config() or {}).get('voice') or 'Aoede'
    pcm, rate = _gemini_tts(text, voice, key)

    seconds = len(pcm) / 2.0 / rate
    rms = _pcm_rms(pcm)
    if seconds < IVR_MIN_SECONDS or rms < IVR_MIN_RMS:
        raise RuntimeError('%s (%s): TTS returned %.2fs at rms %.0f — that is silence, '
                           'not speech; refusing to install it'
                           % (name, lang or 'default', seconds, rms))

    work = Path('/tmp') / f'ivr-{name}-{int(time.time())}.wav'
    with wave.open(str(work), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
    IVR_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    wav = IVR_AUDIO_DIR / f'{name}.wav'
    subprocess.run(['sox', str(work), '-r', '8000', '-c', '1', '-b', '16', str(wav), 'norm', '-3'], check=True)
    # sox has no handler for the .ulaw/.alaw extensions — the raw type must be explicit
    subprocess.run(['sox', str(wav), '-r', '8000', '-c', '1', '-t', 'ul', str(IVR_AUDIO_DIR / f'{name}.ulaw')], check=True)
    subprocess.run(['sox', str(wav), '-r', '8000', '-c', '1', '-t', 'al', str(IVR_AUDIO_DIR / f'{name}.alaw')], check=True)
    for ext in ('wav', 'ulaw', 'alaw'):
        try:
            os.chmod(IVR_AUDIO_DIR / f'{name}.{ext}', 0o644)
        except Exception:
            pass
    try:
        work.unlink()
    except Exception:
        pass
    return {'file': f'{name}.{{wav,ulaw,alaw}}', 'lang': lang,
            'seconds': round(seconds, 2), 'rms': round(rms), 'samples': len(pcm)}


@app.route('/api/mesh/trunk-test/<name>', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('mesh')
def api_mesh_trunk_test(name):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
        return jsonify({'error': 'bad trunk name'}), 400
    t = next((x for x in _mesh_trunks() if x['name'] == name), None)
    if not t:
        return jsonify({'error': 'not an inter-PBX trunk'}), 404
    host = ''
    m = re.search(r'sip:([^:;]+)', t.get('contact', ''))
    if m:
        host = m.group(1)
    ping = None
    if host:
        p = subprocess.run(['ping', '-c', '3', '-W', '2', host],
                           capture_output=True, text=True, timeout=20)
        ping = p.stdout.strip().splitlines()[-2:] if p.stdout else []
    asterisk_cli(f'pjsip qualify aor {name}' if name else 'core show version')
    time.sleep(1.5)
    t2 = next((x for x in _mesh_trunks() if x['name'] == name), t)
    return jsonify({'ok': True, 'host': host, 'ping': ping,
                    'state': t2.get('state'), 'status': t2.get('status'), 'rtt_ms': t2.get('rtt_ms')})


def _bind_address():
    """Where to listen. Loopback, or a Tailscale/CGNAT address — never 0.0.0.0.

    This panel edits the dialplan, reads trunk secrets, places calls and shows
    real patient/customer records. It is reachable from the internet only via
    cloudflared/nginx on the same host, so the socket itself must never be
    exposed: a typo'd site.json must fail to start, not quietly publish the
    admin panel on a public interface.
    """
    b = SITE.get('bind') or {}
    host = str(b.get('host') or '127.0.0.1').strip()
    try:
        port = int(b.get('port') or 5051)
    except (TypeError, ValueError):
        raise SystemExit('pbx-monitor: bind.port must be a number')
    if not (1 <= port <= 65535):
        raise SystemExit('pbx-monitor: bind.port %r out of range' % port)
    if host in ('127.0.0.1', 'localhost', '::1'):
        return '127.0.0.1', port
    octets = host.split('.')
    if len(octets) == 4 and all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
        if int(octets[0]) == 100 and 64 <= int(octets[1]) <= 127:   # 100.64.0.0/10
            return host, port
    raise SystemExit(
        'pbx-monitor: refusing to bind %r — bind.host must be 127.0.0.1 or a '
        'Tailscale address in 100.64.0.0/10' % host)



# ==================== MESH — add / remove inter-PBX trunks ====================
# Creating a trunk by hand means five PJSIP objects plus a dialplan context, kept
# consistent across two boxes. Doing it from the UI writes both ends' worth of
# config here and hands you the peer's half to paste on the other box.
#
# Everything a trunk owns is wrapped in markers so removal is exact:
#   ; >>> MESH-TRUNK <name> >>>   ...   ; <<< MESH-TRUNK <name> <<<

MESH_TRUNK_BEGIN = '; >>> MESH-TRUNK %s >>>'
MESH_TRUNK_END = '; <<< MESH-TRUNK %s <<<'
_TRUNK_NAME_RE = re.compile(r'^[a-z][a-z0-9-]{1,30}$')


def _mesh_transport_name():
    """The Tailscale transport this box's mesh trunks ride on."""
    try:
        for ln in PJSIP_CONF.read_text().splitlines():
            if ln.strip().startswith('[') and 'tailscale' in ln.lower():
                return ln.strip()[1:-1]
    except Exception:
        pass
    return 'transport-tailscale'


def _mesh_trunk_pjsip(name, peer_ip, peer_port, in_user, in_pw, out_user, out_pw, codecs):
    tr = _mesh_transport_name()
    allow = '\n'.join('allow=%s' % c for c in codecs)
    return '\n'.join([
        MESH_TRUNK_BEGIN % name,
        '[%s]' % name, 'type=aor',
        'contact=sip:%s:%d' % (peer_ip, peer_port), 'qualify_frequency=30', '',
        '[%s-out-auth]' % name, 'type=auth', 'auth_type=userpass',
        'username=%s' % out_user, 'password=%s' % out_pw, '',
        '[%s-in-auth]' % name, 'type=auth', 'auth_type=userpass',
        'username=%s' % in_user, 'password=%s' % in_pw, '',
        '[%s]' % name, 'type=identify', 'endpoint=%s' % name, 'match=%s' % peer_ip, '',
        '[%s]' % name, 'type=endpoint', 'transport=%s' % tr,
        'context=from-%s' % name, 'disallow=all', allow,
        'auth=%s-in-auth' % name, 'outbound_auth=%s-out-auth' % name,
        'aors=%s' % name, 'direct_media=no', 'rtp_symmetric=yes',
        'force_rport=yes', 'rewrite_contact=yes',
        MESH_TRUNK_END % name, '',
    ])


def _mesh_trunk_dialplan(name, accept_pattern, local_context):
    return '\n'.join([
        MESH_TRUNK_BEGIN % name,
        '[from-%s]' % name,
        'exten => %s,1,NoOp(Inbound from %s: ${EXTEN})' % (accept_pattern, name),
        ' same => n,Goto(%s,${EXTEN},1)' % local_context,
        'exten => _X.,1,Hangup()',
        MESH_TRUNK_END % name, '',
    ])


def _mesh_block_write(path, name, block):
    """Replace (or append) this trunk's marked block. Returns the backup path."""
    try:
        txt = path.read_text()
    except FileNotFoundError:
        txt = ''
    bak = path.with_name(path.name + '.bak-trunk-' +
                         datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    if txt:
        subprocess.run(['cp', '-a', str(path), str(bak)], check=False)
    pat = re.compile(re.escape(MESH_TRUNK_BEGIN % name) + r'.*?' +
                     re.escape(MESH_TRUNK_END % name) + r'\n?', re.S)
    txt = pat.sub('', txt)
    if block:
        txt = txt.rstrip() + '\n\n' + block
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(txt)
    tmp.replace(path)
    return str(bak)


@app.route('/api/mesh/trunks', methods=['POST'])
@login_required
@perm_required('admin')
@feature_required('mesh')
def api_mesh_trunk_add():
    j = request.get_json(silent=True) or {}
    name = str(j.get('name', '')).strip().lower()
    if not _TRUNK_NAME_RE.match(name):
        return jsonify({'error': 'name must be lowercase letters, digits and dashes (2-31 chars)'}), 400
    peer_ip = str(j.get('peer_ip', '')).strip()
    try:
        ipaddress.ip_address(peer_ip)
    except ValueError:
        return jsonify({'error': 'peer_ip must be an IP address'}), 400
    # Mesh trunks ride the tailnet; refusing anything else keeps SIP credentials
    # off unencrypted paths and stops this becoming a way to add a public peer.
    if not (ipaddress.ip_address(peer_ip) in ipaddress.ip_network('100.64.0.0/10')):
        return jsonify({'error': 'peer_ip must be a Tailscale address (100.64.0.0/10)'}), 400
    try:
        peer_port = int(j.get('peer_port') or 5062)
    except Exception:
        return jsonify({'error': 'peer_port must be a number'}), 400
    if not (1 <= peer_port <= 65535):
        return jsonify({'error': 'peer_port out of range'}), 400
    pattern = str(j.get('accept_pattern', '') or '_1XXX').strip()
    if not re.match(r'^[_0-9XZNxzn.!\[\]-]{2,20}$', pattern):
        return jsonify({'error': 'accept_pattern looks wrong (e.g. _1XXX)'}), 400
    local_ctx = str(j.get('local_context', '') or 'from-internal').strip()
    if not re.match(r'^[A-Za-z0-9_-]{1,64}$', local_ctx):
        return jsonify({'error': 'local_context invalid'}), 400
    codecs = [c for c in (j.get('codecs') or ['ulaw', 'alaw', 'opus'])
              if c in ('ulaw', 'alaw', 'opus', 'g722', 'g729')]
    if not codecs:
        codecs = ['ulaw', 'alaw']

    existing = {t['name'] for t in _mesh_trunks()}
    if name in existing and not j.get('replace'):
        return jsonify({'error': 'a trunk called %r already exists' % name}), 409

    # Credentials: generated here unless supplied, and BOTH halves are returned so
    # the same pair can be pasted on the peer (its in/out are our out/in).
    in_user = str(j.get('in_user') or name)[:40]
    out_user = str(j.get('out_user') or ('%s-out' % name))[:40]
    in_pw = str(j.get('in_password') or secrets.token_urlsafe(24))[:64]
    out_pw = str(j.get('out_password') or secrets.token_urlsafe(24))[:64]

    pj_bak = _mesh_block_write(PJSIP_CONF, name, _mesh_trunk_pjsip(
        name, peer_ip, peer_port, in_user, in_pw, out_user, out_pw, codecs))
    dp_bak = _mesh_block_write(EXTENSIONS_CONF, name,
                               _mesh_trunk_dialplan(name, pattern, local_ctx))
    asterisk_cli('module reload res_pjsip.so')
    asterisk_cli('dialplan reload')
    time.sleep(1.5)
    return jsonify({
        'ok': True, 'name': name,
        'backups': {'pjsip': pj_bak, 'dialplan': dp_bak},
        'peer_config': {
            'note': 'Paste these on the OTHER box — its inbound auth is our outbound, and vice versa.',
            'trunk_name_suggestion': (SITE.get('site_id') or 'home') + '-trunk',
            'peer_ip_to_use': j.get('our_ip') or '',
            'in_user': out_user, 'in_password': out_pw,
            'out_user': in_user, 'out_password': in_pw,
        },
        'trunks': _mesh_trunks(),
    })


@app.route('/api/mesh/trunks/<name>', methods=['DELETE'])
@login_required
@perm_required('admin')
@feature_required('mesh')
def api_mesh_trunk_del(name):
    name = str(name).strip().lower()
    if not _TRUNK_NAME_RE.match(name):
        return jsonify({'error': 'bad trunk name'}), 400
    if name not in {t['name'] for t in _mesh_trunks()}:
        return jsonify({'error': 'no such inter-PBX trunk'}), 404
    # Refuse while the IVR still points at it, otherwise callers pressing that
    # option fall through to the local agent with no explanation.
    cfg = _mesh_load()
    used = [o.get('key') for o in cfg.get('options', [])
            if (o.get('target') or {}).get('trunk') == name]
    if used and not request.args.get('force'):
        return jsonify({'error': 'menu option(s) %s still route to this trunk — '
                                 'repoint them first, or pass ?force=1'
                                 % ', '.join(map(str, used))}), 409
    pj_bak = _mesh_block_write(PJSIP_CONF, name, '')
    dp_bak = _mesh_block_write(EXTENSIONS_CONF, name, '')
    asterisk_cli('module reload res_pjsip.so')
    asterisk_cli('dialplan reload')
    time.sleep(1.5)
    return jsonify({'ok': True, 'removed': name,
                    'backups': {'pjsip': pj_bak, 'dialplan': dp_bak},
                    'trunks': _mesh_trunks()})


if __name__ == '__main__':
    _host, _port = _bind_address()
    print('[site] %s (%s) on %s:%d — dashboards=%s features_off=%s' % (
        SITE.get('site_id'), FLAVOUR, _host, _port, ','.join(DASHBOARDS) or '-',
        ','.join(sorted(k for k, v in FEATURES.items() if not v)) or '-'), flush=True)
    app.run(host=_host, port=_port, debug=False)
