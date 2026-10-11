#!/usr/bin/env python3
"""bot-viewer: a local, read-only viewer for the message journal of a Telegram bot.

  bot_viewer.py                    open the viewer in the browser (starts the server in the background)
  bot_viewer.py --serve            run the server in the foreground (debugging)
  bot_viewer.py --stop             stop the background server
  bot_viewer.py --check            load the data, print a summary and exit
  bot_viewer.py --config PATH ...  use another config file (default: config.json next to this script)
  bot_viewer.py --no-browser       start the server and print the URL without opening a browser

The Telegram Bot API has no method to fetch chat history, so the bot itself appends every
incoming and outgoing message to a JSON Lines journal (see bot_journal.py and the README).
This viewer only reads that journal: no network access, no Bot API calls, no bot token.

The server listens on 127.0.0.1 only, rejects foreign Host/Origin headers, serves only files
that are referenced as attachments in the journal and exits after IDLE_EXIT seconds without
an open page. Python 3.8+, standard library only.
"""
import hashlib
import http.server
import json
import mimetypes
import os
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "viewer.html")
CONFIG = os.path.abspath(os.path.expanduser(os.environ.get("BOT_VIEWER_CONFIG") or os.path.join(HERE, "config.json")))
RUN_DIR = os.path.expanduser(os.environ.get("BOT_VIEWER_RUN_DIR") or os.path.join(HERE, "run"))
DEFAULT_PORT = 8797
DEFAULT_IDLE_EXIT = 1800        # seconds without page requests before the background server exits
APP_ID = "bot-viewer"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp"}
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".webm"}
AUDIO_EXT = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".flac"}
mimetypes.add_type("audio/ogg", ".oga")
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/ogg", ".opus")
MESSAGE_FIELDS = ("time", "dir", "chat_id", "message_id", "user", "text", "reply_to", "reply_quote",
                  "forward", "via", "silent")


# ── Config and data ─────────────────────────────────────────────────────
def load_config(path=None):
    """Read config.json (a missing file is fine) and return a normalized dict.

    journals: list of absolute paths; on duplicate messages a later journal wins.
    chats: {chat_id: {"name", "role"}}.
    """
    path = path or CONFIG
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}
    base = os.path.dirname(path)

    def resolve(p):
        return os.path.normpath(os.path.join(base, os.path.expanduser(str(p))))

    journals = raw.get("journals") or ([raw["journal"]] if raw.get("journal") else [])
    if isinstance(journals, str):
        journals = [journals]
    env = os.environ.get("BOT_VIEWER_JOURNAL")
    if env:
        journals = [p for p in env.split(os.pathsep) if p]
    journals = [resolve(p) for p in journals] or [os.path.join(HERE, "data", "journal.jsonl")]
    chats = {}
    for k, v in (raw.get("chats") or {}).items():
        if str(k).startswith("_"):
            continue
        v = {"name": v} if isinstance(v, str) else (v if isinstance(v, dict) else {})
        try:
            chats[int(k)] = {"name": str(v.get("name") or k), "role": str(v.get("role") or "")}
        except ValueError:
            continue
    try:
        port = int(os.environ.get("BOT_VIEWER_PORT") or raw.get("port") or DEFAULT_PORT)
        idle = int(os.environ.get("BOT_VIEWER_IDLE_EXIT") or raw.get("idle_exit") or DEFAULT_IDLE_EXIT)
    except (TypeError, ValueError):
        port, idle = DEFAULT_PORT, DEFAULT_IDLE_EXIT
    return {"path": path, "exists": os.path.isfile(path), "journals": journals, "chats": chats,
            "bot": str(raw.get("bot_name") or ""), "title": str(raw.get("title") or ""),
            "lang": str(raw.get("lang") or "auto"), "port": port, "idle_exit": idle}


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("chat_id") is not None and e.get("dir") in ("in", "out"):
                    out.append(e)
    except OSError:
        pass
    return out


