"""Follow a Codex session log (~/.codex/sessions/.../rollout-*.jsonl) and turn it into the same
events as parse.py does for Claude Code: steps, updates, flags and working/done status.

Codex writes one "item_completed" record per finished step (a message, a command with its output
and exit code, a file change, a web search, a subagent starting or finishing). Steps show up when
they finish: Codex doesn't log a command until it's done. Each subagent has its own log, which
names its parent; its steps arrive as agent "helper:<thread id>".
"""
import glob
import json
import os
import re
import shlex
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from parse import FILE_LINES, TERM_LINES, TERM_TAIL, TEXT_LINES, SessionParser, clip, printable

CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")
CODEX_INDEX = os.path.expanduser("~/.codex/session_index.jsonl")


def iso_ms(ms):
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def text_of(content):
    """Text parts of a Codex message (content is a list of {"type": "Text"/"text", "text": ...})."""
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") in ("Text", "text"))


def command_text(cmd):
    """["/bin/bash", "-lc", "ls -la"] -> "ls -la"; anything else is shell-joined."""
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, list) and len(cmd) == 3 and os.path.basename(str(cmd[0])) in ("bash", "zsh", "sh") and cmd[1] in ("-lc", "-c"):
        return cmd[2]
    return shlex.join(str(c) for c in cmd or [])


def path_of(uri):
    """Codex writes cwd as a file:// URL."""
    if isinstance(uri, str) and uri.startswith("file://"):
        return unquote(urlparse(uri).path)
    return uri


def session_title(sid, index=CODEX_INDEX):
    """Codex's own name for a session, from session_index.jsonl (last entry wins)."""
    title = None
    try:
        with open(index, encoding="utf-8") as f:
            for line in f:
                if sid in line:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("id") == sid and d.get("thread_name"):
                        title = d["thread_name"]
    except OSError:
        pass
    return title


def first_meta(path):
    """The session_meta payload of a log (its first record), or {}."""
    try:
        with open(path, "rb") as f:
            d = json.loads(f.readline())
        return d.get("payload", {}) if d.get("type") == "session_meta" else {}
    except (OSError, ValueError):
        return {}


