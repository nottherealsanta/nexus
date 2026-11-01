// Nexus faux UI: every surface, drawn from tokens. Each part reads the active
// variants so a pick on an axis restyles the whole chooser at once.
import { createContext, Fragment, useContext, useEffect, useRef, type CSSProperties, type ReactNode } from 'react'
import { Archive, ArrowUp, Bell, Check, ChevronDown, ChevronRight, Circle, CircleAlert, Code2, Command, Copy, FilePen, FileText, Files, FolderTree, GitBranch, GitCommitHorizontal, Globe, Info, Layers, Lock, MessageSquareText, Mic, PanelLeft, PanelRight, Paperclip, Pencil, Plus, Search, Settings, Square, Terminal, X } from 'lucide-react'
import { Stream } from '../kit'
import type { Variants } from './styles'

export const VariantCtx = createContext<Variants>(null as unknown as Variants)
export const useV = () => useContext(VariantCtx)
/** True on still tiles: canvases draw one frame and stop. */
export const StillCtx = createContext(true)

const I = { size: 14, strokeWidth: 1.6 }

/* ---------- the activity and context bar ---------- */

// Ported from the TUI's draw_activity_line (nexus-tui/src/render.rs): at rest
// the first `fraction` of the width is lit (context used); while a turn runs a
// soft-edged pulse a fifth of the width glides back and forth on a cosine,
// 90 frames a sweep. Here the cells are glowing LEDs.
export function ActivityBar({ busy, fraction = 0.41 }: { busy?: boolean; fraction?: number }) {
  const v = useV()
  const still = useContext(StillCtx)
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const width = cv.offsetWidth, height = cv.offsetHeight, dpr = 2
    cv.width = width * dpr; cv.height = height * dpr
    const g = cv.getContext('2d')
    if (!g) return
    g.scale(dpr, dpr)
    const css = getComputedStyle(cv)
    const lit = css.color, rest = css.getPropertyValue('--n-line2')
    const n = Math.floor(width)
    const length = Math.max(1, Math.floor(n / 5)), travel = Math.max(0, n - length), half = length / 2
    const cy = height / 2
    const draw = (phase: number) => {
      g.clearRect(0, 0, width, height)
      g.shadowBlur = 0; g.globalAlpha = 1; g.fillStyle = rest
      g.fillRect(0, cy - 0.5, width, 1)
      const centre = travel * (1 - Math.cos(phase * Math.PI * 2)) / 2 + half
      g.fillStyle = lit; g.shadowColor = lit; g.shadowBlur = 4
      for (let i = 0; i < n; i++) {
        let w: number
        if (busy) { const near = Math.max(0, Math.min(1, 1 - Math.abs(i + 0.5 - centre) / (half + 1.5))); w = near * near * (3 - 2 * near) } else w = i < fraction * n ? 1 : 0
        if (w > 0.01) { g.globalAlpha = w * 0.9; g.fillRect(i, cy - 0.5, 1, 1) }
      }
    }
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced) { draw(0.5); return }
    if (!busy || still) { draw(0.3); return }
    let raf = 0
    const tick = (ms: number) => { draw((ms / 3000) % 1); raf = requestAnimationFrame(tick) }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [busy, fraction, v.bar, v.agent, v.theme, still])
  return <canvas ref={ref} className="n-bar" aria-hidden="true" />
}

/* ---------- the connect screen's field, made of the logo's own mark ---------- */

const BLOBS = [[370, 170, 250], [1140, 150, 230], [420, 500, 210], [1120, 470, 260], [760, 70, 170], [800, 720, 200]]
function hash(a: number, b: number) { const s = Math.sin(a * 127.1 + b * 311.7) * 43758.5453; return s - Math.floor(s) }

export function SlashField() {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const w = cv.offsetWidth, h = cv.offsetHeight
    cv.width = w * 2; cv.height = h * 2
    const g = cv.getContext('2d')
    if (!g) return
    g.scale(2, 2)
    const sx = w / 1280, sy = h / 780
    g.font = '11px "Monaspace Argon", ui-monospace, monospace'
    g.fillStyle = getComputedStyle(cv).color
    for (let x = 0; x < w; x += 20) for (let y = 12; y < h; y += 16) {
      if (Math.abs(x - w / 2) < Math.min(w * 0.42, 190) && Math.abs(y - h / 2) < Math.min(h * 0.36, 170)) continue
      let d = 0
      for (const [bx, by, br] of BLOBS) d = Math.max(d, 1 - Math.hypot(x - bx * sx, y - by * sy) / (br * Math.min(sx, sy) * 1.6))
      if (d <= 0 || hash(x, y) > d * 1.15) continue
      g.globalAlpha = Math.min(0.5, d * 0.75 + 0.08)
      g.fillText('_/', x, y)
    }
  }, [])
  return <canvas ref={ref} className="n-slashfield" aria-hidden="true" />
}

/* ---------- small things ---------- */

export const Kbd = ({ children }: { children: ReactNode }) => <kbd className="n-kbd">{children}</kbd>
export const Label = ({ children, end }: { children: ReactNode; end?: ReactNode }) => <div className="n-label"><span>{children}</span>{end && <span className="n-label__end">{end}</span>}</div>
export const Ag = ({ c = 'amber', children }: { c?: string; children: ReactNode }) => <span className={`n-ag n-ag--${c}`}>{children}</span>
export const Code = ({ children }: { children: ReactNode }) => <code className="n-icode">{children}</code>

