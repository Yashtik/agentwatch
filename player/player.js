/* Live agent viewer.
 *
 * Polls /api/sessions for the session menu and follows one session's /api/stream
 * (Server-Sent Events from live/server.py). The backlog arrives first and is drawn at once;
 * after {"type":"ready"} new events animate. Everything on screen comes from the session log:
 * the player only decides layout, pacing and the camera.
 *
 * URL flags: ?session=<id> picks a session; ?follow=main|helper:<agent id> picks an agent.
 * The server's key comes in the URL fragment (#token=...) from the launch page; see SECURITY.md.
 */
(() => {
  'use strict'

  const Q = new URLSearchParams(location.search)
  // The launch page puts the key in the URL fragment, which isn't sent with the page request.
  // It's then sent with each data request.
  // Keep it for this tab only and take it out of the address bar.
  const TOKEN = (() => {
    const k = new URLSearchParams(location.hash.slice(1)).get('token')
    if (k) {
      try { sessionStorage.setItem('agentwatch-key', k) } catch { /* storage off: keep it in memory */ }
      history.replaceState(null, '', location.pathname + location.search)
      return k
    }
    try { return sessionStorage.getItem('agentwatch-key') || '' } catch { return '' }
  })()
  // A random id for this tab: the server stops itself once the last viewer tab is gone.
  const TAB = Array.from(crypto.getRandomValues(new Uint8Array(12)), b => b.toString(16).padStart(2, '0')).join('')
  const api = (path, params = {}) => `${path}?${new URLSearchParams({ ...params, token: TOKEN, tab: TAB })}`
  let NOKEY = false
  let CLOSED = null          // why the viewer stopped, once it has
  let CLOSE_REFUSED = false  // the browser wouldn't let the page close its tab
  let sawWindows = false, emptySince = null, failures = 0, pollTimer = null
  const W = 1920, H = 1080
  const $ = id => document.getElementById(id)
  const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;')
  const num = x => (Number.isFinite(Number(x)) ? Number(x) : 0) // numbers from the log, never raw text
  const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x))
  const ease = x => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2)
  const lerp = (a, b, x) => a + (b - a) * x
  const now = () => performance.now() / 1000

  // Desktop layout in stage pixels.
  const GEO = {
    term: { x: 300, y: 60, w: 1000, h: 640 },
    chrome: { x: 330, y: 86, w: 1260, h: 820 },
    helper: { x: 470, y: 110, w: 1200, h: 800 },
    edit: { x: 520, y: 110, w: 980, h: 720 },
    msg: { x: 470, y: 86, w: 980, h: 800 },
  }
  const APP = { term: 'Terminal', chrome: 'Google Chrome', helper: 'Google Chrome', edit: 'Code', msg: 'Claude' }
  const MENUS = {
    Finder: ['File', 'Edit', 'View', 'Go', 'Window', 'Help'],
    Terminal: ['Shell', 'Edit', 'View', 'Window', 'Help'],
    'Google Chrome': ['File', 'Edit', 'View', 'History', 'Bookmarks', 'Profiles', 'Tab', 'Window', 'Help'],
    Code: ['File', 'Edit', 'Selection', 'View', 'Go', 'Run', 'Terminal', 'Window', 'Help'],
    Claude: ['File', 'Edit', 'View', 'Window', 'Help'],
  }
  const DOCK = [['term', 'ic-term', '<b>&gt;_</b>'], ['chrome', 'ic-chrome', '<i></i>'], ['edit', 'ic-code', '<b>&lt;/&gt;</b>'], ['msg', 'ic-claude', '<b>✳</b>']]

  const CAM_T = 0.75       // one camera move
  const WIN_T = 0.35       // a window opening
  const CLOSE_T = 0.4      // a window closing
  const CO_W = 400         // width of the terminal's callout lane, just outside its right edge
  const SHOW_TOP = 8, SHOW_END = 6
  const LONG_SAY = 280     // longer messages open the message window instead of a subtitle
  const MAX_LAG = 6        // seconds the screen may trail the agent before animations are skipped
  const CO_MIN = 3         // a callout stays up at least this long
  const RESULT_MIN = 2     // a result that lands late stays on screen at least this long
  const THINK_AFTER = 6    // seconds of silence before "thinking" shows
  const HIST = -1e6        // backlog items happened "long ago" on the animation clock

  let S = null             // the followed session's state, reset on every hello
  let SESSIONS = null      // last /api/sessions answer
  let currentId = null
  let follow = 'all'
  let es = null, connLostAt = null, menuOpen = false
  let cache = {}

  function setHTML(id, key, build) {
    if (cache[id] === key) return
    cache[id] = key
    $(id).innerHTML = build()
  }

  /* ---------- text ---------- */

  function lines(s) {
    if (Array.isArray(s)) return s.map(String)
    const t = String(s ?? '').replace(/\r/g, '')
    if (!t) return []
    const a = t.split('\n')
    while (a.length && a[a.length - 1] === '') a.pop()
    return a
  }
  const tildify = p => String(p || '').replace(/^\/Users\/[^/]+/, '~')
  function cwdLabel(p) {
    const s = tildify(p).replace(/\/+$/, '')
    return s === '~' ? '~' : s.split('/').filter(Boolean).pop() || '/'
  }
  const fileName = u => { const b = String(u).split('?')[0].split('/').filter(Boolean).pop() || String(u); try { return decodeURIComponent(b) } catch { return b } }
  const domain = u => { try { return new URL(u).hostname.replace(/^www\./, '') } catch { return '' } }
  const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s)
  const inline = s => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<u>$1</u>')
  const pad = n => String(n).padStart(2, '0')
  function dur(s) { s = Math.max(0, Math.floor(s)); return s >= 60 ? `${Math.floor(s / 60)} m ${pad(s % 60)} s` : `${s} s` }
  function ago(iso) {
    const s = (Date.now() - Date.parse(iso)) / 1000
    if (!(s >= 0)) return ''
    return s < 60 ? 'just now' : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 172800 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} days ago`
  }

  function plain(t) {
    return String(t).replace(/```[\s\S]*?```/g, ' ').replace(/`([^`]+)`/g, '$1').replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/^#+\s*/gm, '').replace(/^\s*[-*]\s+/gm, '').replace(/\s+/g, ' ').trim()
  }
  function wrapWords(c, max) {
    const out = []
    let cur = ''
    for (const w of c.split(' ')) {
      if (cur && (cur + ' ' + w).length > max) { out.push(cur); cur = w } else cur = cur ? cur + ' ' + w : w
    }
    if (cur) out.push(cur)
    return out
  }
  // Split a message into subtitle pages at sentence ends. No words are dropped.
  function paginate(text, max = 170) {
    const p = plain(text)
    const parts = p.match(/[^.!?]+(?:[.!?]+["')\]]*|$)\s*/g) || []
    if (parts.join('').replace(/\s/g, '') !== p.replace(/\s/g, '')) return wrapWords(p, max)
    const out = []
    let cur = ''
    for (const s of parts) {
      if (cur && (cur + s).length > max) { out.push(cur.trim()); cur = '' }
      cur += s
    }
    if (cur.trim()) out.push(cur.trim())
    return out.flatMap(c => (c.length <= max * 1.3 ? [c] : wrapWords(c, max)))
  }

  function mdHTML(text) {
    let html = '', list = null, para = [], code = null
    const flushP = () => { if (para.length) { html += `<p>${inline(para.join(' '))}</p>`; para = [] } }
    const flushL = () => {
      if (!list) return
      html += `<${list.tag}>${list.items.map(i => `<li${i.n ? ` value="${i.n}"` : ''}>${inline(i.t)}</li>`).join('')}</${list.tag}>`
      list = null
    }
    for (const raw of String(text).replace(/\r/g, '').split('\n')) {
      if (code) { if (/^\s*```/.test(raw)) { html += `<pre>${esc(code.join('\n'))}</pre>`; code = null } else code.push(raw); continue }
      if (/^\s*```/.test(raw)) { flushP(); flushL(); code = []; continue }
      const l = raw.trimEnd()
      let m
      if (!l.trim()) { flushP(); flushL(); continue }
      if ((m = l.match(/^\s*#{1,6}\s+(.*)$/))) { flushP(); flushL(); html += `<h4>${inline(m[1])}</h4>`; continue }
      if ((m = l.match(/^\s*[-*•]\s+(.*)$/))) {
        flushP(); if (!list || list.tag !== 'ul') { flushL(); list = { tag: 'ul', items: [] } }
        list.items.push({ t: m[1] }); continue
      }
      if ((m = l.match(/^\s*(\d+)[.)]\s+(.*)$/))) {
        flushP(); if (!list || list.tag !== 'ol') { flushL(); list = { tag: 'ol', items: [] } }
        list.items.push({ n: m[1], t: m[2] }); continue
      }
      if (list && /^\s{2,}\S/.test(raw)) { list.items[list.items.length - 1].t += ' ' + l.trim(); continue }
      flushL(); para.push(l.trim())
    }
    if (code) html += `<pre>${esc(code.join('\n'))}</pre>`
    flushP(); flushL()
    return html
  }

  // What a terminal shows for one command: the head of its output, a marked gap, the tail.
  function termRows(T) {
    const head = lines(T.output), tail = lines(T.output_tail)
    let top = head, end = [], hidden = 0
    if (tail.length) {
      top = head.slice(0, SHOW_TOP); end = tail
      hidden = T.total_lines ? T.total_lines - top.length - end.length : head.length - top.length + (T.omitted_lines || 0)
    } else if (head.length > SHOW_TOP + SHOW_END + 1) {
      top = head.slice(0, SHOW_TOP); end = head.slice(-SHOW_END)
      hidden = head.length - top.length - end.length
    }
    const rows = top.map(t => ({ c: 'out', t }))
    if (hidden > 0) rows.push({ c: 'dim', t: `··· ${hidden} more line${hidden === 1 ? '' : 's'} ···` })
    for (const t of end) rows.push({ c: 'out', t })
    if (T.exit_code) rows.push({ c: 'err', t: T.error_text || `Exit code ${T.exit_code}` })
    else if (T.empty && !rows.length) rows.push({ c: 'dim', t: '(no output)' })
    if (T.interrupted) rows.push({ c: 'err', t: 'Interrupted' })
    if (T.harness_note) rows.push({ c: 'note', t: `↳ Claude Code: ${T.harness_note}` })
    return rows
  }

  const helperOf = key => (S && S.helpers.get(key)) || null
  // Claude Code or Codex: the watched session's tool (from the server's hello).
  const toolOf = () => (S && S.hello && S.hello.session && S.hello.session.tool) || 'claude'
  const toolName = t => (t === 'codex' ? 'Codex' : 'Claude')
  let TOOL = null            // whose windows the menu lists; null means the watched session's tool
  function agentLabel(key) {
    if (!key || key === 'main') return toolName(toolOf())
    const h = helperOf(key), word = toolOf() === 'codex' ? 'Subagent' : 'Helper'
    return h && h.description ? `${word} · ${h.description}` : `${word} agent`
  }

  /* ---------- session state ---------- */

  function reset(hello) {
    S = {
      hello, live: hello.live !== false, items: [], byStep: new Map(), helpers: new Map(),
      ready: false, histN: 0, lastLive: null, catchups: [], floor: 0, floorWin: null, status: 'working', statusAt: now(), lastEventAt: now(), lastAgent: 'main',
      dirty: true, D: null,
    }
    cache = {}
    for (const id of ['term-body', 'term-tabs', 'edit-code', 'chrome-page', 'helper-page', 'msg-body', 'term-co', 'edit-co', 'chrome-co', 'helper-co', 'chips', 'subtitle', 'banner']) {
      const el = $(id)
      if (el) el.innerHTML = ''
    }
  }

  function winFor(s, helper) {
    switch (s.kind) {
      case 'terminal': return 'term'
      case 'read': case 'edit': case 'write': return 'edit'
      case 'search': case 'fetch': return helper ? 'helper' : 'chrome'
      case 'helper_start': case 'helper_end': return 'helper'
      case 'say': return plain((s.say && s.say.text) || '').length > LONG_SAY ? 'msg' : null
      case 'prompt': case 'other': return null
      default: return undefined // not drawn
    }
  }
  function gap(it) {
    if (it.fast) return 0.15
    if (it.re) return 3 // long enough to read a late result
    switch (it.s.kind) {
      case 'terminal': return 1.1
      case 'say': return 0.6
      case 'prompt': return 0.4
      case 'other': return 0.25
      default: return 0.9
    }
  }

  // A helper_start step names the helper; its own steps arrive as agent "helper:<id>".
  function noteHelper(s) {
    const h = s.kind === 'helper_start' && s.helper_start
    if (h && h.id) S.helpers.set('helper:' + h.id, h)
  }
  const helperKeyOf = s => (s.kind === 'helper_start' && s.helper_start && s.helper_start.id ? 'helper:' + s.helper_start.id : null)
  function addStep(s, history) {
    noteHelper(s)
    const agent = s.agent || 'main'
    if (agent.startsWith('helper:') && !S.helpers.has(agent)) {
      // A helper's own steps can arrive before its helper_start says which helper it is:
      // pair them with the newest helper_start that has no helper yet.
      const open = [...S.items].reverse().find(x => x.s.kind === 'helper_start' && !helperKeyOf(x.s) && !x.claimed)
      if (open) { open.claimed = agent; S.helpers.set(agent, open.s.helper_start || {}) }
    }
    const it = { s, i: s.i, agent, helper: agent.startsWith('helper:'), flags: [] }
    it.win = winFor(s, it.helper)
    if (it.win === undefined) return
    if (history || !S.ready) {
      it.v0 = HIST + S.histN++ * 0.01
      it.fast = true
    } else {
      // Live: queue behind the previous live step so bursts play one after another,
      // but never trail real time by more than MAX_LAG.
      const t = now(), prev = S.lastLive
      let v0 = prev ? Math.max(t, prev.v0 + gap(prev)) : t
      // A hold (a callout or a late result being read) also delays window steps that arrive later. A terminal
      // callout sits in its own lane, so more terminal commands can go on under it; only a window change waits.
      if (it.win && !(S.floorWin === 'term' && it.win === 'term')) v0 = Math.max(v0, S.floor)
      if (v0 - t > MAX_LAG) {
        // Too far behind: everything still waiting jumps to now, so the queue stays in order.
        let n = 1
        for (const y of S.items) if (y.v0 > t) { y.v0 = t; y.fast = true; y.caught = true; n++ }
        v0 = t; it.fast = true; it.caught = true
        S.catchups.push({ at: t, n })
      }
      it.v0 = v0
      S.lastLive = it
    }
    it.doneAt = s.state === 'running' ? null : it.v0
    S.items.push(it)
    S.byStep.set(s.i, it)
    S.lastAgent = agent
  }
  function updateStep(s, history) {
    const it = S.byStep.get(s.i)
    if (!it) { addStep(s, history); return }
    noteHelper(s)
    it.s = s
    const finished = s.state !== 'running' && it.doneAt == null
    if (finished) it.doneAt = history || !S.ready ? it.v0 : Math.max(now(), it.v0)
    S.lastAgent = it.agent
    // A result that lands after the agent moved on still gets shown.
    if (finished && !history && S.ready && !it.fast && ['search', 'fetch', 'read', 'edit', 'write'].includes(s.kind)) {
      if (!hold(it, Math.max(now(), phases(it).shown) + RESULT_MIN)) revisit(it)
    }
  }
  function addFlag(f, history) {
    const it = S.byStep.get(f.step)
    if (!it) return
    it.flags.push({ ...f, arrived: history || !S.ready ? HIST : now() })
    if (!history && S.ready && !it.fast && it.win) {
      const P = phases(it)
      if (!hold(it, Math.max(now(), P.shown == null ? now() : P.shown) + 0.15 + CO_MIN)) revisit(it)
    }
  }

  // Keep `it` on screen until `until` by delaying the steps queued after it that haven't started.
  // Returns false if a later step already took the screen (then the caller can revisit instead).
  function hold(it, until) {
    const k = S.items.indexOf(it)
    const later = S.items.slice(k + 1)
    if (later.some(y => y.win && y.v0 <= now())) return false
    if (until - now() <= MAX_LAG + 3 && until > S.floor) { S.floor = until; S.floorWin = it.win } // later arrivals wait too
    const j = later.findIndex(y => y.win && !(it.win === 'term' && y.win === 'term'))
    if (j < 0) return true
    const d = until - later[j].v0
    if (d <= 0) return true
    if (later[later.length - 1].v0 + d - now() > MAX_LAG + 3) return false
    for (const y of later.slice(j)) y.v0 += d
    return true
  }
  // Go back to a step's window briefly (its late result or its flag), queued like a new step.
  // Never for the terminal: re-showing a command there would look like it ran again.
  function revisit(it) {
    if (!it.win || it.win === 'term' || it.win === 'msg') return
    if (it.rev && it.rev.v0 > now()) return // one is already queued; it shares this step's flags
    const t = now(), prev = S.lastLive
    const R = { s: it.s, i: it.i, agent: it.agent, helper: it.helper, claimed: it.claimed, flags: it.flags, win: it.win, re: true }
    R.v0 = Math.max(prev ? Math.max(t, prev.v0 + gap(prev)) : t, S.floor)
    R.doneAt = R.v0
    it.rev = R
    S.items.push(R)
    S.lastLive = R
  }

  function onEvent(m) {
    if (m.type === 'hello') { reset(m); return }
    if (!S) return
    S.lastEventAt = now()
    switch (m.type) {
      case 'ready': S.ready = true; break
      case 'step': addStep(m.step, m.history); break
      case 'update': updateStep(m.step, m.history); break
      case 'flag': addFlag(m.flag, m.history); break
      case 'status': S.status = m.status; S.statusAt = now(); break
    }
    S.dirty = true
    wake()
  }

  /* ---------- phases of one step, on the client clock ---------- */

  function phases(it) {
    const s = it.s, f = it.fast, q = f || it.re // q: no typing (backlog, or a revisit)
    const P = {}
    switch (s.kind) {
      case 'terminal': {
        P.typ = q ? 0 : clamp(0.12 + ((s.terminal && s.terminal.command) || '').length * 0.0022, 0.25, 0.6)
        P.rows = s.state === 'running' ? [] : termRows(s.terminal || {})
        if (it.doneAt != null) {
          P.out = Math.max(it.doneAt, it.v0 + P.typ + (f ? 0 : 0.28))
          P.shown = P.out + (f ? 0 : P.rows.length > 2 ? 0.5 : 0.18)
        }
        break
      }
      case 'search':
        P.typ = q ? 0 : clamp(0.1 + ((s.search && s.search.query) || '').length * 0.004, 0.25, 0.55)
        if (it.doneAt != null) P.out = P.shown = Math.max(it.doneAt, it.v0 + P.typ + (f ? 0 : 0.25))
        break
      case 'fetch':
        P.typ = q ? 0 : 0.45
        if (it.doneAt != null) P.out = P.shown = Math.max(it.doneAt, it.v0 + P.typ + (f ? 0 : 0.3))
        break
      case 'read': case 'edit': case 'write':
        if (it.doneAt != null) P.out = P.shown = Math.max(it.doneAt, it.v0 + (f ? 0 : 0.35))
        break
      default:
        P.out = P.shown = it.v0 + (f ? 0 : 0.3)
    }
    return P
  }

  /* ---------- derived layout: windows, camera, callouts ---------- */

  function camFor(r) {
    const m = 34, availH = H - 170
    const s = clamp(Math.min(W / (r.w + 2 * m), availH / (r.h + 2 * m)), 1, 1.42)
    return {
      s,
      x: clamp(W / 2 - s * (r.x + r.w / 2), W - s * W, 0),
      y: clamp(availH / 2 + 10 - s * (r.y + r.h / 2), H - s * H, 0),
    }
  }
  const union = (a, b) => {
    const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y)
    return { x, y, w: Math.max(a.x + a.w, b.x + b.w) - x, h: Math.max(a.y + a.h, b.y + b.h) - y }
  }
  const mixCam = (a, b, k) => ({ s: lerp(a.s, b.s, k), x: lerp(a.x, b.x, k), y: lerp(a.y, b.y, k) })
  const FULL = { s: 1, x: 0, y: 0 }
  const LANE = { x: GEO.term.x + GEO.term.w + 18, y: GEO.term.y, w: CO_W, h: 200 }
  const RANK = { error: 4, repeat: 3, found: 2, fixed: 2, quiet: 1 }

  function derive() {
    const seq = S.items.filter(it => follow === 'all' || it.agent === follow || (helperKeyOf(it.s) || it.claimed) === follow)
    const winItems = seq.filter(it => it.win)

    // Windows: the terminal stays open; any other window closes once a different window is used.
    const spans = { term: [], chrome: [], helper: [], edit: [], msg: [] }
    const acts = { term: [], chrome: [], helper: [], edit: [], msg: [] }
    const isOpen = w => spans[w].length > 0 && spans[w][spans[w].length - 1].close == null
    const open = (w, t) => { if (!isOpen(w)) spans[w].push({ open: t, close: null }); acts[w].push(t) }
    const shut = (w, t) => { if (isOpen(w)) spans[w][spans[w].length - 1].close = t }
    for (const it of winItems) {
      for (const w of ['chrome', 'helper', 'edit', 'msg']) if (w !== it.win) shut(w, it.v0)
      open(it.win, it.v0)
      if (it.s.kind === 'helper_end') shut('helper', it.v0 + 3.5)
    }

    // Callouts: shown once the step's result is on screen, gone by the next action.
    const marks = []
    winItems.forEach((it, k) => {
      if (!it.flags.length) return
      const P = phases(it)
      if (P.shown == null) return
      const at = Math.max(P.shown + 0.15, ...it.flags.map(f => f.arrived))
      const next = winItems[k + 1]
      const hide = Math.max(at + CO_MIN, Math.min(next ? next.v0 : Infinity, at + 7))
      const level = it.flags.slice().sort((a, b) => (RANK[b.level] || 0) - (RANK[a.level] || 0))[0].level
      marks.push({ item: it, win: it.win, at, hide, level })
    })

    // Camera: one framing per window step; a terminal step with a callout also frames the lane.
    const kfs = [{ t: HIST - 10, cam: FULL }]
    let at = 'full'
    for (const it of winItems) {
      if (it.win !== at) { kfs.push({ t: it.v0, cam: camFor(GEO[it.win]) }); at = it.win }
      const m = it.win === 'term' && marks.find(x => x.item === it)
      if (m) { kfs.push({ t: Math.max(it.v0, m.at - CAM_T), cam: camFor(union(GEO.term, LANE)) }); at = 'term+lane' }
    }
    kfs.sort((a, b) => a.t - b.t)
    kfs[0].from = FULL
    for (let k = 1; k < kfs.length; k++) {
      const p = kfs[k - 1]
      kfs[k].from = mixCam(p.from, p.cam, ease(clamp((kfs[k].t - p.t) / CAM_T)))
    }

    const says = seq.filter(it => it.s.kind === 'say' && !it.win && (!it.fast || it.caught))
    says.forEach((it, k) => {
      const len = plain(it.s.say.text).length
      it.chunks = paginate(it.s.say.text)
      it.until = Math.min(k + 1 < says.length ? says[k + 1].v0 : Infinity, it.v0 + clamp(1.6 + len * 0.03, 3, 7.5))
    })

    // The moments when something moves on screen: typing and output reveals, results that land late,
    // subtitles and the dock sliding, callouts appearing (with the error shake) and fading, camera
    // moves, windows opening and closing. Only then does the loop draw smoothly; see the end of the file.
    const anim = [], soon = now() - 1
    const add = (a, b) => { if (b > soon) anim.push([a, b]) }
    for (const it of seq) {
      add(it.v0, it.v0 + 2)
      if (it.doneAt != null) add(it.doneAt, it.doneAt + 1.2)
      if (it.s.kind === 'prompt') add(it.v0 + 6.6, it.v0 + 7.1)
    }
    for (const it of says) { add(it.v0, it.v0 + 0.5); add(it.until - 0.3, it.until + 0.5) }
    for (const m of marks) { add(m.at - 0.1, m.at + 1.5); add(m.hide - 0.3, m.hide + 0.1) }
    for (const k of kfs) add(k.t, k.t + CAM_T)
    for (const w in spans) for (const sp of spans[w]) { add(sp.open, sp.open + WIN_T); if (sp.close != null) add(sp.close, sp.close + CLOSE_T) }

    S.D = {
      seq, winItems, spans, acts, marks, kfs, says, anim,
      agentsWithTerm: [...new Set(winItems.filter(it => it.win === 'term').map(it => it.agent))],
      termItems: winItems.filter(it => it.win === 'term'),
      editItems: winItems.filter(it => it.win === 'edit'),
      chromeItems: winItems.filter(it => it.win === 'chrome'),
      helperItems: winItems.filter(it => it.win === 'helper'),
      msgItems: winItems.filter(it => it.win === 'msg'),
      prompts: seq.filter(it => it.s.kind === 'prompt' && !it.fast),
      others: seq.filter(it => it.s.kind === 'other' && (!it.fast || it.caught)),
    }
    S.dirty = false
  }

  function latest(list, t) {
    let r = null
    for (const it of list) { if (it.v0 <= t) r = it; else break }
    return r
  }
  function camAt(t) {
    const k = S.D.kfs
    let i = 0
    while (i + 1 < k.length && k[i + 1].t <= t) i++
    return mixCam(k[i].from, k[i].cam, ease(clamp((t - k[i].t) / CAM_T)))
  }

  /* ---------- scene ---------- */

  function buildScene() {
    const lights = '<div class="lights"><i></i><i></i><i></i></div>'
    const win = (id, cls, inner) => {
      const el = document.createElement('div')
      el.id = 'w-' + id
      el.className = 'win ' + cls
      const g = GEO[id]
      Object.assign(el.style, { left: g.x + 'px', top: g.y + 'px', width: g.w + 'px', height: g.h + 'px' })
      el.innerHTML = inner
      $('windows').appendChild(el)
    }
    win('term', 'dark', `<div class="tbar">${lights}<div class="ttl" id="term-title"></div></div><div id="term-tabs"></div><div id="term-body"></div>`)
    for (const id of ['chrome', 'helper']) {
      win(id, 'chrome', `<div class="ch-strip" id="${id}-strip"></div><div class="ch-bar"><span class="ch-nav">‹ › ↻</span><div class="ch-omni" id="${id}-omni"></div></div>` +
        `<div class="ch-page" id="${id}-page"></div><div class="callouts" id="${id}-co"></div>`)
    }
    win('edit', 'dark', `<div class="tbar">${lights}<div class="ttl" id="edit-title"></div></div><div class="ed-tabs" id="edit-tabs"></div><div class="ed-crumbs" id="edit-crumbs"></div>` +
      `<div class="ed-code" id="edit-code"></div><div class="ed-status" id="edit-status"></div><div class="callouts" id="edit-co"></div>`)
    win('msg', 'light', `<div class="tbar">${lights}<div class="ttl" id="msg-title"></div></div><div id="msg-body"></div>`)
    const lane = document.createElement('div') // the terminal's callouts sit just outside its right edge
    lane.id = 'term-co'
    lane.className = 'callouts out'
    Object.assign(lane.style, { left: LANE.x + 'px', top: GEO.term.y + 34 + 'px', width: CO_W + 'px' })
    $('windows').appendChild(lane)
    $('dock').innerHTML = DOCK.map(([w, cls, inner]) => `<div class="dk" id="dk-${w}"><div class="ic ${cls}">${inner}</div><div class="run" id="run-${w}"></div></div>`).join('')
  }

  /* ---------- windows ---------- */

  const BASE_SHADOW = '0 28px 70px rgba(0,0,0,.42), 0 0 0 1px rgba(0,0,0,.28)'
  const GLOW = { error: '215,55,47', quiet: '214,150,40', repeat: '124,77,206', found: '47,111,214', fixed: '46,155,71' }
  function shadowFor(fx) {
    if (!fx || fx.glow <= 0) return BASE_SHADOW
    const c = GLOW[fx.level] || GLOW.quiet, g = fx.glow
    return `0 0 0 ${(3 * g).toFixed(2)}px rgba(${c},${(0.95 * g).toFixed(3)}), 0 0 ${(60 * g).toFixed(1)}px rgba(${c},${(0.6 * g).toFixed(3)}), ${BASE_SHADOW}`
  }
  function effectFor(w, t) {
    let m = null
    for (const f of S.D.marks) if (f.win === w && f.at <= t && t - f.at < 1.4) m = f
    if (!m) return null
    const e = t - m.at
    return {
      level: m.level,
      glow: Math.max(0, 1 - e / 1.4),
      shake: m.level === 'error' ? Math.sin(e * 2 * Math.PI * 7) * 8 * Math.max(0, 1 - e / 0.5) : 0,
    }
  }
  function winStates(t) {
    const st = {}
    for (const w of Object.keys(GEO)) {
      let span = null
      for (const x of S.D.spans[w]) if (x.open <= t && (x.close == null || t < x.close + CLOSE_T)) span = x
      if (!span) { st[w] = { on: false }; continue }
      let o = ease(clamp((t - span.open) / WIN_T)), sc = 0.95 + 0.05 * o
      if (span.close != null && t >= span.close) {
        const k = ease(clamp((t - span.close) / CLOSE_T))
        o *= 1 - k; sc *= 1 - 0.04 * k
      }
      let last = -Infinity
      for (const x of S.D.acts[w]) if (x <= t) last = x
      st[w] = { on: true, o, sc, last }
    }
    return st
  }

  const SPIN = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
  function blockHTML(it, t, P, live) {
    const T = it.s.terminal || {}, cmd = T.command || ''
    const e = t - it.v0
    const typed = !live || e >= P.typ ? cmd.length : Math.floor(cmd.length * clamp(e / P.typ))
    let h = `<div class="tl"><span class="tp">${esc(cwdLabel(T.cwd))}</span> <span class="tpc">%</span> <span class="tc">${esc(cmd.slice(0, typed))}</span>`
    if (P.out == null) {
      // Still running: the cursor waits after the command, with the time it has run so far.
      const blink = Math.floor(t * 2) % 2 === 0
      h += `<span class="cur${live && e < P.typ || blink ? '' : ' off'}"> </span></div>`
      if (!live || e >= P.typ + 0.6) h += `<div class="dim">${SPIN[Math.floor(t * 10) % SPIN.length]} running${live ? ` · ${dur(e)}` : ''}</div>`
      return h
    }
    if (live && t < P.out) return h + '<span class="cur"> </span></div>'
    h += '</div>'
    const n = !live || t >= P.shown ? P.rows.length : Math.ceil(P.rows.length * clamp((t - P.out) / Math.max(0.01, P.shown - P.out)))
    for (const r of P.rows.slice(0, n)) h += `<div class="${r.c}">${esc(r.t) || ' '}</div>`
    return h
  }
  function renderTerm(t) {
    const D = S.D
    const cur = latest(D.termItems, t)
    if (!cur) { setHTML('term-body', 'empty', () => ''); return }
    const agent = cur.agent
    const mine = D.termItems.filter(it => it.agent === agent && it.v0 <= t)
    const P = phases(cur)
    const live = !cur.fast
    const done = P.shown != null && t >= P.shown
    const blink = Math.floor(t * 2) % 2 === 0
    const e = t - cur.v0
    const phase = P.out == null ? `run${Math.floor(t * 10)}` : t < P.shown ? `out${Math.floor((t - P.out) * 30)}` : 'done'
    const typed = !live || e >= P.typ ? 'all' : Math.floor(e * 60)
    setHTML('term-body', `${cur.i}|${cur.s.state}|${typed}|${phase}|${done && blink}|${mine.length}`, () => {
      let h = ''
      for (const it of mine.slice(-6, -1)) h += blockHTML(it, t, phases(it), false)
      h += blockHTML(cur, t, P, live)
      if (done) {
        const T = cur.s.terminal || {}
        h += `<div class="tl"><span class="tp">${esc(cwdLabel(T.cwd_after || T.cwd))}</span> <span class="tpc">%</span> <span class="cur${blink ? '' : ' off'}"> </span></div>`
      }
      return h
    })
    const T = cur.s.terminal || {}
    $('term-title').textContent = `${cwdLabel(done ? T.cwd_after || T.cwd : T.cwd)} — -zsh`
    // One tab per agent that has used the terminal.
    const tabs = D.agentsWithTerm
    $('w-term').classList.toggle('tabs', tabs.length > 1)
    setHTML('term-tabs', `${tabs.join(',')}|${agent}`, () => tabs.map(a => `<div class="tt-tab${a === agent ? ' on' : ''}">${esc(agentLabel(a))}</div>`).join(''))
  }

  function crumbs(p) {
    const parts = tildify(p).split('/').filter(Boolean)
    return (parts.length > 6 ? [...parts.slice(0, 2), '…', ...parts.slice(-3)] : parts).join('  ›  ')
  }
  // Read output keeps Claude Code's "N<tab>" prefixes; split them into a line-number column.
  function numbered(text) {
    const raw = lines(Array.isArray(text) ? text : String(text || '').replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, ''))
    return raw.map((l, k) => { const m = l.match(/^\s*(\d+)[\t→](.*)$/); return m ? [m[1], m[2]] : [String(k + 1), l] })
  }
  function rowsHTML(rows, more) {
    return rows.map(([n, l, cls]) => `<div class="dl ${cls || ''}"><span class="no">${esc(n)}</span><span>${esc(l) || ' '}</span></div>`).join('') +
      (more > 0 ? `<div class="ed-more">··· ${more} more line${more === 1 ? '' : 's'} ···</div>` : '')
  }
  // Claude Code's own patch: " " context, "-" removed, "+" added. New-file numbers from start_line.
  function patchRows(patch, start) {
    let n = Number(start) || 1
    return lines(patch).map(l => {
      if (l.startsWith('@@')) { const m = l.match(/\+(\d+)/); if (m) n = Number(m[1]); return ['', l, 'hunk'] }
      const c = l[0], body = l.slice(1)
      if (c === '-') return ['', body, 'del']
      if (c === '+') return [String(n++), body, 'add']
      return [String(n++), c === ' ' ? body : l, '']
    })
  }
  function renderEdit(t) {
    const it = latest(S.D.editItems, t)
    if (!it) return
    const s = it.s, F = s[s.kind] || {}, path = F.path || ''
    const name = fileName(path), P = phases(it)
    const shown = P.out != null && t >= P.out
    const tool = s.kind === 'read' ? 'Read' : s.kind === 'write' ? 'Write' : 'Edit'
    const tag = s.kind === 'write' ? '<span class="tag" style="background:#2e9b47">NEW</span>' : s.kind === 'edit' ? '<span class="tag" style="background:#b7791f">EDIT</span>' : ''
    setHTML('edit-tabs', `tab${it.i}|${s.kind}`, () => `<div class="ed-tab">${tag}${esc(name)}</div>`)
    setHTML('edit-crumbs', `cr${it.i}`, () => esc(crumbs(path)))
    $('edit-title').textContent = `${name} — ${cwdLabel(String(path).replace(/\/[^/]+$/, ''))}`
    setHTML('edit-code', `${it.i}|${s.state}|${shown}`, () => {
      if (!shown) return `<div class="ed-wait">${s.kind === 'read' ? 'Opening' : s.kind === 'write' ? 'Writing' : 'Editing'} ${esc(name)}…</div>`
      if (F.error || s.status === 'error') {
        return `<div class="ed-err"><div class="t">${tool} failed: ${esc(name)}${F.pages ? `, pages ${esc(F.pages)}` : ''}</div>` +
          `<div class="m">${esc(F.error || '')}</div><div class="who">The error Claude Code's ${tool} tool returned</div></div>`
      }
      if (F.patch && lines(F.patch).length) return rowsHTML(patchRows(F.patch, F.start_line), F.patch_omitted || 0)
      if (s.kind === 'edit') {
        return `${F.replace_all ? '<div class="ed-more">Replace every occurrence</div>' : ''}<div class="ed-lab r">Removed</div>` +
          rowsHTML(lines(F.old).map(l => ['−', l, 'del']), F.old_omitted_lines || 0) +
          '<div class="ed-lab g">Added</div>' + rowsHTML(lines(F.new).map(l => ['+', l, 'add']), F.new_omitted_lines || 0)
      }
      const rows = numbered(F.lines).map(r => [...r, s.kind === 'write' ? 'add' : ''])
      const top = rows.slice(0, 26)
      return rowsHTML(top, rows.length - top.length + (F.omitted_lines || 0))
    })
    const what = s.kind === 'read' ? 'Read' : s.kind === 'write' ? (F.patch ? 'Rewrote file' : 'New file') : 'Edited'
    // Same moment as the body: "Edited" only once the result is on screen.
    setHTML('edit-status', `st${it.i}|${shown}`, () => `<span>${esc(agentLabel(it.agent))}</span><span>${shown ? what : 'Working…'}</span>${shown && num(F.total_lines) ? `<span>${num(F.total_lines)} lines</span>` : ''}`)
  }

  function tabHTML(x, on) {
    if (!x) return '<div class="ch-tab on"><span class="fav" style="background:#9aa0a6"></span><span class="tt">New Tab</span></div>'
    if (x.s.kind === 'search') return `<div class="ch-tab${on ? ' on' : ''}"><span class="fav" style="background:#7b61d1">🔍</span><span class="tt">${esc(x.s.search.query)}</span></div>`
    const F = x.s.fetch, pdf = F.content_type === 'pdf' || /\.pdf($|\?)/i.test(F.url || '')
    return `<div class="ch-tab${on ? ' on' : ''}"><span class="fav" style="background:${pdf ? '#d23f31' : '#5f6368'}">${pdf ? 'PDF' : esc(domain(F.url).slice(0, 1).toUpperCase())}</span><span class="tt">${esc(fileName(F.final_url || F.url))}</span></div>`
  }
  function searchPage(it, t, P) {
    const Sq = it.s.search, res = Sq.results || []
    const e = t - it.v0
    const typed = it.fast || e >= P.typ ? Sq.query.length : Math.floor(Sq.query.length * clamp(e / P.typ))
    const n = P.out == null || t < P.out ? 0 : it.fast ? res.length : Math.min(res.length, Math.ceil(res.length * clamp((t - P.out) / 0.6)))
    const meta = n ? `${res.length} results${Sq.duration ? ` · ${Number(Sq.duration).toFixed(1)} s` : ''}` : e >= P.typ ? `Searching… ${dur(e)}` : '&nbsp;'
    return {
      key: `${typed}|${n}|${P.out == null ? Math.floor(t) : 'd'}`,
      html: () => `<div class="sr"><div class="sr-top"><span class="sr-brand">${esc(toolName(toolOf()))}'s web search tool</span><div class="sr-box">${esc(Sq.query.slice(0, typed))}</div></div>
        <div class="sr-meta">${meta}</div>
        ${res.slice(0, n).map(r => {
          const d = domain(r.url)
          return `<div class="sr-item"><div class="sr-site"><span class="fv">${esc((d[0] || '?').toUpperCase())}</span><div><span class="d">${esc(d)}</span><span class="u">${esc(trunc(r.url, 120))}</span></div></div><div class="sr-title">${esc(r.title)}</div></div>`
        }).join('')}</div>`,
    }
  }
  function fetchPage(it, t, P) {
    const F = it.s.fetch, ready = P.out != null && t >= P.out
    const pdf = F.content_type === 'pdf' || /\.pdf($|\?)/i.test(F.url || '')
    return {
      key: `${ready}|${ready ? '' : Math.floor(t)}`,
      html: () => {
        if (!ready) return `<div class="${pdf ? 'pdfv' : 'fpage'}"><div class="wait">Fetching ${esc(domain(F.url))}… ${dur(t - it.v0)}</div></div>`
        let h = pdf ? `<div class="pdfv"><div class="pdfv-bar"><span>${esc(fileName(F.final_url || F.url))}</span><span class="pg">${num(F.pdf_pages) ? `${num(F.pdf_pages)} pages` : ''}</span></div>` : '<div class="fpage">'
        h += `<div class="raw"><h5>What the fetch tool gave the ${it.helper ? 'helper' : 'agent'}</h5><div class="hdr">${esc(F.header || `HTTP ${F.http ?? ''} ${F.http_text ?? ''}`)}</div>`
        if (F.raw_excerpt) h += `<pre>${esc(lines(F.raw_excerpt).slice(0, 12).join('\n'))}</pre>${F.cut_note ? `<div class="cut">${esc(F.cut_note)}</div>` : ''}`
        else if (F.summary) h += `<div class="txt">${esc(lines(F.summary).slice(0, 18).join('\n'))}</div>`
        return h + '</div></div>'
      },
    }
  }
  function textCard(kick, title, text, extraMore) {
    const L = lines(text), top = L.slice(0, 15), more = L.length - top.length + (extraMore || 0)
    return `<div class="newtab"><div class="hcard"><h4>${kick}</h4>${title ? `<h3>${esc(title)}</h3>` : ''}<div class="body">${esc(top.join('\n'))}</div>` +
      `${more > 0 ? `<div class="more">··· ${more} more line${more === 1 ? '' : 's'} ···</div>` : ''}</div></div>`
  }
  function renderBrowser(win, t) {
    const list = win === 'chrome' ? S.D.chromeItems : S.D.helperItems
    const cur = latest(list, t)
    if (!cur) return
    const P = phases(cur), e = t - cur.v0
    // The helper window shows one helper at a time: whichever acted last.
    const isTask = cur.s.kind === 'helper_start'
    const who = win === 'helper' ? (isTask ? helperKeyOf(cur.s) || cur.claimed || null : cur.agent) : null
    const scope = win === 'helper' ? list.filter(x => who && x.agent === who) : list
    const seen = new Set() // a revisited step keeps its one tab
    const tabs = scope.filter(x => x.v0 <= t && (x.s.kind === 'search' || x.s.kind === 'fetch') && !seen.has(x.i) && seen.add(x.i))
    const activeTab = tabs.find(x => x.i === cur.i) || (tabs.length ? tabs[tabs.length - 1] : null)
    setHTML(win + '-strip', `${tabs.map(x => x.i).join(',')}|${who}|${cur.i}|${activeTab && activeTab.i}`, () =>
      `<div class="lights"><i></i><i></i><i></i></div>${(tabs.length ? tabs.slice(-4) : [null]).map(x => tabHTML(x, x === activeTab)).join('')}` +
      (win === 'helper' ? `<div class="ch-helper">${esc(isTask ? `Helper · ${(cur.s.helper_start || {}).description || 'agent'}` : agentLabel(who))}</div>` : ''))

    let omni = ''
    if (activeTab && activeTab.s.kind === 'search') {
      const q = activeTab.s.search.query, n = activeTab === cur && !cur.fast && e < P.typ ? Math.floor(q.length * e / P.typ) : q.length
      omni = `<span class="q">🔍</span>${esc(q.slice(0, n))}`
    } else if (activeTab) {
      const u = String(activeTab.s.fetch.final_url || activeTab.s.fetch.url).replace(/^https?:\/\//, '')
      const n = activeTab === cur && !cur.fast && e < P.typ ? Math.floor(u.length * e / P.typ) : u.length
      omni = `<span class="lk">🔒</span>${esc(u.slice(0, n))}`
    }
    setHTML(win + '-omni', omni, () => omni)

    const k = cur.s.kind
    const pg = k === 'search' ? searchPage(cur, t, P)
      : k === 'fetch' ? fetchPage(cur, t, P)
      : k === 'helper_start' ? { key: String(t >= P.out), html: () => { const h = cur.s.helper_start || {}; return t < P.out ? '<div class="newtab"></div>' : textCard(`TASK FROM ${esc(toolName(toolOf()).toUpperCase())} · ${esc(h.type || 'helper')} agent`, h.description, h.prompt || (toolOf() === 'codex' ? "Codex doesn't log what it asked this subagent to do. Its steps and its answer show up as it works." : '')) } }
      : { key: String(t >= P.out), html: () => { const h = cur.s.helper_end || {}; return t < P.out ? '<div class="newtab"></div>' : textCard('HANDED BACK TO CLAUDE', '', h.report, h.omitted_lines) } }
    setHTML(win + '-page', `${cur.i}|${cur.s.state}|${pg.key}`, pg.html)
  }

  function renderMsg(t) {
    const it = latest(S.D.msgItems, t)
    if (!it) return
    setHTML('msg-body', 'm' + it.i, () => `<div class="msg-inner" id="msg-inner"><div class="who">${esc(agentLabel(it.agent))}</div>${mdHTML(it.s.say.text)}</div>`)
    $('msg-title').textContent = agentLabel(it.agent)
    // Scroll slowly through a long message, after a pause on its opening.
    const inner = $('msg-inner'), box = $('msg-body')
    const max = Math.max(0, inner.scrollHeight - box.clientHeight)
    const words = plain(it.s.say.text).split(' ').length
    const k = it.fast ? 0 : ease(clamp((t - it.v0 - 3) / clamp(words * 0.22, 6, 90)))
    inner.style.transform = `translateY(${(-max * k).toFixed(1)}px)`
  }

  function renderCallouts(t) {
    for (const w of ['term', 'edit', 'chrome', 'helper']) {
      const id = w + '-co', box = $(id)
      let m = null
      for (const f of S.D.marks) if (f.win === w && f.at <= t && t < f.hide) m = f
      if (!m) { setHTML(id, 'none', () => ''); continue }
      // A terminal callout can land after a newer command is on screen: say which command it's about.
      const cur = w === 'term' ? latest(S.D.termItems, t) : null
      const about = cur && cur.i !== m.item.i ? `<p class="about">About an earlier command: <code>${esc(trunc((m.item.s.terminal || {}).command || '', 70))}</code></p>` : ''
      setHTML(id, `f${m.item.i}|${m.item.flags.length}|${!!about}`, () => m.item.flags.map(f => `<div class="co ${esc(f.level)}">${esc(f.label)}<p>${inline(f.detail || '')}</p>${about}</div>`).join(''))
      const fin = ease(clamp((t - m.at) / 0.25))
      box.style.opacity = Math.min(fin, ease(clamp((m.hide - t) / 0.25)))
      box.style.transform = `translateY(${((1 - fin) * -8).toFixed(2)}px)`
    }
  }

  /* ---------- around the windows ---------- */

  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  function menuClock() {
    const d = new Date(), h = d.getHours()
    return `${DAYS[d.getDay()]} ${MONTHS[d.getMonth()]} ${d.getDate()}  ${h % 12 || 12}:${pad(d.getMinutes())} ${h < 12 ? 'AM' : 'PM'}`
  }
  function sessionTitle() {
    const meta = SESSIONS && SESSIONS.find(x => x.id === currentId)
    return (meta && meta.title) || (S && S.hello && S.hello.session && S.hello.session.title) || (toolOf() === 'codex' ? 'Codex' : 'Claude Code')
  }
  function liveState(t) {
    if (CLOSED) return { cls: 'off', word: 'Stopped' }
    if (connLostAt != null && t - connLostAt > 2) return { cls: 'off', word: 'Reconnecting' }
    if (!S) return { cls: 'done', word: 'Connecting' }
    if (S.status === 'done') return { cls: 'done', word: `Done · ${dur(t - S.statusAt)} ago` }
    if (!S.live) return { cls: 'done', word: 'Not live' }
    return { cls: 'working', word: 'Working' }
  }
  function renderMenubar(t, active) {
    const app = active === 'msg' ? toolName(toolOf()) : active ? APP[active] : 'Finder'
    const L = liveState(t), clock = menuClock(), title = sessionTitle()
    const counts = { claude: 0, codex: 0 }
    for (const x of SESSIONS || []) counts[x.tool === 'codex' ? 'codex' : 'claude']++
    const sel = TOOL || toolOf(), both = counts.claude > 0 && counts.codex > 0
    setHTML('menubar', `${app}|${L.cls}|${L.word}|${title}|${clock}|${both}|${sel}|${counts.claude}|${counts.codex}`, () =>
      `<span class="apple">&#xF8FF;</span><span class="app">${esc(app)}</span>${(MENUS[app] || MENUS.Claude).map(m => `<span>${m}</span>`).join('')}` +
      (both ? `<span class="toolsw">${['claude', 'codex'].map(k => `<b class="${sel === k ? 'on' : ''}" data-tool="${k}">${toolName(k)} <em>${counts[k]}</em></b>`).join('')}</span>` : '') +
      `<span class="live" id="live-pill"><i class="dot-${L.cls}"></i><b>LIVE · ${esc(L.word)}</b><span class="ttl">${esc(trunc(title, 70))}</span><span class="car">▾</span></span>` +
      `<span class="clock">${esc(clock)}</span>`)
  }
  function renderDock(t, hide) {
    $('dock').style.transform = `translateX(-50%) translateY(${(100 * hide).toFixed(2)}px)`
    for (const [w] of DOCK) {
      const a = w === 'chrome' ? [...S.D.acts.chrome, ...S.D.acts.helper] : S.D.acts[w]
      const f = a.length ? Math.min(...a) : Infinity, e = t - f
      $('run-' + w).style.display = e >= 0 ? 'block' : 'none'
      const b = e >= 0 && e < 0.7 ? Math.abs(Math.sin((e / 0.7) * Math.PI * 2)) * 14 * (1 - e / 0.7) : 0
      $('dk-' + w).style.transform = `translateY(${(-b).toFixed(2)}px)`
    }
  }

  /* ---------- overlays ---------- */

  const fade = (t, a, b, f = 0.25) => Math.min(ease(clamp((t - a) / f)), ease(clamp((b - t) / f)))
  function renderSubtitle(t) {
    const el = $('subtitle')
    let cur = null
    for (const it of S.D.says) if (it.v0 <= t && t < it.until) cur = it
    if (!cur) { el.style.display = 'none'; return 0 }
    const n = cur.chunks.length
    const k = Math.min(n - 1, Math.floor(((t - cur.v0) / (cur.until - cur.v0)) * n))
    const o = fade(t, cur.v0, cur.until, 0.2)
    el.style.display = 'block'
    el.style.opacity = o
    el.className = cur.helper ? 'helper' : ''
    setHTML('subtitle', `${cur.i}|${k}`, () => `<div class="who"><i></i>${esc(agentLabel(cur.agent))}</div>${esc(cur.chunks[k])}`)
    return o
  }
  function somethingRunning(t) {
    return S.D.winItems.some(it => it.v0 <= t && it.doneAt == null && it.s.kind !== 'helper_start')
  }
  function renderThinking(t, subOn) {
    const el = $('thinking')
    let text = null
    if (S.live && S.status === 'working' && S.ready && t - S.lastEventAt > THINK_AFTER && !somethingRunning(t)) {
      const lit = Math.floor(t * 3) % 3
      const dots = [0, 1, 2].map(k => `<span style="opacity:${k <= lit ? 1 : 0.25}">.</span>`).join('')
      text = `${esc(agentLabel(S.lastAgent))} is thinking${dots} <b>${dur(t - S.lastEventAt)}</b>`
    }
    if (!text) { el.style.display = 'none'; return 0 }
    el.style.display = 'block'
    el.style.bottom = subOn ? '150px' : '34px'
    setHTML('thinking', text, () => text)
    return 1
  }
  function renderBanner(t) {
    const el = $('banner')
    const it = latest(S.D.prompts, t)
    if (!it || t > it.v0 + 7) { el.style.display = 'none'; return }
    el.style.display = 'block'
    el.style.opacity = fade(t, it.v0, it.v0 + 7, 0.3)
    setHTML('banner', 'b' + it.i, () => `<b>YOU</b>${esc(trunc(plain((it.s.prompt && it.s.prompt.text) || ''), 220))}`)
  }
  function renderChips(t) {
    const show = S.D.others.filter(it => it.v0 <= t && t < it.v0 + 4.5).slice(-3)
    const jump = S.catchups.filter(c => c.at <= t && t < c.at + 4).pop()
    setHTML('chips', show.map(it => it.i + '|' + it.s.state).join(',') + (jump ? '|j' + jump.at : ''), () => (jump
      ? `<div class="chip"><b>Skipped ahead</b> · ${jump.n} step${jump.n === 1 ? '' : 's'} drawn without animation to keep up with the agent</div>` : '') + show.map(it => {
      const O = it.s.other || {}
      const out = lines(O.output)[0] || ''
      return `<div class="chip"><b>${esc(O.name || it.s.tool || 'Tool')}</b> · ${esc(trunc(O.input_summary || '', 90))}${out ? ` → ${esc(trunc(out, 70))}` : it.s.state === 'running' ? ' …' : ''}</div>`
    }).join(''))
  }
  function renderEmpty() {
    const el = $('empty')
    if (CLOSED) {
      el.style.display = 'block'
      // Browsers only let a page close its own tab right after a click, so offer the click.
      setHTML('empty', 'closed' + CLOSE_REFUSED, () => `<h2>The viewer stopped</h2>${esc(CLOSED)} To open it again, run <b>agentwatch</b>.` +
        (CLOSE_REFUSED ? '<p class="note">Your browser won\'t let this page close its tab. Close it the usual way.</p>'
          : '<p><button class="closetab" data-closetab="1">Close this tab</button></p>'))
      return
    }
    if (NOKEY) {
      el.style.display = 'block'
      setHTML('empty', 'nokey', () => '<h2>This viewer needs its key</h2>Start it with the <b>agentwatch</b> command. The key changes every time the viewer starts, so an old tab stops working.')
      return
    }
    const none = SESSIONS && SESSIONS.length === 0 && !S
    el.style.display = none ? 'block' : 'none'
    if (none) setHTML('empty', 'none', () => '<h2>No Claude Code or Codex windows are open</h2>Start one in a terminal. It shows up here once you send it a message, and from then on you see it work live.')
  }

  /* ---------- session menu ---------- */

  function renderMenu() {
    const el = $('menu')
    el.style.display = menuOpen ? 'block' : 'none'
    if (!menuOpen) return
    // The selected tool's open windows. Under the one being watched, its helpers (Claude) or subagents
    // (Codex): click one to follow just it, or click its window to follow all of them again.
    const sel = TOOL || toolOf(), name = toolName(sel)
    const mine = (SESSIONS || []).filter(x => (x.tool || 'claude') === sel)
    const rows = mine.map(x => {
      const cur = x.id === currentId
      let h = `<div class="mrow${cur && follow === 'all' ? ' on' : ''}" data-session="${esc(x.id)}"><i class="dot-${x.status === 'working' ? 'working' : 'done'}"></i>` +
        `<div><div class="t">${esc(trunc(x.title || x.id, 90))}</div><div class="m">${x.window ? `${esc(x.window)} · ` : ''}${esc(cwdLabel(x.cwd || x.project || ''))} · ${x.status === 'working' ? 'working' : 'idle'} · ${esc(ago(x.last_activity))}</div></div></div>`
      if (cur && S) {
        for (const k of S.helpers.keys()) {
          const done = S.items.some(it => it.agent === k && it.s.kind === 'helper_end')
          h += `<div class="mrow sub${follow === k ? ' on' : ''}" data-follow="${esc(k)}"><i class="dot-${done ? 'done' : 'working'}"></i>` +
            `<div><div class="t">${esc(agentLabel(k))}</div><div class="m">${done ? 'done' : 'working'}</div></div></div>`
        }
      }
      return h
    }).join('')
    el.innerHTML = `<div class="mh">${esc(name.toUpperCase())} · ${mine.length} OPEN WINDOW${mine.length === 1 ? '' : 'S'}</div>${rows || `<div class="none">No ${esc(name)} windows are open.</div>`}` +
      '<div class="sep"></div><div class="mrow quit" data-close="1"><div><div class="t">Close viewer</div><div class="m">Stops it too. Run agentwatch to open it again.</div></div></div>'
  }
  function onClick(e) {
    const tgt = e.target.closest ? e.target : e.target.parentElement
    if (tgt.closest('#live-pill')) { menuOpen = !menuOpen; renderMenu(); return }
    if (tgt.closest('[data-close]')) { menuOpen = false; renderMenu(); closeViewer('You closed the viewer.'); return }
    const sw = tgt.closest('[data-tool]')
    if (sw) {
      TOOL = sw.dataset.tool
      if (toolOf() !== TOOL) {                    // watch that tool's busiest window
        const mine = (SESSIONS || []).filter(x => (x.tool || 'claude') === TOOL)
        const pick = mine.find(x => x.status === 'working') || mine[0]
        if (pick) connect(pick.id)
      }
      renderMenu(); wake(); return
    }
    if (tgt.closest('[data-closetab]')) { window.close(); setTimeout(() => { CLOSE_REFUSED = true; wake() }, 400); return }
    const row = tgt.closest('[data-session],[data-follow]')
    if (row && row.dataset.session) {
      if (row.dataset.session !== currentId) connect(row.dataset.session)
      else { follow = 'all'; if (S) S.dirty = true }
      menuOpen = false; renderMenu(); wake(); return
    }
    if (row && row.dataset.follow) { follow = row.dataset.follow; if (S) S.dirty = true; menuOpen = false; renderMenu(); wake(); return }
    if (menuOpen && !tgt.closest('#menu')) { menuOpen = false; renderMenu() }
  }

  /* ---------- connection ---------- */

  function connect(id) {
    if (es) es.close()
    currentId = id
    S = null
    follow = id === Q.get('session') && Q.get('follow') ? Q.get('follow') : 'all'
    es = new EventSource(api('/api/stream', { session: id }))
    es.onmessage = ev => { try { onEvent(JSON.parse(ev.data)) } catch (err) { console.error('bad event', err) } }
    es.onerror = () => { if (connLostAt == null) connLostAt = now() } // EventSource retries by itself; hello resets state
    es.onopen = () => { connLostAt = null }
  }
  function clearScreen() {
    for (const w of Object.keys(GEO)) { const el = $('w-' + w); if (el) el.style.display = 'none' }
    for (const id of ['term-co', 'chips']) { const el = $(id); if (el) el.innerHTML = '' }              // redrawn from scratch
    for (const id of ['subtitle', 'thinking', 'banner']) { const el = $(id); if (el) el.style.display = 'none' } // these re-show themselves
    cache = {}
  }
  // Stop: tell the server this tab is gone (it stops once no tab is left), then close the tab. Browsers
  // allow that here because the launch page replaced itself with this one; if not, a message stays.
  function closeViewer(why) {
    if (CLOSED) return
    CLOSED = why
    try { navigator.sendBeacon(api('/api/bye')) } catch { /* the server also stops on its own after a while */ }
    if (es) es.close()
    es = null; currentId = null; S = null
    clearInterval(pollTimer)
    clearScreen()
    wake()
    window.close()
  }
  addEventListener('pagehide', () => { if (!CLOSED) navigator.sendBeacon(api('/api/bye')) })

  async function pollSessions() {
    if (CLOSED) return
    let reached = false
    try {
      const r = await fetch(api('/api/sessions'), { cache: 'no-store' })
      reached = true
      NOKEY = r.status === 401
      if (r.ok) SESSIONS = await r.json()
    } catch { /* keep the last list */ }
    failures = reached ? 0 : failures + 1
    if (failures >= 3) { closeViewer("The viewer isn't running any more."); return }
    // When the last Claude Code window has been closed for 10 seconds, close too. Not when the
    // viewer was opened before any window: then it's waiting for one.
    if (SESSIONS && SESSIONS.length) { sawWindows = true; emptySince = null }
    else if (SESSIONS && sawWindows) {
      if (emptySince == null) emptySince = now()
      else if (now() - emptySince >= 10) { closeViewer('All your Claude Code and Codex windows are closed.'); return }
    }
    if (currentId && SESSIONS && !SESSIONS.some(x => x.id === currentId)) {
      // The window we were watching closed: clear the screen, then follow another open one if any.
      if (es) es.close()
      es = null; currentId = null; S = null
      clearScreen()
    }
    if (!currentId && SESSIONS && SESSIONS.length) {
      const want = Q.get('session')
      const pick = (want && SESSIONS.find(x => x.id === want)) || SESSIONS.find(x => x.status === 'working') || SESSIONS[0]
      connect(pick.id)
    }
    renderMenu()
  }

  /* ---------- one frame ---------- */

  function render() {
    const t = now()
    renderEmpty()
    if (!S) { renderMenubar(t, null); return }
    if (S.dirty || !S.D) derive()
    const st = winStates(t)
    let active = null, best = -Infinity
    for (const w in st) if (st[w].on && st[w].last >= best) { best = st[w].last; active = w }
    const order = Object.keys(st).filter(w => st[w].on).sort((a, b) => st[a].last - st[b].last)
    for (const w of Object.keys(GEO)) {
      const el = $('w-' + w), s = st[w]
      if (!s.on) { el.style.display = 'none'; continue }
      const fx = effectFor(w, t)
      el.style.display = 'block'
      el.style.zIndex = String(10 + order.indexOf(w))
      el.style.opacity = s.o
      el.style.transform = `translateX(${fx ? fx.shake.toFixed(2) : 0}px) scale(${s.sc.toFixed(4)})`
      el.style.boxShadow = shadowFor(fx)
      el.classList.toggle('active', w === active)
    }
    if (st.term.on) renderTerm(t)
    if (st.edit.on) renderEdit(t)
    if (st.chrome.on) renderBrowser('chrome', t)
    if (st.helper.on) renderBrowser('helper', t)
    if (st.msg.on) renderMsg(t)
    renderCallouts(t)
    const cam = camAt(t)
    $('cam').style.transform = `translate(${cam.x.toFixed(2)}px, ${cam.y.toFixed(2)}px) scale(${cam.s.toFixed(4)})`
    renderMenubar(t, active)
    const subO = renderSubtitle(t)
    const thinkO = renderThinking(t, subO > 0)
    renderBanner(t)
    renderChips(t)
    renderDock(t, Math.max(subO, thinkO))
  }

  function fit() {
    const s = Math.min(innerWidth / W, innerHeight / H)
    const st = $('stage')
    st.style.transform = `scale(${s})`
    st.style.left = (innerWidth - W * s) / 2 + 'px'
    st.style.top = (innerHeight - H * s) / 2 + 'px'
  }

  buildScene()
  fit()
  addEventListener('resize', fit)
  addEventListener('click', onClick)
  addEventListener('keydown', e => { if (e.key === 'Escape' && menuOpen) { menuOpen = false; renderMenu() } })
  pollSessions()
  pollTimer = setInterval(pollSessions, 5000)
  // Draw every frame only while something moves; otherwise 2 frames a second is enough for the clock,
  // timers and the blinking cursor. Most of the time the window just sits open, so this saves most of the CPU
  // (measured: 41% of a core drawing every frame, about 3.5% this way).
  // While something moves, draw at most 30 frames a second, which is smooth enough for this screen.
  function moving(t) {
    if (!S) return false
    if (S.dirty || !S.D) return true
    const quiet = t - S.lastEventAt                 // the "thinking" label appears, and the dock slides away
    if (S.ready && quiet > THINK_AFTER - 0.05 && quiet < THINK_AFTER + 0.5) return true
    return S.D.anim.some(([a, b]) => t >= a - 0.05 && t <= b)
  }
  let idleTimer = null, lastDraw = -1
  const loop = () => {
    idleTimer = null
    const t = now(), busy = moving(t)
    if (!busy || t - lastDraw >= 1 / 30 - 0.004) {
      lastDraw = t
      try { render() } catch (err) { console.error(err) }
    }
    if (busy) requestAnimationFrame(loop)
    else idleTimer = setTimeout(() => requestAnimationFrame(loop), 500)
  }
  function wake() { if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; requestAnimationFrame(loop) } }
  requestAnimationFrame(loop)
})()
