#!/usr/bin/env python3
"""Naxter PBX Monitor — full admin panel."""
import os, csv, json, subprocess, datetime, re, time, socket, uuid, struct, hashlib, wave
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

BASE = Path('/opt/pbx-monitor')
AUTH_FILE = BASE / 'instance' / 'auth.json'
AMI_FILE = BASE / 'instance' / 'ami.json'
AWS_FILE = BASE / 'instance' / 'aws.json'
TTS_CACHE = Path('/var/lib/asterisk/sounds/custom/tts-cache')
TTS_CACHE_SHARED = Path('/usr/share/asterisk/sounds/custom/tts-cache')
CDR_PATH = Path('/var/log/asterisk/cdr-csv/Master.csv')
LOG_PATH = Path('/var/log/asterisk/messages.log')
EXTENSIONS_CONF = Path('/etc/asterisk/extensions.conf')
PJSIP_CONF = Path('/etc/asterisk/pjsip.conf')
SIP_CAPTURE_DIR = Path('/var/log/sip-capture')
PCAP_PATH = Path('/tmp/sip-monitor.pcap')  # legacy fallback; real capture is in SIP_CAPTURE_DIR

# Shared data tree with the sampath-ai voice-agent bridge. Dashboards whose
# records are REAL (captured from live calls or entered by staff) read/write here.
SAMPATH_DATA = Path(os.environ.get('SAMPATH_DATA_DIR', '/var/lib/sampath-ai'))
BOOKINGS_DIR = SAMPATH_DATA / 'bookings'   # BOOKINGS_DIR/<vertical>/<collection>/<id>.json
REFDATA_DIR = SAMPATH_DATA / 'refdata'     # REFDATA_DIR/<vertical>.json (dummy reference catalog)
ACTIVE_FLOW_FILE = SAMPATH_DATA / 'active-flow.json'   # which flow the voice agent answers as
FLOWS_DIR_PATH = SAMPATH_DATA / 'flows'
OUTBOUND_DIR = SAMPATH_DATA / 'outbound'   # per-call order context for outbound AI confirm-calls


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
VM_ROOT = Path('/var/spool/asterisk/voicemail/default')
RECORDINGS_DIR = Path('/var/spool/asterisk/recordings')
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
DASHBOARDS = {
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
}

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
            'dashboards_registry': DASHBOARDS}


# ==================== HELPERS ====================
def asterisk_cli(cmd, timeout=8):
    try:
        r = subprocess.run(['asterisk', '-rx', cmd], capture_output=True, text=True, timeout=timeout)
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

def read_cdr(n=200):
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

def services_status():
    out = {}
    for svc in ['asterisk', 'cloudflared', 'sip-trunk-route', 'tailscaled', 'pbx-monitor']:
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
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        pw = request.form.get('password', '')
        user = AUTH['users'].get(email)
        ok = isinstance(user, dict) and check_password_hash(user.get('password_hash', ''), pw)
        if ok:
            session['user'] = email
            session.permanent = True
            return redirect(request.args.get('next') or url_for('dashboard'))
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
def webphone_page():
    return render_template('webphone.html', nav='webphone')

@app.route('/calls')
@login_required
def calls():
    return render_template('calls.html', nav='calls')

@app.route('/voicemail')
@login_required
def voicemail_page():
    return render_template('voicemail.html', nav='voicemail')

@app.route('/broadcast')
@login_required
@perm_required('call')
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
DASH_COLLECTIONS = {
    'hospital': {'appointments', 'labs', 'patients'},
    'reservations': {'reservations'},
    'sales': {'orders', 'leads'},
}
_REC_PREFIX = {'appointments': 'AP', 'labs': 'LAB', 'reservations': 'RS', 'orders': 'ORD', 'leads': 'LEAD'}
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
    return BOOKINGS_DIR / vertical / collection


