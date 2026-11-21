"""Security tests for live/server.py: start it on the demo fixture and try to get data out of it
the ways an attacker would. Standard library only.

Run: python3 -I live/test_server.py
"""
import glob
import http.client
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time

import tempfile  # noqa: E402

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PORT = 8791
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp()   # never touch the real ~/.local/state
STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "agentwatch")
PAGE = os.path.join(STATE, f"launch-{PORT}.html")
problems = []


def check(ok, what):
    if not ok:
        problems.append(what)


def get(path, headers=None, host=f"127.0.0.1:{PORT}", read=True):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    h = {"Host": host}
    h.update(headers or {})
    c.request("GET", path, headers=h)
    r = c.getresponse()
    body = r.read() if read and r.getheader("Content-Type", "") != "text/event-stream" else b""
    return r, body, c


def start():
    out = open(os.path.join(STATE + "-test-out.txt"), "w")
    proc = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "live", "server.py"), "--fixture",
                             os.path.join(ROOT, "fixtures", "demo"), "--speed", "50", "--port", str(PORT), "--no-open"],
                            stdout=out, stderr=subprocess.STDOUT)
    for _ in range(40):
        time.sleep(0.25)
        if os.path.exists(PAGE):
            return proc, re.search(r"#token=([A-Za-z0-9_\-]+)", open(PAGE).read()).group(1)
    proc.kill()
    raise SystemExit("server didn't start")


