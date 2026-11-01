// The selected design (Void, with the user's picks from 2026-11-01) is fixed in
// the parts and nexus.css. These are the decisions still open.

export type Variants = {
  bar: 'dots' | 'line'
  barpos: 'foot' | 'top'
  field: 'margins' | 'empty' | 'all'
  panel: 'overlay' | 'push'
  newses: 'docked' | 'line' | 'center'
  wait: 'float' | 'joined'
  agent: 'names' | 'both' | 'mono'
  theme: 'dark' | 'light'
  mono: 'geist' | 'argon'
  connect: 'heading' | 'word' | 'none'
  sidetop: 'icons' | 'seg'
  /** Window chrome: a browser, macOS with a hidden title bar, or macOS with the system's. */
  chrome: 'web' | 'hidden' | 'system'
  closed: boolean
  /** Show the collapsed rail's project hover card. */
  railHover?: boolean
}
export type Axis = Exclude<keyof Variants, 'chrome' | 'closed' | 'railHover'>

// All decided on 2026-11-01. `theme` is only overridden for the light previews.
export const DEFAULTS: Variants = { bar: 'line', barpos: 'foot', field: 'empty', panel: 'push', newses: 'docked', wait: 'float', agent: 'both', theme: 'dark', mono: 'argon', connect: 'heading', sidetop: 'icons', chrome: 'web', closed: false }

export type AxisInfo = { axis: Axis; title: string; question: string; options: { key: string; label: string; note: string }[] }

export const AXES: AxisInfo[] = []

export const SELECTED: [string, string, string?][] = [
  ['Style', 'Void'],
  ['Ground', 'Nothing'],
  ['Working', 'Shimmer'],
  ['User message', 'Full-width band'],
  ['Tool activity', 'Spine'],
  ['Composer', 'Docked bar', 'path, branch and context on their own line'],
  ['Context meter', 'TUI activity bar', 'a line, slightly glowing, in the agent\u2019s color'],
  ['Session row', 'Two lines'],
  ['Waiting card', 'Key chips, floating'],
  ['Right sidebar', 'Icon rail', 'pushes the page'],
  ['Window', 'Chrome band'],
  ['Selection', 'Soft pill'],
  ['New session', 'Context on the timeline'],
  ['Agent color', 'Composer name and the bar', 'not in the session sidebar or turn footer'],
  ['Code font', 'Monaspace Argon'],
  ['Connect', 'Plain heading, _/ field'],
  ['Themes', 'Dark and light'],
  ['Logo', 'Slash N, not bold', 'icons only'],
  ['macOS window', 'Traffic lights in the band'],
  ['TUI', 'Unchanged'],
]
