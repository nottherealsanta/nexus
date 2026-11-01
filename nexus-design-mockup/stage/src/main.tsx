import { useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { Check, Copy, RotateCcw } from 'lucide-react'
import { Collapse, Phone, Stage, type Act } from '../kit'
import '../kit/stageshow.css'
import './nexus.css'
import './page.css'
import { AXES, DEFAULTS, SELECTED, type Axis, type Variants } from './styles'
import {
  Activity, Approval, Banner, Children, ClosedRail, CodeBlock, CollapsedRail, Composer, Connect, DiffBlock, Editor, FileTree, Footer, GitPanel, Header, JumpPill, ModelPicker, Palette, PhoneHeader, PhoneTabs, Question, Reply, REPLY, Right, SessionsList, SessionRow, SESSIONS, SettingsView, Sidebar, SlashField, StartContext, StillCtx, Thought, Toasts, TrafficLights, TOOLS, UserMsg, VariantCtx,
} from './parts'
import icon1024 from '../../../interface/brand/nexus-1024.png'
import icon180 from '../../../interface/brand/nexus-180.png'
import icon32 from '../../../interface/brand/nexus-32.png'
import icon16 from '../../../interface/brand/nexus-16.png'

/* ---------- scope: tokens plus the open decisions ---------- */

function Nx({ v, children, live }: { v: Variants; children: ReactNode; open?: boolean; live?: boolean }) {
  return (
    <StillCtx.Provider value={!live}>
      <VariantCtx.Provider value={v}>
        <div className="nx" data-style={v.theme === 'light' ? 'void-light' : 'void'} data-frame="band" data-gr="dust" data-select="pill" data-composer="dock" data-wait={v.wait} data-agent={v.agent} data-mono={v.mono} data-chrome={v.chrome} data-closed={v.closed || undefined}>
          <div className="n-ground" />
          {children}
        </div>
      </VariantCtx.Provider>
    </StillCtx.Provider>
  )
}

/* ---------- the shell ---------- */

type Scene = { typed: string; sent: boolean; tools: number; live: boolean; wait: boolean; words: number; done: boolean }
const POSTER: Scene = { typed: '', sent: true, tools: 5, live: true, wait: true, words: 0, done: false }
const START: Scene = { typed: '', sent: false, tools: 0, live: false, wait: false, words: 0, done: false }
const ASK = 'Make reconnect survive a daemon restart without losing the open turn.'

function Shell({ s, sidebar, main, overlay, panel, fresh }: { s?: Scene; sidebar?: ReactNode; main?: ReactNode; overlay?: ReactNode; panel?: boolean; fresh?: boolean }) {
  const sc = s ?? POSTER
  const wordCount = REPLY.split(' ').length
  return (
    <div className="n-win">
      {useContext(VariantCtx).chrome === 'system' && <div className="n-titlebar"><TrafficLights /><span>Nexus</span></div>}
      {useContext(VariantCtx).chrome === 'hidden' && <TrafficLights />}
      {useContext(VariantCtx).closed ? <ClosedRail /> : sidebar ?? <Sidebar />}
      <main className="n-main">
        {fresh ? <Header status="idle" title="New session" solo /> : <Header status={sc.wait ? 'wait' : sc.live ? 'run' : 'idle'} />}
        {main ?? (
          <>
            <div className="n-time"><div className="n-col">
              <Thought time="1.4s">The earlier turn left the retry path in place.</Thought>
              <Activity count={2} summary="1 thought, 2 reads" tools={TOOLS.slice(0, 2)} />
              <p className="n-p">The event store already keeps ids across a restart, so the fix belongs in the client.</p>
              <Footer />
              <Collapse open={sc.sent} reveal="drop" from="below"><UserMsg>{ASK}</UserMsg></Collapse>
              <Collapse open={sc.sent} reveal="drop" from="below"><Thought>The socket close path clears the cursor before it retries.</Thought></Collapse>
              <Collapse open={sc.tools > 0} reveal="drop" from="below"><Activity tools={TOOLS} count={sc.tools} live={sc.live} summary="2 thoughts, 3 commands, 2 reads, 1 edit" /></Collapse>
              <Collapse open={sc.words > 0} reveal="drop" from="below"><Reply count={sc.done ? wordCount : sc.words} /></Collapse>
              <Collapse open={sc.done}><Footer /></Collapse>
            </div></div>
            <div className="n-dockwrap"><div className="n-col n-col--dock"><Collapse open={sc.wait} reveal="drop" from="below"><Approval /></Collapse></div></div>
            <Composer text={sc.typed || undefined} running={sc.live} />
          </>
        )}
      </main>
      <Right open={panel} />
      {overlay && <div className="n-scrim">{overlay}</div>}
    </div>
  )
}

async function heroScript(a: Act<Scene>) {
  await a.wait(600)
  a.move('input', { duration: 500 })
  for (let i = 1; i <= ASK.length; i += 3) { a.put({ typed: ASK.slice(0, i) }); await a.wait(26) }
  a.put({ typed: ASK })
  await a.wait(250)
  await a.move('send', { duration: 480 })
  await a.click()
  a.put({ typed: '', sent: true, live: true })
  for (let t = 1; t <= 3; t++) { await a.wait(620); a.put({ tools: t }) }
  await a.wait(500)
  a.put({ wait: true })
  await a.wait(1100)
  await a.move('allow', { duration: 560 })
  await a.click()
  await a.wait(320)
  a.put({ wait: false, tools: 4 })
  await a.wait(700)
  a.put({ tools: 5 })
  await a.wait(900)
  a.put({ live: false })
  const n = REPLY.split(' ').length
  for (let w = 1; w <= n; w += 2) { a.put({ words: w }); await a.wait(55) }
  a.put({ done: true, words: n })
  await a.wait(2400)
  a.put({ sent: false, tools: 0, words: 0, done: false })
  await a.wait(700)
}

function ShellStill({ v, overlay, sidebar, main, open, panel }: { v: Variants; overlay?: ReactNode; sidebar?: ReactNode; main?: ReactNode; open?: boolean; panel?: boolean }) {
  return <Stage width={1280} height={780} theme="dark" unframed fill className="st">{() => <Nx v={v} open={open}><Shell overlay={overlay} sidebar={sidebar} main={main} panel={panel} fresh={open} /></Nx>}</Stage>
}

function Hero({ v }: { v: Variants }) {
  return (
    <Stage<Scene> width={1280} height={780} theme="dark" unframed initial={START} poster={POSTER} script={heroScript} cursor={{ at: { x: 760, y: 720 }, idle: false }} fill className="st st--hero">
      {(s) => <Nx v={v} live><Shell s={s} /></Nx>}
    </Stage>
  )
}

function Tile({ w, h, v, children, plain, className, live, open }: { w: number; h: number; v: Variants; children: ReactNode; plain?: boolean; className?: string; live?: boolean; open?: boolean }) {
  return <Stage width={w} height={h} theme="dark" unframed fill className="st">{() => <Nx v={v} live={live} open={open}><div className={`n-tilebody${plain ? ' n-tilebody--plain' : ''} ${className ?? ''}`}>{children}</div></Nx>}</Stage>
}

/* ---------- the new session, by decision ---------- */

function NewMain() {
  return <><div className="n-time n-time--top"><div className="n-col"><StartContext /></div></div><Composer placeholder="What should this session do?" /></>
}

/* ---------- demos for each open decision ---------- */

const barDemo = () => <><Reply /><Composer running /></>
const demos: Record<Axis, (v: Variants) => ReactNode> = {
  bar: (v) => <Tile v={v} w={620} h={190} className="n-end" live>{barDemo()}</Tile>,
  barpos: (v) => <Tile v={v} w={620} h={190} className="n-end" live>{barDemo()}</Tile>,
  field: (v) => <ShellStill v={v} />,
  panel: (v) => <ShellStill v={v} panel />,
  newses: (v) => <ShellStill v={v} main={<NewMain />} open />,
  wait: (v) => <Tile v={v} w={760} h={340} className="n-end"><Approval /><Composer running /></Tile>,
  agent: (v) => <Tile v={v} w={620} h={300} className="n-end">{SESSIONS.slice(1, 3).map((s) => <SessionRow key={s.t} s={s} />)}<Composer running /></Tile>,
  theme: (v) => <ShellStill v={v} />,
  mono: (v) => <Tile v={v} w={620} h={240} className="n-center"><div className="n-fullw"><CodeBlock /></div></Tile>,
  connect: (v) => <Tile v={v} w={520} h={380} plain open className="n-center"><Connect /></Tile>,
  sidetop: (v) => <Tile v={v} w={372} h={240} plain className="n-sidetile"><Sidebar /></Tile>,
}

/* ---------- every surface ---------- */

type Surf = { id: string; title: string; note: string; span: number; render: (v: Variants) => ReactNode }
const PHONE_ASK = 'Make reconnect survive a restart without losing the open turn.'
const FILE_MAIN = <div className="n-time n-time--flush"><Editor /></div>
const GIT_MAIN = <div className="n-time n-time--flush"><GitPanel /></div>

const SURFACES: Surf[] = [
  { id: 'palette', title: 'Command palette', note: 'Scrim over the session, no border', span: 6, render: (v) => <ShellStill v={v} overlay={<Palette />} /> },
  { id: 'settings', title: 'Settings', note: 'Section order from the manifest', span: 6, render: (v) => <ShellStill v={v} overlay={<SettingsView />} /> },
  { id: 'new', title: 'New session', note: 'Context on the timeline', span: 6, render: (v) => <ShellStill v={v} open main={<NewMain />} /> },
  { id: 'panel', title: 'Session panel', note: 'Opened from the icon rail, pushing the page', span: 6, render: (v) => <ShellStill v={v} panel /> },
  { id: 'files', title: 'Files and editor', note: 'Desktop and web only', span: 6, render: (v) => <ShellStill v={v} sidebar={<Sidebar tab="files"><FileTree /></Sidebar>} main={FILE_MAIN} /> },
  { id: 'git', title: 'Git', note: 'Desktop and web only', span: 6, render: (v) => <ShellStill v={v} main={GIT_MAIN} /> },
  { id: 'turn', title: 'A full turn', note: 'Band, reasoning, spine, answer, diff, code, footer', span: 7, render: (v) => <Tile v={v} w={860} h={690} className="n-end"><UserMsg>{ASK}</UserMsg><Thought>The socket close path clears the cursor before it retries.</Thought><Activity summary="2 thoughts, 3 commands, 2 reads, 1 edit" tools={TOOLS.map((t) => ({ ...t, state: undefined }))} /><Reply /><DiffBlock /><CodeBlock /><p className="n-p">The new test kills the daemon mid-turn and checks that every event arrives exactly once.</p><Footer /></Tile> },
  { id: 'sessions', title: 'Sessions and collapsed rail', note: 'Attention, finished, running and idle counts on the rail', span: 5, render: (v) => <Tile v={v} w={560} h={640} plain className="n-sidepair"><div className="n-chrome n-sidepair__rail"><CollapsedRail /></div><div className="n-chrome n-sidepair__list"><SessionsList /></div></Tile> },
  { id: 'diff', title: 'Diff', note: 'Split when wide, unified when narrow', span: 7, render: (v) => <Tile v={v} w={760} h={230} className="n-center"><div className="n-fullw"><DiffBlock /></div></Tile> },
  { id: 'kids', title: 'Child sessions', note: 'Each child is a link', span: 5, render: (v) => <Tile v={v} w={520} h={230} className="n-center"><Children /></Tile> },
  { id: 'approval', title: 'Approval', note: 'Key chips above the dock', span: 6, render: (v) => <Tile v={v} w={620} h={400} className="n-end"><Approval /><Composer running /></Tile> },
  { id: 'question', title: 'Question', note: 'Multi-select with your own answer', span: 6, render: (v) => <Tile v={v} w={620} h={400} className="n-end"><Question /><Composer running /></Tile> },
  { id: 'completion', title: 'Completion', note: 'As wide as the composer, no border', span: 6, render: (v) => <Tile v={v} w={620} h={340} className="n-end"><div className="n-compl"><div className="n-mrow n-sel" data-on="1"><span>nexus-nsh/src/core/sync.ts</span><span className="n-grow" /><span className="n-faint">file</span></div><div className="n-mrow n-sel"><span>nexus-nsh/src/store.ts</span><span className="n-grow" /><span className="n-faint">file</span></div><div className="n-mrow n-sel"><span>$stageshow</span><span className="n-grow" /><span className="n-faint">skill</span></div></div><Composer text={<>Look at @s<i className="n-caret" /></>} /></Tile> },
  { id: 'model', title: 'Model picker', note: 'Fuzzy search, details beneath', span: 6, render: (v) => <Tile v={v} w={620} h={340} className="n-center"><ModelPicker /></Tile> },
  { id: 'alerts', title: 'Toasts, banner and jump', note: 'Long notifications open a modal when clicked', span: 8, render: (v) => <Tile v={v} w={800} h={470} plain><div className="n-alerts"><Banner /><div className="n-alerts__b"><p className="n-p">The run keeps going while the client retries. Nothing is lost: the timeline fills in from the last event it saw.</p><Activity summary="1 command, 1 edit" tools={TOOLS.slice(2, 4)} /><p className="n-p">The daemon came back after 3 seconds and replayed 41 events.</p><Toasts /></div><div className="n-alerts__f"><JumpPill /><Composer /></div></div></Tile> },
  { id: 'connect', title: 'Connect', note: 'No logo, a field of its marks', span: 4, render: (v) => <Tile v={v} w={420} h={500} plain open className="n-center"><SlashField /><Connect /></Tile> },
  { id: 'phone', title: 'iPhone', note: 'Bottom tabs and a back control, as phones work today. No files, editor, Git or logo', span: 12, render: (v) => <Tile v={v} w={1240} h={790} plain className="n-phones">
    <Phone width={340} dark={v.theme !== 'light'} wallpaper="var(--n-canvas)"><div className="n-phone"><PhoneHeader /><div className="n-time n-time--phone"><UserMsg>{PHONE_ASK}</UserMsg><Thought>The socket close path clears the cursor.</Thought><Activity live tools={TOOLS.slice(1)} /><p className="n-p">The event store keeps ids across a restart, so the fix belongs in the client.</p></div><div className="n-phonedock"><Composer running placeholder="Ask anything" /></div></div></Phone>
    <Phone width={340} dark={v.theme !== 'light'} wallpaper="var(--n-canvas)"><div className="n-phone"><PhoneHeader status="wait" /><div className="n-time n-time--phone n-time--phoneend"><UserMsg>{PHONE_ASK}</UserMsg><Activity live tools={TOOLS.slice(2)} /></div><div className="n-phonedock"><Approval /><Composer running placeholder="Ask anything" /></div></div></Phone>
    <Phone width={340} dark={v.theme !== 'light'} wallpaper="var(--n-chrome)"><div className="n-phone n-chrome"><div className="n-phonelist"><SessionsList /></div><PhoneTabs /></div></Phone>
  </Tile> },
]

/* ---------- the page ---------- */

const KEY = 'nexus-void-design-1'
function load() { try { return JSON.parse(localStorage.getItem(KEY) ?? '{}') } catch { return {} } }

function App() {
  const saved = useMemo(load, [])
  const [picks, setPicks] = useState<Partial<Variants>>(saved.picks ?? {})
  const [notes, setNotes] = useState<Record<string, string>>(saved.notes ?? {})
  const [copied, setCopied] = useState<'' | 'ok' | 'no'>('')
  const chosen: Variants = { ...DEFAULTS, ...picks }
  // The light theme is judged on its own tile; the page itself stays dark.
  const v: Variants = { ...chosen, theme: 'dark' }

  useEffect(() => { try { localStorage.setItem(KEY, JSON.stringify({ picks, notes })) } catch { /* private window: choices last for this visit */ } }, [picks, notes])

  const exportJson = JSON.stringify({
    exported: new Date().toISOString().slice(0, 10),
    decisions: Object.fromEntries(AXES.map(({ axis, options }) => [axis, { pick: options.find((o) => o.key === chosen[axis])?.label, from: picks[axis] ? 'picked' : 'recommended', note: notes[axis] ?? '' }])),
    overall_note: notes.overall ?? '',
    surface_notes: Object.fromEntries(SURFACES.filter((x) => notes[x.id]).map((x) => [x.id, notes[x.id]])),
  }, null, 2)

  const copy = async () => {
    try { await navigator.clipboard.writeText(exportJson); setCopied('ok') } catch { setCopied('no') }
    setTimeout(() => setCopied((c) => (c === 'ok' ? '' : c)), 2200)
  }
  const nPicks = Object.keys(picks).length
  // The page renders after load, so a link to a section has to scroll itself.
  useEffect(() => { if (location.hash) document.querySelector(location.hash)?.scrollIntoView() }, [])

  return (
    <div className="pg">
      <nav className="pg-bar">
        <span className="pg-bar__t">Nexus design: Void</span>
        <a href="#selected">Selected</a><a href="#notes">Your notes</a><a href="#window">Window</a><a href="#light">Light theme</a><a href="#surfaces">All surfaces</a>
        <span className="pg-grow" />
        {nPicks > 0 && <button className="pg-btn" onClick={() => setPicks({})}><RotateCcw size={13} />Reset {nPicks} {nPicks === 1 ? 'pick' : 'picks'}</button>}
        <button className="pg-btn pg-btn--pri" onClick={copy}>{copied === 'ok' ? <Check size={13} /> : <Copy size={13} />}{copied === 'ok' ? 'Copied' : 'Copy notes'}</button>
      </nav>

      <main className="pg-main">
        {copied === 'no' && <section className="pg-fallback"><p>Your browser blocked the clipboard. Select this text and copy it, then paste it into the session.</p><textarea id="export-fallback" readOnly value={exportJson} rows={8} /></section>}

        <section id="selected" className="pg-sec pg-first">
          <div className="pg-sec__h"><h1>Your selected design</h1><p>Void with all your decisions in, playing a full turn. Below are your latest corrections, the light theme and every surface.</p></div>
          <div className="pg-frame"><Hero v={v} /></div>
          <dl className="pg-picked">{SELECTED.map(([k, val, sub]) => <div key={k}><dt>{k}</dt><dd>{val}{sub && <small>{sub}</small>}</dd></div>)}</dl>
        </section>

        <section id="notes" className="pg-sec">
          <div className="pg-sec__h"><h2>Your notes, applied</h2></div>
          <div className="pg-notes">
            <article className="pg-card pg-card--wide">
              <h3>The activity bar is a line</h3>
              <p>It behaves like the TUI's rule, with a slight glow and in the agent's color. At rest it fills to show context used. While a turn runs, a soft pulse sweeps across it.</p>
              <div className="pg-bars">
                <figure><div className="pg-shot"><Tile v={v} w={620} h={116} className="n-end n-bardemo"><Composer /></Tile></div><figcaption>At rest: 41% of context used</figcaption></figure>
                <figure><div className="pg-shot"><Tile v={v} w={620} h={116} className="n-end n-bardemo" live><Composer running /></Tile></div><figcaption>Running: the pulse sweeps</figcaption></figure>
              </div>
            </article>
            <article className="pg-card">
              <h3>New session context is on the timeline</h3>
              <p>Back at the top of the context line, with the composer docked below. The window has nothing behind it.</p>
              <div className="pg-shot"><ShellStill v={v} open main={<NewMain />} /></div>
            </article>
            <article className="pg-card">
              <h3>Glyphs are gone, except on Connect</h3>
              <p>Your note asked for a glyph made of <code>_/</code> here, so this is the one screen that keeps a field, built only from the logo's mark.</p>
              <div className="pg-shot"><Tile v={v} w={716} h={436} plain open className="n-center"><SlashField /><Connect /></Tile></div>
            </article>
            <article className="pg-card pg-card--wide">
              <h3>The logo is thin at every size</h3>
              <p>All sizes now use the slash N as written, not bold. At 16 and 32 pixels the marks sit on the pixel grid, so they stay marks rather than blur. These are shown at their real size.</p>
              <div className="pg-icons">
                <div className="pg-tab"><span className="pg-tabstrip"><img src={icon16} width="16" height="16" alt="Nexus tab icon at 16 pixels" /><span>Nexus</span><i>×</i></span><small>Browser tab, 16px</small></div>
                <figure><img src={icon32} width="32" height="32" alt="Nexus icon at 32 pixels" /><figcaption>32</figcaption></figure>
                <figure><img src={icon180} width="90" height="90" className="pg-round" alt="Nexus icon at 180 pixels, shown at half size" /><figcaption>180, iPhone</figcaption></figure>
                <figure><img src={icon1024} width="150" height="150" className="pg-round2" alt="Nexus app icon at 1024 pixels, shown small" /><figcaption>1024, app icon</figcaption></figure>
              </div>
            </article>
          </div>
        </section>

        <section id="light" className="pg-sec">
          <div className="pg-sec__h"><h2>Light theme</h2><p>The same parts with black ink on white.</p></div>
          <div className="pg-grid">
            <figure className="pg-surf" style={{ gridColumn: 'span 6' }}><div className="pg-surf__shot"><ShellStill v={{ ...v, theme: 'light' }} /></div><figcaption><b>Session</b></figcaption></figure>
            <figure className="pg-surf" style={{ gridColumn: 'span 6' }}><div className="pg-surf__shot"><ShellStill v={{ ...v, theme: 'light' }} open main={<NewMain />} /></div><figcaption><b>New session</b></figcaption></figure>
          </div>
        </section>

        <section id="window" className="pg-sec">
          <div className="pg-sec__h"><h2>The left toggle and the traffic lights</h2><p>The toggle is now the first control at the top left, and it stays there when the sidebar closes. On the web it sits flush left. On macOS, the toggle sits right after the traffic lights in the band.</p></div>
          <div className="pg-grid">
            <figure className="pg-surf" id="window-web" style={{ gridColumn: 'span 6' }}><div className="pg-surf__shot"><ShellStill v={v} /></div><figcaption><b>Web, sidebar open</b><span>Flush left. Nothing else to think about</span></figcaption></figure>
            <figure className="pg-surf" id="window-web-closed" style={{ gridColumn: 'span 6' }}><div className="pg-surf__shot"><ShellStill v={{ ...v, closed: true }} /></div><figcaption><b>Web, sidebar closed</b><span>The toggle doesn't move</span></figcaption></figure>
            <figure className="pg-surf" id="window-rail-hover" style={{ gridColumn: 'span 12' }}><div className="pg-surf__shot"><ShellStill v={{ ...v, closed: true, railHover: true }} /></div><figcaption><b>Hovering a project on the closed sidebar</b><span>Its name and activity, as in the TUI</span></figcaption></figure>
            <figure className="pg-surf" id="window-hidden" style={{ gridColumn: 'span 6' }}><div className="pg-surf__shot"><ShellStill v={{ ...v, chrome: 'hidden' }} /></div><figcaption><b>macOS: lights in the band</b><span>Toggle right after the lights. In full screen the lights go away and it moves flush left</span></figcaption></figure>
            <figure className="pg-surf" id="window-hidden-closed" style={{ gridColumn: 'span 6' }}><div className="pg-surf__shot"><ShellStill v={{ ...v, chrome: 'hidden', closed: true }} /></div><figcaption><b>macOS, sidebar closed</b><span>Lights and toggle hold the same spot; the rail starts below them</span></figcaption></figure>
          </div>
        </section>

        <section id="surfaces" className="pg-sec">
          <div className="pg-sec__h"><h2>Every surface</h2><p>Everything Nexus shows, with your selection and the decisions above.</p></div>
          <div className="pg-grid">
            {SURFACES.map((x) => (
              <figure key={x.id} id={`surface-${x.id}`} className="pg-surf" style={{ gridColumn: `span ${x.span}` }}>
                <div className="pg-surf__shot">{x.render(v)}</div>
                <figcaption><b>{x.title}</b><span>{x.note}</span></figcaption>
                <label className="pg-note" htmlFor={`note-${x.id}`}><span className="pg-vh">Notes on {x.title}</span><input id={`note-${x.id}`} value={notes[x.id] ?? ''} onChange={(e) => setNotes({ ...notes, [x.id]: e.target.value })} placeholder="Notes (optional)" /></label>
              </figure>
            ))}
          </div>
          <label className="pg-note" htmlFor="note-overall"><span>Anything else</span><textarea id="note-overall" rows={3} value={notes.overall ?? ''} onChange={(e) => setNotes({ ...notes, overall: e.target.value })} /></label>
        </section>
      </main>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