proc, key = start()
try:
    # The key: long, random, private, and never on a command line.
    check(len(key) >= 40, f"key too short: {len(key)}")
    mode = stat.S_IMODE(os.stat(PAGE).st_mode)
    check(mode == 0o600, f"launch page mode {oct(mode)}, want 0o600")
    check(stat.S_IMODE(os.stat(STATE).st_mode) == 0o700, "state folder isn't 0700")
    ps = subprocess.run(["ps", "-ax", "-o", "args="], capture_output=True, text=True).stdout
    check(key not in ps, "the key shows up in the process list")

    check(key not in open(STATE + "-test-out.txt").read(), "the key is printed in the server's output")
    for method, want in (("POST", 405), ("HEAD", 501), ("PUT", 501), ("DELETE", 501)):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        c.request(method, f"/api/sessions?token={key}", headers={"Host": f"127.0.0.1:{PORT}"})
        r = c.getresponse()
        check(r.status == want and "default-src 'none'" in r.getheader("Content-Security-Policy", ""),
              f"{method} -> {r.status} (want {want}), CSP {bool(r.getheader('Content-Security-Policy'))}")
        r.read()

    # Static files: served, with security headers, and nothing outside player/.
    r, body, _ = get("/")
    check(r.status == 200 and b"player.js" in body, f"index: {r.status}")
    csp = r.getheader("Content-Security-Policy", "")
    for part in ("default-src 'none'", "script-src 'self'", "connect-src 'self'", "frame-ancestors 'none'"):
        check(part in csp, f"CSP lacks {part!r}")
    for h, v in (("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
                 ("Cross-Origin-Resource-Policy", "same-origin"), ("Cache-Control", "no-store")):
        check(r.getheader(h) == v, f"header {h} = {r.getheader(h)!r}")
    check("Python" not in (r.getheader("Server") or ""), f"Server header leaks the runtime: {r.getheader('Server')}")
    check(key.encode() not in body, "the key is in the page")
    for path in ("/../live/server.py", "/%2e%2e/live/server.py", "/..%2flive/server.py", "/player/../live/parse.py",
                 "/fixtures/demo/x", "/.git/config", "//etc/passwd", "/live/server.py"):
        r, _, _ = get(path)
        check(r.status == 404, f"{path} -> {r.status}, want 404")
    r, _, _ = get("/player.js")
    check(r.status == 200 and "javascript" in r.getheader("Content-Type", ""), "player.js not served")

    # Health check: open, but says nothing about sessions.
    r, body, _ = get("/api/health")
    check(r.status == 200 and json.loads(body) == {"app": "agentwatch", "ok": True}, f"health: {r.status} {body[:80]}")

    # Data needs the key...
    for path in ("/api/sessions", "/api/sessions?token=", "/api/sessions?token=wrong", f"/api/sessions?token={key}x",
                 "/api/stream?session=5e7d3a2c-1f4b-4c8e-9a6d-2b0c8e4f7a10"):
        r, body, _ = get(path)
        check(r.status == 401, f"{path[:40]} -> {r.status}, want 401")
        check(b"5e7d3a2c" not in body, f"{path[:40]} leaked data")
    r, body, _ = get(f"/api/sessions?token={key}")
    check(r.status == 200 and json.loads(body)[0]["id"] == "5e7d3a2c-1f4b-4c8e-9a6d-2b0c8e4f7a10", f"sessions with key: {r.status}")

    # ...and even with the key, other hosts and other sites are refused.
    for headers, host, why in (({}, f"evil.example:{PORT}", "DNS rebinding Host"), ({}, "127.0.0.1", "Host without port"),
                               ({"Origin": "https://evil.example"}, f"127.0.0.1:{PORT}", "foreign Origin"),
                               ({"Origin": "null"}, f"127.0.0.1:{PORT}", "null Origin"),
                               ({"Sec-Fetch-Site": "cross-site"}, f"127.0.0.1:{PORT}", "cross-site fetch"),
                               ({"Sec-Fetch-Site": "same-site"}, f"127.0.0.1:{PORT}", "same-site fetch")):
        r, body, _ = get(f"/api/sessions?token={key}", headers, host)
        check(r.status == 403 and b"5e7d3a2c" not in body, f"{why}: {r.status}, want 403")
    r, _, _ = get(f"/api/sessions?token={key}", {"Origin": f"http://127.0.0.1:{PORT}", "Sec-Fetch-Site": "same-origin"})
    check(r.status == 200, f"same-origin request refused: {r.status}")

    # The stream: valid events, redacted, and a cap on how many can be open.
    r, _, c = get(f"/api/stream?session=5e7d3a2c-1f4b-4c8e-9a6d-2b0c8e4f7a10&token={key}")
    check(r.status == 200 and r.getheader("Content-Type") == "text/event-stream", f"stream: {r.status}")
    first = r.fp.readline().decode()
    check(first.startswith("data: ") and json.loads(first[6:])["type"] == "hello", f"first event: {first[:60]}")
    c.close()
    r, _, _ = get(f"/api/stream?session=../../etc/passwd&token={key}")
    check(r.status == 404, f"odd session id -> {r.status}")
    # Cap on open streams (16). A closed client can hold its slot until the next ping, so count
    # instead of assuming: of 17 more streams, at most 16 may open and at least one must be refused.
    socks, codes = [], []
    for _ in range(17):
        s = socket.create_connection(("127.0.0.1", PORT))
        s.settimeout(5)
        s.sendall(f"GET /api/stream?session=5e7d3a2c-1f4b-4c8e-9a6d-2b0c8e4f7a10&token={key} HTTP/1.1\r\n"
                  f"Host: 127.0.0.1:{PORT}\r\n\r\n".encode())
        socks.append(s)
        codes.append(s.recv(64).split(b" ")[1])
    check(codes.count(b"200") <= 16 and b"503" in codes, f"stream cap: {codes.count(b'200')} opened, 503s: {codes.count(b'503')}")
    for s in socks:
        s.close()
finally:
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)
time.sleep(0.2)
check(not os.path.exists(PAGE), "launch page left behind after stop")

# A new start makes a new key.
proc2, key2 = start()
proc2.send_signal(signal.SIGTERM)
proc2.wait(timeout=5)
check(key2 != key, "the key didn't change between starts")

# ---- Only open windows: the registry reader ----
import shutil  # noqa: E402
import tempfile  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "live"))
import server  # noqa: E402

