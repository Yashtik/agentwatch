"""Write fixtures/demo-codex: a made-up Codex session (plus one subagent) in Codex's log format.

Nothing in it comes from a real machine. It covers every step kind the Codex translator draws
and the automatic flags. The subagent's log sits in sub/, the way Codex keeps one log per thread.

Run: python3 -I fixtures/make_demo_codex.py
"""
import json
import os
import shutil
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "demo-codex")
MAIN = "019b2c3d-4e5f-7a6b-8c7d-9e0f1a2b3c4d"
CHILD = "019b2c3d-6a7b-7c8d-9e0f-1a2b3c4d5e6f"
CWD = "/home/dev/api-server"
T0 = datetime(2026, 1, 15, 17, 0, 0, tzinfo=timezone.utc)

TESTS_FAIL = """..F...                                                                   [100%]
=================================== FAILURES ===================================
____________________________ test_login_with_token _____________________________
>       assert is_expired(token) is False
E       TypeError: can't compare offset-naive and offset-aware datetimes
auth/tokens.py:18: TypeError
=========================== short test summary info ============================
FAILED tests/test_login.py::test_login_with_token - TypeError: can't compare offset-naive and of...
1 failed, 5 passed in 0.08s"""
TOKENS = """from datetime import datetime, timedelta


def make_token(user, minutes=30):
    return {"user": user, "expires": datetime.utcnow() + timedelta(minutes=minutes)}


def is_expired(token, now=None):
    now = now or datetime.now().astimezone()
    return token["expires"] <= now
"""
NEW_TEST = '''from datetime import datetime, timedelta, timezone

from auth.tokens import is_expired


def test_expired_token():
    token = {"user": "a", "expires": datetime.now(timezone.utc) - timedelta(minutes=1)}
    assert is_expired(token)
'''
PUSH_REJECTED = """To github.example:dev/api-server.git
 ! [rejected]        main -> main (fetch first)
error: failed to push some refs to 'github.example:dev/api-server.git'"""
RESULTS = [
    {"type": "text_result", "domain": "docs.python.org", "ref_id": "turn0search0", "url": "https://docs.python.org/3/library/datetime.html",
     "title": "datetime — Basic date and time types", "snippet": "datetime.utcnow() is deprecated; use datetime.now(timezone.utc).", "thumbnail_url": None},
    {"type": "text_result", "domain": "docs.python.org", "ref_id": "turn0search1", "url": "https://docs.python.org/3/library/datetime.html#aware-and-naive-objects",
     "title": "Aware and naive objects", "snippet": "Aware objects carry time zone information.", "thumbnail_url": None},
]
FINAL = """The login test passes now, and there's a new test for expired tokens. All 7 tests pass.

What was wrong: tokens stored a naive UTC time (datetime.utcnow()) but were compared with an aware local time, which Python refuses to compare.

What I changed:
- auth/tokens.py now uses datetime.now(timezone.utc) for both, so the comparison is always between aware times.
- tests/test_expired.py checks that a token from a minute ago counts as expired.

A subagent ran the type checker: no errors. I couldn't push: the remote has commits this copy doesn't. Run git pull --rebase, then push."""