export function Working({ label = 'Working', time = '42s' }: { label?: string; time?: string }) {
  return <div className="n-work n-work--shimmer"><span className="n-shimmer">{label}</span><span className="n-work__t">{time}</span></div>
}

export const Meter = () => <span className="n-meter"><span className="n-meter__n">82k · 41%</span></span>

/* ---------- sidebar ---------- */

export type Sess = { t: string; time: string; agent: string; c: string; branch: string; kids?: number; state: 'run' | 'wait' | 'unread' | 'fail' | 'idle'; on?: boolean }
export const SESSIONS: Sess[] = [
  { t: 'Survive a daemon restart', time: '42s', agent: 'Build', c: 'amber', branch: 'main', kids: 2, state: 'run', on: true },
  { t: 'Retention stalls the clients', time: '6m', agent: 'Build', c: 'amber', branch: 'retention', state: 'wait' },
  { t: 'Native Core access on iOS', time: '38m', agent: 'Review', c: 'teal', branch: 'nsh-ios', state: 'unread' },
  { t: 'Model picker parity', time: '1h', agent: 'Build', c: 'amber', branch: 'main', state: 'idle' },
  { t: 'Project MCP opt-out', time: '3h', agent: 'Plan', c: 'violet', branch: 'mcp', kids: 1, state: 'fail' },
  { t: 'Release dry run', time: '5h', agent: 'Build', c: 'amber', branch: 'main', state: 'idle' },
]
export const SESSIONS2: Sess[] = [
  { t: 'Sign the desktop package', time: '1d', agent: 'Build', c: 'amber', branch: 'main', state: 'idle' },
  { t: 'Hot reload in the web host', time: '2d', agent: 'Plan', c: 'violet', branch: 'dev-web', state: 'idle' },
  { t: 'Fat LTO in the release build', time: '3d', agent: 'Build', c: 'amber', branch: 'main', state: 'idle' },
  { t: 'Shader grounds on iOS', time: '4d', agent: 'Explore', c: 'teal', branch: 'shaders', state: 'idle' },
]

function Mark({ state }: { state: Sess['state'] }) {
  return <span className="n-mark" data-state={state}>{state === 'run' ? <span className="n-spin" /> : state === 'wait' ? '◆' : state === 'unread' ? '●' : state === 'fail' ? '✕' : '·'}</span>
}

export function SessionRow({ s }: { s: Sess }) {
  return (
    <div className="n-row n-row--two n-sel" data-on={s.on || undefined} data-state={s.state}>
      <div className="n-row__l1"><Mark state={s.state} /><span className="n-row__t">{s.t}</span><span className="n-row__time">{s.time}</span></div>
      <div className="n-row__l2"><Ag c={s.c}>{s.agent}</Ag><span className="n-row__br"><GitBranch size={10} strokeWidth={1.8} />{s.branch}</span>{s.kids ? <span className="n-row__kids">+{s.kids}</span> : null}</div>
    </div>
  )
}

export function SessionsList({ short }: { short?: boolean }) {
  return (
    <div className="n-sessions">
      <div className="n-search"><Search {...I} /><span>Search sessions</span><Kbd>⌘K</Kbd></div>
      <Label end={<Plus size={13} strokeWidth={1.7} />}>nexus</Label>
      <div className="n-rows">{SESSIONS.slice(0, short ? 4 : 6).map((s) => <SessionRow key={s.t} s={s} />)}</div>
      <Label end={<Plus size={13} strokeWidth={1.7} />}>nsh</Label>
      <div className="n-rows">{SESSIONS2.map((s) => <SessionRow key={s.t} s={s} />)}</div>
    </div>
  )
}

/** The collapsed left sidebar: the toggle on top on the web; the header holds it beside the traffic lights. */
export function ClosedRail() {
  const v = useV()
  return (
    <aside className="n-left n-left--closed n-chrome">
      <div className="n-left__top">{v.chrome !== 'hidden' && <PanelLeft {...I} className="n-ico n-lefttoggle" />}</div>
      <CollapsedRail />
      {v.railHover && <div className="n-railcard"><b>nexus</b><div><span data-k="a">1 attention</span><span data-k="f">0 unread</span></div><div><span data-k="r">2 running</span><span data-k="i">3 idle</span></div></div>}
    </aside>
  )
}

export const TrafficLights = () => <span className="n-lights" aria-hidden="true"><i /><i /><i /></span>

export function Sidebar({ tab = 'sessions', children }: { tab?: 'sessions' | 'files' | 'projects'; children?: ReactNode }) {
  const v = useV()
  const tabs: ['sessions' | 'files' | 'projects', string, typeof Files][] = [['sessions', 'Sessions', MessageSquareText], ['files', 'Files', Files], ['projects', 'Projects', FolderTree]]
  return (
    <aside className="n-left n-chrome">
      <div className="n-left__top" data-kind={v.sidetop}>
        <PanelLeft {...I} className="n-ico n-lefttoggle" />
        {v.sidetop === 'seg'
          ? <span className="n-segtabs">{tabs.map(([id, name]) => <span key={id} data-on={tab === id || undefined}>{name}</span>)}</span>
          : <span className="n-tabs">{tabs.map(([id, name, Ic]) => <span key={id} title={name} data-on={tab === id || undefined}><Ic {...I} /></span>)}</span>}
      </div>
      <div className="n-left__body">{children ?? <SessionsList />}</div>
      <div className="n-left__foot"><Settings {...I} /><span>Settings</span><span className="n-grow" /><span className="n-conn"><i />hamming.local</span></div>
    </aside>
  )
}

