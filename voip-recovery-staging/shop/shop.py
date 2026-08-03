#!/usr/bin/env python3
"""Naxter Store — public storefront for the Sales vertical (shop.easmoney.me).

Shares the product catalog (REFDATA/sales.json) with the voice agent + dashboard,
shows LIVE availability (stock minus quantities already in non-cancelled orders),
and on checkout writes a real order into the SAME store the Sales dashboard reads
(BOOKINGS/sales/orders/<ref>.json) so it appears on the dashboard within seconds
and enters the AI confirmation-call queue.

Public + unauthenticated by design — so all order input is validated and
sanitised server-side (prices/totals are recomputed from the catalog, customer
text is HTML-escaped, quantities are clamped, basic per-IP rate limiting).
"""
import os, json, html, re, time, random, string, datetime, threading
from pathlib import Path
from flask import Flask, request, jsonify, render_template, abort

DATA = Path(os.environ.get('SAMPATH_DATA_DIR', '/var/lib/sampath-ai'))
CATALOG_FILE = DATA / 'refdata' / 'sales.json'
ORDERS_DIR = DATA / 'bookings' / 'sales' / 'orders'

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024  # plenty for an order payload

_lock = threading.Lock()
_rate = {}  # ip -> [timestamps]  (best-effort throttle)


# ----------------------------- catalog / stock -----------------------------
def load_catalog():
    try:
        return json.load(CATALOG_FILE.open())
    except Exception:
        return {'currency': 'Rs', 'store_name': 'Naxter Store', 'products': []}


def _reserved_by_sku():
    """How many of each SKU are already committed in non-cancelled orders."""
    reserved = {}
    if ORDERS_DIR.exists():
        for f in ORDERS_DIR.glob('*.json'):
            try:
                o = json.load(f.open())
            except Exception:
                continue
            if o.get('status') == 'cancelled':
                continue
            for ln in (o.get('lines') or []):
                sku = ln.get('sku')
                if sku:
                    reserved[sku] = reserved.get(sku, 0) + int(ln.get('qty') or 0)
    return reserved


def products_view():
    cat = load_catalog()
    reserved = _reserved_by_sku()
    out = []
    for p in cat.get('products', []):
        avail = max(0, int(p.get('stock', 0)) - reserved.get(p.get('sku'), 0))
        out.append({
            'sku': p.get('sku'), 'name': p.get('name'), 'price': p.get('price', 0),
            'category': p.get('category', ''), 'emoji': p.get('emoji', '📦'),
            'blurb': p.get('blurb', ''), 'available': avail, 'in_stock': avail > 0,
        })
    return cat, out


# ----------------------------- helpers --------------------------------------
def clean(s, n=120):
    s = html.escape(str(s or '').strip())[:n]
    return re.sub(r'[\x00-\x1f\x7f]', '', s)


def new_ref():
    stamp = datetime.datetime.now().strftime('%y%m%d')
    return 'ORD-' + stamp + '-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))


def client_ip():
    return request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()


def rate_ok(ip, limit=6, window=60):
    now = time.time()
    with _lock:
        hits = [t for t in _rate.get(ip, []) if now - t < window]
        if len(hits) >= limit:
            _rate[ip] = hits
            return False
        hits.append(now)
        _rate[ip] = hits
        return True


# ----------------------------- routes ---------------------------------------
@app.route('/')
def store():
    cat = load_catalog()
    return render_template('store.html', store_name=cat.get('store_name', 'Naxter Store'),
                           tagline=cat.get('store_tagline', ''), currency=cat.get('currency', 'Rs'))


@app.route('/healthz')
def healthz():
    return jsonify({'ok': True})


@app.route('/api/products')
def api_products():
    cat, prods = products_view()
    cats = sorted({p['category'] for p in prods if p['category']})
    return jsonify({'currency': cat.get('currency', 'Rs'), 'categories': cats, 'products': prods})


@app.route('/api/order', methods=['POST'])
def api_order():
    if not rate_ok(client_ip()):
        return jsonify({'ok': False, 'error': 'Too many requests — please wait a moment.'}), 429
    j = request.get_json(silent=True) or {}
    name = clean(j.get('customer'), 80)
    phone = clean(j.get('phone'), 24)
    address = clean(j.get('address'), 200)
    payment = 'Card' if str(j.get('payment')).lower() == 'card' else 'COD'
    if not name or len(re.sub(r'\D', '', phone)) < 7:
        return jsonify({'ok': False, 'error': 'A valid name and phone number are required.'}), 400

    items = j.get('items') or []
    if not isinstance(items, list) or not items:
        return jsonify({'ok': False, 'error': 'Your cart is empty.'}), 400

    cat, prods = products_view()
    by_sku = {p['sku']: p for p in prods}
    lines, total, parts = [], 0, []
    for it in items[:30]:
        sku = str((it or {}).get('sku') or '')
        qty = max(1, min(10, int((it or {}).get('qty') or 1)))
        p = by_sku.get(sku)
        if not p:
            return jsonify({'ok': False, 'error': 'An item is no longer available.'}), 400
        if qty > p['available']:
            return jsonify({'ok': False, 'error': f"Sorry, only {p['available']} × {p['name']} left in stock."}), 409
        lines.append({'sku': sku, 'name': p['name'], 'qty': qty, 'price': p['price']})
        total += p['price'] * qty
        parts.append(f"{qty}× {p['name']}")

    ref = new_ref()
    order = {
        'id': ref, 'ref': ref, 'customer': name, 'phone': phone, 'address': address,
        'items': ', '.join(parts), 'lines': lines, 'qty': sum(l['qty'] for l in lines),
        'total': total, 'payment': payment, 'channel': 'Website', 'source': 'Website',
        'status': 'pending', 'paid': False,
        'created': datetime.datetime.now().isoformat(timespec='seconds'), 'created_by': 'website',
    }
    try:
        with _lock:  # serialise the read-check-write so concurrent orders can't oversell
            _, fresh = products_view()
            fresh_by = {p['sku']: p for p in fresh}
            for ln in lines:
                if ln['qty'] > fresh_by.get(ln['sku'], {}).get('available', 0):
                    return jsonify({'ok': False, 'error': f"Sorry, {ln['name']} just sold out."}), 409
            ORDERS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = ORDERS_DIR / (ref + '.json.tmp')
            tmp.write_text(json.dumps(order, indent=2))
            tmp.replace(ORDERS_DIR / (ref + '.json'))
    except Exception as e:
        app.logger.error('order write failed: %s', e)
        return jsonify({'ok': False, 'error': 'Could not place the order, please try again.'}), 500

    return jsonify({'ok': True, 'ref': ref, 'total': total,
                    'message': "Order placed! We'll call you shortly to confirm."})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('SHOP_PORT', '5055')))
