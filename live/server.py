"""Live agent viewer: serves player/ and streams a Claude Code session as it happens.

  python3 -I live/server.py                       # watch the Claude Code windows open right now
  python3 -I live/server.py --fixture fixtures/demo --speed 4   # test: a made-up run, as if live

Security model (see SECURITY.md):
- Listens on 127.0.0.1 only. Requests whose Host header isn't this server's own address are
  refused, so a web page can't reach it through DNS rebinding.
- Every data request needs a random key made fresh at each start. The key is never put on a
  command line: it goes into a launch page in a private folder (0600 file, 0700 folder), which
  hands it to the viewer in the URL fragment; the viewer then sends it with each data request.
  So other programs and other users on the machine can't read your sessions through it.
- Cross-site requests are refused (Origin and Sec-Fetch-Site checks), responses carry a strict
  Content-Security-Policy, and every string is redacted (secrets, emails) before it's sent.

No history: the menu lists only Claude Code windows that are open now (from Claude Code's own
registry of running sessions), and a stream starts at the moment you open it.
"""
import argparse
import copy
import glob
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse import SessionParser, redact_obj, summarize  # noqa: E402
from parse_codex import CODEX_SESSIONS, CodexParser, first_meta, summarize_codex  # noqa: E402

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PLAYER = os.path.realpath(os.path.join(ROOT, "player"))
PROJECTS = os.path.expanduser("~/.claude/projects")
REGISTRY = os.path.expanduser("~/.claude/sessions")   # Claude Code's <pid>.json per open window
CODEX = CODEX_SESSIONS                                  # Codex's rollout-*.jsonl logs
STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "agentwatch")
MAX_STREAMS = 16          # open event streams at once
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), clipboard-read=()",
    "Cache-Control": "no-store",
}
ARGS = None
TOKEN = secrets.token_urlsafe(32)
STREAMS = threading.BoundedSemaphore(MAX_STREAMS)
SUMMARY_CACHE = {}        # path -> main_only SessionParser, kept so each poll only reads new lines
CACHE_LOCK = threading.Lock()


def tz():
    lt = time.localtime()
    return lt.tm_gmtoff // 60, time.strftime("%Z", lt)


def open_sessions():
    """Claude Code windows open right now: entries in Claude Code's registry whose process is alive.
    Reads only the <pid>.json files (never the .key files beside them), and keeps a few fields."""
    found = {}
    for f in glob.glob(os.path.join(REGISTRY, "*.json")):
        try:
            if os.path.getsize(f) > 65536:
                continue
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            pid, sid = d.get("pid"), d.get("sessionId")
            if not isinstance(pid, int) or pid <= 0 or not re.fullmatch(r"[0-9a-f-]{36}", str(sid)):
                continue
            os.kill(pid, 0)                        # is the window still running?
        except (OSError, ValueError, AttributeError):
            continue                               # gone, unreadable, or someone else's process
        name, status = d.get("name"), d.get("status")
        found[sid] = {"name": name if isinstance(name, str) else None, "busy": status == "busy"}
    return found


CODEX_CACHE = {"at": -1e9, "found": {}}


def open_codex_sessions():
    """Codex sessions open right now. Codex keeps no list of open windows, so: the logs its running
    processes hold open (lsof), or, if that finds none, logs written in the last 15 minutes while
    Codex runs. Only fixed commands (pgrep, lsof) are run, never anything from a log."""
    now = time.monotonic()
    if now - CODEX_CACHE["at"] < 3:
        return CODEX_CACHE["found"]
    root, paths, found = os.path.realpath(CODEX), set(), {}
    try:
        pids = [x for x in subprocess.run(["pgrep", "-x", "codex"], capture_output=True, text=True, timeout=2).stdout.split() if x.isdigit()]
    except (OSError, subprocess.SubprocessError):
        pids = []
    if pids:
        try:
            out = subprocess.run(["lsof", "-Fn", "-p", ",".join(pids)], capture_output=True, text=True, timeout=4).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            if line.startswith("n") and line.endswith(".jsonl"):
                path = os.path.realpath(line[1:])
                if path.startswith(root + os.sep) and os.path.basename(path).startswith("rollout-"):
                    paths.add(path)
        if not paths:
            cutoff = time.time() - 15 * 60
            paths = {p for p in glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True) if os.path.getmtime(p) >= cutoff}
    for path in paths:
        meta = first_meta(path)
        if meta and meta.get("thread_source") != "subagent" and re.fullmatch(r"[0-9a-f-]{36}", str(meta.get("id"))):
            found[meta["id"]] = path           # subagents show inside their parent, not as windows
    CODEX_CACHE.update(at=now, found=found)
    return found


