"""Offline tests: config, journal merging, the journal writer and the HTTP server's access rules.

    python3 -m unittest -v
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("BOT_VIEWER_JOURNAL", None)

import bot_journal  # noqa: E402
import bot_viewer  # noqa: E402

OWNER, GUEST = 100000001, 100000002
DEMO_CONFIG = os.path.join(ROOT, "demo", "config.json")


def write_jsonl(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write((e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)) + "\n")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bot-viewer-test-")
        self.cfg = os.path.join(self.dir, "config.json")

    def test_relative_paths_and_chats(self):
        with open(self.cfg, "w") as f:
            json.dump({"journal": "j/journal.jsonl", "chats": {str(OWNER): {"name": "Owner", "role": "owner"},
                                                             str(GUEST): "Guest", "_note": "x", "bad": "y"},
                       "lang": "ru", "port": 9001}, f)
        c = bot_viewer.load_config(self.cfg)
        self.assertEqual(c["journals"], [os.path.join(self.dir, "j", "journal.jsonl")])
        self.assertEqual(c["chats"], {OWNER: {"name": "Owner", "role": "owner"}, GUEST: {"name": "Guest", "role": ""}})
        self.assertEqual((c["lang"], c["port"]), ("ru", 9001))

    def test_missing_config_and_env_override(self):
        c = bot_viewer.load_config(self.cfg)
        self.assertFalse(c["exists"])
        self.assertEqual(c["journals"], [os.path.join(bot_viewer.HERE, "data", "journal.jsonl")])
        os.environ["BOT_VIEWER_JOURNAL"] = os.pathsep.join(["/a/one.jsonl", "/b/two.jsonl"])
        try:
            self.assertEqual(bot_viewer.load_config(self.cfg)["journals"], ["/a/one.jsonl", "/b/two.jsonl"])
        finally:
            del os.environ["BOT_VIEWER_JOURNAL"]


class BuildTest(unittest.TestCase):
    def test_merge_dedupe_edits_and_files(self):
        d = tempfile.mkdtemp(prefix="bot-viewer-test-")
        pic = os.path.join(d, "p.png")
        with open(pic, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        old, live = os.path.join(d, "old.jsonl"), os.path.join(d, "live.jsonl")
        write_jsonl(old, [
            {"time": "2026-01-10 10:00:00", "dir": "in", "chat_id": OWNER, "message_id": 5, "text": "old"},
            {"time": "2026-01-10 10:01:00", "dir": "out", "chat_id": OWNER, "message_id": 6, "text": "from old"},
        ])
        write_jsonl(live, [
            {"time": "2026-01-10 10:01:00", "dir": "out", "chat_id": OWNER, "message_id": 6, "text": "from live"},
            {"time": "2026-01-11 09:00:00", "dir": "in", "chat_id": GUEST, "message_id": 7, "text": "before",
             "attachments": [{"kind": "photo", "path": "p.png"}, {"kind": "photo", "path": "/no/such/file.jpg"}]},
            {"time": "2026-01-11 09:00:00", "dir": "in", "chat_id": GUEST, "message_id": 7, "text": "after",
             "edited": True, "edit_time": "2026-01-11 09:05:00",
             "attachments": [{"kind": "photo", "path": "p.png"}, {"kind": "photo", "path": "/no/such/file.jpg"}]},
            {"time": "2026-01-11 09:10:00", "dir": "in", "chat_id": 555, "user": "Stranger", "message_id": 8, "text": "?"},
            "garbage", "not json", {"dir": "in", "text": "no chat_id"},
        ])
        cfg = {"journals": [old, live], "chats": {OWNER: {"name": "Owner", "role": "owner"},
                                                  GUEST: {"name": "Guest", "role": ""}, 42: {"name": "Quiet", "role": ""}},
               "bot": "b", "title": "", "lang": "auto"}
        data, files = bot_viewer.build(cfg)
        msgs = data["messages"]
        self.assertEqual([(m["chat_id"], m["message_id"], m.get("text")) for m in msgs],
                         [(OWNER, 5, "old"), (OWNER, 6, "from live"), (GUEST, 7, "after"), (555, 8, "?")])
        g = msgs[2]
        self.assertEqual(g["edited"], "2026-01-11 09:05:00")
        self.assertEqual(g["attachments"][0]["type"], "image")
        self.assertEqual(g["attachments"][0]["path"], pic)              # relative to the journal's folder
        self.assertEqual(files, {g["attachments"][0]["id"]: pic})        # only attachment files are servable
        self.assertTrue(g["attachments"][1]["missing"])
        self.assertEqual([(c["name"], c["count"], c["configured"]) for c in data["chats"]],
                         [("Owner", 2, True), ("Guest", 1, True), ("Quiet", 0, True), ("Stranger", 1, False)])

    def test_demo_data(self):
        cfg = bot_viewer.load_config(DEMO_CONFIG)
        data, files = bot_viewer.build(cfg)
        self.assertGreater(len(data["messages"]), 20)
        atts = [a for m in data["messages"] for a in m.get("attachments", [])]
        self.assertFalse([a for a in atts if a.get("missing")], "demo attachments must exist")
        self.assertEqual(len(files), 3)
        self.assertTrue(next(c for c in data["chats"] if c["id"] < 0)["group"])


class JournalWriterTest(unittest.TestCase):
    def test_text_reply_and_append(self):
        msg = {"message_id": 11, "date": 1760000000, "chat": {"id": OWNER, "type": "private"},
               "from": {"id": OWNER, "first_name": "Ann", "last_name": "Lee"}, "text": "yes",
               "reply_to_message": {"message_id": 10, "text": "Ready?"}}
        e = bot_journal.entry_from_message(msg, "in", via="telegram")
        self.assertEqual({k: e[k] for k in ("dir", "chat_id", "user", "message_id", "text", "reply_to", "reply_quote", "via")},
                         {"dir": "in", "chat_id": OWNER, "user": "Ann Lee", "message_id": 11, "text": "yes",
                          "reply_to": 10, "reply_quote": "Ready?", "via": "telegram"})
        self.assertRegex(e["time"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        path = os.path.join(tempfile.mkdtemp(prefix="bot-viewer-test-"), "sub", "journal.jsonl")
        bot_journal.append(path, e)
        bot_journal.log_message(path, dict(msg, message_id=12, text="и ещё"), "out", silent=True)
        rows = bot_viewer.read_jsonl(path)
        self.assertEqual([(r["message_id"], r["dir"], r["text"]) for r in rows], [(11, "in", "yes"), (12, "out", "и ещё")])
        self.assertTrue(rows[1]["silent"])

    def test_media_forward_edit(self):
        msg = {"message_id": 3, "date": "2026-01-02T03:04:05+00:00", "edit_date": 1760000100,
               "chat": {"id": -100123, "title": "Club"}, "from": {"id": 1, "username": "bob"},
               "caption": "look", "photo": [{"file_id": "s", "file_size": 10}, {"file_id": "l", "file_size": 2000}],
               "forward_origin": {"type": "user", "sender_user": {"id": 2, "first_name": "Carol"}}}
        e = bot_journal.entry_from_message(msg, "in", path="/tmp/l.jpg")
        self.assertEqual((e["user"], e["text"], e["forward"]), ("@bob", "look", "Carol"))
        self.assertEqual(e["attachments"], [{"kind": "photo", "size": 2000, "path": "/tmp/l.jpg"}])
        self.assertTrue(e["edited"])
        doc = bot_journal.entry_from_message({"message_id": 4, "chat": {"id": 1}, "forward_sender_name": "Hidden",
                                              "document": {"file_name": "a.pdf", "file_size": 5}}, "in")
        self.assertEqual((doc["forward"], doc["attachments"]), ("Hidden", [{"kind": "document", "name": "a.pdf", "size": 5}]))
        with self.assertRaises(ValueError):
            bot_journal.entry_from_message(msg, "sideways")


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = bot_viewer.make_server(0, DEMO_CONFIG)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        conn.request(method, path, headers=h)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r, body

    def test_listens_on_loopback_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_page_data_and_files(self):
        r, body = self.request("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))
        r, body = self.request("GET", "/api/data")
        data = json.loads(body)
        self.assertEqual(data["bot"], "trip_helper_bot")
        r, _ = self.request("GET", "/api/data?stamp=" + data["stamp"])
        self.assertEqual(r.status, 200)
        att = next(a for m in data["messages"] for a in m.get("attachments", []) if a.get("id"))
        r, body = self.request("GET", f"/file/{att['id']}/{att['name']}")
        self.assertEqual((r.status, len(body)), (200, att["size"]))
        r, body = self.request("GET", f"/file/{att['id']}", {"Range": "bytes=0-3"})
        self.assertEqual((r.status, len(body)), (206, 4))

    def test_rejects_foreign_hosts_paths_and_origins(self):
        self.assertEqual(self.request("GET", "/api/data", {"Host": "evil.example:80"})[0].status, 403)
        self.assertEqual(self.request("GET", "/file/" + "0" * 20)[0].status, 404)
        self.assertEqual(self.request("GET", "/../../etc/passwd")[0].status, 404)
        self.assertEqual(self.request("GET", "/file/../../etc/passwd")[0].status, 404)
        data = json.loads(self.request("GET", "/api/data")[1])
        fid = next(a["id"] for m in data["messages"] for a in m.get("attachments", []) if a.get("id"))
        self.assertEqual(self.request("POST", f"/open/{fid}")[0].status, 403)                     # no Origin
        self.assertEqual(self.request("POST", f"/open/{fid}", {"Origin": "http://evil.example"})[0].status, 403)
        self.assertEqual(self.request("POST", "/open/" + "0" * 20,
                                      {"Origin": f"http://127.0.0.1:{self.port}"})[0].status, 404)


if __name__ == "__main__":
    unittest.main()