/* ---------- header ---------- */

export function Header({ status = 'run', child, title = 'Survive a daemon restart', solo }: { status?: 'run' | 'wait' | 'idle'; child?: boolean; title?: string; solo?: boolean }) {
  const v = useV()
  return (
    <header className="n-head n-chrome">
      {v.closed && v.chrome === 'hidden' && <PanelLeft {...I} className="n-ico n-lefttoggle" />}
      <div className="n-head__tabs">
        <span className="n-htab" data-on="1">{status === 'run' ? <span className="n-spin" /> : status === 'wait' ? <span className="n-accentc">◆</span> : <span className="n-faint">·</span>}{child ? <>{title}<ChevronRight size={12} className="n-faint" />Trace the cursor</> : title}<X size={11} className="n-faint" /></span>
        {!solo && <span className="n-htab"><span className="n-accentc">◆</span>Retention stalls<X size={11} className="n-faint" /></span>}
        <span className="n-htab"><Plus size={13} strokeWidth={1.7} /></span>
      </div>
      <span className="n-grow" />
      <span className="n-head__st">{status === 'run' ? <><span className="n-spin" />working 42s</> : status === 'wait' ? <><span className="n-accentc">◆</span>waiting for you</> : null}</span>
      <span className="n-bell"><Bell {...I} /><b>2</b></span>
    </header>
  )
}

/* ---------- phone: bottom tabs and a back control ---------- */

export function PhoneHeader({ status = 'run' }: { status?: 'run' | 'wait' }) {
  return <div className="n-phead"><ChevronRight size={18} strokeWidth={1.8} style={{ transform: 'rotate(180deg)' }} /><span className="n-phead__t">Survive a daemon restart</span>{status === 'run' ? <span className="n-spin" /> : <span className="n-accentc">◆</span>}</div>
}

export function PhoneTabs() {
  return <div className="n-ptabs"><span data-on="1"><MessageSquareText size={19} strokeWidth={1.6} />Sessions</span><span><Settings size={19} strokeWidth={1.6} />Settings</span></div>
}

/* ---------- timeline ---------- */

export function UserMsg({ children, n = 12, time = '18:52' }: { children: ReactNode; n?: number; time?: string }) {
  return <div className="n-user n-user--band"><div className="n-user__in"><div className="n-user__t">{children}</div><span className="n-user__m">#{n} · {time}</span></div></div>
}

export function Thought({ time = '2.1s', children }: { time?: string; children: ReactNode }) {
  return <div className="n-thought"><ChevronRight size={12} strokeWidth={1.8} /><span>Thought for {time}</span><em>{children}</em></div>
}

export type Tool = { verb: string; icon: 'shell' | 'read' | 'edit' | 'web' | 'child'; target: string; meta?: string; state?: 'run' | 'ok' | 'fail' }
const TOOL_ICON = { shell: Terminal, read: FileText, edit: FilePen, web: Globe, child: GitBranch }

export const TOOLS: Tool[] = [
  { verb: 'Read', icon: 'read', target: 'nexus-nsh/src/core/sync.ts', meta: '412 lines' },
  { verb: 'Read', icon: 'read', target: 'nexus-core/nexus/api/events.py', meta: '238 lines' },
  { verb: 'Run', icon: 'shell', target: 'uv run pytest -q tests/test_events.py', meta: '4.2s' },
  { verb: 'Edit', icon: 'edit', target: 'nexus-nsh/src/core/sync.ts', meta: '+18 −4' },
  { verb: 'Run', icon: 'shell', target: 'nsh test --target web sync', meta: '12s', state: 'run' },
]

export function ToolRow({ tool }: { tool: Tool }) {
  const Ic = TOOL_ICON[tool.icon]
  const glyph = tool.state === 'run' ? <span className="n-spin" /> : tool.state === 'fail' ? <span className="n-err">✕</span> : <Ic size={13} strokeWidth={1.6} />
  return <div className="n-sp-r" data-state={tool.state}><span className="n-sp-r__g">{glyph}</span><span className="n-sp-r__v">{tool.verb}</span><span className="n-sp-r__t">{tool.target}</span><span className="n-sp-r__m">{tool.meta}</span></div>
}

export function Activity({ tools = TOOLS, count, live, summary }: { tools?: Tool[]; count?: number; live?: boolean; summary?: string }) {
  const shown = tools.slice(0, count ?? tools.length)
  return (
    <div className="n-act n-act--spine">
      {live ? <Working /> : <div className="n-act__sum"><ChevronDown size={12} strokeWidth={1.8} /><span>{summary ?? '2 thoughts, 3 commands, 2 reads, 1 edit'}</span>{shown.some((t) => t.icon === 'edit') && <><em className="n-add">+18</em><em className="n-del">−4</em></>}</div>}
      <div className="n-act__list">{shown.map((t, i) => <ToolRow key={i} tool={t} />)}</div>
    </div>
  )
}

