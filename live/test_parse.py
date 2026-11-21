"""Run the made-up demo session (fixtures/demo) through the parser and check what comes out,
then check the rule flags and the redaction on small made-up inputs.

Run: python3 -I live/test_parse.py   (regenerate the fixture with: python3 -I fixtures/make_demo.py)
"""
import collections
import glob
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse import SECRET_RES, SessionParser, redact, redact_obj  # noqa: E402

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
p = SessionParser(os.path.join(ROOT, "fixtures/demo/5e7d3a2c-1f4b-4c8e-9a6d-2b0c8e4f7a10.jsonl"))
events = p.poll()
kinds = collections.Counter(s["kind"] for s in p.steps)
flags = [(p.steps[e["flag"]["step"]]["kind"], e["flag"]["label"]) for e in events if e["type"] == "flag"]
statuses = [e["status"] for e in events if e["type"] == "status"]

expect = {"prompt": 1, "say": 4, "terminal": 7, "write": 2, "read": 2, "search": 1, "helper_start": 1, "fetch": 1,
          "helper_end": 1, "edit": 2, "other": 1}
problems = []
if dict(kinds) != expect:
    problems.append(f"step kinds {dict(kinds)} != {expect}")
if any(s["state"] != "done" for s in p.steps):
    problems.append("steps left running")
want_flags = [("terminal", "Tests failed"), ("read", "Couldn't read the file"), ("terminal", "Search found nothing"),
              ("fetch", "200 OK, but unreadable"), ("terminal", "Exit code 1"), ("terminal", "Exit code 1"),
              ("terminal", "Same error again")]
if flags != want_flags:
    problems.append(f"flags {flags}")
if statuses != ["working", "done"]:
    problems.append(f"statuses {statuses}")
if p.steps[-1]["kind"] != "say" or not p.steps[-1]["say"]["text"].startswith("Done. The word counter works"):
    problems.append("last step isn't the final answer")
helper = next(s for s in p.steps if s["kind"] == "helper_start")["helper_start"]
if helper["id"] != "agent-a1b2c3d4e5f60789":
    problems.append(f"helper not linked: {helper['id']}")
if [s["agent"] for s in p.steps if s["kind"] == "fetch"] != ["helper:agent-a1b2c3d4e5f60789"]:
    problems.append("the helper's fetch isn't tagged with the helper")
edits = [s["edit"] for s in p.steps if s["kind"] == "edit"]
if [e.get("start_line") for e in edits] != [1, 8] or "+    words = WORD.findall(text.lower())" not in edits[1].get("patch", ""):
    problems.append(f"edit patches {[(e.get('start_line'), e.get('patch', '')[:40]) for e in edits]}")
if p.meta["title"] != "Word counter with tests" or p.meta["cwd"] != "/home/dev/wordfreq":
    problems.append(f"meta {p.meta}")
# Step snapshots: a step event must not change when its result arrives later.
first = next(e for e in events if e["type"] == "step" and e["step"]["kind"] == "terminal")
if first["step"]["state"] != "running":
    problems.append("step event isn't a running snapshot")

# ---- Redaction ----
hidden = {  # each of these must lose its secret
    "mail me at a.b@example.com": "mail me at [redacted]", "key sk-ant-" + "x" * 30: "key [redacted]",
    "sk-proj-" + "a" * 40: "[redacted]", "sk_live_" + "b" * 24: "[redacted]", "ghp_" + "c" * 36: "[redacted]",
    "glpat-" + "d" * 20: "[redacted]", "npm_" + "e" * 36: "[redacted]", "hf_" + "f" * 34: "[redacted]",
    "AIza" + "g" * 35: "[redacted]", "AKIA" + "ABCDEFGHIJKLMNOP": "[redacted]",  # built at runtime so secret scanners don't flag the test
    ".".join(["eyJ" + "h" * 16, "eyJ" + "p" * 20, "s" * 30]): "[redacted]",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456": "Authorization: Bearer [redacted]",
    "git clone https://alice:hunter2pass@host.example/x.git": "git clone https://[redacted]@host.example/x.git",
    "DATABASE_PASSWORD=correcthorsebattery": "DATABASE_PASSWORD=[redacted]",
    '{"access_token": "abcdef123456789"}': '{"access_token": "[redacted]"}',
    "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\nafter": "[redacted private key]\nafter",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl": "[redacted private key]",
}
for raw, want in hidden.items():
    if redact(raw) != want:
        problems.append(f"redaction: {raw[:40]!r} -> {redact(raw)!r}")
more_hidden = {
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF\n-----END PGP PRIVATE KEY BLOCK-----": "[redacted private key]",
    "DB_PASS=hunter2hunter2": "DB_PASS=[redacted]", "MYSQL_PWD=s3cretvalue": "MYSQL_PWD=[redacted]",
    "STRIPE_KEY=abcdef1234567890": "STRIPE_KEY=[redacted]", "AWS_CREDENTIALS: abcdef123456": "AWS_CREDENTIALS: [redacted]",
}
for raw, want in more_hidden.items():
    if redact(raw) != want:
        problems.append(f"redaction: {raw[:40]!r} -> {redact(raw)!r}")