class CodexParser(SessionParser):
    def __init__(self, path, root=None, meta_only=False, index=CODEX_INDEX):
        super().__init__(path, main_only=meta_only, meta_only=meta_only)
        self.root = root or CODEX_SESSIONS
        self.index = index
        self.dir = path[:-len(".jsonl")]
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path)
        self.meta["id"] = m.group(1) if m else os.path.basename(path)
        self.children = {}          # subagent thread id -> its log
        self.child_name = {}        # subagent thread id -> its name (agent_path or nickname)
        self.child_report = {}      # subagent thread id -> its last message
        self.helper_end_step = {}   # subagent thread id -> step index of its helper_end
        if not meta_only:
            self.scan_children()

    # ---------- files: the main log plus each subagent's log ----------

    def files(self):
        return [(self.path, "main")] + ([] if self.main_only else [(p, "helper:" + t) for t, p in self.children.items()])

    def scan_children(self):
        """Subagent logs written since this session started whose session_meta names it as parent."""
        # The log's name holds its start time (local): rollout-2026-11-21T16-40-47-<id>.jsonl
        m = re.search(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-", os.path.basename(self.path))
        try:
            start = datetime.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S").timestamp() if m else os.path.getmtime(self.path) - 86400
        except (ValueError, OSError):
            return
        for p in glob.glob(os.path.join(self.root, "**", "rollout-*.jsonl"), recursive=True):
            if p == self.path or p in self.children.values():
                continue
            try:
                if os.path.getmtime(p) < start - 60:
                    continue
            except OSError:
                continue
            meta = first_meta(p)
            if meta.get("thread_source") == "subagent" and meta.get("parent_thread_id") == self.meta["id"]:
                self.children[meta.get("id")] = p
                self.child_name[meta.get("id")] = meta.get("agent_nickname") or (meta.get("agent_path") or "").split("/")[-1]

    def find_child(self, tid):
        if tid in self.children or not re.fullmatch(r"[0-9a-f-]{36}", tid or ""):
            return
        hits = glob.glob(os.path.join(self.root, "**", f"rollout-*-{tid}.jsonl"), recursive=True)
        if hits:
            self.children[tid] = hits[0]

    # ---------- records ----------

    def handle(self, d):
        if self.meta_only:
            return self.handle_meta(d)
        t, agent, ts = d.get("type"), d["_agent"], d.get("timestamp")
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if agent == "main" and ts:
            self.meta["started"] = self.meta["started"] or ts
            self.meta["last"] = ts
        if t == "session_meta":
            if agent == "main":
                self.meta["cwd"] = path_of(p.get("cwd")) or self.meta["cwd"]
                self.meta["title"] = session_title(self.meta["id"], self.index)
            return []
        if t == "turn_context" and agent == "main":
            self.meta["cwd"] = path_of(p.get("cwd")) or self.meta["cwd"]
            return []
        if t != "event_msg":
            return []
        kind = p.get("type")
        if kind == "task_started" and agent == "main":
            return self.set_status("working", ts)
        if kind == "task_complete":
            if agent == "main":
                return self.set_status("done", ts)
            return self.child_done(agent.split(":", 1)[1], p.get("last_agent_message") or "", ts)
        if kind == "item_completed" and isinstance(p.get("item"), dict):
            return self.item(p["item"], agent, iso_ms(p.get("started_at_ms")) or ts, iso_ms(p.get("completed_at_ms")) or ts)
        return []

    def handle_meta(self, d):
        """Menu mode: title, folder, times and working/done status."""
        t, ts = d.get("type"), d.get("timestamp")
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if ts:
            self.meta["started"] = self.meta["started"] or ts
            self.meta["last"] = ts
        if t == "session_meta":
            self.meta["cwd"] = path_of(p.get("cwd")) or self.meta["cwd"]
            self.meta["title"] = session_title(self.meta["id"], self.index)
        elif t == "turn_context":
            self.meta["cwd"] = path_of(p.get("cwd")) or self.meta["cwd"]
        elif t == "event_msg":
            if p.get("type") == "task_started":
                self.status = "working"
            elif p.get("type") == "task_complete":
                self.status = "done"
            elif p.get("type") == "item_completed" and isinstance(p.get("item"), dict) and p["item"].get("type") == "UserMessage":
                self.meta["first_prompt"] = self.meta["first_prompt"] or text_of(p["item"].get("content")).strip()[:500]
        return []

    def done_step(self, agent, ts, ts_end, kind, body, status="ok", tool=None, err=""):
        ev = self.new_step(agent, ts, kind, body, ts_end=ts_end, status=status, tool=tool or kind)
        if status == "error":
            self.err_text[ev["step"]["i"]] = err[:2000]   # what the rules read, like Claude's "Exit code N\n<output>"
        return [ev] + self.flags(self.steps[ev["step"]["i"]])

    def item(self, it, agent, ts, ts_end):
        k = it.get("type")
        if k == "UserMessage":
            text = text_of(it.get("content")).strip()
            if agent != "main" or not text:
                return []      # a subagent's log starts with a copy of its parent's conversation
            self.meta["first_prompt"] = self.meta["first_prompt"] or text
            return [self.new_step(agent, ts, "prompt", {"text": text})] + self.set_status("working", ts)
        if k == "AgentMessage":
            text = text_of(it.get("content")).strip()
            if agent != "main":
                self.child_report[agent.split(":", 1)[1]] = text or self.child_report.get(agent.split(":", 1)[1], "")
            return [self.new_step(agent, ts_end, "say", {"text": text})] if text else []
        if k == "CommandExecution":
            return self.command(it, agent, ts, ts_end)
        if k == "FileChange":
            out = []
            for path, ch in (it.get("changes") or {}).items():
                out += self.file_change(path, ch if isinstance(ch, dict) else {}, it, agent, ts, ts_end)
            return out
        if k == "Extension" and it.get("kind") == "web.search":
            action = it.get("action") if isinstance(it.get("action"), dict) else {}
            query = it.get("query") or "; ".join(action.get("queries") or []) or action.get("query") or ""
            results = [{"title": r.get("title") or r.get("domain") or "", "url": r.get("url") or ""}
                       for r in it.get("results") or [] if isinstance(r, dict)]
            secs = None
            if ts and ts_end:
                secs = round((datetime.fromisoformat(ts_end.replace("Z", "+00:00")) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds(), 1)
            return self.done_step(agent, ts, ts_end, "search", {"query": query, "results": results, "summary": "", "duration": secs},
                                  tool="web.search")
        if k == "SubAgentActivity":
            return self.subagent(it, agent, ts_end)
        if k == "Reasoning":
            return []
        name = it.get("tool") or it.get("kind") or k or "step"
        summary = json.dumps({x: y for x, y in it.items() if x not in ("type", "id")}, ensure_ascii=False)
        return self.done_step(agent, ts, ts_end, "other", {"name": str(name), "input_summary": summary[:300] + ("…" if len(summary) > 300 else ""),
                                                          "output": "", "omitted_lines": 0}, tool=str(k))

    def command(self, it, agent, ts, ts_end):
        cwd = path_of(it.get("cwd")) or self.cwd.get(agent, self.meta["cwd"] or os.path.expanduser("~"))
        self.cwd[agent] = cwd
        out = printable(it.get("aggregated_output") or "")
        code = it.get("exit_code")
        failed = (isinstance(code, int) and code != 0) or it.get("status") == "failed"
        head, tail, om, total = clip(out, TERM_LINES, TERM_TAIL)
        body = {"command": command_text(it.get("command")), "description": "", "cwd": cwd, "cwd_after": cwd,
                "output": head, "output_tail": tail, "omitted_lines": om, "total_lines": total, "empty": out.strip() == "",
                "exit_code": code if isinstance(code, int) else (1 if failed else 0),
                "error_text": f"Exit code {code}" if failed and isinstance(code, int) else ("Command failed" if failed else ""),
                "harness_note": "", "interrupted": False}
        return self.done_step(agent, ts, ts_end, "terminal", body, "error" if failed else "ok", tool="exec",
                              err=f"{body['error_text']}\n{out}")

    def file_change(self, path, ch, it, agent, ts, ts_end):
        kind, status = ch.get("type"), "error" if it.get("status") == "failed" else "ok"
        err = it.get("stderr") or it.get("stdout") or "The change wasn't applied."
        if kind == "add":
            lines, _, om, total = clip(ch.get("content") or "", FILE_LINES)
            return self.done_step(agent, ts, ts_end, "write", {"path": path, "lines": lines, "omitted_lines": om, "total_lines": total},
                                  status, tool="apply_patch", err=err)
        if kind == "update":
            # Show the first hunk with real line numbers; count the rest as omitted.
            hunks = re.split(r"(?m)^(?=@@ )", ch.get("unified_diff") or "")
            hunks = [h for h in hunks if h.startswith("@@")]
            first = hunks[0] if hunks else ""
            m = re.match(r"@@ -(\d+)", first)
            body_lines = first.split("\n")[1:]
            patch, _, om, _ = clip("\n".join(body_lines), FILE_LINES)
            rest = sum(len(h.split("\n")) - 1 for h in hunks[1:])
            target = ch.get("move_path") if ch.get("move_path") not in (None, "null", "") else path
            return self.done_step(agent, ts, ts_end, "edit", {"path": target, "old": "", "new": "", "old_omitted_lines": 0,
                                                              "new_omitted_lines": 0, "replace_all": False,
                                                              "start_line": int(m.group(1)) if m else None, "patch": patch,
                                                              "patch_omitted": om + rest, "hunks": len(hunks)}, status, tool="apply_patch", err=err)
        return self.done_step(agent, ts, ts_end, "other", {"name": "Delete file" if kind == "delete" else f"File {kind}",
                                                          "input_summary": path, "output": "", "omitted_lines": 0}, status, tool="apply_patch", err=err)

    def subagent(self, it, agent, ts):
        tid, what = it.get("agent_thread_id"), it.get("kind")
        if not tid:
            return []
        name = self.child_name.get(tid) or (it.get("agent_path") or "").split("/")[-1] or "subagent"
        if what == "started":
            self.find_child(tid)
            ev = self.new_step(agent, ts, "helper_start", {"id": tid, "description": name, "type": "subagent", "prompt": ""},
                               state="running", ts_end=None, tool="spawn_agent")
            self.helper_of[tid] = ev["step"]["i"]
            return [ev]
        if what == "completed":
            out = []
            i = self.helper_of.get(tid)
            if i is not None and self.steps[i]["state"] == "running":
                self.steps[i].update(state="done", ts_end=ts)
                out.append({"type": "update", "step": self.snap(self.steps[i])})
            report, _, om, _ = clip(self.child_report.get(tid, ""), TEXT_LINES)
            ev = self.new_step("helper:" + tid, ts, "helper_end", {"id": tid, "report": report, "omitted_lines": om})
            self.helper_end_step[tid] = ev["step"]["i"]
            return out + [ev]
        return []

    def child_done(self, tid, message, ts):
        """A subagent's own log says it finished: keep its last message as the report."""
        self.child_report[tid] = message or self.child_report.get(tid, "")
        i = self.helper_end_step.get(tid)
        if i is None or not message:
            return []
        report, _, om, _ = clip(message, TEXT_LINES)
        self.steps[i]["helper_end"].update(report=report, omitted_lines=om)
        return [{"type": "update", "step": self.snap(self.steps[i])}]


def summarize_codex(parser):
    """Menu entry for an open Codex session (parser is meta_only, kept between calls)."""
    parser.poll()
    m = parser.meta
    return {"id": m["id"], "project": "codex", "cwd": m["cwd"], "tool": "codex", "window": None,
            "title": m["title"] or (m["first_prompt"] or "")[:120] or "New Codex session",
            "first_prompt": (m["first_prompt"] or "")[:120], "started": m["started"], "last_activity": m["last"],
            "status": parser.status or "done", "helpers": 0}