def _new_rec_id(collection):
    import random, string
    stamp = datetime.datetime.now().strftime('%y%m%d')
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{_REC_PREFIX.get(collection, 'REC')}-{stamp}-{rand}"


def _gen_appt_id():
    """Durdans appointment reference (DUR-AP-482917) — same brand/format the voice
    agent uses, so manual and AI-booked appointments look identical."""
    import random
    d = _coll_dir('hospital', 'appointments')
    for _ in range(50):
        rid = f"DUR-AP-{random.randint(100000, 999999)}"
        if not (d / f"{rid}.json").exists():
            return rid
    return f"DUR-AP-{random.randint(100000, 999999)}"


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


def _read_records(vertical, collection):
    d = _coll_dir(vertical, collection)
    out = []
    if d.exists():
        for f in d.glob('*.json'):
            try:
                out.append(json.load(f.open()))
            except Exception:
                continue
    out.sort(key=lambda r: str(r.get('created', '')), reverse=True)
    return out


def _write_record(vertical, collection, rec):
    d = _coll_dir(vertical, collection)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{rec['id']}.json"
    tmp = f.with_suffix('.json.tmp')
    with tmp.open('w') as fh:
        json.dump(rec, fh, indent=2)
    tmp.replace(f)
    return rec


def _load_refdata(vertical):
    try:
        return json.load((REFDATA_DIR / f'{vertical}.json').open())
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
    # POST = staff manually creates a record from the dashboard
    j = request.get_json(force=True) or {}
    if vertical == 'hospital' and collection == 'patients':
        mrn = _upsert_patient(j.get('name'), j.get('phone'),
                              {k: v for k, v in j.items() if k in ('age', 'gender', 'allergies', 'address', 'nic')})
        try:
            return jsonify({'ok': True, 'id': mrn, 'record': json.load((_coll_dir('hospital', 'patients') / f'{mrn}.json').open())})
        except Exception:
            return jsonify({'ok': True, 'id': mrn})
    rid = _gen_appt_id() if (vertical == 'hospital' and collection == 'appointments') else _new_rec_id(collection)
    rec = {k: v for k, v in j.items() if k not in ('id', 'created', 'created_by')}
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
        # Channelling queue number — assigned once the doctor/branch are known.
        rec['queue_no'] = _next_queue_no(rec.get('doctor'), rec.get('branch'), rec.get('date'))
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
    for k, v in patch.items():
        if k in _REC_PATCHABLE:
            rec[k] = v
    rec['updated'] = datetime.datetime.now().isoformat(timespec='seconds')
    rec['updated_by'] = session.get('user')
    _write_record(vertical, collection, rec)
    return jsonify({'ok': True, 'record': rec})