for keep in ("git commit abc1234", "token count: 5", "def get_token():", "see https://docs.python.org/3/",
             "git clone git@github.com:org/repo.git", "PWD=/home/dev/wordfreq", "OLDPWD=/home/dev",
             "Author: Jane Doe", "passive voice", "bypass", "authority: high"):
    if redact(keep) != keep:
        problems.append(f"redaction hid ordinary text: {keep!r}")
ev = {"text": "line one\nuser@example.com", "x": "caf\u00e9 bob@example.org", "n": [1, "c@d.io"]}
try:
    back = json.loads(json.dumps(redact_obj(ev)))  # redacting values can't break the JSON
    if "@" in json.dumps(back):
        problems.append("redact_obj missed an email")
except ValueError:
    problems.append("redact_obj produced invalid JSON")
for blob in ("a" * 300000, "a.b" * 100000, "sk-" * 100000, "a-" * 150000, ("token=" + "z" * 50) * 5000):
    t = time.time()
    for rx, repl in SECRET_RES:
        rx.sub(repl, blob)
    if time.time() - t > 3:
        problems.append(f"redaction is slow on {blob[:8]!r}x{len(blob)}: {time.time() - t:.1f}s")
if len(redact("y" * 200000)) > 60000:
    problems.append("very long strings aren't cut")
from parse import cut_line  # noqa: E402
key = "sk-ant-" + "k" * 90
shown = redact(cut_line("x" * 1950 + " " + key + " tail"))
if "sk-ant" in shown or "kkkk" in shown:
    problems.append(f"a key split by the line cut leaks: {shown[-60:]!r}")

# ---- Memory: the menu keeps no steps; the stream forgets finished ones; half-written lines wait
demo = os.path.join(ROOT, "fixtures/demo/5e7d3a2c-1f4b-4c8e-9a6d-2b0c8e4f7a10.jsonl")
m = SessionParser(demo, meta_only=True)
m.poll()
if m.steps or m.meta["title"] != "Word counter with tests" or m.status != "done":
    problems.append(f"menu mode: {len(m.steps)} steps, title {m.meta['title']!r}, status {m.status}")
lines_ = open(demo).read().splitlines()
cut = max(i for i, l in enumerate(lines_) if '"id": "tu15"' in l)
tmp2 = tempfile.mkdtemp()
part = os.path.join(tmp2, "s.jsonl")
with open(part, "w") as fh:
    fh.write("\n".join(lines_[:cut + 1]) + "\n" + lines_[cut + 1][:40])     # the next line is half-written
q2 = SessionParser(part)
q2.emit = False
q2.poll()
q2.forget_finished()
running = [s for s in q2.steps if s["state"] == "running"]
if len(running) != 1 or running[0]["terminal"].get("command") != "git push origin main":
    problems.append(f"running step after catch-up: {running}")
if any(s[s["kind"]] for s in q2.steps if s["state"] == "done" and s["kind"] != "helper_start"):
    problems.append("finished steps kept their content")
with open(part, "a") as fh:                                                  # the line gets finished
    fh.write(lines_[cut + 1][40:] + "\n")
q2.emit = True
upd = [e for e in q2.poll() if e["type"] == "update"]
if not upd or upd[0]["step"]["terminal"]["exit_code"] != 1:
    problems.append("the half-written line wasn't picked up once finished")
shutil.rmtree(tmp2)
json.dumps(events)  # everything must serialize

# ---- The rule flags, on a small made-up session ----
DENY = ("The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, "
        "the new_string was NOT written to the file). STOP what you are doing and wait for the user to tell you how to proceed.")
calls = [  # (tool, input, result text, is_error)
    ("Bash", {"command": "cd proj && python3 -m pytest -q"},
     "Exit code 1\nFAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed, 3 passed in 0.05s", True),
    ("Bash", {"command": "pytest -q | tail -1"}, "2 failed, 5 passed in 0.10s", False),
    ("Bash", {"command": "npm test"}, "Tests: 5 passed, 5 total", False),
    ("Bash", {"command": "npx tsc --noEmit"}, "Exit code 2\nsrc/a.ts(3,1): error TS2322: Type 'string' is not assignable", True),
    ("Bash", {"command": 'git commit -m "make tests pass"'}, "Exit code 1\nnothing to commit, working tree clean", True),
    ("Bash", {"command": "git push"}, "Exit code 1\n ! [rejected]        main -> main (fetch first)", True),
    ("Bash", {"command": "git push origin main"}, "Exit code 1\n ! [rejected]        main -> main (fetch first)", True),
    ("Edit", {"file_path": "/p/f.py", "old_string": "a = 1", "new_string": "a = 2"}, "The file /p/f.py has been updated.", False),
    ("Edit", {"file_path": "/p/f.py", "old_string": "a = 2", "new_string": "a = 1"}, "The file /p/f.py has been updated.", False),
    ("Bash", {"command": "rm -rf build"}, DENY, True),
    ("Edit", {"file_path": "/p/g.py", "old_string": "x", "new_string": "y"}, DENY, True),
]
recs = [{"type": "user", "uuid": "p0", "timestamp": "2026-11-21T10:00:00.000Z", "message": {"role": "user", "content": "run the tests"}}]
for n, (tool, inp, res, err) in enumerate(calls, 1):
    t = f"2026-11-21T10:{n:02d}:00.000Z"
    recs.append({"type": "assistant", "uuid": f"a{n}", "timestamp": t, "message": {"role": "assistant", "stop_reason": "tool_use",
                 "content": [{"type": "tool_use", "id": f"tu{n}", "name": tool, "input": inp}]}})
    r = {"type": "user", "uuid": f"u{n}", "timestamp": t.replace(":00.000Z", ":30.000Z"),
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu{n}", "content": res, "is_error": err}]}}
    if tool == "Bash" and not err:
        r["toolUseResult"] = {"stdout": res, "stderr": "", "interrupted": False}
    recs.append(r)
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "rules.jsonl")
with open(path, "w") as fh:
    fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