def file_stamp(*paths):
    parts = []
    for p in paths:
        try:
            st = os.stat(p)
            parts.append(f"{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append("-")
    return "|".join(parts)


def att_id(path):
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:20]


def build(cfg):
    """Merge the journals into {chats, messages}; also return {attachment id: path}."""
    merged = {}
    for src, journal in enumerate(cfg["journals"]):
        base = os.path.dirname(journal)
        for n, e in enumerate(read_jsonl(journal)):
            e["_base"] = base
            mid = e.get("message_id")
            key = (e["dir"], e["chat_id"], mid, bool(e.get("edited"))) if mid else (src, n)
            merged[key] = e                          # the later line / later journal wins
    msgs, edits = [], {}
    for e in merged.values():
        if e.get("edited") and (e["dir"], e["chat_id"], e.get("message_id"), False) in merged:
            edits[(e["dir"], e["chat_id"], e.get("message_id"))] = e
            continue
        msgs.append(e)
    files, out = {}, []
    for e in sorted(msgs, key=lambda x: (str(x.get("time") or ""), x.get("message_id") or 0)):
        m = {k: e.get(k) for k in MESSAGE_FIELDS if e.get(k) not in (None, "")}
        ed = edits.get((e["dir"], e["chat_id"], e.get("message_id")))
        if ed:
            m["text"], m["edited"] = ed.get("text") or "", ed.get("edit_time") or ed.get("time") or True
            if ed.get("attachments"):
                e = dict(e, attachments=ed["attachments"], _base=ed["_base"])
        elif e.get("edited"):
            m["edited"] = e.get("edit_time") or True
        atts = []
        for a in e.get("attachments") or []:
            if not isinstance(a, dict):
                continue
            x = {k: a.get(k) for k in ("kind", "name", "size", "note") if a.get(k) not in (None, "")}
            path = a.get("path")
            if path:
                path = os.path.normpath(os.path.join(e["_base"], os.path.expanduser(str(path))))
                x["path"] = path
                ext = os.path.splitext(path)[1].lower()
                x["type"] = ("image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT
                             else "audio" if ext in AUDIO_EXT else "file")
                if os.path.isfile(path):
                    fid = att_id(path)
                    files[fid] = path
                    x["id"] = fid
                    x.setdefault("size", os.path.getsize(path))
                else:
                    x["missing"] = True
                x.setdefault("name", os.path.basename(path))
            atts.append(x)
        if atts:
            m["attachments"] = atts
        out.append(m)
    users = cfg["chats"]
    chat_ids = list(users) + [c for c in dict.fromkeys(m["chat_id"] for m in out) if c not in users]
    chats = []
    for cid in chat_ids:
        mine = [m for m in out if m["chat_id"] == cid]
        senders = {m.get("user") for m in mine if m["dir"] == "in" and m.get("user")}
        name = (users.get(cid) or {}).get("name") or next(
            (m["user"] for m in reversed(mine) if m["dir"] == "in" and m.get("user")), str(cid))
        chats.append({"id": cid, "name": name, "role": (users.get(cid) or {}).get("role") or "",
                      "configured": cid in users, "group": len(senders) > 1 or (isinstance(cid, int) and cid < 0),
                      "count": len(mine), "in": sum(m["dir"] == "in" for m in mine)})
    return {"bot": cfg["bot"], "title": cfg["title"], "lang": cfg["lang"], "journals": cfg["journals"],
            "chats": chats, "messages": out, "built": time.strftime("%Y-%m-%d %H:%M:%S")}, files


class Store:
    """Caches the merged data and rebuilds it only when the config or a journal changes."""

    def __init__(self, config_path=None):
        self.config_path = config_path or CONFIG
        self.lock = threading.Lock()
        self.stamp = None
        self.data = None
        self.files = {}     # attachment id -> path: the only files the server will serve

    def get(self):
        cfg = load_config(self.config_path)
        stamp = file_stamp(self.config_path, *cfg["journals"])
        with self.lock:
            if stamp != self.stamp:
                self.data, self.files = build(cfg)
                self.data["stamp"] = self.stamp = stamp
            return self.data

    def path(self, fid):
        self.get()
        with self.lock:
            return self.files.get(fid)