def _place_outbound_call(vertical, collection, rid, kind):
    """Place a REAL outbound AI call about a record — order confirm (sales),
    appointment reminder/confirm, or lab results-ready / critical callback. Writes
    the call context to OUTBOUND_DIR/<uuid>.json, then AMI-originates; on answer the
    [ai-outbound] dialplan bridges to the agent on 9092 with the right persona (by
    'kind'), which records the outcome on the record.
    SAFETY: refdata[vertical].confirm_test_number, if set, overrides the dialled
    number so every call rings that test number instead of the real contact."""
    g = _dash_guard(vertical, collection)
    if g:
        return g
    role = AUTH['users'].get(session['user'], {}).get('role', 'viewer')
    if 'call' not in ROLE_PERMS.get(role, set()):
        return jsonify({'error': 'forbidden — needs call permission'}), 403
    if '/' in rid or '..' in rid:
        return jsonify({'error': 'bad id'}), 400
    f = _coll_dir(vertical, collection) / f'{rid}.json'
    if not f.exists():
        return jsonify({'error': 'record not found'}), 404
    try:
        o = json.load(f.open())
    except Exception:
        return jsonify({'error': 'unreadable record'}), 500
    ref = _load_refdata(vertical)
    phone = re.sub(r'[^0-9+]', '', str(o.get('phone') or ''))
    test_num = re.sub(r'[^0-9+]', '', str(ref.get('confirm_test_number') or ''))
    dial = test_num or phone
    if len(re.sub(r'\D', '', dial)) < 7:
        return jsonify({'error': 'no valid phone number on this record'}), 400
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
        'customer': customer, 'phone': phone, 'summary': summary,
        'total': o.get('total'), 'currency': ref.get('currency', 'Rs'), 'payment': o.get('payment'),
        'created': datetime.datetime.now().isoformat(timespec='seconds'), 'by': session.get('user'),
    }
    try:
        OUTBOUND_DIR.mkdir(parents=True, exist_ok=True)
        tmp = OUTBOUND_DIR / (u + '.json.tmp')
        with tmp.open('w') as fh:
            json.dump(ctx, fh, indent=2)
        tmp.replace(OUTBOUND_DIR / (u + '.json'))
    except Exception as e:
        return jsonify({'error': 'context write failed: %s' % e}), 500
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
        return jsonify({'error': 'originate failed: %s' % e}), 502
    return jsonify({'ok': ok, 'uuid': u, 'dialled': dial, 'test_mode': bool(test_num), 'kind': kind, 'response': r})


@app.route('/api/dash/<vertical>/orders/<rid>/confirm-call', methods=['POST'])
@login_required
def api_dash_confirm_call(vertical, rid):
    return _place_outbound_call(vertical, 'orders', rid, 'order_confirm')


@app.route('/api/dash/hospital/appointments/<rid>/call', methods=['POST'])
@login_required
def api_hosp_appt_call(rid):
    return _place_outbound_call('hospital', 'appointments', rid, 'appt_confirm')


@app.route('/api/dash/hospital/labs/<rid>/call', methods=['POST'])
@login_required
def api_hosp_lab_call(rid):
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
def agent_mode_page():
    return render_template('agent_mode.html', nav='agent-mode')


@app.route('/api/agent-mode')
@login_required
@perm_required('admin')
def api_agent_mode_get():
    active = _active_flow_id()
    modes = [{**m, 'available': (FLOWS_DIR_PATH / (m['flow'] + '.json')).exists(),
              'active': m['flow'] == active} for m in AGENT_MODES]
    return jsonify({'active_flow': active, 'modes': modes})