SID = "5e7d3a2c-1f4b-4c8e-9a6d-2b0c8e4f7a10"
reg = tempfile.mkdtemp()
dead = subprocess.Popen(["true"]); dead.wait()        # a pid that no longer runs
entries = {
    "live.json": {"pid": os.getpid(), "sessionId": SID, "name": "win-1", "status": "busy"},
    "dead.json": {"pid": dead.pid, "sessionId": "11111111-1111-4111-8111-111111111111", "status": "idle"},
    "badsid.json": {"pid": os.getpid(), "sessionId": "../../etc/passwd"},
    "root.json": {"pid": 1, "sessionId": "22222222-2222-4222-8222-222222222222"},  # not your process
    "123.abc.key": {"pid": os.getpid(), "sessionId": "33333333-3333-4333-8333-333333333333"},  # must never be read
}
for name, d in entries.items():
    with open(os.path.join(reg, name), "w") as f:
        json.dump(d, f)
with open(os.path.join(reg, "broken.json"), "w") as f:
    f.write("{not json")
with open(os.path.join(reg, "huge.json"), "w") as f:
    f.write(json.dumps({"pid": os.getpid(), "sessionId": "44444444-4444-4444-8444-444444444444", "pad": "x" * 70000}))
server.REGISTRY = reg
got = server.open_sessions()
check(server.Handler.timeout == 60, f"idle connection timeout is {server.Handler.timeout}")
check(set(got) == {SID} and got[SID] == {"name": "win-1", "busy": True}, f"open_sessions -> {got}")
shutil.rmtree(reg)

# ---- No history: a real (non-fixture) server, on a fake home folder ----
home = tempfile.mkdtemp()
proj = os.path.join(home, ".claude", "projects", "-home-dev-wordfreq")
os.makedirs(proj)
os.makedirs(os.path.join(home, ".claude", "sessions"))
lines = open(os.path.join(ROOT, "fixtures", "demo", SID + ".jsonl")).read().splitlines()
cut = max(i for i, l in enumerate(lines) if '"id": "tu15"' in l)       # stop mid-command: the last push is running
log = os.path.join(proj, SID + ".jsonl")
with open(log, "w") as f:
    f.write("\n".join(lines[:cut + 1]) + "\n")
with open(os.path.join(home, ".claude", "sessions", f"{os.getpid()}.json"), "w") as f:
    json.dump({"pid": os.getpid(), "sessionId": SID, "name": "win-1", "status": "busy"}, f)
LIVE_PORT = 8786
env = dict(os.environ, HOME=home, XDG_STATE_HOME=os.path.join(home, "state"))
proc3 = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "live", "server.py"), "--port", str(LIVE_PORT), "--no-open"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
page3 = os.path.join(home, "state", "agentwatch", f"launch-{LIVE_PORT}.html")
try:
    for _ in range(40):
        time.sleep(0.25)
        if os.path.exists(page3):
            break
    key3 = re.search(r"#token=([A-Za-z0-9_\-]+)", open(page3).read()).group(1)
    c = http.client.HTTPConnection("127.0.0.1", LIVE_PORT, timeout=10)
    c.request("GET", f"/api/sessions?token={key3}", headers={"Host": f"127.0.0.1:{LIVE_PORT}"})
    listed = json.loads(c.getresponse().read())
    check([(x["id"], x["status"], x["window"]) for x in listed] == [(SID, "working", "win-1")], f"open windows: {listed}")
    c = http.client.HTTPConnection("127.0.0.1", LIVE_PORT, timeout=10)
    c.request("GET", f"/api/stream?session={SID}&token={key3}", headers={"Host": f"127.0.0.1:{LIVE_PORT}"})
    r = c.getresponse()
    events = []
    while True:
        line = r.fp.readline().decode()
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
            if events[-1]["type"] == "ready":
                break
    shown = [(e["step"]["kind"], e["step"]["state"], e["step"]["terminal"]["command"]) for e in events if e["type"] == "step"]
    check(shown == [("terminal", "running", "git push origin main")], f"backlog should be only the running step: {shown}")
    with open(log, "a") as f:                       # the command finishes now
        f.write("\n".join(lines[cut + 1:cut + 2]) + "\n")
    after, t0 = [], time.time()
    while time.time() - t0 < 5 and not any(e["type"] == "update" for e in after):
        line = r.fp.readline().decode()
        if line.startswith("data: "):
            after.append(json.loads(line[6:]))
    upd = [e for e in after if e["type"] == "update"]
    check(upd and upd[0]["step"]["state"] == "done" and upd[0]["step"]["terminal"]["exit_code"] == 1,
          f"live update after the result was written: {[e['type'] for e in after]}")
    c.close()