export const REPLY = 'The client dropped its cursor when the socket closed, so a restart looked like a new stream and the open turn was lost. It now keeps the last event id and resumes from it, and the daemon replays anything newer on reconnect.'

export function Reply({ count }: { count?: number }) {
  return <p className="n-p">{count === undefined ? <>The client dropped its cursor when the socket closed, so a restart looked like a new stream and the open turn was lost. It now keeps the last event id in <Code>sync.ts</Code> and resumes from it, and the daemon replays anything newer on reconnect.</> : <Stream text={REPLY} count={count} />}</p>
}

const Tk = ({ k, children }: { k: 'kw' | 'fn' | 'str' | 'ty' | 'com' | 'num'; children: ReactNode }) => <span className={`tk-${k}`}>{children}</span>

export function CodeBlock() {
  return (
    <div className="n-fence n-edge">
      <div className="n-fence__h"><span>ts</span><span className="n-faint">nexus-nsh/src/core/sync.ts</span><span className="n-grow" /><Copy size={12} strokeWidth={1.6} /></div>
      <pre><code>
        <span className="ln"><i>41</i><Tk k="kw">const</Tk> resume = <Tk k="kw">async</Tk> (cursor: <Tk k="ty">EventId</Tk>) =&gt; {'{'}</span>
        <span className="ln"><i>42</i>  <Tk k="kw">const</Tk> res = <Tk k="kw">await</Tk> <Tk k="fn">stream</Tk>(<Tk k="str">`/api/events?after=</Tk>{'${cursor}'}<Tk k="str">`</Tk>)</span>
        <span className="ln"><i>43</i>  <Tk k="kw">for await</Tk> (<Tk k="kw">const</Tk> ev <Tk k="kw">of</Tk> res) <Tk k="fn">reduce</Tk>(state, ev)</span>
        <span className="ln"><i>44</i>{'}'}</span>
      </code></pre>
    </div>
  )
}

export function Footer() {
  return <div className="n-foot">▪ <Ag>Build</Ag> · Opus 5.5 · xhigh · 8 tools · 1m 05s · 41.2k<u>i</u> 3.2k<u>o</u> 28k<u>r</u> · $0.28</div>
}

export function DiffBlock({ wide = true }: { wide?: boolean }) {
  const L: [string, string, 'ctx' | 'del' | 'add' | 'nil'][] = [
    ['88', 'socket.onclose = () => {', 'ctx'],
    ['89', '  state.cursor = null', 'del'],
    ['90', '  connect()', 'del'],
    ['91', '}', 'ctx'],
  ]
  const R: [string, string, 'ctx' | 'del' | 'add' | 'nil'][] = [
    ['88', 'socket.onclose = () => {', 'ctx'],
    ['89', '  retry.after(backoff.next())', 'add'],
    ['90', '  connect({ after: state.cursor })', 'add'],
    ['91', '}', 'ctx'],
  ]
  const col = (rows: typeof L) => <div className="n-diff__c">{rows.map(([n, t, k], i) => <div key={i} className="n-diff__r" data-k={k}><i>{n}</i><b>{k === 'add' ? '+' : k === 'del' ? '−' : ' '}</b><span>{t}</span></div>)}</div>
  return (
    <div className="n-diff n-edge">
      <div className="n-fence__h"><FilePen size={12} strokeWidth={1.6} /><span>nexus-nsh/src/core/sync.ts</span><span className="n-grow" /><em className="n-add">+18</em><em className="n-del">−4</em></div>
      <div className="n-diff__g" data-wide={wide || undefined}>{col(L)}{wide && col(R)}</div>
    </div>
  )
}

/* ---------- composer ---------- */

const ModelMark = () => <span className="n-modelmark" aria-hidden="true"><svg viewBox="0 0 16 16" width="13" height="13"><path d="M8 1v14M1 8h14M3 3l10 10M13 3 3 13" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" /></svg></span>

export function Composer({ text, running, placeholder = 'Ask anything. @ files, $ skills, / commands' }: { text?: ReactNode; running?: boolean; placeholder?: string }) {
  const v = useV()
  const body = <span className={text ? 'n-comp__t' : 'n-comp__ph'} data-target="input">{text || placeholder}</span>
  const send = <span className="n-send" data-target="send" data-run={running || undefined}>{running ? <Square size={11} fill="currentColor" strokeWidth={0} /> : <ArrowUp size={14} strokeWidth={2} />}</span>
  return (
    <div className="n-comp n-comp--dock">
      {v.barpos === 'top' && <ActivityBar busy={running} />}
      <div className="n-comp__in">
        <div className="n-comp__l">{body}</div>
        <div className="n-comp__ctl"><Paperclip {...I} className="n-ico" /><Ag>Build</Ag><span className="n-comp__m"><ModelMark />Opus 5.5 <em>xhigh</em></span><span className="n-grow" /><span className="n-micwrap"><Mic {...I} className="n-ico" /></span>{send}</div>
        <div className="n-comp__strip"><span>~/repos/nexus</span><span className="n-comp__br"><GitBranch size={11} strokeWidth={1.7} />main</span><span className="n-grow" /><Meter /></div>
      </div>
      {v.barpos === 'foot' && <ActivityBar busy={running} />}
    </div>
  )
}

