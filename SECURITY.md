# Security

agentwatch shows your Claude Code and Codex session logs, which can hold private code, file contents, commands and anything your agents read on the web. Here is what it does to keep that private, and what it can't do.

## What it protects against

**Other websites.** The server listens on `127.0.0.1` only. It refuses any request whose `Host` header isn't its own address, which stops DNS-rebinding tricks. It refuses data requests that come from another site (`Origin` and `Sec-Fetch-Site` checks) and sends no CORS headers, so a page you visit can't read your sessions.

**Other programs and other users on the same machine.** Every data request needs a random key, made fresh each time the server starts. The key is written into a launch page in a private folder (`~/.local/state/agentwatch`, mode 0700; the page is 0600), and `agentwatch` opens that page, so the key never appears on a command line where `ps` could show it. The page passes the key to the viewer in the URL fragment, which the browser doesn't send to the server; the viewer clears it from the address bar and then sends it with each data request (in the query string, to `127.0.0.1` only, never logged). Without the key, the server returns nothing about your sessions. The browser may still record the launch address in its history before it's cleared; that key stops working when the viewer restarts.

**Malicious content in the logs.** Agents read web pages, and a page can contain text written to attack whatever displays it. Everything from the logs is escaped before it's put on the page. On top of that, a strict Content-Security-Policy allows only the viewer's own script, with no inline scripts, no `eval` and no connections to any other server. So even a missed spot couldn't run code or send your data out.

**Screen sharing.** API keys and tokens in common formats, private keys, passwords in URLs, `password=` style values and email addresses are replaced with `[redacted]` before anything is drawn.

**Stopping.** The server stops itself a few seconds after the last viewer tab closes. A tab tells it so with a `POST /api/bye` that needs the same key and same-origin checks as everything else, so a web page can't stop it for you.

**Overload.** Logs are read line by line (never whole), finished steps are dropped from memory once shown, very long lines are cut, every redaction pattern is bounded so a crafted line can't make it run for minutes, idle connections close after 60 seconds, and at most 16 viewers can be open at once.

## What it can't protect against

- **Anything already running as you.** A program running under your account can read `~/.claude` and `~/.codex` directly. agentwatch doesn't make that better or worse.
- **Redaction misses.** It's pattern matching, so a secret in an unusual format can get through. Treat the viewer like your terminal: don't share your screen with it open unless you'd share the session itself.
- **People who can see your screen.** It shows what your agents did.

## By design

- Read-only. It never writes to `~/.claude` or `~/.codex`, never runs commands from the logs, and has no way to control your agents.
- To know which windows are open, it reads Claude Code's small registry files (`~/.claude/sessions/<pid>.json`: session id, window name, busy or idle). It never opens the `.key` files kept beside them.
- Codex keeps no such list, so agentwatch runs two fixed commands, `pgrep -x codex` and `lsof -p <those pids>`, to see which session logs (`~/.codex/sessions/.../rollout-*.jsonl`) running Codex processes have open. Nothing from a log ever goes into a command. It never reads Codex's `auth.json` or config.
- No history: only open windows are listed, and nothing from before you opened the viewer is shown.
- No dependencies to run it (Python standard library only), and nothing is loaded from the internet.
- Nothing leaves your machine.

## Reporting a problem

Please report security issues privately, using GitHub's "Report a vulnerability" button on the repository's Security tab, rather than opening a public issue.