finally:
    proc3.send_signal(signal.SIGTERM)
    proc3.wait(timeout=5)
    shutil.rmtree(home)

# ---- It stops itself when the last viewer tab closes, but not on a reload, and not with an open stream
def launch(port, *extra):
    page = os.path.join(STATE, f"launch-{port}.html")
    proc = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "live", "server.py"), "--fixture",
                             os.path.join(ROOT, "fixtures", "demo"), "--speed", "50", "--port", str(port), "--no-open", *extra],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(0.25)
        if os.path.exists(page):
            break
    return proc, re.search(r"#token=([A-Za-z0-9_\-]+)", open(page).read()).group(1), page


def call(port, method, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": f"127.0.0.1:{port}"}
    h.update(headers or {})
    c.request(method, path, headers=h)
    r = c.getresponse()
    r.read()
    return r.status


def stopped_within(proc, secs):
    try:
        proc.wait(timeout=secs)
        return True
    except subprocess.TimeoutExpired:
        proc.kill()
        return False


A = "5e7d3a2c-1f4b-4c8e-9a6d-2b0c8e4f7a10"
proc5, k5, page5 = launch(8783)
call(8783, "GET", f"/api/sessions?token={k5}&tab=tabAAAAAAAA")                     # a viewer tab opens
check(call(8783, "POST", f"/api/bye?token=wrong&tab=tabAAAAAAAA") == 401, "goodbye without the key wasn't refused")
check(call(8783, "POST", f"/api/bye?token={k5}&tab=tabAAAAAAAA", {"Origin": "https://evil.example"}) == 403,
      "goodbye from another site wasn't refused")
check(call(8783, "GET", f"/api/bye?token={k5}&tab=tabAAAAAAAA") == 404, "goodbye works over GET")
check(call(8783, "POST", f"/api/bye?token={k5}&tab=tabAAAAAAAA") == 204, "goodbye refused")  # a reload: it leaves...
time.sleep(1.5)
call(8783, "GET", f"/api/sessions?token={k5}&tab=tabBBBBBBBB")                     # ...and comes back
time.sleep(6.5)
check(proc5.poll() is None, "it stopped during a page reload")
check(call(8783, "POST", f"/api/bye?token={k5}&tab=tabBBBBBBBB") == 204, "second goodbye refused")  # the last tab closes
check(stopped_within(proc5, 12), "it didn't stop after the last tab closed")
check(not os.path.exists(page5), "launch page left behind after stopping on its own")

proc6, k6, _ = launch(8782)                                                         # a tab with only its stream open
s6 = socket.create_connection(("127.0.0.1", 8782))
s6.sendall(f"GET /api/stream?session={A}&token={k6}&tab=tabCCCCCCCC HTTP/1.1\r\nHost: 127.0.0.1:8782\r\n\r\n".encode())
s6.settimeout(5)
s6.recv(64)
time.sleep(8)
check(proc6.poll() is None, "it stopped while a tab still had its stream open")
call(8782, "POST", f"/api/bye?token={k6}&tab=tabCCCCCCCC")
s6.close()
check(stopped_within(proc6, 12), "it didn't stop after the streaming tab said goodbye")

proc7, k7, _ = launch(8781, "--no-auto-stop")
call(8781, "GET", f"/api/sessions?token={k7}&tab=tabDDDDDDDD")
call(8781, "POST", f"/api/bye?token={k7}&tab=tabDDDDDDDD")
time.sleep(7)
check(proc7.poll() is None, "--no-auto-stop didn't keep it running")
proc7.send_signal(signal.SIGTERM)
proc7.wait(timeout=5)

# ---- Open Codex windows: the logs a running codex process holds open; subagents stay inside their parent
CMAIN = "019b2c3d-4e5f-7a6b-8c7d-9e0f1a2b3c4d"
fake = tempfile.mkdtemp()
day = os.path.join(fake, "sessions", "2026", "01", "15")
os.makedirs(day)
main_log = shutil.copy(glob.glob(os.path.join(ROOT, "fixtures", "demo-codex", "rollout-*.jsonl"))[0], day)
child_log = shutil.copy(glob.glob(os.path.join(ROOT, "fixtures", "demo-codex", "sub", "rollout-*.jsonl"))[0], day)
bindir = tempfile.mkdtemp()
shutil.copy("/bin/sleep", os.path.join(bindir, "codex"))                 # looks like Codex from outside
holders = [subprocess.Popen([os.path.join(bindir, "codex"), "30"], stdin=open(x)) for x in (main_log, child_log)]
time.sleep(0.8)
server.CODEX = os.path.join(fake, "sessions")
server.CODEX_CACHE["at"] = -1e9
got = server.open_codex_sessions()
check(set(got) == {CMAIN}, f"open Codex windows: {got}")
for h in holders:
    h.kill()
    h.wait()
for x in (main_log, child_log):                                            # not recently written either
    os.utime(x, (time.time() - 3600, time.time() - 3600))
server.CODEX_CACHE["at"] = -1e9
check(server.open_codex_sessions() == {}, "a Codex log no process holds open still counts as open")
shutil.rmtree(fake)
shutil.rmtree(bindir)

procC = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "live", "server.py"), "--fixture", os.path.join(ROOT, "fixtures", "demo-codex"),
                          "--speed", "50", "--port", "8779", "--no-open", "--no-auto-stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    pageC = os.path.join(STATE, "launch-8779.html")
    for _ in range(40):
        time.sleep(0.25)
        if os.path.exists(pageC):
            break
    kC = re.search(r"#token=([A-Za-z0-9_\-]+)", open(pageC).read()).group(1)
    c = http.client.HTTPConnection("127.0.0.1", 8779, timeout=5)
    c.request("GET", f"/api/sessions?token={kC}", headers={"Host": "127.0.0.1:8779"})
    listed = json.loads(c.getresponse().read())
    check([(x["id"], x["tool"]) for x in listed] == [(CMAIN, "codex")], f"codex fixture sessions: {listed}")
    c = http.client.HTTPConnection("127.0.0.1", 8779, timeout=5)
    c.request("GET", f"/api/stream?session={CMAIN}&token={kC}", headers={"Host": "127.0.0.1:8779"})
    rC = c.getresponse()                      # keep the response: dropping it closes the stream
    first = json.loads(rC.fp.readline().decode()[6:])
    check(first["type"] == "hello" and first["session"]["tool"] == "codex", f"codex hello: {first}")
    c.close()
finally:
    procC.terminate()
    procC.wait(timeout=5)

# ---- A state folder that's a symlink is refused (someone could point it at their own folder)
evil_home = tempfile.mkdtemp()
os.makedirs(os.path.join(evil_home, "elsewhere"))
os.symlink(os.path.join(evil_home, "elsewhere"), os.path.join(evil_home, "agentwatch"))
r4 = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "live", "server.py"), "--fixture", os.path.join(ROOT, "fixtures", "demo"),
                     "--port", "8784", "--no-open"], env=dict(os.environ, XDG_STATE_HOME=evil_home), capture_output=True, text=True, timeout=20)
check(r4.returncode != 0 and "refusing" in r4.stderr + r4.stdout and not os.listdir(os.path.join(evil_home, "elsewhere")),
      f"symlinked state folder: exit {r4.returncode}, {(r4.stderr + r4.stdout)[-120:]!r}")
shutil.rmtree(evil_home)

print("ok" if not problems else "PROBLEMS:\n - " + "\n - ".join(problems))
sys.exit(1 if problems else 0)