/* ---------- waiting ---------- */

export function Approval(_: { decided?: boolean }) {
  const cmd = 'uv run pytest -q tests/test_reconnect.py'
  return (
    <div className="n-wait n-wait--chips n-edge" data-target="wait">
      <div className="n-wait__h"><span>◆</span><b>Allow bash?</b><span className="n-grow" /><span className="n-wait__tm">waiting on you · 0:42</span></div>
      <div className="n-kv"><span>command</span><code>{cmd}</code><span>directory</span><code>~/repos/nexus/nexus-core</code></div>
      <div className="n-wait__list">
        <div className="n-choice n-sel" data-on="1" data-target="allow"><Kbd>y</Kbd>Allow once</div>
        <div className="n-choice n-sel"><Kbd>a</Kbd>Allow for this session</div>
        <div className="n-choice n-sel"><Kbd>n</Kbd>Deny</div>
      </div>
    </div>
  )
}

export function Question() {
  return (
    <div className="n-wait n-wait--q n-edge">
      <div className="n-wait__h"><span className="n-multic">◇</span><b>Which clients should resume automatically?</b><span className="n-grow" /><span className="n-wait__tm">1 of 2 · pick any</span></div>
      <div className="n-wait__list">
        <div className="n-choice n-sel" data-on="1"><span className="n-tick" data-on="1"><Check size={10} strokeWidth={3} /></span>TUI<em>reconnects on its own today</em></div>
        <div className="n-choice n-sel"><span className="n-tick" data-on="1"><Check size={10} strokeWidth={3} /></span>NSH web<em>reloads the session</em></div>
        <div className="n-choice n-sel"><span className="n-tick" />NSH iOS<em>suspended in the background</em></div>
      </div>
      <div className="n-qfield"><span>Add your own answer…</span><Kbd>⌥A</Kbd></div>
    </div>
  )
}

/* ---------- right sidebar ---------- */

export function RightBody() {
  return (
    <div className="n-rb">
      <section><Label>Session</Label><div className="n-kv n-kv--r"><span>agent</span><span><Ag>Build</Ag></span><span>model</span><span>Opus 5.5 · xhigh</span><span>started</span><span>18:51</span><span>turns</span><span>12</span><span>cost</span><span>$0.28</span></div></section>
      <section><Label end="82k · 41%">Context</Label><div className="n-kv n-kv--r n-kv--num"><span>system prompt</span><span>2.4k</span><span>AGENTS.md</span><span>1.8k</span><span>tools</span><span>9.2k</span><span>skills</span><span>1.1k</span><span>conversation</span><span>67.5k</span></div></section>
      <section><Label end={<><em className="n-add">+18</em> <em className="n-del">−4</em></>}>Git</Label><div className="n-rb__git"><div><GitBranch size={12} strokeWidth={1.7} />main<span className="n-faint">↑1</span></div><div className="n-fl"><b className="n-warnc">M</b>src/core/sync.ts</div><div className="n-fl"><b className="n-warnc">M</b>nexus/api/events.py</div><div className="n-fl"><b className="n-addc">A</b>tests/test_reconnect.py</div></div></section>
    </div>
  )
}

export function Right({ open }: { open?: boolean }) {
  const v = useV()
  return (
    <>
      {open && <aside className={`n-panel n-panel--${v.panel} n-chrome${v.panel === 'overlay' ? ' n-edge' : ''}`}><RightBody /></aside>}
      <aside className="n-right n-right--rail n-chrome">
        <span title="Toggle panel" data-on={open || undefined}><PanelRight {...I} /></span>
        <i className="n-rail__sep" />
        <span title="Session" data-on={open || undefined}><Info {...I} /></span>
        <span title="Context"><Layers {...I} /></span>
        <span title="Git" className="n-railg"><GitBranch {...I} /><b>3</b></span>
      </aside>
    </>
  )
}

/* ---------- overlays ---------- */

export function Palette() {
  const rows: [ReactNode, string, string?, boolean?][] = [
    [<Plus {...I} />, 'New session', '⌘N', true],
    [<GitBranch {...I} />, 'Switch worktree'],
    [<Pencil {...I} />, 'Rename session', 'F2'],
    [<Archive {...I} />, 'Archive session'],
  ]
  return (
    <div className="n-modal n-modal--palette">
      <div className="n-modal__s"><Command {...I} /><span>new s<i className="n-caret" /></span></div>
      <Label>Session</Label>
      <div>{rows.map(([ic, t, k, on]) => <div key={t} className="n-mrow n-sel" data-on={on || undefined}>{ic}<span>{t}</span><span className="n-grow" />{k && <Kbd>{k}</Kbd>}</div>)}</div>
      <Label>Model</Label>
      <div className="n-mrow n-sel"><ModelMark /><span>Switch model</span><span className="n-grow" /><span className="n-faint">Opus 5.5</span></div>
      <div className="n-modal__k"><Kbd>↵</Kbd>run<Kbd>↑↓</Kbd>move<Kbd>esc</Kbd>close</div>
    </div>
  )
}