q = SessionParser(path)
got = [(e["flag"]["step"], e["flag"]["level"], e["flag"]["label"]) for e in q.poll() if e["type"] == "flag"]
want = [(1, "error", "Tests failed"), (2, "error", "Tests failed"), (4, "error", "Build failed"), (5, "error", "Exit code 1"),
        (6, "error", "Exit code 1"), (7, "error", "Exit code 1"), (7, "repeat", "Same error again"),
        (9, "repeat", "Undid its last edit"), (10, "quiet", "Stopped by you"), (11, "quiet", "Stopped by you")]
if got != want:
    problems.append(f"rule flags\n   got  {got}\n   want {want}")
shutil.rmtree(tmp)

# ---- Codex: the made-up demo session and its subagent (fixtures/demo-codex)
from parse_codex import CodexParser, command_text, summarize_codex  # noqa: E402

CHILD = "019b2c3d-6a7b-7c8d-9e0f-1a2b3c4d5e6f"
croot = os.path.join(ROOT, "fixtures", "demo-codex")
cpath = glob.glob(os.path.join(croot, "rollout-*.jsonl"))[0]
cp = CodexParser(cpath, root=croot)
cev = cp.poll()
ckinds = dict(collections.Counter(s["kind"] for s in cp.steps))
if ckinds != {"prompt": 1, "say": 5, "terminal": 7, "search": 1, "edit": 1, "write": 1, "helper_start": 1, "helper_end": 1, "other": 1}:
    problems.append(f"codex step kinds {ckinds}")
cflags = [(cp.steps[e["flag"]["step"]]["kind"], e["flag"]["label"]) for e in cev if e["type"] == "flag"]
if cflags != [("terminal", "Tests failed"), ("terminal", "Search found nothing"), ("terminal", "Exit code 1"),
              ("terminal", "Exit code 1"), ("terminal", "Same error again")]:
    problems.append(f"codex flags {cflags}")
if [e["status"] for e in cev if e["type"] == "status"] != ["working", "done"]:
    problems.append("codex statuses")
hs = next(s for s in cp.steps if s["kind"] == "helper_start")
if hs["helper_start"]["id"] != CHILD or hs["state"] != "done" or hs["helper_start"]["description"] != "check_types":
    problems.append(f"codex subagent start {hs}")
kids = [(s["kind"], s.get("helper_end", {}).get("report")) for s in cp.steps if s["agent"] == "helper:" + CHILD]
if kids != [("say", None), ("terminal", None), ("say", None), ("helper_end", "No type errors.")]:
    problems.append(f"codex subagent steps {kids}")
ed = next(s["edit"] for s in cp.steps if s["kind"] == "edit")
if not ed["path"].endswith("auth/tokens.py") or ed["start_line"] != 1 or ed["patch_omitted"] != 5 \
        or "+from datetime import datetime, timedelta, timezone" not in ed["patch"]:
    problems.append(f"codex edit {ed}")
wr = next(s["write"] for s in cp.steps if s["kind"] == "write")
if not wr["path"].endswith("tests/test_expired.py") or not wr["lines"].startswith("from datetime"):
    problems.append(f"codex new file {wr['path']}")
t0 = next(s["terminal"] for s in cp.steps if s["kind"] == "terminal")
if t0["command"] != "python3 -m pytest -q" or t0["cwd"] != "/home/dev/api-server" or t0["exit_code"] != 1:
    problems.append(f"codex terminal {t0['command']!r} {t0['cwd']!r}")
if command_text(["/bin/bash", "-lc", "ls -la"]) != "ls -la" or command_text(["git", "commit", "-m", "a b"]) != "git commit -m 'a b'":
    problems.append("codex command_text")
cm = CodexParser(cpath, root=croot, meta_only=True)
info = summarize_codex(cm)
if cm.steps or info["tool"] != "codex" or info["status"] != "done" or not info["title"].startswith("The login test fails"):
    problems.append(f"codex menu summary {info}")

print("ok" if not problems else "PROBLEMS:\n - " + "\n - ".join(problems))
sys.exit(1 if problems else 0)