@app.route('/api/agent-mode', methods=['POST'])
@login_required
@perm_required('admin')
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
def api_config(name):
    if request.method == 'POST' and 'config' not in ROLE_PERMS.get(AUTH['users'][session['user']]['role'], set()):
        return jsonify({'error': 'forbidden'}), 403
    files = {'extensions': EXTENSIONS_CONF, 'pjsip': PJSIP_CONF}
    if name not in files: abort(404)
    path = files[name]
    if request.method == 'POST':
        new = request.json.get('content', '')
        if len(new) < 50: return jsonify({'error': 'content too short'}), 400
        backup = path.with_name(path.name + '.bak-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
        try:
            subprocess.run(['cp', str(path), str(backup)], check=True, timeout=5)
            path.write_text(new)
            return jsonify({'ok': True, 'backup': backup.name})
        except Exception as e: return jsonify({'error': str(e)}), 500
    try: return Response(path.read_text(), mimetype='text/plain')
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
@perm_required('call')
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
def api_webphone_config():
    host = request.host.split(':')[0]
    # Prefer wss:// if the page itself was loaded over HTTPS; mixed-content blocked otherwise
    scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
    ws_scheme = 'wss' if scheme == 'https' else 'ws'
    sip_realm = '192.168.1.132'
    return jsonify({
        'extension': '1010',
        'password': 'WebRtc1010!',
        'sip_uri': f'sip:1010@{sip_realm}',
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

AI_CONFIG_PATH = Path('/opt/sampath-ai/agent-config.json')
AI_SESSIONS_DIR = Path('/var/lib/sampath-ai/sessions')
AI_CUSTOMERS_DIR = Path('/var/lib/sampath-ai/customers')
AI_VOICE_SAMPLE_DIR = Path('/var/lib/sampath-ai/voice-samples')
AI_VOICE_SAMPLE_SCRIPT = '/opt/sampath-ai/voice-sample.cjs'

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
    }
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
                    if ev.get('role') == 'user':
                        out['last_user_text'] = ev.get('text', '')
                    elif ev.get('role') == 'agent':
                        out['last_agent_text'] = ev.get('text', '')
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
def ai_agent_page():
    return render_template('ai_agent.html', nav='ai-agent')


@app.route('/api/ai-agent/config', methods=['GET', 'PUT'])
@login_required
def api_ai_config():
    if request.method == 'PUT':
        if 'config' not in ROLE_PERMS.get(AUTH['users'][session['user']]['role'], set()):
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
def api_ai_voices():
    return jsonify(AI_AVAILABLE_VOICES)


@app.route('/api/ai-agent/voice-test', methods=['POST'])
@login_required
@perm_required('config')
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
def api_ai_voice_test_audio(h):
    safe = re.sub(r'[^0-9a-f]', '', h)[:16]
    p = AI_VOICE_SAMPLE_DIR / f"{safe}.wav"
    if not p.exists():
        abort(404)
    return send_file(str(p), mimetype='audio/wav')


@app.route('/api/ai-agent/sessions')
@login_required
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
def api_ai_restart_bridge():
    try:
        r = subprocess.run(
            ['sudo', '-n', 'systemctl', 'restart', 'sampath-ai'],
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

    rc, out = _run(['sudo', '-n', 'systemctl', 'restart', 'sampath-ai'], timeout=15)
    steps.append({'step': 'sampath-ai', 'rc': rc, 'output': out or 'OK'})

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

FLOWS_DIR = Path('/var/lib/sampath-ai/flows')
ACTIVE_FLOW_POINTER = Path('/var/lib/sampath-ai/active-flow.json')

FLOW_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,62}$')
PHONE_RE = re.compile(r'^[0-9+]{4,18}$')
SAMPATH_ENV_PATH = Path('/opt/sampath-ai/.env')


def _read_env_var(name):
    """Read NAME from /opt/sampath-ai/.env; fall back to process env."""
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
def flows_page():
    return render_template('flows.html', nav='flows')


@app.route('/api/flows')
@login_required
@perm_required('admin')
def api_flows_list():
    return jsonify({
        'flows': _flows_list(),
        'active_id': _read_active_id(),
    })


@app.route('/api/flows/_tool-catalog')
@login_required
@perm_required('admin')
def api_flows_tool_catalog():
    return jsonify(FLOW_TOOL_CATALOG)


@app.route('/api/flows/_voices')
@login_required
@perm_required('admin')
def api_flows_voices():
    return jsonify(AI_AVAILABLE_VOICES)


@app.route('/api/flows/<flow_id>', methods=['GET'])
@login_required
@perm_required('admin')
def api_flow_get(flow_id):
    return jsonify(_flow_load(flow_id))


@app.route('/api/flows/<flow_id>', methods=['PUT'])
@login_required
@perm_required('admin')
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
def customers_page():
    return render_template('customers.html', nav='customers')


@app.route('/api/customers')
@login_required
@perm_required('read')
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

AI_CHANNELS_DIR = Path('/var/lib/sampath-ai/channels')


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
def api_calls_live():
    return jsonify({'calls': _list_live_calls()})


# Live transcript stream is already provided by /api/ai-agent/sessions/<sid>/stream.
# We expose a thin alias at /api/calls/<id>/stream so the new Live Calls page
# uses a stable path.
@app.route('/api/calls/<sid>/stream')
@login_required
@perm_required('read')
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


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5051, debug=False)