def open_path(path, reveal=False):
    """Open a file with the default app, or show it in the file manager."""
    if sys.platform == "darwin":
        cmd = ["open", "-R", path] if reveal else ["open", path]
    elif os.name == "nt":
        if not reveal:
            return os.startfile(path)    # noqa: S606 - a file from the journal, never from the request
        cmd = ["explorer", "/select,", path]
    else:
        cmd = ["xdg-open", os.path.dirname(path) if reveal else path]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── HTTP ────────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = APP_ID
    store = None         # set by make_server()
    state = None

    def log_message(self, fmt, *args):     # quiet
        pass

    def local_origins(self):
        port = self.server.server_address[1]
        return f"127.0.0.1:{port}", f"localhost:{port}"

    def allowed_host(self):
        """Reject requests whose Host is not ours (DNS rebinding)."""
        return (self.headers.get("Host") or "").lower() in self.local_origins()

    def send_body(self, code, body, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_body(code, body, "application/json; charset=utf-8", [("Cache-Control", "no-store")])

    def deny(self, code=403, text="forbidden"):
        self.send_body(code, text.encode(), "text/plain; charset=utf-8")

    def do_GET(self):
        if not self.allowed_host():
            return self.deny()
        self.state["last_hit"] = time.time()
        url = urllib.parse.urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as f:
                    body = f.read()
            except OSError:
                return self.deny(500, "viewer.html is missing")
            return self.send_body(200, body, "text/html; charset=utf-8", [
                ("Cache-Control", "no-store"), ("X-Frame-Options", "DENY"),
                ("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self'; "
                 "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")])
        if url.path == "/api/ping":
            return self.send_json({"app": APP_ID, "pid": os.getpid(), "config": self.store.config_path})
        if url.path == "/api/data":
            q = urllib.parse.parse_qs(url.query)
            data = self.store.get()
            if q.get("stamp", [None])[0] == data["stamp"]:
                return self.send_json({"unchanged": True, "stamp": data["stamp"]})
            return self.send_json(data)
        m = re.fullmatch(r"/file/([0-9a-f]{20})(?:/.*)?", url.path)
        if m:
            path = self.store.path(m.group(1))
            if not path:
                return self.deny(404, "no such attachment")
            return self.send_file(path)
        return self.deny(404, "not found")

    def do_POST(self):
        """Open an attachment with the default app or reveal it in the file manager."""
        if not self.allowed_host():
            return self.deny()
        origin = self.headers.get("Origin") or ""
        if origin not in tuple("http://" + o for o in self.local_origins()):
            return self.deny()
        self.state["last_hit"] = time.time()
        m = re.fullmatch(r"/(open|reveal)/([0-9a-f]{20})", urllib.parse.urlsplit(self.path).path)
        path = self.store.path(m.group(2)) if m else None
        if not path:
            return self.deny(404, "no such attachment")
        try:
            open_path(path, reveal=m.group(1) == "reveal")
        except OSError as e:
            return self.send_json({"ok": False, "error": str(e)}, 500)
        return self.send_json({"ok": True})

    def send_file(self, path):
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError:
            return self.deny(404, "file not found")
        with f:
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            start, end = 0, size - 1
            rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if rng and (rng.group(1) or rng.group(2)):
                if rng.group(1):
                    start = int(rng.group(1))
                    end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
                else:
                    start = max(0, size - int(rng.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("Content-Disposition",
                             "inline; filename*=UTF-8''" + urllib.parse.quote(os.path.basename(path)))
            self.end_headers()
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    block = f.read(min(1 << 16, left))
                    if not block:
                        break
                    self.wfile.write(block)
                    left -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(port, config_path=None):
    """An HTTP server on 127.0.0.1 (a busy port falls back to any free one)."""
    handler = type("BoundHandler", (Handler,), {"store": Store(config_path), "state": {"last_hit": time.time()}})
    try:
        return Server(("127.0.0.1", port), handler)
    except OSError:
        return Server(("127.0.0.1", 0), handler)


def run_file(config_path):
    """One background server per config file."""
    return os.path.join(RUN_DIR, "server-" + hashlib.sha1(config_path.encode("utf-8")).hexdigest()[:8] + ".json")


def ping(port, timeout=0.7):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def running_port(config_path):
    try:
        with open(run_file(config_path)) as f:
            port = int(json.load(f)["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    info = ping(port)
    return port if info and info.get("app") == APP_ID and info.get("config") == config_path else None


def log(*args):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def serve(config_path):
    cfg = load_config(config_path)
    os.makedirs(RUN_DIR, exist_ok=True)
    httpd = make_server(cfg["port"], config_path)
    port = httpd.server_address[1]
    rf = run_file(config_path)
    with open(rf + ".tmp", "w") as f:
        json.dump({"port": port, "pid": os.getpid(), "config": config_path,
                   "started": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    os.replace(rf + ".tmp", rf)
    log(f"serving http://127.0.0.1:{port}/ (pid {os.getpid()}, config {config_path})")
    state = httpd.RequestHandlerClass.state

    def watchdog():
        while True:
            time.sleep(30)
            if time.time() - state["last_hit"] > cfg["idle_exit"]:
                log("no page open for a while, exiting")
                httpd.shutdown()
                return
    threading.Thread(target=watchdog, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown).start())
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            with open(rf) as f:
                mine = json.load(f).get("pid") == os.getpid()
            if mine:
                with open(rf, "w") as f:
                    json.dump({"port": None, "pid": None, "stopped": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
        except (OSError, ValueError):
            pass


def launch(config_path, open_browser=True):
    port = running_port(config_path)
    if not port:
        os.makedirs(RUN_DIR, exist_ok=True)
        logfile = open(os.path.join(RUN_DIR, "server.log"), "a")
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve", "--config", config_path], cwd=HERE,
                         stdin=subprocess.DEVNULL, stdout=logfile, stderr=logfile, start_new_session=True)
        for _ in range(60):
            time.sleep(0.1)
            port = running_port(config_path)
            if port:
                break
        if not port:
            print(f"the server did not start, see {os.path.join(RUN_DIR, 'server.log')}", file=sys.stderr)
            sys.exit(1)
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    print(url)


def stop(config_path):
    try:
        with open(run_file(config_path)) as f:
            info = json.load(f)
    except (OSError, ValueError):
        info = {}
    p = info.get("port") and ping(info["port"])
    if info.get("pid") and p and p.get("pid") == info["pid"]:
        os.kill(int(info["pid"]), signal.SIGTERM)
        print("stopped")
    else:
        print("not running")


def check(config_path):
    cfg = load_config(config_path)
    if not cfg["exists"]:
        print(f"config: {config_path} (not found; copy config.example.json to config.json)")
    else:
        print(f"config: {config_path}")
    for j in cfg["journals"]:
        print(f"journal: {j} ({'found' if os.path.exists(j) else 'not found'})")
    data = Store(config_path).get()
    for c in data["chats"]:
        print(f"  {c['name']} ({c['id']}): {c['count']} messages, {c['in']} incoming")
    atts = [a for m in data["messages"] for a in m.get("attachments", [])]
    print(f"attachments: {len(atts)}, files found: {sum('id' in a for a in atts)}, "
          f"files missing: {sum(bool(a.get('missing')) for a in atts)}, "
          f"without a file: {sum('path' not in a for a in atts)}")


def main(argv):
    config_path = CONFIG
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 >= len(argv):
            sys.exit("--config needs a path")
        config_path = os.path.abspath(os.path.expanduser(argv[i + 1]))
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
    elif "--serve" in argv:
        serve(config_path)
    elif "--stop" in argv:
        stop(config_path)
    elif "--check" in argv:
        check(config_path)
    else:
        launch(config_path, open_browser="--no-browser" not in argv)


if __name__ == "__main__":
    main(sys.argv[1:])
