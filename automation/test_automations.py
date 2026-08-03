"""
Unit + API tests for the automations engine. Run:

    python test_automations.py        # or:  python -m unittest test_automations -v

Uses a throwaway temp data dir so it never touches ./data or any real storage.
"""
import os
import tempfile
import unittest

# Point storage at a temp dir BEFORE importing the module (it mkdirs at import).
os.environ["AUTOMATIONS_DATA_DIR"] = tempfile.mkdtemp(prefix="auto-test-")

import automations as A  # noqa: E402


class ExpressionTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(A._render("Hi {{ $json.name }}", {"name": "Sam"}), "Hi Sam")

    def test_dotted_path(self):
        self.assertEqual(A._render("{{ $json.a.b }}", {"a": {"b": 2}}), "2")

    def test_missing_is_blank(self):
        self.assertEqual(A._render("[{{ $json.nope }}]", {}), "[]")

    def test_non_string_passthrough(self):
        self.assertEqual(A._render(7, {"x": 1}), 7)


class IfTests(unittest.TestCase):
    def test_string_equal(self):
        t, f = A._exec_if({"value1": "{{ $json.s }}", "operator": "equal", "value2": "ok"},
                          [{"s": "ok"}, {"s": "no"}])
        self.assertEqual(t, [{"s": "ok"}])
        self.assertEqual(f, [{"s": "no"}])

    def test_numeric_gt(self):
        t, f = A._exec_if({"value1": "{{ $json.n }}", "operator": "gt", "value2": "10"},
                          [{"n": 20}, {"n": 5}])
        self.assertEqual(t, [{"n": 20}])
        self.assertEqual(f, [{"n": 5}])

    def test_is_empty_and_regex(self):
        t, _ = A._exec_if({"value1": "{{ $json.x }}", "operator": "isEmpty", "value2": ""}, [{"x": ""}])
        self.assertEqual(len(t), 1)
        t2, _ = A._exec_if({"value1": "{{ $json.email }}", "operator": "regex", "value2": r".+@.+"},
                           [{"email": "a@b.com"}])
        self.assertEqual(len(t2), 1)


class SetWaitTests(unittest.TestCase):
    def test_set_add(self):
        out = A._exec_set({"assignments": [{"name": "full", "value": "{{ $json.a }}-x"}]},
                          [{"a": "1"}])[0]
        self.assertEqual(out, [{"a": "1", "full": "1-x"}])

    def test_set_keep_only(self):
        out = A._exec_set({"assignments": [{"name": "x", "value": "1"}], "keepOnlySet": True},
                          [{"a": 1, "b": 2}])[0]
        self.assertEqual(out, [{"x": "1"}])

    def test_wait_cap(self):
        self.assertEqual(A._clamp_wait_seconds({"amount": 2, "unit": "hours"}), 15)
        self.assertEqual(A._clamp_wait_seconds({"amount": 3, "unit": "seconds"}), 3)


class HttpTests(unittest.TestCase):
    def test_no_url(self):
        out = A._exec_http({"method": "GET", "url": ""}, [{}])[0]
        self.assertEqual(out[0]["_http"], {"error": "no url"})

    def test_network_error_is_soft(self):
        # Unreachable port -> soft error inside the item, NOT an exception.
        out = A._exec_http({"method": "GET", "url": "http://127.0.0.1:1/x"}, [{}])[0]
        self.assertIn("error", out[0]["_http"])


class StorageTests(unittest.TestCase):
    def test_create_save_load(self):
        wf = A.create_workflow("My Test Flow!!")
        self.assertTrue(A._ID_RE.match(wf["id"]))
        self.assertEqual(A._wf_load(wf["id"])["name"], "My Test Flow!!")

    def test_validate(self):
        self.assertIsNone(A._wf_validate(
            {"id": "ok-1", "nodes": [{"id": "t", "type": "manualTrigger"}], "connections": {}}))
        self.assertIsNotNone(A._wf_validate(
            {"id": "bad", "nodes": [{"id": "a", "type": "nopeNode"}], "connections": {}}))
        self.assertIsNotNone(A._wf_validate({"id": "Bad ID", "nodes": [], "connections": {}}))