def find_session(sid):
    if not re.fullmatch(r"[0-9a-f-]{36}", sid or ""):
        return None
    hits = glob.glob(os.path.join(PROJECTS, "*", sid + ".jsonl"))
    if hits:
        return hits[0]
    if sid in open_codex_sessions():
        return open_codex_sessions()[sid]
    hits = glob.glob(os.path.join(CODEX, "**", f"rollout-*-{sid}.jsonl"), recursive=True)
    return hits[0] if hits else None


def session_id_of(path):
    """Claude Code names a log <id>.jsonl; Codex names it rollout-<time>-<id>.jsonl."""
    name = os.path.basename(path)[:-len(".jsonl")]
    return name[-36:] if name.startswith("rollout-") else name


def parser_for(path, meta_only=False):
    """Codex logs are rollout-*.jsonl; everything else is a Claude Code session log."""
    if os.path.basename(path).startswith("rollout-"):
        return CodexParser(path, root=ARGS.fixture or CODEX, meta_only=meta_only)
    return SessionParser(path, meta_only=meta_only)


def summary(parser):
    if isinstance(parser, CodexParser):
        return summarize_codex(parser)
    return dict(summarize(parser), tool="claude")


def fixture_path():
    hits = glob.glob(os.path.join(ARGS.fixture, "*.jsonl"))
    if len(hits) != 1:
        raise SystemExit(f"fixture folder needs exactly one session .jsonl, found {hits}")
    return hits[0]


def parse_ts(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def retime(obj, fn):
    """Map every timestamp in an event through fn (fixture mode plays an old run as if now)."""
    for key in ("ts", "ts_end", "started"):
        if obj.get(key):
            obj[key] = fn(parse_ts(obj[key])).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    for key in ("step", "session"):
        if isinstance(obj.get(key), dict):
            retime(obj[key], fn)
    return obj


# ---------- open viewer tabs: the server stops itself when the last one closes ----------

TAB_TIMEOUT = 150   # a tab that vanished without saying goodbye (say the browser crashed) counts as gone
BYE_GRACE = 5       # wait this long after the last tab leaves: a page reload says goodbye and comes back
STARTUP_WAIT = 120  # stop if no tab ever shows up
TABS, GONE = {}, set()   # tab id -> last time seen; tabs that said goodbye
TABS_LOCK = threading.Lock()
LIFE = {"seen_any": False, "last_gone": 0.0}


def valid_tab(tab):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,40}", tab or ""))


def touch(tab):
    """A viewer tab is alive (it made a request, or its stream is open)."""
    if not valid_tab(tab):
        return
    with TABS_LOCK:
        if tab not in GONE:
            TABS[tab] = time.monotonic()
            LIFE["seen_any"] = True


def bye(tab):
    with TABS_LOCK:
        GONE.add(tab)
        if TABS.pop(tab, None) is not None and not TABS:
            LIFE["last_gone"] = time.monotonic()


def watchdog(srv):
    start = time.monotonic()
    while True:
        time.sleep(1)
        now = time.monotonic()
        with TABS_LOCK:
            for tab, seen in list(TABS.items()):
                if now - seen > TAB_TIMEOUT:
                    del TABS[tab]
                    if not TABS:
                        LIFE["last_gone"] = now
            done = not TABS and (now - LIFE["last_gone"] > BYE_GRACE if LIFE["seen_any"] else now - start > STARTUP_WAIT)
        if done:
            print("No viewer open any more: stopping.", flush=True)
            srv.shutdown()
            return


# ---------- the private launch page ----------

def private_dir():
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    st = os.lstat(STATE)
    if os.path.islink(STATE) or st.st_uid != os.getuid():
        raise SystemExit(f"refusing to use {STATE}: it's a symlink or not owned by you")
    os.chmod(STATE, 0o700)
    return STATE


def launch_path(port):
    return os.path.join(STATE, f"launch-{port}.html")


def write_launch_page(port):
    """A local page (readable only by you) that opens the viewer with its key in the URL fragment."""
    url = f"http://127.0.0.1:{port}/#token={TOKEN}"
    # location.replace leaves the viewer as the only page in this tab's history, which is what lets the
    # viewer close its own tab later (browsers only allow that for such tabs).
    page = ('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Agent viewer</title>'
            f'<script>location.replace({json.dumps(url)})</script>'
            f'<noscript><meta http-equiv="refresh" content="0;url={url}"></noscript>'
            f'<p><a href="{url}">Open the agent viewer</a></p>\n')
    path = launch_path(port)
    tmp = path + f".{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(page)
    os.replace(tmp, path)
    return path


# ---------- requests ----------

