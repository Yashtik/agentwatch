"""Follow a Claude Code session log (and its helpers' logs) and turn new lines into events.

Events: step (a tool call or message appears), update (its result arrived), flag (something
looks wrong), status (working / waiting for you). Everything comes from the log; the flags
are simple rules, no AI.
"""
import copy
import heapq
import json
import os
import re
import shlex
import time

TERM_LINES, TERM_TAIL = 30, 6
FILE_LINES = 40
TEXT_LINES = 60
SEARCH_CMDS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "/usr/bin/grep"}
DENIED = ("The user doesn't want to proceed with this tool use", "The tool use was rejected")  # Claude Code's words
TEST_PROGS = {"pytest", "py.test", "nose2", "tox", "jest", "vitest", "mocha", "rspec", "phpunit", "ctest"}
JS_PMS = {"npm", "pnpm", "yarn", "bun"}
LANG_TOOLS = {"cargo", "go", "swift", "dotnet", "deno", "mix"}
BUILD_PROGS = {"tsc", "make", "gradle", "gradlew", "mvn", "xcodebuild"}
# Lines that say a test run or build failed even when a pipe (`| tail`) hid the exit code.
FAIL_SUMMARY = re.compile(r"^.*(\b[1-9]\d* (failed|errors?)\b|\bFAILED\b|^FAIL\b|--- FAIL|test result: FAILED"
                          r"|npm ERR!|BUILD FAILED|Build failed).*$", re.M)
# Weaker signs, only trusted when the command also exited non-zero.
FAIL_LINE = re.compile(r"^.*(\berror(\[\w+\]| TS\d+)?:|\bError:|Traceback \(most recent call last\)).*$", re.M)
HOME = os.path.expanduser("~")


def clip(text, keep, tail=0):
    """First lines (plus `tail` last lines). Returns head, tail, omitted, total."""
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if total > keep:
        head = keep - tail
        lines, end, omitted = lines[:head], lines[total - tail:] if tail else [], total - head - tail
    else:
        end, omitted = [], 0
    return "\n".join(map(cut_line, lines)), "\n".join(map(cut_line, end)), omitted, total


def cut_line(line, limit=2000):
    """One very long line (minified code, a binary dump) shouldn't flood the screen. Cut at a space
    when there's one nearby, so a key isn't split in half (half a key wouldn't match a pattern)."""
    if len(line) <= limit:
        return line
    cut = max(line.rfind(" ", limit - 400, limit), line.rfind("\t", limit - 400, limit))
    cut = cut if cut > 0 else limit
    return line[:cut] + f" … ({len(line) - cut} more characters)"


def printable(s):
    s = str(s).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "·", s)


def result_text(content):
    if isinstance(content, str):
        return content
    out = []
    for p in content or []:
        if isinstance(p, dict):
            out.append(p.get("text", "") if p.get("type") == "text" else f"[{p.get('type', 'part')}]")
    return "\n".join(out)


