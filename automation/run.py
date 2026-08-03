"""
Standalone dev server for the automations engine — for testing the feature in
isolation, with NO auth (local only). Run:

    python run.py            # serves http://127.0.0.1:5099

This is intentionally separate from the main pbx-monitor app.py so the feature
stays independent. When we later integrate, the same engine is wired in with
`init_automations(app, login_required=..., perm_required=...)` instead.
"""
import os

from flask import Flask, jsonify

from automations import init_automations, AUTOMATIONS_DIR


def create_app():
    app = Flask(__name__)
    init_automations(app)                 # no auth decorators -> open, for local testing

    @app.route("/")
    def index():
        return jsonify({
            "service": "naxter-automations (standalone dev server)",
            "storage": str(AUTOMATIONS_DIR),
            "try": [
                "GET    /api/automations/_node-catalog",
                "GET    /api/automations",
                'POST   /api/automations            {"name": "..."}',
                "GET    /api/automations/<id>",
                "PUT    /api/automations/<id>        {nodes, connections}",
                'POST   /api/automations/<id>/run    {"payload": {...}}',
                "GET    /api/automations/<id>/runs",
            ],
            "open": "/automations  (the visual builder)",
        })

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")           # service binds localhost; override for previews
    port = int(os.environ.get("PORT", "5099"))
    debug = os.environ.get("AUTO_DEBUG") == "1"          # off by default (service mode)
    print(f"  automations -> http://{host}:{port}/automations  (storage: {AUTOMATIONS_DIR}, debug={debug})")
    create_app().run(host=host, port=port, debug=debug, threaded=True)