class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        # A viewer going away mid-request is normal: no traceback for it.
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agentwatch"
    timeout = 60                                   # idle connections close; streams ping every 15 s
    sys_version = ""

    def log_message(self, *a):
        pass

    def end_headers(self):
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    def send_body(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def allowed_origin(self):
        origins = {f"http://127.0.0.1:{ARGS.port}", f"http://localhost:{ARGS.port}"}
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        return (origin is None or origin in origins) and (site is None or site in ("same-origin", "none"))

    def authorized(self, query):
        given = parse_qs(query).get("token", [""])[0]
        return hmac.compare_digest(given.encode(), TOKEN.encode())

    def do_GET(self):
        if self.headers.get("Host") not in {f"127.0.0.1:{ARGS.port}", f"localhost:{ARGS.port}"}:
            return self.send_body(403, "forbidden", "text/plain")
        url = urlparse(self.path)
        if url.path == "/api/health":            # no data: lets the launcher tell this server apart
            return self.send_body(200, '{"app": "agentwatch", "ok": true}')
        if url.path.startswith("/api/"):
            if not self.allowed_origin():
                return self.send_body(403, "forbidden", "text/plain")
            if not self.authorized(url.query):
                return self.send_body(401, '{"error": "missing or wrong key: open the viewer with agentwatch"}')
            q = parse_qs(url.query)
            tab = q.get("tab", [""])[0]
            touch(tab)
            if url.path == "/api/sessions":
                return self.send_body(200, json.dumps(redact_obj(self.sessions())))
            if url.path == "/api/stream":
                return self.stream(q.get("session", [""])[0], tab)
            return self.send_body(404, "not found", "text/plain")
        return self.static(url.path)

    def do_POST(self):
        """Only /api/bye: a viewer tab closing (sent with navigator.sendBeacon)."""
        if self.headers.get("Host") not in {f"127.0.0.1:{ARGS.port}", f"localhost:{ARGS.port}"}:
            return self.send_body(403, "forbidden", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > 4096:
            return self.send_body(413, "too large", "text/plain")
        if length:
            self.rfile.read(length)
        url = urlparse(self.path)
        if url.path != "/api/bye":
            return self.send_body(405, "method not allowed", "text/plain")
        if not self.allowed_origin():
            return self.send_body(403, "forbidden", "text/plain")
        if not self.authorized(url.query):
            return self.send_body(401, '{"error": "missing or wrong key"}')
        tab = parse_qs(url.query).get("tab", [""])[0]
        if valid_tab(tab):
            bye(tab)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.realpath(os.path.join(PLAYER, rel))
        ext = os.path.splitext(full)[1]
        if os.path.commonpath([full, PLAYER]) != PLAYER or not os.path.isfile(full) or ext not in TYPES:
            return self.send_body(404, "not found", "text/plain")
        with open(full, "rb") as f:
            self.send_body(200, f.read(), TYPES[ext])

    def sessions(self):
        if ARGS.fixture:
            s = summary(parser_for(fixture_path(), meta_only=True))
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            return [dict(s, status="working", last_activity=now, started=now)]
        out, live, codex = [], open_sessions(), open_codex_sessions()
        with CACHE_LOCK:
            paths = {sid: find_session(sid) for sid in live}
            for p in list(SUMMARY_CACHE):          # forget windows that closed
                if p not in paths.values() and p not in codex.values():
                    del SUMMARY_CACHE[p]
            for sid, p in codex.items():
                if p not in SUMMARY_CACHE:
                    SUMMARY_CACHE[p] = parser_for(p, meta_only=True)
                try:
                    out.append(summary(SUMMARY_CACHE[p]))
                except OSError:
                    continue
            for sid, info in live.items():
                p = paths[sid]
                if not p:                          # a new window has no log until its first message
                    continue
                if p not in SUMMARY_CACHE:
                    SUMMARY_CACHE[p] = SessionParser(p, meta_only=True)
                try:
                    s = summary(SUMMARY_CACHE[p])
                except OSError:
                    continue
                s["status"] = "working" if info["busy"] else "done"
                s["window"] = info["name"]
                out.append(s)
        out.sort(key=lambda s: s["last_activity"] or "", reverse=True)   # newest first,
        return sorted(out, key=lambda s: s["status"] != "working")      # working windows on top

    # ---------- the event stream ----------

    def send_event(self, ev):
        self.wfile.write(b"data: " + json.dumps(redact_obj(ev), ensure_ascii=False).encode() + b"\n\n")
        self.wfile.flush()

    def ping(self):
        self.wfile.write(b": ping\n\n")
        self.wfile.flush()

    def stream(self, sid, tab=""):
        path = fixture_path() if ARGS.fixture else find_session(sid)
        if not path or (ARGS.fixture and session_id_of(path) != sid):
            return self.send_body(404, '{"error": "no such session"}')
        if not STREAMS.acquire(blocking=False):
            return self.send_body(503, '{"error": "too many open viewers"}')
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if ARGS.fixture:
                self.stream_fixture(path, tab)
            else:
                self.stream_live(path, tab)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            STREAMS.release()

    def hello(self, parser, live, fn=None):
        off, name = tz()
        m = parser.meta
        ev = {"type": "hello", "live": live, "session": {
            "id": m["id"], "cwd": m["cwd"], "title": m["name"] or m["title"] or (m["first_prompt"] or "")[:120]
            or ("New Codex session" if isinstance(parser, CodexParser) else ""),
            "tool": "codex" if isinstance(parser, CodexParser) else "claude",
            "started": m["started"], "tz_offset_minutes": off, "tz_name": name}}
        return retime(ev, fn) if fn else ev

    def stream_live(self, path, tab=""):
        # Read the log to know where things stand, but show nothing from before now:
        # only steps still running at this moment (a command mid-run, a helper at work).
        parser = parser_for(path)
        parser.emit = False                        # catching up: nothing from before now is sent
        parser.poll()
        parser.forget_finished()
        parser.emit = True
        self.send_event(self.hello(parser, True))
        for s in parser.steps:
            if s["state"] == "running":
                self.send_event({"type": "step", "history": True, "step": s})
        if parser.status:
            self.send_event({"type": "status", "history": True, "status": parser.status, "ts": parser.meta["last"]})
        self.send_event({"type": "ready"})
        last_ping = time.time()
        while True:
            time.sleep(0.5)
            touch(tab)                             # an open stream keeps its tab alive
            for ev in parser.poll():
                self.send_event(dict(ev, history=False))
            parser.forget_finished()               # sent is sent: don't keep it
            if time.time() - last_ping > 15:
                self.ping()
                last_ping = time.time()

    def stream_fixture(self, path, tab=""):
        """Play a recorded run as if it were happening now, at --speed times real time."""
        parser = parser_for(path)
        timed = []                                 # (original time, event snapshot)
        for d in parser.new_records():
            for ev in parser.handle(d):
                ev = copy.deepcopy(ev)
                body = ev.get("step") or {}
                at = (body.get("ts_end") if ev["type"] == "update" else body.get("ts")) or ev.get("ts") or d.get("timestamp")
                timed.append((at or timed[-1][0], ev))
        if not timed:
            return self.send_event({"type": "ready"})
        t0 = parse_ts(timed[0][0])
        start = datetime.now(timezone.utc)
        speed = max(ARGS.speed, 0.01)
        when = lambda t: start + (t - t0) / speed      # original time -> time it plays
        self.send_event(self.hello(parser, True, lambda t: max(when(t), start)))
        self.send_event({"type": "ready"})
        last_ping = time.time()
        for at, ev in timed:
            # Flags carry no time of their own; they go out right after their step's update.
            due = (parse_ts(at) - t0).total_seconds() / speed
            while (datetime.now(timezone.utc) - start).total_seconds() < due:
                time.sleep(min(0.25, due - (datetime.now(timezone.utc) - start).total_seconds()) if due > 0 else 0)
                touch(tab)
                if time.time() - last_ping > 15:
                    self.ping()
                    last_ping = time.time()
            self.send_event(dict(retime(ev, when), history=False))
        while True:                                # keep the stream open like a finished live session
            for _ in range(30):
                time.sleep(0.5)
                touch(tab)
            self.ping()


def main():
    global ARGS
    ap = argparse.ArgumentParser(description="Live viewer for Claude Code sessions")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--fixture", help="folder with one session .jsonl (and its subagents/) to play as if live")
    ap.add_argument("--speed", type=float, default=1.0, help="fixture playback speed")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--no-auto-stop", action="store_true", help="keep running after the last viewer tab closes")
    ARGS = ap.parse_args()
    os.umask(0o077)                                # anything this process writes is private
    if ARGS.fixture:
        ARGS.fixture = os.path.abspath(ARGS.fixture)
        fixture_path()
    try:
        srv = Server(("127.0.0.1", ARGS.port), Handler)
    except OSError as e:
        raise SystemExit(f"can't listen on 127.0.0.1:{ARGS.port}: {e.strerror}")
    private_dir()
    page = write_launch_page(ARGS.port)
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        if not ARGS.no_auto_stop:
            threading.Thread(target=watchdog, args=(srv,), daemon=True).start()
        print(f"Agent viewer on http://127.0.0.1:{ARGS.port}/ (open it with: agentwatch)"
              + (f"  (fixture {ARGS.fixture} at {ARGS.speed}x)" if ARGS.fixture else ""), flush=True)
        if not ARGS.no_open:
            webbrowser.open("file://" + page)      # the key travels in the file, not on a command line
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try:
            os.remove(page)
        except OSError:
            pass
    # Skip Python's normal shutdown: it stops request threads where they are, and one stopped while
    # writing output makes Python abort (on macOS: a "Python quit unexpectedly" dialog).
    os._exit(0)


if __name__ == "__main__":
    main()
