import { Bar, Collapse, Roll, Stage, States, Text, Window, type Act } from '../../kit'
import './context.css'

// A short promo, played once. Nexus is an agent harness: the timeline shows
// what the agent was given, a change to AGENTS.md does not rewrite this
// session, and the terminal and NSH share one core.
// Holds match the voice in reports/nexus-tweet-kit/script.md.

type Caption = 'context' | 'calls' | 'order' | 'kept' | 'next' | 'core'

type S = {
  tool: boolean
  caption: Caption
  line: 'old' | 'new'
  next: boolean
  term: boolean
  core: boolean
}

const initial: S = {
  tool: false,
  caption: 'context',
  line: 'old',
  next: false,
  term: false,
  core: false,
}

const poster: S = {
  tool: true,
  caption: 'core',
  line: 'new',
  next: true,
  term: true,
  core: true,
}

const CAPTION: Record<Caption, string> = {
  context: 'Nexus, an agent harness',
  calls: 'You see what the agent was given',
  order: 'Every step, in order',
  kept: 'The file changed. The session didn’t.',
  next: 'New sessions pick up the file',
  core: 'Terminal and NSH. One core.',
}

async function script(a: Act<S>) {
  // The recorder sets this once the capture has started, so the opening
  // caption is in the file and not lost during page load.
  if (typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('record')) {
    await new Promise<void>((resolve) => {
      const tick = () => {
        if ((window as unknown as { __nexusGo?: boolean }).__nexusGo) resolve()
        else setTimeout(tick, 30)
      }
      tick()
    })
  }
  a.mark('open')
  await a.wait(5400)
  a.mark('calls')
  a.put({ tool: true, caption: 'calls' })
  await a.wait(6000)
  a.mark('order')
  a.put({ caption: 'order' })
  await a.wait(6200)
  a.mark('edit')
  a.put({ line: 'new' })
  await a.wait(2200)
  a.mark('kept')
  a.put({ caption: 'kept' })
  await a.wait(3800)
  a.mark('next')
  a.put({ next: true, caption: 'next' })
  await a.wait(3800)
  a.mark('term')
  a.put({ term: true })
  await a.wait(3800)
  a.mark('core')
  a.put({ core: true, caption: 'core' })
  await a.wait(4600)
  a.mark('end')
}

function Side() {
  return (
    <div className="nv-side">
      <div className="nv-search">
        <Bar w={92} h={7} r={3.5} tone={3} />
      </div>
      <div className="nv-sess" data-on>
        <Text>Auth change</Text>
      </div>
      <div className="nv-sess">
        <Bar w={78} h={7} r={3.5} tone={3} />
      </div>
      <div className="nv-sess">
        <Bar w={104} h={7} r={3.5} tone={2} />
      </div>
      <div className="nv-sess">
        <Bar w={64} h={7} r={3.5} tone={2} />
      </div>
    </div>
  )
}

function Timeline({ s }: { s: S }) {
  return (
    <div className="nv-body">
      <States
        className="nv-caption"
        align="start"
        value={s.caption}
        states={{
          context: <Text>{CAPTION.context}</Text>,
          calls: <Text>{CAPTION.calls}</Text>,
          order: <Text>{CAPTION.order}</Text>,
          kept: <Text>{CAPTION.kept}</Text>,
          next: <Text>{CAPTION.next}</Text>,
          core: <Text>{CAPTION.core}</Text>,
        }}
      />
      <section className="nv-card">
        <Text className="nv-kicker">Starting context</Text>
        <div className="nv-row">
          <Text>System prompt</Text>
        </div>
        <div className="nv-row" data-held={s.line === 'new' || undefined}>
          <Text>AGENTS.md</Text>
          <Text className="nv-dot">·</Text>
          <Text className="nv-held">Be concise.</Text>
          <span className="nv-snap">this session</span>
        </div>
        <div className="nv-row">
          <Text>Tools</Text>
          <Text className="nv-dot">·</Text>
          <Text className="nv-faint">read, edit, bash</Text>
        </div>
      </section>
      <div className="nv-you">
        <Text>Summarize the auth change.</Text>
      </div>
      <div className="nv-toolslot">
        <Collapse open={!s.tool}>
          <div className="nv-fold">
            <span className="nv-chev" aria-hidden>
              ›
            </span>
            <Text>1 read</Text>
          </div>
        </Collapse>
        <Collapse open={s.tool}>
          <div className="nv-detail">
            <div className="nv-row">
              <Text className="nv-k">read</Text>
              <Text className="nv-path">auth.py</Text>
            </div>
            <Text className="nv-result">86 lines came back</Text>
          </div>
        </Collapse>
      </div>
      <Text className="nv-answer" wrap>
        The check now rejects empty tokens.
      </Text>
      <div className="nv-tail" data-crop>
        <Bar w={210} h={8} r={4} tone={2} />
        <Bar w={148} h={8} r={4} tone={1} />
        <Bar w={176} h={8} r={4} tone={1} />
        <Bar w={124} h={8} r={4} tone={1} />
        <Bar w={188} h={8} r={4} tone={1} />
        <Bar w={96} h={8} r={4} tone={1} />
      </div>
    </div>
  )
}

export default function NexusContext() {
  return (
    <Stage
      width={960}
      height={540}
      compact={{ below: 600, width: 358, height: 640 }}
      theme="dark"
      initial={initial}
      poster={poster}
      script={script}
      play="once"
      fill
      className="nv"
    >
      {(s, _rect, layout) => (
        <>
          <Window
            title="Auth change"
            className="nv-win"
            sidebarWidth={168}
            sidebar={layout === 'wide' ? <Side /> : undefined}
            lead={<span className="nv-client">NSH</span>}
          >
            <Timeline s={s} />
          </Window>
          <aside className="nv-file" aria-hidden={false}>
            <div className="nv-file-head">
              <Text>AGENTS.md</Text>
            </div>
            <div className="nv-file-line" data-new={s.line === 'new' || undefined}>
              <Roll>{s.line === 'new' ? 'Explain the tradeoff.' : 'Be concise.'}</Roll>
            </div>
            <Collapse open={s.next}>
              <div className="nv-next">
                <Text className="nv-kicker">Next session</Text>
                <Text>Explain the tradeoff.</Text>
              </div>
            </Collapse>
          </aside>
          <Window
            chrome="terminal"
            lights="grey"
            title="Terminal"
            className="nv-term"
            data-in={s.term || undefined}
          >
            <div className="nv-trow">
              <span className="dim">file</span>
              <span>Be concise.</span>
            </div>
            <div className="nv-trow">
              <span className="dim">you</span>
              <span>Summarize the auth change.</span>
            </div>
            <div className="nv-trow">
              <span className="dim">read</span>
              <span>auth.py</span>
            </div>
          </Window>
          <div className="nv-core" data-on={s.core || undefined}>
            Core
          </div>
        </>
      )}
    </Stage>
  )
}