export function ModelPicker() {
  const rows: [string, string, boolean?, boolean?][] = [['Opus 5.5', 'anthropic', true, true], ['Sonnet 5.5', 'anthropic'], ['Fable 5.1', 'anthropic'], ['gpt-5-codex', 'codex']]
  return (
    <div className="n-modal n-modal--picker">
      <div className="n-modal__t"><b>Model</b><span className="n-faint">esc</span></div>
      <div className="n-modal__s"><Search {...I} /><span>Search models<i className="n-caret" /></span></div>
      <div>{rows.map(([t, p, on, cur]) => <div key={t} className="n-mrow n-sel" data-on={on || undefined}><span>{t}</span><span className="n-grow" />{cur && <span className="n-accentc n-small">current</span>}<span className="n-faint">{p}</span></div>)}</div>
      <div className="n-modal__d"><code>anthropic/claude-opus-5-5</code><span>1M context · low, medium, high, xhigh, max</span></div>
    </div>
  )
}

export function SettingsView() {
  const sec = ['Appearance', 'Providers', 'Agents', 'Tools', 'Skills', 'MCP', 'Context', 'Dictation', 'Keybindings', 'Notifications', 'About']
  const rows: [string, ReactNode, boolean?][] = [
    ['Theme', <span className="n-seg"><i data-on="1">Dark</i><i>Light</i><i>System</i></span>],
    ['Density', 'Comfortable', true],
    ['Font family', 'Monaspace Argon'],
    ['Font size', '14'],
    ['Animations', <span className="n-toggle" data-on="1"><i /></span>],
    ['Monochrome', <span className="n-toggle"><i /></span>],
    ['Timeline detail', 'Grouped'],
  ]
  return (
    <div className="n-modal n-settings">
      <div className="n-settings__l"><div className="n-modal__s"><Search {...I} /><span>Search</span></div>{sec.map((s, i) => <div key={s} className="n-srow n-sel" data-on={i === 0 || undefined}>{s}</div>)}</div>
      <div className="n-settings__r"><div className="n-modal__t"><b>Appearance</b><span className="n-faint">esc</span></div>{rows.map(([k, val, on]) => <div key={k} className="n-mrow n-sel" data-on={on || undefined}><span>{k}</span><span className="n-grow" /><span className="n-sval">{val}</span></div>)}</div>
    </div>
  )
}

const CTX_TOOLS = ['read', 'bash', 'edit', 'write', 'ask', 'code', 'session', 'web_search', 'web_fetch']
const CTX_SKILLS = ['stageshow', 'dataviz', 'code-review', 'simplify', 'run', 'loop']

/** The starting context on the timeline, as the TUI draws it: the first thing in the context line. */
export function StartContext() {
  const sec = (title: string, meta: string, items?: string[]) => (
    <div className="n-ctxsec" key={title}>
      <div className="n-ctxsec__h"><ChevronRight size={12} strokeWidth={1.8} /><b>{title}</b><span>{meta}</span></div>
      {items && <div className="n-ctxgrid">{items.map((t) => <span key={t}><i />{t}</span>)}</div>}
    </div>
  )
  return (
    <div className="n-ctx">
      <div className="n-ctx__h"><ChevronDown size={12} strokeWidth={1.8} /><b>Context</b><span>· 5 items · ~14.5k tokens</span></div>
      {sec('System prompt', '2.4k')}
      {sec('AGENTS.md', '1.8k')}
      {sec('Tools', '9.2k · 9', CTX_TOOLS)}
      {sec('Skills', '1.1k · 6', CTX_SKILLS)}
      {sec('MCP', '3 connected')}
    </div>
  )
}

export function Toasts() {
  return (
    <div className="n-toasts">
      <div className="n-toast" data-lv="ok"><Check size={14} strokeWidth={2} /><div><b>Turn finished</b><span>Survive a daemon restart · 1m 05s</span></div><X size={12} className="n-faint" /></div>
      <div className="n-toast" data-lv="warn"><CircleAlert size={14} strokeWidth={1.8} /><div><b>Rate limit near</b><span>Opus 5.5 · 92% of this window</span></div><X size={12} className="n-faint" /></div>
    </div>
  )
}

export const Banner = () => <div className="n-banner"><CircleAlert size={13} strokeWidth={1.8} />Reconnecting to Core at hamming.local · attempt 3<span className="n-grow" /><u>Retry now</u></div>
export const JumpPill = () => <span className="n-jump"><ArrowUp size={12} style={{ transform: 'rotate(180deg)' }} />Jump to latest · 3 new</span>

/* ---------- files, editor, git ---------- */