def segments(command):
    """The words of each pipeline/chain segment, without leading VAR=value assignments."""
    out = []
    for seg in re.split(r"\|\||&&|[|;&\n]", command):
        try:
            toks = shlex.split(seg, posix=True)
        except ValueError:
            toks = seg.split()
        toks = [t for t in toks if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
        if toks:
            out.append(toks)
    return out


def first_words(command):
    """The program name of each pipeline/chain segment."""
    return ["git grep" if toks[:2] == ["git", "grep"] else toks[0] for toks in segments(command)]


def runner(command):
    """'test' if a segment of the command runs a test suite, else 'build' if one runs a build, else None.
    Looks at program names only, so words in an echo or a commit message don't count."""
    found = None
    for toks in segments(command):
        if toks[0] == "npx" and len(toks) > 1:
            toks = toks[1:]
        prog, rest = toks[0].split("/")[-1], toks[1:]
        sub = [t for t in rest if not t.startswith("-")]
        if prog.startswith("python") and "-m" in rest and rest.index("-m") + 1 < len(rest):
            prog, sub = rest[rest.index("-m") + 1], []
        if (prog in TEST_PROGS or (prog in JS_PMS and (sub[:1] == ["test"] or sub[:2] == ["run", "test"]))
                or (prog in LANG_TOOLS and sub[:1] == ["test"]) or (prog == "make" and sub[:1] in (["test"], ["check"]))
                or prog == "unittest"):
            return "test"
        if (prog in BUILD_PROGS or (prog in JS_PMS and (sub[:1] == ["build"] or sub[:2] == ["run", "build"]))
                or (prog in LANG_TOOLS and sub[:1] == ["build"]) or (prog in ("vite", "next") and sub[:1] == ["build"])):
            found = "build"
    return found


# What gets hidden before anything reaches the screen (screen-sharing safety, best effort).
# Every repeat is bounded so a crafted line can't make a pattern run for minutes.
# Order matters: whole key blocks first, then known token formats, then generic ones.
SECRET_PATTERNS = [
    (r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----|$)",
     "[redacted private key]"),                                          # PEM and PGP
    (r"(\b[a-zA-Z][a-zA-Z0-9+.\-]{0,20}://)[^\s/:@]{1,200}:[^\s/@]{1,200}@", r"\1[redacted]@"),  # user:pass@host
    (r"sk-ant-[A-Za-z0-9_\-]{20,300}", "[redacted]"),
    (r"\bsk-[A-Za-z0-9_\-]{20,300}", "[redacted]"),                      # OpenAI and similar
    (r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,200}", "[redacted]"),   # Stripe
    (r"\bgh[pousr]_[A-Za-z0-9]{30,200}", "[redacted]"),
    (r"\bgithub_pat_[A-Za-z0-9_]{30,300}", "[redacted]"),
    (r"\bglpat-[A-Za-z0-9_\-]{20,200}", "[redacted]"),                     # GitLab
    (r"\bnpm_[A-Za-z0-9]{36,100}", "[redacted]"),
    (r"\bhf_[A-Za-z0-9]{30,100}", "[redacted]"),                           # Hugging Face
    (r"\bAIza[0-9A-Za-z_\-]{30,100}", "[redacted]"),                       # Google API key
    (r"\bya29\.[0-9A-Za-z_\-]{20,500}", "[redacted]"),                     # Google OAuth token
    (r"\bxox[abposr]-[A-Za-z0-9\-]{10,200}", "[redacted]"),                # Slack
    (r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{10,200}", "[redacted]"),
    (r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "[redacted]"),                      # AWS access key id
    (r"\beyJ[A-Za-z0-9_\-]{10,2000}\.[A-Za-z0-9_\-]{10,2000}\.[A-Za-z0-9_\-]{10,2000}", "[redacted]"),  # JWT
    (r"(?i)(\bauthorization[\"']?\s{0,5}[:=]\s{0,5}[\"']?(?:basic|bearer|token|digest)?\s{0,5})[A-Za-z0-9._~+/=\-]{8,2000}",
     r"\1[redacted]"),
    (r"(?i)\bbearer\s{1,5}[A-Za-z0-9._~+/=\-]{20,2000}", "Bearer [redacted]"),
    # NAME=value / "name": "value" where the name says it's a secret. Keeps the name, hides the value.
    (r"(?i)(\b(?!(?:old)?pwd\b)(?:[a-z0-9_.\-]{0,40}(?:secret|token|passw(?:or)?d|pass(?![a-z])|pwd|credential|auth(?!or)"
     r"|api[_\-]?key|access[_\-]?key|private[_\-]?key)[a-z0-9_.\-]{0,40}|[a-z0-9_.\-]{1,40}[_\-]key))"
     r"([\"']?\s{0,5}[=:]\s{0,5}[\"']?)(?!\[redacted)([^\s\"',;]{6,500})", r"\1\2[redacted]"),
    (r"(?<![A-Za-z0-9._%+\-])(?!git@)[A-Za-z0-9._%+\-]{1,64}@(?:[A-Za-z0-9\-]{1,63}\.){1,8}[A-Za-z]{2,24}\b",
     "[redacted]"),                                                       # email addresses (git@host:repo isn't one)
]
SECRET_RES = [(re.compile(p), r) for p, r in SECRET_PATTERNS]
MAX_STRING = 50_000  # longer strings are cut before they reach the screen


def redact(text):
    """Hide secrets and email addresses in one string."""
    if len(text) > MAX_STRING:
        text = text[:MAX_STRING] + f"\n… ({len(text) - MAX_STRING} more characters not shown)"
    for rx, repl in SECRET_RES:
        text = rx.sub(repl, text)
    return text


def redact_obj(obj):
    """Redact every string inside an event (keys stay as they are). Done on the values, not on
    the JSON text, so redaction can never break the JSON or skip a value."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj


class SessionParser:
    def __init__(self, path, main_only=False, meta_only=False):
        self.path = path
        self.meta_only = meta_only     # menu: title, folder, times and status only; no steps kept
        self.main_only = main_only or meta_only
        self.emit = True               # False while catching up: events aren't shown, so skip the copies
        self.dir = path[:-len(".jsonl")]
        self.offsets = {}          # file -> bytes read
        self.seen = set()          # record uuids already handled
        self.steps = []
        self.by_tool = {}          # tool_use_id -> step index
        self.helper_of = {}        # helper agent id -> its helper_start step index
        self.helper_ended = set()
        self.unlinked = set()      # helper_start steps whose helper id isn't known yet
        self.cwd = {}              # agent -> shell cwd
        self.cmd_count = {}
        self.err_seen = set()
        self.err_text = {}         # step index -> the start of the tool's error text
        self.edit_raw = {}         # edit step index -> (path, hash of old_string, hash of new_string)
        self.last_edit = {}        # path -> (step index, old hash, new hash) of its last successful edit
        self.status = None
        self.meta = {"id": os.path.basename(self.dir), "cwd": None, "title": None, "name": None,
                     "first_prompt": None, "started": None, "last": None}

    # ---------- reading ----------

    def files(self):
        out = [(self.path, "main")]
        sub = os.path.join(self.dir, "subagents")
        if os.path.isdir(sub) and not self.main_only:
            for f in sorted(os.listdir(sub)):
                if f.endswith(".jsonl"):
                    out.append((os.path.join(sub, f), "helper:" + f[:-len(".jsonl")]))
        return out

    def read_file(self, f, agent):
        """New complete records from one log, one line at a time, so a big log is never held in
        memory whole. A last line that's still being written is left for the next poll."""
        try:
            fh = open(f, "rb")
        except OSError:
            return
        with fh:
            fh.seek(self.offsets.get(f, 0))
            last_ts = ""
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                self.offsets[f] = self.offsets.get(f, 0) + len(raw)
                if len(raw) < 3 or b'"api-request' in raw[:60]:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(d, dict) and ("timestamp" in d or d.get("type") in ("ai-title", "agent-name")):
                    d["_agent"] = agent
                    last_ts = d.get("timestamp") or last_ts
                    yield last_ts, d

    def new_records(self):
        """New records from the main log and the helpers' logs, merged in time order as they're read."""
        for _, d in heapq.merge(*(self.read_file(f, a) for f, a in self.files()), key=lambda x: x[0]):
            yield d

    def poll(self):
        # A helper's meta.json (which names the Agent call it belongs to) can appear after the call.
        events = self.link_helpers()
        for d in self.new_records():
            events += self.handle(d)
        return events + self.link_helpers()

    # ---------- records -> events ----------

    def handle(self, d):
        if self.meta_only:
            return self.handle_meta(d)
        uid = d.get("uuid")
        if uid:
            if uid in self.seen:
                return []
            self.seen.add(uid)
        t, agent, ts = d.get("type"), d["_agent"], d.get("timestamp")
        if t == "ai-title":
            self.meta["title"] = d.get("aiTitle")
            return []
        if t == "agent-name":
            self.meta["name"] = d.get("agentName")
            return []
        if agent == "main":
            self.meta["cwd"] = d.get("cwd") or self.meta["cwd"]
            if ts:
                self.meta["started"] = self.meta["started"] or ts
                self.meta["last"] = ts
        ev = []
        msg = d.get("message") or {}
        content = msg.get("content")
        if t == "user":
            if isinstance(content, str) or any(isinstance(p, dict) and p.get("type") == "text" for p in content or []):
                text = content if isinstance(content, str) else result_text(content)
                if agent == "main" and not d.get("isMeta") and not text.lstrip().startswith("<"):
                    text = re.sub(r"\s*</?pasted_content[^>]*>\s*", "\n", text).strip()
                    self.meta["first_prompt"] = self.meta["first_prompt"] or text
                    ev.append(self.new_step(agent, ts, "prompt", {"text": text}))
                    ev += self.set_status("working", ts)
            for p in content if isinstance(content, list) else []:
                if isinstance(p, dict) and p.get("type") == "tool_result":
                    ev += self.finish(p, d, ts)
        elif t == "assistant":
            for p in content or []:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text" and p["text"].strip():
                    ev.append(self.new_step(agent, ts, "say", {"text": p["text"]}))
                elif p.get("type") == "tool_use":
                    ev += self.start(p, agent, ts)
            if agent == "main":
                # Each content block is its own record carrying the message's stop_reason; wait for the text.
                has_text = any(isinstance(p, dict) and p.get("type") == "text" for p in content or [])
                if msg.get("stop_reason") == "end_turn" and has_text:
                    ev += self.set_status("done", ts)
                elif msg.get("stop_reason") == "tool_use":
                    ev += self.set_status("working", ts)
        elif t == "system" and d.get("subtype") == "turn_duration" and agent == "main":
            ev += self.set_status("done", ts)
        return ev

    def handle_meta(self, d):
        """Menu mode: keep only what the session list shows."""
        t, ts, msg = d.get("type"), d.get("timestamp"), d.get("message") or {}
        if t == "ai-title":
            self.meta["title"] = d.get("aiTitle")
            return []
        if t == "agent-name":
            self.meta["name"] = d.get("agentName")
            return []
        self.meta["cwd"] = d.get("cwd") or self.meta["cwd"]
        if ts:
            self.meta["started"] = self.meta["started"] or ts
            self.meta["last"] = ts
        content = msg.get("content")
        if t == "user" and not d.get("isMeta"):
            text = content if isinstance(content, str) else "\n".join(
                p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")
            if text.strip() and not text.lstrip().startswith("<"):
                self.meta["first_prompt"] = self.meta["first_prompt"] or re.sub(r"\s*</?pasted_content[^>]*>\s*", "\n", text).strip()[:500]
                self.status = "working"
        elif t == "assistant":
            has_text = any(isinstance(p, dict) and p.get("type") == "text" for p in content or [])
            if msg.get("stop_reason") == "end_turn" and has_text:
                self.status = "done"
            elif msg.get("stop_reason") == "tool_use":
                self.status = "working"
        elif t == "system" and d.get("subtype") == "turn_duration":
            self.status = "done"
        return []

    def snap(self, s):
        return copy.deepcopy(s) if self.emit else s

    def forget_finished(self):
        """Past steps are never shown again, so drop their content and keep memory flat.
        Running steps and helper tasks stay whole: their results and helper ids still arrive."""
        for s in self.steps:
            if s["state"] == "done" and s["kind"] != "helper_start" and s.get(s["kind"]):
                s[s["kind"]] = {}

    def set_status(self, status, ts):
        if status == self.status:
            return []
        self.status = status
        return [{"type": "status", "status": status, "ts": ts}]

    def new_step(self, agent, ts, kind, body, **extra):
        s = {"i": len(self.steps), "agent": agent, "kind": kind, "ts": ts, "ts_end": ts,
             "state": "done", "status": "ok", kind: body}
        s.update(extra)
        self.steps.append(s)
        return {"type": "step", "step": self.snap(s)}  # a snapshot: the step changes when its result lands

    def start(self, p, agent, ts):
        name, inp = p["name"], p.get("input") or {}
        cwd = self.cwd.get(agent, self.meta["cwd"] or HOME)
        common = dict(tool=name, tool_use_id=p["id"], state="running", ts_end=None)
        if name in ("Bash", "Monitor"):
            kind, body = "terminal", {"command": inp.get("command", ""), "description": inp.get("description", ""),
                                      "cwd": cwd, "cwd_after": cwd}
        elif name == "WebSearch":
            kind, body = "search", {"query": inp.get("query", ""), "results": [], "summary": ""}
        elif name == "WebFetch":
            kind, body = "fetch", {"url": inp.get("url", ""), "final_url": inp.get("url", ""), "prompt": inp.get("prompt", "")}
        elif name == "Read":
            kind, body = "read", {"path": inp.get("file_path", ""), "pages": inp.get("pages"), "lines": "", "error": None}
        elif name == "Edit":
            old, _, o_om, _ = clip(inp.get("old_string", ""), FILE_LINES)
            new, _, n_om, _ = clip(inp.get("new_string", ""), FILE_LINES)
            kind, body = "edit", {"path": inp.get("file_path", ""), "old": old, "new": new, "old_omitted_lines": o_om,
                                  "new_omitted_lines": n_om, "replace_all": bool(inp.get("replace_all"))}
        elif name == "Write":
            lines, _, om, total = clip(inp.get("content", ""), FILE_LINES)
            kind, body = "write", {"path": inp.get("file_path", ""), "lines": lines, "omitted_lines": om, "total_lines": total}
        elif name == "Agent":
            kind, body = "helper_start", {"id": None, "description": inp.get("description", ""),
                                          "type": inp.get("subagent_type"), "prompt": inp.get("prompt", "")}
        elif name == "SubagentHandback":
            report, _, om, _ = clip(inp.get("message", ""), TEXT_LINES)
            self.helper_ended.add(agent.split(":", 1)[1])
            kind, body = "helper_end", {"id": agent.split(":", 1)[1], "report": report, "omitted_lines": om}
            common.update(state="done", ts_end=ts)
        else:
            summary = json.dumps(inp, ensure_ascii=False)
            kind, body = "other", {"name": name, "input_summary": summary[:300] + ("…" if len(summary) > 300 else ""),
                                  "output": "", "omitted_lines": 0}
        ev = self.new_step(agent, ts, kind, body, **common)
        self.by_tool[p["id"]] = ev["step"]["i"]
        if name == "Edit":
            old, new = inp.get("old_string", ""), inp.get("new_string", "")
            self.edit_raw[ev["step"]["i"]] = (inp.get("file_path", ""), hash(old) if old else None, hash(new))
        if name == "Agent":
            self.unlinked.add(ev["step"]["i"])
            return [ev] + self.link_helpers()
        return [ev]

    def link_helpers(self):
        """Give each helper_start its helper's id as soon as the helper's meta.json names it."""
        sub = os.path.join(self.dir, "subagents")
        if not self.unlinked or not os.path.isdir(sub):
            return []
        events = []
        for f in os.listdir(sub):
            if f.endswith(".meta.json"):
                try:
                    meta = json.load(open(os.path.join(sub, f)))
                except (OSError, json.JSONDecodeError):
                    continue
                i = self.by_tool.get(meta.get("toolUseId"))
                if i in self.unlinked:
                    aid = f[:-len(".meta.json")]
                    self.steps[i]["helper_start"]["id"] = aid
                    self.helper_of[aid] = i
                    self.unlinked.discard(i)
                    events.append({"type": "update", "step": self.snap(self.steps[i])})
        return events

    def finish(self, p, d, ts):
        i = self.by_tool.get(p.get("tool_use_id"))
        if i is None:
            return []
        s = self.steps[i]
        if s["state"] == "done" and s["kind"] != "helper_end":
            return []
        res = result_text(p.get("content"))
        tur = d.get("toolUseResult")
        err = bool(p.get("is_error"))
        if err:
            self.err_text[i] = res[:2000]
        s.update(state="done", ts_end=ts, status="error" if err else "ok")
        k = s["kind"]
        if k == "terminal":
            self.fill_terminal(s, res, tur, err)
        elif k == "search":
            links, summary = [], ""
            for part in (tur or {}).get("results", []) if isinstance(tur, dict) else []:
                if isinstance(part, dict):
                    links += part.get("content", [])
                elif isinstance(part, str):
                    summary += part
            s["search"].update(results=links, summary=summary,
                               duration=(tur or {}).get("durationSeconds") if isinstance(tur, dict) else None)
        elif k == "fetch":
            tur = tur if isinstance(tur, dict) else {}
            first = res.split("\n")[0]
            binary = "%PDF" in res[:5000] or "Binary content" in res
            pos = res.find("%PDF")
            cut = res.find("[The verbatim page text stops here")
            cut_end = res.find("]", cut) + 1 if cut >= 0 else -1
            end = res.find("</fetched-web-content>")
            s["fetch"].update(final_url=tur.get("url", s["fetch"]["url"]), http=tur.get("code"),
                              http_text=tur.get("codeText"), bytes=tur.get("bytes"), duration_ms=tur.get("durationMs"),
                              content_type="pdf" if "application/pdf" in first else ("binary" if binary else "html"),
                              header=first, raw_excerpt=printable(res[pos:pos + 1600]) if pos >= 0 else "",
                              cut_note=res[cut:cut_end] if cut >= 0 else "",
                              summary=(res[cut_end:end] if cut >= 0 and end > cut_end else "").strip())
            if err:
                s["fetch"]["error"] = res[:500]
        elif k == "read":
            if err:
                s["read"]["error"] = res
            else:
                lines, _, om, total = clip(res, FILE_LINES)
                f = (tur or {}).get("file", {}) if isinstance(tur, dict) else {}
                s["read"].update(lines=lines, omitted_lines=om, total_lines=f.get("totalLines") or total,
                                 start_line=f.get("startLine", 1))
        elif k in ("edit", "write"):
            if err:
                s[k]["error"] = res
            elif isinstance(tur, dict) and tur.get("structuredPatch"):
                hunk = tur["structuredPatch"][0]
                s[k]["start_line"] = hunk.get("oldStart")
                patch, _, om, _ = clip("\n".join(hunk.get("lines", [])), FILE_LINES)
                s[k].update(patch=patch, patch_omitted=om, hunks=len(tur["structuredPatch"]))
        elif k == "helper_start":
            pre = self.link_helpers()
            aid = s["helper_start"]["id"]
            if aid and aid not in self.helper_ended:
                report, _, om, _ = clip(res, TEXT_LINES)
                self.helper_ended.add(aid)
                out = pre + [{"type": "update", "step": self.snap(s)}, self.new_step("helper:" + aid, ts, "helper_end",
                                                                    {"id": aid, "report": report, "omitted_lines": om})]
                return out + self.flags(s)
        elif k == "other":
            out, _, om, _ = clip(res, 12)
            s["other"].update(output=out, omitted_lines=om)
        return [{"type": "update", "step": self.snap(s)}] + self.flags(s)

    def fill_terminal(self, s, res, tur, err):
        T = s["terminal"]
        note, interrupted = "", False
        if isinstance(tur, dict):
            out = tur.get("stdout", "")
            note = (tur.get("stderr") or "").strip()
            interrupted = bool(tur.get("interrupted"))
            if tur.get("backgroundTaskId"):
                note = (note + "\n" if note else "") + "Running in the background"
        else:
            out = res
        exit_code, error_text = 0, ""
        if err:
            m = re.match(r"Exit code (\d+)\n?([\s\S]*)", res)
            if m:
                exit_code, out, error_text = int(m.group(1)), m.group(2), f"Exit code {m.group(1)}"
            else:
                exit_code, out, error_text = None, "", res.strip()[:500]
        m = re.match(r"\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*&&", T["command"])
        cwd_after = T["cwd"]
        if m:
            p = m.group(1).strip("\"'")
            cwd_after = HOME + p[1:] if p.startswith("~") else p if p.startswith("/") else os.path.join(T["cwd"], p)
        if "Shell cwd was reset to " in note:
            cwd_after = note.split("Shell cwd was reset to ")[1].split("\n")[0].strip()
        self.cwd[s["agent"]] = cwd_after
        out = printable(out)
        head, tail, om, total = clip(out, TERM_LINES, TERM_TAIL)
        T.update(cwd_after=cwd_after, output=head, output_tail=tail, omitted_lines=om, total_lines=total,
                 empty=out.strip() == "", exit_code=exit_code, error_text=error_text,
                 harness_note=note, interrupted=interrupted)

    # ---------- automatic flags (rules only) ----------

    def flags(self, s):
        out = []

        def flag(level, label, detail):
            out.append({"type": "flag", "flag": {"step": s["i"], "level": level, "label": label, "detail": detail}})

        k = s["kind"]
        body = s.get(k, {})
        if s["status"] == "error":
            text = self.err_text.get(s["i"], "")
            first = text.strip().split("\n")[0][:200]
            if any(d in text for d in DENIED):
                # Declined, so it never ran: not an error to count or repeat.
                flag("quiet", "Stopped by you", "You declined this step, so it didn't run.")
                return out
            failed = k == "terminal" and self.run_failed(body)
            if failed:
                flag("error", *failed)
            elif k == "terminal":
                flag("error", first or "Command failed", "The command failed."
                     + ("" if body.get("output") else " It printed nothing else."))
            else:
                flag("error", {"read": "Couldn't read the file", "edit": "Edit failed", "write": "Write failed",
                               "fetch": "Fetch failed"}.get(k, f"{s.get('tool', 'Tool')} failed"), first)
            what = first
            if k == "terminal":
                # Every failed command starts "Exit code N", so compare what it printed (or the command, if nothing).
                line = next((x.strip() for x in (body.get("output") or "").split("\n") if x.strip()), "")
                what = line[:200] or re.sub(r"\s+", " ", body.get("command", "")).strip()[:200]
            key = (k, first, what)
            if what and key in self.err_seen:
                flag("repeat", "Same error again", f"This error already happened earlier: {what}")
            self.err_seen.add(key)
        if k == "terminal" and s["status"] == "ok":
            failed = self.run_failed(body)  # e.g. `pytest | tail` exits 0 even when tests fail
            if failed:
                flag("error", *failed)
        if k == "edit" and s["status"] == "ok" and s["i"] in self.edit_raw:
            path, old, new = self.edit_raw[s["i"]]
            prev = self.last_edit.get(path)
            if prev and old and old != new and old == prev[2] and new == prev[1]:
                flag("repeat", "Undid its last edit",
                     f"This puts back exactly what the previous edit to {os.path.basename(path)} changed.")
            self.last_edit[path] = (s["i"], old, new)
        if k == "terminal" and s["status"] == "ok" and body.get("empty"):
            if any(w.split("/")[-1] in SEARCH_CMDS or w in SEARCH_CMDS for w in first_words(body["command"])):
                flag("quiet", "Search found nothing", "The command ran without errors but printed nothing.")
        if k == "fetch":
            if body.get("http") and body["http"] >= 400:
                flag("error", f"HTTP {body['http']}", f"The page answered {body['http']} {body.get('http_text') or ''}".strip() + ".")
            elif body.get("raw_excerpt"):
                flag("quiet", f"{body.get('http') or ''} OK, but unreadable".strip(),
                     "The fetch succeeded, but what came back was raw binary bytes, not readable text.")
        if k == "terminal":
            norm = re.sub(r"\s+", " ", body["command"]).strip()
            n = self.cmd_count[(s["agent"], norm)] = self.cmd_count.get((s["agent"], norm), 0) + 1
            if n == 3:
                flag("repeat", "Same command, 3rd time", "The agent has now run exactly this command three times.")
        return out

    def run_failed(self, T):
        """(label, detail) if the command ran tests or a build that failed, else None."""
        what = runner(T.get("command", ""))
        if not what:
            return None
        label = "Tests failed" if what == "test" else "Build failed"
        text = "\n".join(x for x in (T.get("output"), T.get("output_tail")) if x)
        code = T.get("exit_code")
        m = FAIL_SUMMARY.search(text) or (FAIL_LINE.search(text) if code else None)
        if m:
            return label, m.group(0).strip()[:200]
        if code:
            return label, f"The command exited with code {code}."
        return None


def summarize(parser):
    """Session info for the menu. `parser` is a meta_only SessionParser kept between calls."""
    parser.poll()
    sub = os.path.join(parser.dir, "subagents")
    helpers = len([f for f in os.listdir(sub) if f.endswith(".meta.json")]) if os.path.isdir(sub) else 0
    m = parser.meta
    return {"id": m["id"], "project": os.path.basename(os.path.dirname(parser.path)), "cwd": m["cwd"],
            "title": m["name"] or m["title"] or (m["first_prompt"] or "")[:120] or m["id"][:8],
            "first_prompt": (m["first_prompt"] or "")[:120], "started": m["started"], "last_activity": m["last"],
            "status": parser.status or "done", "helpers": helpers}