class Log:
    def __init__(self, thread):
        self.rows, self.thread = [], thread

    def ts(self, sec):
        return (T0 + timedelta(seconds=sec)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def ms(self, sec):
        return int((T0 + timedelta(seconds=sec)).timestamp() * 1000)

    def rec(self, sec, kind, payload):
        self.rows.append({"timestamp": self.ts(sec), "type": kind, "payload": payload})

    def event(self, sec, payload):
        self.rec(sec, "event_msg", payload)

    def item(self, start, end, item):
        self.event(end, {"type": "item_completed", "thread_id": self.thread, "turn_id": "turn-1", "item": item,
                         "started_at_ms": self.ms(start), "completed_at_ms": self.ms(end)})

    def say(self, sec, text, phase="commentary"):
        self.item(sec - 0.5, sec, {"type": "AgentMessage", "id": f"msg-{sec}", "content": [{"type": "Text", "text": text}], "phase": phase})

    def run(self, start, end, cmd, out, code=0):
        self.item(start, end, {"type": "CommandExecution", "id": f"exec-{start}", "process_id": "4242", "command": ["/bin/bash", "-lc", cmd],
                               "cwd": "file://" + CWD, "parsed_cmd": [{"type": "unknown", "cmd": cmd}], "source": "unified_exec_startup",
                               "status": "completed" if code == 0 else "failed", "aggregated_output": out, "exit_code": code,
                               "duration": {"secs": int(end - start), "nanos": 0}})


def main():
    m, c = Log(MAIN), Log(CHILD)
    m.rec(0, "session_meta", {"id": MAIN, "session_id": MAIN, "timestamp": m.ts(0), "cwd": CWD, "originator": "codex_cli_rs",
                              "cli_version": "0.160.0", "source": "cli", "thread_source": "user", "model_provider": "openai"})
    m.rec(0.2, "turn_context", {"cwd": CWD, "approval_policy": "on-request", "turn_id": "turn-1"})
    m.event(0.3, {"type": "task_started", "turn_id": "turn-1"})
    m.item(0.4, 0.4, {"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text":
           "The login test fails. Fix it, and add a test for expired tokens.", "text_elements": []}]})
    m.say(4, "I'll run the tests first to see the failure.")
    m.run(5, 6.4, "python3 -m pytest -q", TESTS_FAIL, code=1)
    m.say(10, "It's a naive vs aware datetime comparison. Reading the token code.")
    m.run(11, 11.2, "sed -n '1,40p' auth/tokens.py", TOKENS)
    m.item(14, 17.5, {"type": "Extension", "kind": "web.search", "id": "ext-1", "query": "python datetime utcnow deprecated timezone aware",
                      "action": {"type": "search", "query": None, "queries": ["python datetime utcnow deprecated timezone aware"]}, "results": RESULTS})
    m.item(22, 22.3, {"type": "FileChange", "id": "fc-1", "status": "completed", "stdout": "Success. Updated the following files:\nM auth/tokens.py\n",
                      "stderr": "", "changes": {f"{CWD}/auth/tokens.py": {"type": "update", "move_path": None, "unified_diff":
                          "@@ -1,5 +1,5 @@\n-from datetime import datetime, timedelta\n+from datetime import datetime, timedelta, timezone\n \n \n"
                          " def make_token(user, minutes=30):\n-    return {\"user\": user, \"expires\": datetime.utcnow() + timedelta(minutes=minutes)}\n"
                          "+    return {\"user\": user, \"expires\": datetime.now(timezone.utc) + timedelta(minutes=minutes)}\n"
                          "@@ -8,3 +8,3 @@\n def is_expired(token, now=None):\n-    now = now or datetime.now().astimezone()\n+    now = now or datetime.now(timezone.utc)\n"
                          "     return token[\"expires\"] <= now\n"}}})
    m.item(26, 26.2, {"type": "FileChange", "id": "fc-2", "status": "completed", "stdout": "Success. Updated the following files:\nA tests/test_expired.py\n",
                      "stderr": "", "changes": {f"{CWD}/tests/test_expired.py": {"type": "add", "content": NEW_TEST}}})
    m.item(30, 30, {"type": "SubAgentActivity", "id": "call-spawn", "kind": "started", "agent_thread_id": CHILD, "agent_path": "/root/check_types"})
    # The subagent's own log.
    c.rec(30.5, "session_meta", {"id": CHILD, "session_id": MAIN, "timestamp": c.ts(30.5), "cwd": CWD, "originator": "codex_cli_rs",
                                 "cli_version": "0.160.0", "thread_source": "subagent", "parent_thread_id": MAIN, "agent_path": "/root/check_types",
                                 "agent_nickname": "check_types", "source": {"subagent": {"thread_spawn": {"parent_thread_id": MAIN, "depth": 1}}}})
    c.event(31, {"type": "task_started", "turn_id": "turn-c1"})
    c.say(33, "Running the type checker on auth/.")
    c.run(34, 37.5, "mypy auth", "Success: no issues found in 4 source files")
    c.say(39, "No type errors.", "final_answer")
    c.event(39.2, {"type": "task_complete", "turn_id": "turn-c1", "last_agent_message": "No type errors."})
    m.item(40, 40, {"type": "SubAgentActivity", "id": "subagent-completed-1", "kind": "completed", "agent_thread_id": CHILD,
                    "agent_path": "/root/check_types"})
    m.item(40.5, 41, {"type": "CollabAgentToolCall", "id": "call-wait", "tool": "wait", "status": "completed", "sender_thread_id": MAIN,
                      "receiver_thread_ids": [], "receiver_agents": [], "agents_states": {}})
    m.run(44, 45.3, "python3 -m pytest -q", ".......                                                                  [100%]\n7 passed in 0.06s")
    m.run(48, 48.2, "grep -rn 'utcnow' auth | head", "")
    m.run(52, 54, "git add -A && git commit -qm 'Use aware UTC times for tokens' && git push origin main", PUSH_REJECTED, code=1)
    m.run(59, 60.4, "git push origin main", PUSH_REJECTED, code=1)
    m.say(66, FINAL, "final_answer")
    m.event(66.2, {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": FINAL})

    m.rows.sort(key=lambda r: r["timestamp"])
    c.rows.sort(key=lambda r: r["timestamp"])
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(os.path.join(OUT, "sub"))
    with open(os.path.join(OUT, f"rollout-2026-01-15T11-00-00-{MAIN}.jsonl"), "w") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in m.rows))
    with open(os.path.join(OUT, "sub", f"rollout-2026-01-15T11-00-30-{CHILD}.jsonl"), "w") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in c.rows))
    print(f"wrote {os.path.relpath(OUT)}: {len(m.rows)} main records, {len(c.rows)} subagent records")


if __name__ == "__main__":
    main()