export function FileTree() {
  const rows: [number, 'd' | 'f', string, string?, boolean?][] = [
    [0, 'd', 'nexus-nsh'], [1, 'd', 'src'], [2, 'd', 'core'], [3, 'f', 'api.ts'], [3, 'f', 'reduce.ts'], [3, 'f', 'sync.ts', 'M', true], [3, 'f', 'transport.ts'], [2, 'd', 'ui'], [3, 'f', 'composer.tsx'], [3, 'f', 'timeline.tsx', 'M'], [3, 'f', 'sidebar.tsx'], [3, 'f', 'settings.tsx'], [2, 'f', 'store.ts'], [1, 'f', 'App.tsx'], [1, 'f', 'package.json'], [0, 'd', 'nexus-core'], [1, 'd', 'nexus'], [2, 'd', 'api'], [3, 'f', 'events.py', 'M'], [2, 'f', 'loop.py'], [1, 'd', 'tests'], [2, 'f', 'test_reconnect.py', 'A'],
  ]
  return (
    <div className="n-tree">
      <div className="n-search"><Search {...I} /><span>Go to file</span><Kbd>⌘P</Kbd></div>
      {rows.map(([d, k, t, m, on], i) => <div key={i} className="n-tree__r n-sel" data-on={on || undefined} style={{ '--d': d } as CSSProperties}>{Array.from({ length: d }, (_, j) => <i key={j} className="n-guide" />)}{k === 'd' ? <ChevronDown size={11} strokeWidth={1.8} className="n-faint" /> : <Code2 size={11} strokeWidth={1.7} className="n-faint" />}<span>{t}</span>{m && <b className="n-warnc">{m}</b>}</div>)}
    </div>
  )
}

export function Editor() {
  const src: ReactNode[] = [
    <><Tk k="com">// Resume from the last event the client reduced.</Tk></>,
    <><Tk k="kw">export function</Tk> <Tk k="fn">connect</Tk>(opts: {'{'} after?: <Tk k="ty">EventId</Tk> {'}'}) {'{'}</>,
    <>  <Tk k="kw">const</Tk> url = <Tk k="fn">eventsUrl</Tk>(opts.after)</>,
    <>  <Tk k="kw">const</Tk> socket = <Tk k="kw">new</Tk> <Tk k="ty">EventStream</Tk>(url)</>,
    <>  socket.onevent = (ev) =&gt; {'{'}</>,
    <>    state.cursor = ev.id</>,
    <>    <Tk k="fn">reduce</Tk>(state, ev)</>,
    <>  {'}'}</>,
    <>  socket.onclose = () =&gt; {'{'}</>,
    <>    retry.<Tk k="fn">after</Tk>(backoff.<Tk k="fn">next</Tk>())</>,
    <>    <Tk k="fn">connect</Tk>({'{'} after: state.cursor {'}'})</>,
    <>  {'}'}</>,
    <>{'}'}</>,
    <></>,
    <><Tk k="com">// Applied in order. An event seen twice is ignored.</Tk></>,
    <><Tk k="kw">function</Tk> <Tk k="fn">apply</Tk>(ev: <Tk k="ty">StreamEvent</Tk>) {'{'}</>,
    <>  <Tk k="kw">if</Tk> (ev.id &lt;= state.cursor) <Tk k="kw">return</Tk></>,
    <>  <Tk k="kw">switch</Tk> (ev.type) {'{'}</>,
    <>    <Tk k="kw">case</Tk> <Tk k="str">"turn.started"</Tk>: <Tk k="kw">return</Tk> <Tk k="fn">openTurn</Tk>(ev)</>,
    <>    <Tk k="kw">case</Tk> <Tk k="str">"tool.finished"</Tk>: <Tk k="kw">return</Tk> <Tk k="fn">closeTool</Tk>(ev)</>,
    <>    <Tk k="kw">case</Tk> <Tk k="str">"turn.finished"</Tk>: <Tk k="kw">return</Tk> <Tk k="fn">closeTurn</Tk>(ev)</>,
    <>  {'}'}</>,
    <>{'}'}</>,
    <></>,
    <><Tk k="kw">export const</Tk> backoff = <Tk k="fn">exponential</Tk>({'{'} from: <Tk k="num">250</Tk>, to: <Tk k="num">8000</Tk> {'}'})</>,
  ]
  return (
    <div className="n-editor">
      <div className="n-editor__tabs"><span data-on="1"><Code2 size={12} strokeWidth={1.7} />sync.ts<b className="n-warnc">●</b></span><span><Code2 size={12} strokeWidth={1.7} />reduce.ts</span></div>
      <div className="n-editor__crumb">nexus-nsh <ChevronRight size={10} /> src <ChevronRight size={10} /> core <ChevronRight size={10} /> sync.ts</div>
      <pre className="n-editor__code">{src.map((l, i) => <span key={i} className="ln" data-hl={i === 9 || i === 10 || undefined}><i>{84 + i}</i>{l}</span>)}</pre>
    </div>
  )
}