class EngineTests(unittest.TestCase):
    def test_manual_then_set(self):
        wf = {"id": "r1", "nodes": [
            {"id": "t", "type": "manualTrigger", "parameters": {}},
            {"id": "s", "type": "set", "parameters": {"assignments": [{"name": "hello", "value": "world"}]}},
        ], "connections": {"t": {"main": [[{"node": "s", "index": 0}]]}}}
        run = A.run_workflow(wf)
        self.assertEqual(run["status"], "success")
        s = next(n for n in run["node_runs"] if n["node_id"] == "s")
        self.assertEqual(s["output"], [{"hello": "world"}])

    def test_payload_passthrough(self):
        wf = {"id": "r1b", "nodes": [{"id": "t", "type": "manualTrigger", "parameters": {}}],
              "connections": {}}
        run = A.run_workflow(wf, trigger_payload={"email": "x@y.com"})
        self.assertEqual(run["node_runs"][0]["output"], [{"email": "x@y.com"}])

    def test_if_branch_pruning(self):
        wf = {"id": "r2", "nodes": [
            {"id": "t", "type": "manualTrigger", "parameters": {}},
            {"id": "seed", "type": "set", "parameters": {"assignments": [{"name": "s", "value": "ok"}]}},
            {"id": "cond", "type": "if",
             "parameters": {"value1": "{{ $json.s }}", "operator": "equal", "value2": "ok"}},
            {"id": "yes", "type": "set", "parameters": {"assignments": [{"name": "b", "value": "T"}]}},
            {"id": "no", "type": "set", "parameters": {"assignments": [{"name": "b", "value": "F"}]}},
        ], "connections": {
            "t": {"main": [[{"node": "seed", "index": 0}]]},
            "seed": {"main": [[{"node": "cond", "index": 0}]]},
            "cond": {"main": [[{"node": "yes", "index": 0}], [{"node": "no", "index": 0}]]},
        }}
        run = A.run_workflow(wf)
        st = {n["node_id"]: n["status"] for n in run["node_runs"]}
        self.assertEqual(st["yes"], "success")
        self.assertEqual(st["no"], "skipped")     # FALSE branch got no items -> didn't fire

    def test_no_trigger_fails(self):
        run = A.run_workflow({"id": "r3", "nodes": [
            {"id": "s", "type": "set", "parameters": {}}], "connections": {}})
        self.assertEqual(run["status"], "failed")

    def test_hard_error_stops_run(self):
        # A node type with no executor (a not-yet-built type) is a hard error.
        wf = {"id": "r4", "nodes": [
            {"id": "t", "type": "manualTrigger", "parameters": {}},
            {"id": "x", "type": "futureNode", "parameters": {}},
        ], "connections": {"t": {"main": [[{"node": "x", "index": 0}]]}}}
        run = A.run_workflow(wf)
        self.assertEqual(run["status"], "failed")
        self.assertIsNotNone(run["error"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        app = Flask(__name__)
        A.init_automations(app)
        self.c = app.test_client()

    def test_catalog_has_core_nodes(self):
        r = self.c.get("/api/automations/_node-catalog")
        self.assertEqual(r.status_code, 200)
        types = {n["type"] for n in r.get_json()["nodes"]}
        self.assertTrue({"manualTrigger", "httpRequest", "if", "set", "wait"} <= types)

    def test_full_cycle(self):
        # create
        r = self.c.post("/api/automations", json={"name": "Api Flow"})
        self.assertEqual(r.status_code, 201)
        wid = r.get_json()["id"]
        # edit: add a Set node wired to the trigger, then save
        wf = self.c.get(f"/api/automations/{wid}").get_json()
        wf["nodes"].append({"id": "s", "type": "set", "position": {"x": 380, "y": 160},
                            "parameters": {"assignments": [{"name": "ok", "value": "yes"}]}})
        wf["connections"] = {"trigger": {"main": [[{"node": "s", "index": 0}]]}}
        r = self.c.put(f"/api/automations/{wid}", json=wf)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["version"], 2)
        # run
        r = self.c.post(f"/api/automations/{wid}/run", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "success")
        # runs + stats
        self.assertGreaterEqual(len(self.c.get(f"/api/automations/{wid}/runs").get_json()["runs"]), 1)
        lst = self.c.get("/api/automations").get_json()["workflows"]
        mine = next(w for w in lst if w["id"] == wid)
        self.assertEqual(mine["stats"]["total"], 1)
        self.assertEqual(mine["stats"]["succeeded"], 1)

    def test_save_rejects_bad_node(self):
        wid = self.c.post("/api/automations", json={"name": "Bad"}).get_json()["id"]
        wf = self.c.get(f"/api/automations/{wid}").get_json()
        wf["nodes"].append({"id": "z", "type": "totallyFakeNode", "parameters": {}})
        r = self.c.put(f"/api/automations/{wid}", json=wf)
        self.assertEqual(r.status_code, 400)

    def test_versioning_and_restore(self):
        wid = self.c.post("/api/automations", json={"name": "Versioned"}).get_json()["id"]
        wf = self.c.get(f"/api/automations/{wid}").get_json()      # v1 = trigger only
        wf["nodes"].append({"id": "s", "type": "set",
                            "parameters": {"assignments": [{"name": "a", "value": "1"}]}})
        wf["connections"] = {"trigger": {"main": [[{"node": "s", "index": 0}]]}}
        self.c.put(f"/api/automations/{wid}", json=wf)             # -> v2 (snapshots v1)
        wf2 = self.c.get(f"/api/automations/{wid}").get_json()
        wf2["nodes"][1]["parameters"]["assignments"][0]["value"] = "2"
        self.c.put(f"/api/automations/{wid}", json=wf2)            # -> v3 (snapshots v2)
        vers = self.c.get(f"/api/automations/{wid}/versions").get_json()["versions"]
        self.assertGreaterEqual(len(vers), 2)
        # restore v1 (trigger only) -> becomes a new top version (undoable)
        r = self.c.post(f"/api/automations/{wid}/restore/1", json={})
        self.assertEqual(r.status_code, 200)
        restored = r.get_json()
        self.assertEqual(len(restored["nodes"]), 1)
        self.assertGreater(restored["version"], 3)

    def test_export_import(self):
        wid = self.c.post("/api/automations", json={"name": "Exp"}).get_json()["id"]
        ex = self.c.get(f"/api/automations/{wid}/export")
        self.assertEqual(ex.status_code, 200)
        self.assertIn("attachment", ex.headers.get("Content-Disposition", ""))
        obj = ex.get_json()
        obj["name"] = "Imported copy"
        imp = self.c.post("/api/automations/import", json=obj)
        self.assertEqual(imp.status_code, 201)
        self.assertNotEqual(imp.get_json()["id"], wid)            # fresh id
        self.assertFalse(imp.get_json()["active"])                # never auto-active

    def test_builder_page_loads(self):
        r = self.c.get("/automations")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"automations.js", r.data)

    def test_webhook_trigger_fires(self):
        wid = self.c.post("/api/automations", json={"name": "Hook flow"}).get_json()["id"]
        wf = self.c.get(f"/api/automations/{wid}").get_json()
        wf["nodes"] = [
            {"id": "trigger", "type": "webhookTrigger",
             "parameters": {"method": "POST", "responseMode": "usingRespondNode"}},
            {"id": "r", "type": "respondToWebhook",
             "parameters": {"statusCode": 200, "bodyType": "json",
                            "body": '{"hello": "{{ $json.body.name }}"}'}},
        ]
        wf["connections"] = {"trigger": {"main": [[{"node": "r", "index": 0}]]}}
        saved = self.c.put(f"/api/automations/{wid}", json=wf).get_json()
        token = saved["nodes"][0]["parameters"]["path"]          # backend assigned a URL token
        self.assertTrue(token)
        # inactive -> 403
        self.assertEqual(self.c.post(f"/api/automations/hook/{token}", json={"name": "Sam"}).status_code, 403)
        # activate, then fire it
        self.c.post(f"/api/automations/{wid}/activate", json={"active": True})
        r = self.c.post(f"/api/automations/hook/{token}", json={"name": "Sam"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json().get("hello"), "Sam")       # Respond node rendered the body

    def test_webhook_secret(self):
        wid = self.c.post("/api/automations", json={"name": "Secret hook"}).get_json()["id"]
        wf = self.c.get(f"/api/automations/{wid}").get_json()
        wf["nodes"] = [{"id": "trigger", "type": "webhookTrigger",
                        "parameters": {"method": "POST", "auth": "header secret"}}]
        wf["connections"] = {}
        saved = self.c.put(f"/api/automations/{wid}", json=wf).get_json()
        token = saved["nodes"][0]["parameters"]["path"]
        secret = saved["nodes"][0]["parameters"]["secret"]
        self.assertTrue(secret)
        self.c.post(f"/api/automations/{wid}/activate", json={"active": True})
        self.assertEqual(self.c.post(f"/api/automations/hook/{token}").status_code, 401)          # no secret
        ok = self.c.post(f"/api/automations/hook/{token}", headers={"X-Webhook-Secret": secret})
        self.assertEqual(ok.status_code, 200)

    def test_form_trigger(self):
        wid = self.c.post("/api/automations", json={"name": "Form flow"}).get_json()["id"]
        wf = self.c.get(f"/api/automations/{wid}").get_json()
        wf["nodes"] = [{"id": "trigger", "type": "formTrigger",
                        "parameters": {"title": "Contact us",
                                       "fields": [{"label": "Email", "type": "email", "required": True}]}}]
        wf["connections"] = {}
        saved = self.c.put(f"/api/automations/{wid}", json=wf).get_json()
        token = saved["nodes"][0]["parameters"]["path"]
        self.c.post(f"/api/automations/{wid}/activate", json={"active": True})
        page = self.c.get(f"/automations/form/{token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Contact us", page.data)
        sub = self.c.post(f"/automations/form/{token}", data={"Email": "a@b.com"})
        self.assertEqual(sub.status_code, 200)
        self.assertIn(b"Thanks", sub.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