export function GitPanel() {
  const log: [string, string, string, 0 | 1][] = [
    ['fix(nsh): keep the cursor across a restart', 'a41c9e', '2m', 0],
    ['test: replay after reconnect', '7be012', '9m', 0],
    ['Merge branch retention', 'c3fa18', '1h', 1],
    ['feat(ui): improve native phone navigation', '95798a', '3h', 0],
    ['fix(nsh): NSAllowsArbitraryLoads alone', '4c5fc9', '5h', 0],
    ['fix(nsh): reach Core over plain HTTP on a tailnet', '762cd9', '6h', 0],
    ['docs(nsh): document installing the iPhone app', '601b6d', '7h', 0],
  ]
  return (
    <div className="n-git">
      <div className="n-git__l">
        <div className="n-git__bh"><GitBranch {...I} /><b>main</b><span className="n-faint">↑1 ↓0</span><span className="n-grow" /><span className="n-wbtn">Push</span></div>
        <Label end="3">Changes</Label>
        <div className="n-tree__r n-sel" data-on="1"><b className="n-warnc">M</b><span>src/core/sync.ts</span><span className="n-grow" /><em className="n-add">+18</em><em className="n-del">−4</em></div>
        <div className="n-tree__r n-sel"><b className="n-warnc">M</b><span>nexus/api/events.py</span><span className="n-grow" /><em className="n-add">+6</em><em className="n-del">−1</em></div>
        <div className="n-tree__r n-sel"><b className="n-addc">A</b><span>tests/test_reconnect.py</span><span className="n-grow" /><em className="n-add">+44</em></div>
        <div className="n-git__msg n-edge"><span>fix(nsh): keep the cursor across a restart<i className="n-caret" /></span><span className="n-wbtn n-wbtn--pri">Commit 3 files</span></div>
        <Label end="3">Worktrees</Label>
        <div className="n-tree__r n-sel"><GitBranch size={12} strokeWidth={1.7} /><span>main</span><span className="n-grow" /><span className="n-faint n-small">~/repos/nexus</span></div>
        <div className="n-tree__r n-sel"><GitBranch size={12} strokeWidth={1.7} /><span>retention</span><span className="n-grow" /><span className="n-faint n-small">2 sessions</span></div>
        <div className="n-tree__r n-sel"><GitBranch size={12} strokeWidth={1.7} /><span>nsh-ios</span><span className="n-grow" /><span className="n-faint n-small">1 session</span></div>
      </div>
      <div className="n-git__r">
        <DiffBlock wide={false} />
        <Label end="439 commits">History</Label>
        {log.map(([m, h, t, side], i) => (
          <div className="n-log" key={h}>
            <svg width="26" height="30" viewBox="0 0 26 30" aria-hidden="true">
              <path d="M8 0V30" stroke="var(--n-accent)" strokeWidth="1.4" opacity=".7" />
              {i === 1 && <path d="M8 22C8 30 20 22 20 30" stroke="var(--n-info)" strokeWidth="1.4" fill="none" />}
              {side === 1 && <path d="M20 0C20 12 8 6 8 15" stroke="var(--n-info)" strokeWidth="1.4" fill="none" />}
              <circle cx="8" cy="15" r="3" fill={i === 0 ? 'var(--n-accent)' : 'var(--n-canvas)'} stroke="var(--n-accent)" strokeWidth="1.4" />
            </svg>
            <span className="n-log__m">{m}</span><code>{h}</code><span className="n-faint">{t}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/* ---------- children ---------- */

export function Children() {
  const kids: [string, string, string, string, Sess['state']][] = [
    ['Trace the cursor', 'Explore', 'teal', '38s', 'run'],
    ['Replay test', 'Build', 'amber', '21s', 'run'],
    ['Docs for reconnect', 'Plan', 'violet', '1m 12s', 'idle'],
  ]
  return (
    <div className="n-kids n-edge">
      <div className="n-kids__h"><GitBranch size={13} strokeWidth={1.7} /><b>3 child sessions</b><span className="n-faint">2 working</span></div>
      {kids.map(([t, a, c, tm, st]) => <div key={t} className="n-mrow n-sel"><Mark state={st} /><span>{t}</span><Ag c={c}>{a}</Ag><span className="n-grow" /><span className="n-faint n-num">{tm}</span><ChevronRight size={12} className="n-faint" /></div>)}
    </div>
  )
}

/* ---------- compact rail ---------- */

export function CollapsedRail() {
  const rows: [string, number, number, number, number, boolean?][] = [['NX', 1, 0, 2, 3, true], ['NS', 0, 2, 0, 1], ['MC', 0, 0, 1, 0], ['TC', 0, 0, 0, 4]]
  return (
    <div className="n-crail">
      {rows.map(([n, a, f, r, i, on]) => (
        <div key={n} className="n-crail__c n-sel" data-on={on || undefined}>
          <div className="n-crail__t"><b className="n-accentc">{a || ' '}</b><b className="n-addc">{f || ' '}</b></div>
          <span className="n-crail__n">{n}</span>
          <div className="n-crail__t"><b className="n-infoc">{r || ' '}</b><b className="n-faint">{i || ' '}</b></div>
        </div>
      ))}
      <span className="n-grow" />
      <div className="n-crail__c"><Settings {...I} className="n-ico" /></div>
    </div>
  )
}

/* ---------- connect ---------- */

export function Connect() {
  const v = useV()
  return (
    <div className="n-connect">
      {v.connect === 'heading' && <h2 className="n-connect__h">Connect to Core</h2>}
      {v.connect === 'word' && <h2 className="n-connect__w">Nexus</h2>}
      <div className="n-connect__f"><Lock size={13} strokeWidth={1.7} /><span>http://hamming.local:4747</span></div>
      <span className="n-wbtn n-wbtn--pri n-wbtn--wide">Connect</span>
      <span className="n-faint n-small">Run <code>nexus web</code> on the machine that holds your sessions.</span>
    </div>
  )
}

export const Dot = Circle
