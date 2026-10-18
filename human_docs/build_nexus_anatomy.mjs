import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as k from "/Users/santa/repos/nexus/.agents/skills/anatomy/kit/iso-kit.mjs";

const theme = process.argv.includes("--light") ? "light" : "dark";

const WIDTH = 680;
const HEIGHT = 440;

// World geometry definition
const PLATE = { x: -140, y: -80, w: 280, d: 160, r: 8 };
const PLATE_H = 12;
const FOOT_H = 5;

// Spool / Chrono-Spine (SQLite append-only event reel)
const SPOOL = [-88, -20];
const SPOOL_R = 24;
const SPOOL_Z = 0;
const SPOOL_H = 26;

// Prism (pure event reducer: view/reduce.py -> ConversationView)
const PRISM_BASE = { x: -32, y: -26, w: 46, d: 42, r: 3 };
const PRISM = [-9, -5];
const PRISM_Z = 16;
const PRISM_H = 20;

// Geneva Turret (protocol execution loop: core/loop.py)
const TURRET = [68, -12];
const TURRET_R = 28;
const TURRET_Z = 0;
const TURRET_H = 14;

// Dial Indicator (Context Window Token Budget)
const DIAL = [68, -58];
const DIAL_R = 18;
const DIAL_Z = 72;
const DIAL_H = 8;
const DIAL_TOP = DIAL_Z + DIAL_H;

// Projection setup
const P = k.fitProjection(
  [
    ...k.boxCorners(PLATE, -PLATE_H - FOOT_H, 0),
    [SPOOL[0] - SPOOL_R, SPOOL[1] - SPOOL_R, SPOOL_H + 8],
    [TURRET[0] + TURRET_R, TURRET[1] + TURRET_R, TURRET_H + 12],
    [DIAL[0] + DIAL_R, DIAL[1], DIAL_TOP + 4],
  ],
  WIDTH,
  HEIGHT,
  { pad: 28, azimuth: 44, elevation: 32 }
);

const at = (point) => k.iso(point, P);
const S = (paths, style) => k.solidSvg(paths, style);
const L = (d, style) => k.lineSvg(d, style);
const D = (points, style) => k.dotsSvg(points, style);
const byDepth = (points) => [...points].sort((a, b) => k.depthOf([a[0], a[1], 0], P) - k.depthOf([b[0], b[1], 0], P));

// 1. Base Plate & Hardware
function basePlate() {
  const out = [`<path class="iso-halo" d="${k.haloOf(PLATE, -PLATE_H - FOOT_H, PLATE_H, P)}"/>`];
  
  for (const [x, y] of byDepth(k.corners(PLATE, 20))) {
    out.push(S(k.cylinder(x, y, 7, -PLATE_H - FOOT_H, 1.8, P, 28), { tone: "lo" }));
    out.push(S(k.cylinder(x, y, 5.2, -PLATE_H - FOOT_H + 1.8, FOOT_H - 1.8, P, 28), { tone: "mid" }));
  }
  
  out.push(S(k.slabOf(PLATE, -PLATE_H, PLATE_H, P, 8, 1.5), { tone: "mid" }));
  out.push(L(k.planOutline(k.insetPlan(PLATE, 5), 0, P), { tone: "faint" }));
  
  const screws = k.corners(PLATE, 10);
  out.push(L(screws.map(([x, y]) => k.ring(x, y, 2.2, 0, P, 16)).join(""), { tone: "lo" }));
  out.push(D(screws.map(([x, y]) => at([x, y, 0])), { size: 0.5, tone: "mid" }));

  const strip = { x: -116, y: PLATE.y + PLATE.d - 14, w: 232, d: 7, r: 1 };
  out.push(S(k.slabOf(strip, 0, 0.6, P, 3), { tone: "lo", crease: "none" }));
  const ticks = k.topTicks(strip.x + 4, strip.x + strip.w - 4, 4, 5, strip.y + 0.8, 0.6, [2, 3.6], P);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  out.push(D([at([strip.x + 2, strip.y + 3.5, 0.6]), at([strip.x + strip.w - 2, strip.y + 3.5, 0.6])], { size: 0.45 }));

  const plaque = { x: -124, y: PLATE.y + PLATE.d - 36, w: 54, d: 16, r: 1.5 };
  out.push(S(k.slabOf(plaque, 0, 0.8, P, 4), { tone: "mid" }));
  out.push(L(k.planOutline(k.insetPlan(plaque, 1.4), 0.8, P), { tone: "faint" }));
  out.push(L(k.lineOnTop([plaque.x + 4, plaque.y + 5], [plaque.x + plaque.w - 4, plaque.y + 5], 0.8, P), { tone: "lo", free: true }));
  out.push(L(k.lineOnTop([plaque.x + 4, plaque.y + 9], [plaque.x + plaque.w - 12, plaque.y + 9], 0.8, P), { tone: "lo", free: true }));
  out.push(L(k.lineOnTop([plaque.x + 4, plaque.y + 13], [plaque.x + plaque.w - 8, plaque.y + 13], 0.8, P), { tone: "faint", free: true }));
  out.push(D([at([plaque.x + 2.4, plaque.y + 2.4, 0.8]), at([plaque.x + plaque.w - 2.4, plaque.y + plaque.d - 2.4, 0.8])], { size: 0.35, tone: "mid" }));

  return out.join("");
}

// 2. The Append-Only Chrono-Spine Reel
function chronoSpool() {
  const [cx, cy] = SPOOL;
  const out = [];

  const pad = { x: cx - SPOOL_R - 6, y: cy - SPOOL_R - 6, w: (SPOOL_R + 6) * 2, d: (SPOOL_R + 6) * 2, r: 4 };
  out.push(S(k.slabOf(pad, 0, 2.5, P, 5, 0.6), { tone: "mid" }));
  out.push(L(k.planOutline(k.insetPlan(pad, 2), 2.5, P), { tone: "faint" }));

  out.push(S(k.cylinder(cx, cy, 5.5, 2.5, 30, P, 24), { tone: "mid" }));
  out.push(S(k.cylinder(cx, cy, SPOOL_R, 6, 2.2, P, 48), { tone: "mid" }));
  out.push(L(k.ring(cx, cy, SPOOL_R - 1.2, 8.2, P, 48), { tone: "lo" }));

  out.push(S(k.cylinder(cx, cy, SPOOL_R - 4, 8.2, 14, P, 40), { tone: "hi" }));
  out.push(L(k.sideArc(cx, cy, SPOOL_R - 4, 12, P, 32), { tone: "faint" }));
  out.push(L(k.sideArc(cx, cy, SPOOL_R - 4, 16, P, 32), { tone: "faint" }));
  out.push(L(k.sideArc(cx, cy, SPOOL_R - 4, 20, P, 32), { tone: "faint" }));

  out.push(S(k.cylinder(cx, cy, SPOOL_R, 22.2, 2.2, P, 48), { tone: "mid" }));
  out.push(L(k.ring(cx, cy, SPOOL_R - 1.2, 24.4, P, 48), { tone: "lo" }));
  out.push(L(k.ring(cx, cy, 14, 24.4, P, 32), { tone: "faint" }));

  out.push(S(k.cylinder(cx, cy, 7.5, 24.4, 4.5, P, 28), { tone: "hi" }));
  out.push(L(k.knurl(cx, cy, 7.5, 24.4, 28.9, 32, P), { tone: "lo" }));

  for (const [rx, ry] of [[cx + 22, cy + 18], [cx + 34, cy + 12]]) {
    out.push(S(k.cylinder(rx, ry, 2.8, 0, 16, P, 20), { tone: "mid" }));
    out.push(D([at([rx, ry, 16])], { size: 0.45, tone: "hi" }));
  }

  const hammer = { x: cx - SPOOL_R - 4, y: cy - 4, w: 8, d: 8, r: 1.5 };
  out.push(S(k.slabOf(hammer, 4, 14, P, 3, 0.4), { tone: "mid" }));
  out.push(S(k.cylinder(cx - SPOOL_R, cy, 2.2, 18, 4, P, 16), { tone: "hi" }));

  return out.join("");
}

// 3. The Pure Optical Reducer Prism
function opticalPrism() {
  const out = [];

  out.push(S(k.slabOf(PRISM_BASE, 0, 3.5, P, 4, 0.6), { tone: "mid" }));
  out.push(L(k.planOutline(k.insetPlan(PRISM_BASE, 2), 3.5, P), { tone: "faint" }));

  for (const [x, y] of k.corners(PRISM_BASE, 6)) {
    out.push(S(k.cylinder(x, y, 2.2, 3.5, 12.5, P, 20), { tone: "mid", crease: "none" }));
  }

  const gimbal = { x: PRISM_BASE.x + 2, y: PRISM_BASE.y + 2, w: PRISM_BASE.w - 4, d: PRISM_BASE.d - 4, r: 2 };
  out.push(S(k.slabOf(gimbal, 16, 2.4, P, 4, 0.4), { tone: "mid" }));

  const [px, py] = PRISM;
  const prismCore = { x: px - 11, y: py - 11, w: 22, d: 22, r: 2 };
  out.push(S(k.slabOf(prismCore, 18.4, 14, P, 4, 1.2), { tone: "hi", lit: true }));
  out.push(L(k.planOutline(k.insetPlan(prismCore, 1.6), 32.4, P), { tone: "mid" }));

  const conduitFrom = [px + 8, py + 8, 22];
  const conduits = [
    { to: [PRISM_BASE.x + PRISM_BASE.w + 14, PRISM_BASE.y + 6, 6], label: "Ratatui" },
    { to: [PRISM_BASE.x + PRISM_BASE.w + 18, PRISM_BASE.y + 16, 6], label: "GPUI" },
    { to: [PRISM_BASE.x + PRISM_BASE.w + 22, PRISM_BASE.y + 26, 6], label: "CLI" },
    { to: [PRISM_BASE.x + PRISM_BASE.w + 26, PRISM_BASE.y + 36, 6], label: "Web" },
  ];

  for (const c of conduits) {
    out.push(k.wireSvg(at(conduitFrom), at(c.to), { tone: "mid", size: 0.8 }));
    out.push(S(k.cylinder(c.to[0], c.to[1], 2.4, 0, 6, P, 16), { tone: "mid" }));
    out.push(D([at([c.to[0], c.to[1], 6])], { size: 0.45, tone: "hi" }));
  }

  return out.join("");
}

// 4. The Geneva Turret
function genevaTurret() {
  const [tx, ty] = TURRET;
  const out = [];

  out.push(S(k.cylinder(tx, ty, TURRET_R + 2, 0, 3, P, 48), { tone: "mid" }));
  out.push(S(k.cylinder(tx, ty, TURRET_R - 2, 3, 6, P, 44), { tone: "hi" }));
  out.push(L(k.knurl(tx, ty, TURRET_R - 2, 3, 9, 36, P), { tone: "lo" }));

  out.push(S(k.cylinder(tx, ty, 6, 9, 16, P, 24), { tone: "mid" }));
  out.push(D([at([tx, ty, 25])], { size: 0.6, tone: "hi" }));

  const st1 = { x: tx - 6, y: ty - TURRET_R + 3, w: 12, d: 8, r: 1.2 };
  out.push(S(k.slabOf(st1, 9, 7, P, 3, 0.4), { tone: "mid" }));

  const st2 = { x: tx + TURRET_R - 11, y: ty - 5, w: 8, d: 10, r: 1.2 };
  out.push(S(k.slabOf(st2, 9, 10, P, 3, 0.5), { tone: "hi" }));
  out.push(L(k.sideArc(tx + TURRET_R - 7, ty, 3, 14, P, 16), { tone: "faint" }));

  const st3 = { x: tx - TURRET_R + 3, y: ty - 5, w: 8, d: 10, r: 1.2 };
  out.push(S(k.slabOf(st3, 9, 6, P, 3, 0.4), { tone: "mid" }));

  return out.join("");
}

// 5. Sliding Tool Carriage
function toolCarriage() {
  const [tx, ty] = TURRET;
  const out = [];

  const rail = { x: tx - 9, y: ty + 12, w: 18, d: 28, r: 1.5 };
  out.push(S(k.slabOf(rail, 0, 3, P, 3, 0.4), { tone: "mid" }));
  out.push(L(k.lineOnTop([rail.x + 3, rail.y], [rail.x + 3, rail.y + rail.d], 3, P), { tone: "faint" }));
  out.push(L(k.lineOnTop([rail.x + rail.w - 3, rail.y], [rail.x + rail.w - 3, rail.y + rail.d], 3, P), { tone: "faint" }));

  const car = { x: tx - 7, y: ty + 16, w: 14, d: 14, r: 1.5 };
  out.push(S(k.slabOf(car, 3, 6, P, 4, 0.5), { tone: "hi" }));
  out.push(D([at([car.x + 3, car.y + 3, 9]), at([car.x + car.w - 3, car.y + car.d - 3, 9])], { size: 0.4, tone: "mid" }));

  out.push(S(k.cylinder(tx, ty + 23, 2.4, 5, 2, P, 16), { tone: "mid" }));
  out.push(S(k.cylinder(tx, ty + 30, 1.4, 5.5, 1, P, 16), { tone: "hi", lit: true }));

  return out.join("");
}

// 6. Token Budget Dial Indicator
function dialGauge() {
  const [dx, dy] = DIAL;
  const out = [];

  const stand = { x: dx - 12, y: dy - 12, w: 24, d: 24, r: 3 };
  out.push(S(k.slabOf(stand, 0, 5, P, 4, 0.8), { tone: "mid" }));
  out.push(D(k.corners(stand, 4).map(([x, y]) => at([x, y, 5])), { size: 0.4, tone: "mid" }));

  out.push(S(k.cylinder(dx, dy, 4.2, 5, DIAL_Z - 5, P, 24), { tone: "mid", crease: "none" }));
  out.push(L(k.sideArc(dx, dy, 4.2, 35, P, 16), { tone: "faint" }));
  out.push(L(k.sideArc(dx, dy, 4.2, 55, P, 16), { tone: "faint" }));

  out.push(S(k.cylinder(dx, dy, DIAL_R, DIAL_Z, DIAL_H, P, 56, 0.8), { tone: "hi", crease: "lo" }));
  out.push(L(k.knurl(dx, dy, DIAL_R, DIAL_TOP - 2.8, DIAL_TOP, 72, P, 0.3), { tone: "lo" }));
  out.push(L(k.sideArc(dx, dy, DIAL_R, DIAL_TOP - 2.8, P, 36), { tone: "faint" }));

  out.push(L(k.ring(dx, dy, DIAL_R - 2, DIAL_TOP, P, 56), { tone: "lo" }));
  const ticks = k.radialTicks(dx, dy, DIAL_R - 2.8, 40, 5, DIAL_TOP, [1.2, 2.4], P);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  out.push(L(k.ring(dx, dy, 1.8, DIAL_TOP, P, 16), { tone: "mid" }));

  out.push(k.faceTextSvg(k.topMatrix([dx, dy + 7], DIAL_TOP, P, "x"), "TOKENS", { size: 2.2, tone: "lo", anchor: "middle" }));

  return out.join("");
}

const needleAt = (ratio) => {
  const angle = ratio * Math.PI * 1.6 + 0.2;
  const [dx, dy] = DIAL;
  const tipR = DIAL_R - 3.8;
  return k.lineOnTop(
    [dx - Math.sin(angle) * 2.8, dy + Math.cos(angle) * 2.8],
    [dx + Math.sin(angle) * tipR, dy - Math.cos(angle) * tipR],
    DIAL_TOP + 0.3,
    P
  );
};

// 7. Controls
function controls() {
  const out = [];
  const kx = 112, ky = 22;
  out.push(S(k.cylinder(kx, ky, 6.5, 0, 5.5, P, 28), { tone: "mid" }));
  out.push(L(k.knurl(kx, ky, 6.5, 0, 5.5, 32, P), { tone: "lo" }));
  out.push(L(k.lineOnTop([kx, ky], [kx + 6.5, ky], 5.5, P), { tone: "lit" }));
  out.push(D([at([kx, ky, 5.5])], { size: 0.5, tone: "hi" }));

  const jx = 112, jy = 42;
  out.push(S(k.cylinder(jx, jy, 4, 0, 2.5, P, 20), { tone: "mid" }));
  out.push(S(k.cylinder(jx, jy, 2.6, 2.5, 1.8, P, 16), { tone: "hi", lit: true }));

  return out.join("");
}

// Assemble SVG
const svg = k.figureSvg({
  width: WIDTH,
  height: HEIGHT,
  label:
    "The Nexus Chrono-Dispatcher Console. An append-only tape spool records monotonic events in durable SQLite storage. An elevated quartz reducer prism projects clean views to four client surfaces. A Geneva turret indexes through the execution turn cycle. A dial indicator measures context window token consumption.",
  body: [
    basePlate(),
    chronoSpool(),
    opticalPrism(),
    genevaTurret(),
    `<g data-part="tool-carriage" transform="${k.translateAlong(P, [0, 0, 0])}">${toolCarriage()}</g>`,
    dialGauge(),
    `<path data-part="needle" class="iso-line" data-tone="lit" data-free="start" d="${needleAt(0.24)}" style="stroke-width:1.1"/>`,
    controls(),
  ],
});

// Build The Master Technical Publication
const fullDocumentBody = `
<!-- Top Bar Header -->
<header class="nx-header">
  <div class="nx-brand">
    <span class="nx-logo">⬡</span>
    <span class="nx-brand-title">NEXUS</span>
    <span class="nx-brand-badge">COMPREHENSIVE ARCHITECTURE & FSM MANUAL</span>
  </div>
  <nav class="nx-nav">
    <a href="#console">The Console</a>
    <a href="#state-machine">State Machine</a>
    <a href="#layers">8 Layers</a>
    <a href="#contracts">5 Contracts</a>
    <a href="#flowcharts">Execution Flowcharts</a>
    <a href="#subsystems">Subsystems</a>
    <a href="#cli">CLI Reference</a>
  </nav>
</header>

<!-- Hero Section -->
<section class="nx-hero">
  <div class="nx-eyebrow">THE ARCHITECTURE OF A PROVIDER-AGNOSTIC AGENT HARNESS</div>
  <h1 class="nx-title">Understanding Nexus: From High-Level Vision to Low-Level Invariants</h1>
  <p class="nx-lead">
    Nexus is a provider-agnostic Python agent harness engineered around absolute context transparency.
    A single per-workspace daemon owns sessions and turns, feeding four presentation clients
    through an append-only SQLite event spine, a pure event reducer, and a strictly validated
    two-tier state machine.
  </p>
  <div class="nx-quote">
    <strong>The Core Invariant:</strong>
    "Show everything that matters (every parameter, every output), labelled and readable,
    never a raw JSON dump, and never hide information from the user that the agent can see."
  </div>
</section>

<!-- SECTION 01: THE ISOMETRIC ANATOMY CONSOLE -->
<section id="console" class="nx-section">
  <div class="nx-section-header">
    <span class="nx-sec-num">01</span>
    <h2>The Physical Embodiment</h2>
    <p>In the Anatomy methodology, <em>the object is the explanation</em>. Nexus is realized here as a precision physical instrument: <strong>The Chrono-Dispatcher Console (Model 1)</strong>.</p>
  </div>

  <div class="nx-figure-wrap">
    ${k.plateHtml({
      fig: "Fig 1",
      title: "THE NEXUS CHRONO-DISPATCHER CONSOLE",
      hint: "Click console or use Left/Right arrows to cycle execution stages",
      readout: "IDLE · SEQ 104 · 12.4k TOKENS",
      keys: [
        { mark: "lit", label: "Pure Reducer Prism (view/reduce.py)" },
        { mark: "raised", label: "Append-Only Chrono-Spine (SQLite seq log)" },
        { mark: "flat", label: "Geneva Protocol Turret (core/loop.py)" },
      ],
      caption:
        "A precision mechanical embodiment of the Nexus architecture: an append-only event reel guarantees deterministic replay, an optical beam-splitter prism drives four client surfaces without local state divergence, and a 4-station Geneva turret executes turns across protocol-isolated boundaries.",
      body: svg,
    })}

    <!-- Interactive Execution Phase Control Deck -->
    <div class="nx-controls-deck">
      <div class="nx-phase-pills" id="phasePills">
        <button class="nx-pill active" data-phase="0">1. IDLE</button>
        <button class="nx-pill" data-phase="1">2. ASSEMBLE</button>
        <button class="nx-pill" data-phase="2">3. STREAM</button>
        <button class="nx-pill" data-phase="3">4. GATE</button>
        <button class="nx-pill" data-phase="4">5. DISPATCH</button>
        <button class="nx-pill" data-phase="5">6. REDUCE</button>
      </div>
      <div class="nx-nav-buttons">
        <button id="btnPrev" class="nx-btn">◀ Previous</button>
        <button id="btnNext" class="nx-btn nx-btn-primary">Next Step ▶</button>
        <button id="btnAuto" class="nx-btn">Auto Tour</button>
      </div>
    </div>

    <!-- Live Mechanism Inspector -->
    <div class="nx-inspector" id="inspectorDeck">
      <div class="nx-inspect-col">
        <div class="nx-col-label">PHYSICAL MECHANISM ACTION</div>
        <div class="nx-col-body" id="inspectPhysical">
          The Geneva turret rests at Station 1. The Chrono-Spine reel is locked with ratchet pawl engaged. Token dial needle rests at base standby mark.
        </div>
      </div>
      <div class="nx-inspect-col">
        <div class="nx-col-label">SOFTWARE SUBSYSTEM EXECUTION</div>
        <div class="nx-col-body" id="inspectSoftware">
          <strong>Workspace Daemon (UDS Loopback):</strong> Listening on <code>~/.nexus/nexus.sock</code>. Supervisor holds per-session FIFO queue. No active turn running.
        </div>
      </div>
    </div>
  </div>

  <!-- The Component Rosetta Stone (Grid) -->
  <div class="nx-grid" style="margin-top: 24px;">
    <div class="nx-card">
      <div class="nx-card-badge">APPEND-ONLY STORE</div>
      <h3>The Chrono-Spine Reel</h3>
      <div class="nx-code-file">nexus/events.py · ~/.nexus/nexus.db</div>
      <p>A precision metallic tape reel that can only wind forward, stamped with an incrementing sequence counter (<code>seq: 001</code>, <code>002</code>...). Every turn event is appended to a shared SQLite database in WAL mode with <code>BEGIN IMMEDIATE</code>. The log is never mutated; session replay simply rolls through the recorded tape.</p>
    </div>

    <div class="nx-card">
      <div class="nx-card-badge">PURE REDUCER</div>
      <h3>The Quartz Reducer Prism</h3>
      <div class="nx-code-file">nexus/view/reduce.py → ConversationView</div>
      <p>A hexagonal optical quartz prism mounted above the tape. As raw embossed event tape passes underneath, it refracts four distortion-free optical beams to four client viewing ports (Ratatui TUI, GPUI Desktop, CLI stream, Web UI). The prism has zero internal memory, ensuring 100% deterministic projection.</p>
    </div>

    <div class="nx-card">
      <div class="nx-card-badge">PROTOCOL LOOP</div>
      <h3>The Geneva Turn Turret</h3>
      <div class="nx-code-file">nexus/core/loop.py</div>
      <p>A 4-position Geneva indexing turret that steps through turn execution. Crucially, <code>core/loop.py</code> knows <em>only protocols</em>. It imports zero concrete managers, decoupling execution logic from specific storage, providers, or tool dispatchers.</p>
    </div>

    <div class="nx-card">
      <div class="nx-card-badge">CONTEXT & BUDGET</div>
      <h3>Context Caliper & Dial Gauge</h3>
      <div class="nx-code-file">nexus/context/ · Token Indicator</div>
      <p>Station 1's precision caliper measures context history, compacts old turns, and sets the token boundary. The elevated dial gauge measures token window consumption (e.g. 34.2k / 200k) and monitors caching breaklines.</p>
    </div>

    <div class="nx-card">
      <div class="nx-card-badge">MULTI-MODEL HEAD</div>
      <h3>Provider Resonator Chamber</h3>
      <div class="nx-code-file">nexus/model/ (Claude, GPT, Gemini, Ollama)</div>
      <p>Station 2 houses swappable crystal heads for Anthropic, OpenAI, Gemini, and Ollama. It resonates with streaming <code>TextDelta</code> and opaque <code>ThinkingDelta</code> chunks, parsing partial JSON streams via <code>ToolCallAccumulator</code>.</p>
    </div>

    <div class="nx-card">
      <div class="nx-card-badge">TOOL DISPATCH & SAFETY</div>
      <h3>Security Gate & Tool Actuators</h3>
      <div class="nx-code-file">nexus/tools/ · nexus/tools/permissions.py</div>
      <p>Station 3's mechanical safety shutter intercepts tool calls, verifying path containment and human approval policies. Once cleared, Station 4's sliding carriage extends an actuator probe to execute bash jobs, file edits, grep searches, or subagent worktrees.</p>
    </div>
  </div>
</section>

<!-- SECTION 02: THE STATE MACHINE FORMALISM & INTERACTIVE SIMULATOR -->
<section id="state-machine" class="nx-section">
  <div class="nx-section-header">
    <span class="nx-sec-num">02</span>
    <h2>The Nexus State Machine Architecture</h2>
    <p>Nexus operates a strictly validated dual-layer Finite State Machine (FSM): <strong>The Session View FSM</strong> (<code>nexus/view/reduce.py</code>) and <strong>The Turn Execution FSM</strong> (<code>nexus/core/turn.py</code>). Both are synchronized with the Chrono-Dispatcher console.</p>
  </div>

  <div class="nx-fsm-container">
    <div class="nx-fsm-diagram">
      <div class="nx-fsm-header-bar">
        <span class="nx-fsm-title">INTERACTIVE STATE TRANSITION GRAPH</span>
        <span class="nx-fsm-status-badge" id="fsmCurrentBadge">ACTIVE: IDLE</span>
      </div>

      <!-- Graph Stage Layout -->
      <div class="nx-fsm-nodes-layout">
        <!-- Session Level Top Row -->
        <div class="nx-fsm-row">
          <div class="nx-fsm-node active" id="node-idle" data-fsm="idle">
            <div class="nx-fsm-node-type">SESSION STATE</div>
            <div class="nx-fsm-node-name">IDLE</div>
            <div class="nx-fsm-node-desc">Waiting on socket</div>
          </div>
          <div class="nx-fsm-arrow">── start_turn ──►</div>
          <div class="nx-fsm-node" id="node-running" data-fsm="running">
            <div class="nx-fsm-node-type">SESSION STATE</div>
            <div class="nx-fsm-node-name">RUNNING (NEW)</div>
            <div class="nx-fsm-node-desc">Turn initialized</div>
          </div>
          <div class="nx-fsm-arrow">── assemble ──►</div>
          <div class="nx-fsm-node" id="node-awaiting-model" data-fsm="awaiting_model">
            <div class="nx-fsm-node-type">TURN PHASE</div>
            <div class="nx-fsm-node-name">AWAITING_MODEL</div>
            <div class="nx-fsm-node-desc">Streaming LLM IR</div>
          </div>
        </div>

        <!-- Middle Execution / Gating Row -->
        <div class="nx-fsm-row">
          <div class="nx-fsm-node" id="node-awaiting-tools" data-fsm="awaiting_tools">
            <div class="nx-fsm-node-type">TURN PHASE</div>
            <div class="nx-fsm-node-name">AWAITING_TOOLS</div>
            <div class="nx-fsm-node-desc">Batch dispatching</div>
          </div>
          <div class="nx-fsm-arrow">◄── tool.requested ──</div>
          <div class="nx-fsm-node" id="node-awaiting-permission" data-fsm="awaiting_permission">
            <div class="nx-fsm-node-type">GATE PHASE</div>
            <div class="nx-fsm-node-name">AWAITING_PERMISSION</div>
            <div class="nx-fsm-node-desc">User approval barrier</div>
          </div>
          <div class="nx-fsm-arrow">── resolve ──►</div>
          <div class="nx-fsm-node" id="node-reduce" data-fsm="reduce">
            <div class="nx-fsm-node-type">REDUCER SPUR</div>
            <div class="nx-fsm-node-name">REDUCE / REPLAY</div>
            <div class="nx-fsm-node-desc">SQLite seq append</div>
          </div>
        </div>

        <!-- Terminal States Row -->
        <div class="nx-fsm-row nx-fsm-terminals">
          <div class="nx-fsm-node nx-node-terminal nx-node-success" id="node-completed" data-fsm="completed">
            <div class="nx-fsm-node-type">TERMINAL STATE</div>
            <div class="nx-fsm-node-name">COMPLETED</div>
            <div class="nx-fsm-node-desc">end_turn / tool_done</div>
          </div>
          <div class="nx-fsm-node nx-node-terminal nx-node-warn" id="node-cancelled" data-fsm="cancelled">
            <div class="nx-fsm-node-type">TERMINAL STATE</div>
            <div class="nx-fsm-node-name">CANCELLED</div>
            <div class="nx-fsm-node-desc">Cooperative cancel token</div>
          </div>
          <div class="nx-fsm-node nx-node-terminal nx-node-danger" id="node-failed" data-fsm="failed">
            <div class="nx-fsm-node-type">TERMINAL STATE</div>
            <div class="nx-fsm-node-name">FAILED</div>
            <div class="nx-fsm-node-desc">Harness error / trip</div>
          </div>
        </div>
      </div>

      <!-- Simulator Interactive Triggers -->
      <div class="nx-fsm-triggers">
        <div class="nx-trigger-label">SIMULATOR TRANSITION TRIGGERS (CLICK TO TEST TRANSITIONS):</div>
        <div class="nx-trigger-buttons">
          <button class="nx-btn" id="fsmTrigStart">1. start_turn</button>
          <button class="nx-btn" id="fsmTrigStream">2. model.started</button>
          <button class="nx-btn" id="fsmTrigNoTool">3. model.stopped (No Tools)</button>
          <button class="nx-btn" id="fsmTrigToolReq">4. tool.requested</button>
          <button class="nx-btn" id="fsmTrigAskPerm">5. permission.requested (Ask)</button>
          <button class="nx-btn" id="fsmTrigResolvePerm">6. permission.resolved (Approve)</button>
          <button class="nx-btn" id="fsmTrigToolDone">7. tool.result (Next Iteration)</button>
          <button class="nx-btn nx-btn-warn" id="fsmTrigCancel">Cancel Token</button>
          <button class="nx-btn nx-btn-danger" id="fsmTrigError">Harness Error</button>
          <button class="nx-btn" id="fsmTrigReset">Reset to IDLE</button>
        </div>
      </div>

      <!-- FSM Formal Invariant Inspector -->
      <div class="nx-fsm-inspector-box">
        <div class="nx-fsm-metric">
          <span class="nx-metric-lbl">CURRENT STATE</span>
          <span class="nx-metric-val" id="fsmStateName">IDLE</span>
        </div>
        <div class="nx-fsm-metric">
          <span class="nx-metric-lbl">LEGAL TRANSITIONS (_ALLOWED_TRANSITIONS)</span>
          <span class="nx-metric-val" id="fsmLegalTrans">awaiting_model, failed, cancelled</span>
        </div>
        <div class="nx-fsm-metric">
          <span class="nx-metric-lbl">TERMINAL RESURRECTION INVARIANT</span>
          <span class="nx-metric-val" id="fsmTerminalRule">ALLOWED (Active Phase)</span>
        </div>
        <div class="nx-fsm-metric">
          <span class="nx-metric-lbl">DURABLE SQLite EVENT</span>
          <span class="nx-metric-val" id="fsmSqliteEvent">None (Awaiting Client)</span>
        </div>
      </div>
    </div>

    <!-- The Mathematical Specification Table -->
    <div class="nx-fsm-specs">
      <h3>Mathematical Formalism: TurnState Machine</h3>
      <div class="nx-code-block">
        <code>
# From nexus/core/turn.py: Strict Transition Invariants
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "new":            frozenset({"awaiting_model", "failed", "cancelled"}),
    "awaiting_model": frozenset({"awaiting_tools", "completed", "failed", "cancelled"}),
    "awaiting_tools": frozenset({"awaiting_model", "completed", "failed", "cancelled"}),
    "completed":      frozenset(),  # Terminal: frozen
    "failed":         frozenset(),  # Terminal: frozen
    "cancelled":      frozenset(),  # Terminal: frozen
}
        </code>
      </div>
      <div class="nx-specs-text">
        <h4>Critical State Machine Invariants:</h4>
        <ol>
          <li><strong>Terminal Immutability:</strong> Once a turn reaches <code>completed</code>, <code>failed</code>, or <code>cancelled</code>, it is mathematically impossible to transition. Any attempt raises a <code>TurnStateError</code>.</li>
          <li><strong>Assistant Message Persisted First:</strong> When transitioning from <code>awaiting_model</code> to <code>awaiting_tools</code>, the assistant message is persisted to SQLite <em>before</em> any tool handling occurs. A crash leaves a dangling <code>ToolUse</code> which resume safely handles.</li>
          <li><strong>Atomic Batch Permission:</strong> If a model produces 5 tool calls, the state moves to <code>awaiting_permission</code> for the entire batch. A denial cancels the whole batch, preventing partial execution bugs.</li>
          <li><strong>Manifest Pinning:</strong> The loop pins exactly one extension generation per iteration. A tool written mid-iteration is only callable in iteration $N+1$.</li>
        </ol>
      </div>
    </div>

    <!-- Formal State Machine Diagrams rendered via Pele -->
    <div class="nx-flow-card" style="margin-top: 24px;">
      <div class="nx-flow-badge">STATE MACHINE 2.1 · CONVERSATION VIEW FSM</div>
      <h3>Session-Level Lifecycle State Machine</h3>
      <p>Reduced by <code>nexus/view/reduce.py</code> into immutable <code>ConversationView</code> states across multiple turn leases.</p>
      <div class="nx-diagram-canvas">
        <pre class="mermaid">
stateDiagram-v2
    [*] --> idle: session.created
    idle --> running: start_turn (input consumed)
    running --> awaiting_permission: permission.requested (sensitive tool)
    awaiting_permission --> running: permission.resolved (approved/denied)
    running --> idle: turn.completed / turn.failed / turn.cancelled
    idle --> awaiting_input: queue.input
    awaiting_input --> running: start_turn (dequeued)
    idle --> closed: session.closed
    running --> closed: session.closed
    closed --> [*]
        </pre>
      </div>
    </div>

    <div class="nx-flow-card" style="margin-top: 24px;">
      <div class="nx-flow-badge">STATE MACHINE 2.2 · TURN EXECUTION FSM</div>
      <h3>TurnState Iteration Machine</h3>
      <p>Governs step-by-step iteration in <code>nexus/core/turn.py</code> with strict transition invariants and terminal freeze.</p>
      <div class="nx-diagram-canvas">
        <pre class="mermaid">
stateDiagram-v2
    [*] --> new: TurnState.create()
    
    new --> awaiting_model: begin_iteration(0)
    new --> failed: setup error
    new --> cancelled: cancel token tripped
    
    awaiting_model --> awaiting_tools: model streams tool_use calls
    awaiting_model --> completed: model emits end_turn (no tools)
    awaiting_model --> failed: provider exception / overload timeout
    awaiting_model --> cancelled: cancel token tripped
    
    awaiting_tools --> awaiting_model: begin_iteration(N+1) [results persisted]
    awaiting_tools --> completed: limits exceeded (budget / max_iterations)
    awaiting_tools --> failed: unrecoverable tool dispatch failure
    awaiting_tools --> cancelled: cancel token tripped
    
    completed --> [*]: Terminal (Lease Released)
    failed --> [*]: Terminal (Lease Released)
    cancelled --> [*]: Terminal (Lease Released)
        </pre>
      </div>
    </div>
  </div>
</section>

<!-- SECTION 03: STRICT ARCHITECTURAL LAYERS (HIGH TO LOW) -->
<section id="layers" class="nx-section">
  <div class="nx-section-header">
    <span class="nx-sec-num">03</span>
    <h2>Strict One-Way Architectural Layering (L0 to L5)</h2>
    <p>Nexus enforces strict downward-only import rules verified by automated test suites (<code>tests/test_layering.py</code> and <code>tests/test_ui_layering.py</code>). A lower layer never imports a higher one.</p>
  </div>

  <div class="nx-layers-box">
    <div class="nx-layer-row">
      <span class="nx-lvl">L5</span>
      <div class="nx-layer-info">
        <strong>UI Surfaces Layer</strong> (<code>nexus/ui/</code>, <code>nexus/ui_support/</code>, <code>nexus/client/</code>)
        <p>Presentation only: Native Ratatui TUI, Rust GPUI Desktop, One-shot CLI & JSONL stream, Web app. Prohibited from touching database files or importing managers directly.</p>
      </div>
    </div>
    <div class="nx-layer-row">
      <span class="nx-lvl">L4½</span>
      <div class="nx-layer-info">
        <strong>Host Subsystem</strong> (<code>nexus/host/</code>, <code>nexus/host_support/</code>, <code>nexus/observability/</code>)
        <p>The only public surface API: <code>HostFacade</code>, <code>Supervisor</code> (turn FIFO queue & limits), <code>Presence</code> (first-responder approvals), and UDS Daemon.</p>
      </div>
    </div>
    <div class="nx-layer-row">
      <span class="nx-lvl">L4</span>
      <div class="nx-layer-info">
        <strong>Runtime Composition Root</strong> (<code>nexus/runtime.py</code>)
        <p>Instantiates and coordinates all concrete managers, providers, routers, and extension hot-reloading.</p>
      </div>
    </div>
    <div class="nx-layer-row">
      <span class="nx-lvl">L3</span>
      <div class="nx-layer-info">
        <strong>Domain Managers</strong> (<code>session/</code>, <code>context/</code>, <code>tools/</code>, <code>skills/</code>, <code>mcp/</code>, <code>agents/</code>, <code>hooks/</code>, <code>voice/</code>)
        <p>Concrete implementations of domain capabilities: session storage, context budgeting, sandboxed tool execution, skill resolution, and MCP clients.</p>
      </div>
    </div>
    <div class="nx-layer-row">
      <span class="nx-lvl">L2</span>
      <div class="nx-layer-info">
        <strong>Core Execution Loop</strong> (<code>nexus/core/loop.py</code>, <code>turn.py</code>, <code>bus.py</code>, <code>cancel.py</code>)
        <p>Protocol-isolated turn loop. Imports ZERO concrete managers; interacts strictly via protocol interfaces.</p>
      </div>
    </div>
    <div class="nx-layer-row">
      <span class="nx-lvl">L1</span>
      <div class="nx-layer-info">
        <strong>Model System</strong> (<code>nexus/model/</code>)
        <p>Message IR, Provider Protocol, Streaming Event parser, ModelRouter, Tiers, and adapters (Anthropic, OpenAI, Gemini, Ollama, DeepSeek).</p>
      </div>
    </div>
    <div class="nx-layer-row">
      <span class="nx-lvl">L0½</span>
      <div class="nx-layer-info">
        <strong>Pure View Reducer</strong> (<code>nexus/view/reduce.py</code>, <code>nexus/view/model.py</code>)
        <p>Imports only <code>events.py</code>. Folds the monotonic event stream into an immutable <code>ConversationView</code> without I/O or side-effects.</p>
      </div>
    </div>
    <div class="nx-layer-row">
      <span class="nx-lvl">L0</span>
      <div class="nx-layer-info">
        <strong>Primitives</strong> (<code>config/</code>, <code>errors.py</code>, <code>events.py</code>, <code>util.py</code>)
        <p>Immutable event envelopes, path configuration, error hierarchies, and core utilities.</p>
      </div>
    </div>
  </div>
</section>

<!-- SECTION 04: THE FIVE CORE CONTRACTS -->
<section id="contracts" class="nx-section">
  <div class="nx-section-header">
    <span class="nx-sec-num">04</span>
    <h2>The Five Core Contracts</h2>
    <p>Five explicit, frozen contracts govern all data boundaries across the entire system.</p>
  </div>

  <div class="nx-contracts-table">
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Contract</th>
          <th>Location</th>
          <th>Essence & Key Invariants</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><strong>1</strong></td>
          <td><strong>Message IR</strong></td>
          <td><code>model/message.py</code></td>
          <td><code>Text</code>, <code>Thinking(signature)</code>, <code>ToolUse</code>, <code>ToolResult</code>, <code>Image</code>, <code>Document</code>. Role is strictly <code>user | assistant</code> (no system role in IR; context manager injects system text into provider adapters). <code>Thinking.signature</code> is opaque and replayed verbatim.</td>
        </tr>
        <tr>
          <td><strong>2</strong></td>
          <td><strong>Provider Protocol</strong></td>
          <td><code>model/provider.py</code></td>
          <td><code>name</code>, <code>capabilities(model)</code>, <code>stream(req)</code>, <code>count_tokens(req)</code>, <code>aclose()</code>. The core loop inspects capabilities dynamically and adapts without hardcoding.</td>
        </tr>
        <tr>
          <td><strong>3</strong></td>
          <td><strong>Stream Events</strong></td>
          <td><code>model/stream.py</code></td>
          <td><code>MessageStart</code>, <code>TextDelta</code>, <code>ThinkingDelta</code>, <code>ThinkingEnd</code>, <code>ToolCallStart/Delta/End</code>, <code>Usage</code>, <code>MessageStop</code>. <code>ToolCallAccumulator</code> stitches partial JSON fragments.</td>
        </tr>
        <tr>
          <td><strong>4</strong></td>
          <td><strong>Tool Contract</strong></td>
          <td><code>tools/spec.py</code></td>
          <td><code>ToolSpec</code> (declaration schema + harness fields) and <code>ToolExecutionResult</code>. <code>ToolContext</code> intentionally lacks access to the <code>Runtime</code> to preserve sandbox isolation.</td>
        </tr>
        <tr>
          <td><strong>5</strong></td>
          <td><strong>Event Envelope</strong></td>
          <td><code>events.py</code></td>
          <td><code>Event(type, data, seq, ts, session, turn, id)</code>. Monotonic <code>seq</code> per session is the append-only spine. Everything reduces from this.</td>
        </tr>
      </tbody>
    </table>
  </div>
</section>

<!-- SECTION 05: DETAILED END-TO-END EXECUTION FLOWCHARTS -->
<section id="flowcharts" class="nx-section">
  <div class="nx-section-header">
    <span class="nx-sec-num">05</span>
    <h2>Detailed End-to-End Execution Flowcharts</h2>
    <p>Follow the exact lifecycle of an instruction through all subsystems, from user keystroke to terminal render. Rendered dynamically as theme-adaptive vector SVGs with interactive pan/zoom via Pele.</p>
  </div>

  <!-- Flowchart 5.1: Request Ingress & Supervision -->
  <div class="nx-flow-card">
    <div class="nx-flow-badge">FLOWCHART 5.1 · INGRESS & HOST SUPERVISION</div>
    <h3>Client Ingress to Turn Enqueue</h3>
    <p>Traces command delivery from client keystroke across loopback Unix Domain Sockets into host daemon supervisor queues.</p>
    <div class="nx-diagram-canvas">
      <pre class="mermaid">
flowchart TD
    A["User Keystroke in nexus chat / desktop"] --> B["Unix Domain Socket (~/.nexus/nexus.sock)"]
    B --> C["nexus/host/daemon.py (Loopback Listener)"]
    C --> D["nexus/host/facade.py: HostFacade"]
    D --> E{"Supervisor: Global Turn Cap Exceeded?"}
    E -- Yes --> F["Wait in Per-Session FIFO Queue"]
    E -- No --> G["Grant Single Turn Lease to Session"]
    G --> H["core/loop.py: run_turn(session, protocols)"]
      </pre>
    </div>
  </div>

  <!-- Flowchart 5.2: The Core Loop Iteration Engine -->
  <div class="nx-flow-card" style="margin-top: 24px;">
    <div class="nx-flow-badge">FLOWCHART 5.2 · THE CORE LOOP</div>
    <h3>run_turn Step-by-Step Iteration Cycle</h3>
    <p>The core execution heartbeat: manifest pinning, context assembly, provider streaming, durable message persistence, batch gating, and tool dispatch.</p>
    <div class="nx-diagram-canvas">
      <pre class="mermaid">
flowchart TD
    A["Begin Iteration N"] --> B["Pin Extension Manifest Generation"]
    B --> C["Assemble Request Context (PreCompact Gate)"]
    C --> D["Resolve Model via ModelRouter"]
    D --> E["Stream Provider (Collect Text & Tool Calls)"]
    E --> F["DURABLE FIRST: Persist Assistant Message to SQLite"]
    F --> G{"Any Tool Calls Requested?"}
    G -- No --> H{"Steering Messages Present?"}
    H -- Yes --> A
    H -- No --> I["Complete Turn (stop_reason: end_turn)"]
    G -- Yes --> J["Evaluate Batch Permissions (PathGuard & Auto-Approve)"]
    J --> K{"All Tools Approved?"}
    K -- No / Ask --> L["Halt in awaiting_permission (User Prompt in UI)"]
    L --> K
    K -- Approved --> M["Dispatch Approved Tools (Concurrency Planner)"]
    M --> N["DURABLE: Persist ToolResult Blocks in Call Order"]
    N --> O["Release Manifest Pin & Advance Loop Iteration N+1"]
    O --> A
      </pre>
    </div>
  </div>

  <!-- Flowchart 5.3: Context Assembly & Budgeting -->
  <div class="nx-flow-card" style="margin-top: 24px;">
    <div class="nx-flow-badge">FLOWCHART 5.3 · CONTEXT ASSEMBLY</div>
    <h3>Budget Priority Cascade & Compaction</h3>
    <p>Strict deterministic priority hierarchy ensuring identity and tools never drop while history compacts within window limits.</p>
    <div class="nx-diagram-canvas">
      <pre class="mermaid">
flowchart TD
    A["Trigger Context Assembly"] --> B["Priority 0 (Mandatory): identity, soul, tools, user prompt"]
    B --> C{"Remaining Token Budget > 0?"}
    C -- Yes --> D["Priority 1: environment (git, platform), AGENTS.md, skills index, memory"]
    C -- No --> E["Halt Context Assembly (Priority 0 fits)"]
    D --> F{"Remaining Token Budget > 0?"}
    F -- Yes --> G["Priority 2: mcp_index (bounded), attachments"]
    F -- No --> E
    G --> H{"Remaining Token Budget > 0?"}
    H -- Yes --> I["Priority 3: history (Contiguous Suffix with Sliding Compaction)"]
    H -- No --> E
    I --> J["Calculate Semantic Cache Breakline (CacheBoundary)"]
    J --> K["Emit context.assembled Event"]
      </pre>
    </div>
  </div>

  <!-- Flowchart 5.4: Multi-Model Streaming & Capability Degradation -->
  <div class="nx-flow-card" style="margin-top: 24px;">
    <div class="nx-flow-badge">FLOWCHART 5.4 · PROVIDER STREAMING</div>
    <h3>Multi-Model Streaming & Capability Degradation</h3>
    <p>Capability inspection, graceful degradation policies, chunk parsing, delta accumulator stitching, and exponential backoff retry.</p>
    <div class="nx-diagram-canvas">
      <pre class="mermaid">
flowchart TD
    A["Request Dispatched to Provider Adapter"] --> B["Check Adapter Capabilities (tools, vision, reasoning)"]
    B --> C{"Capability Mismatch?"}
    C -- Yes --> D["Apply Degradation Policy (drop / to_text / error)"]
    C -- No --> E["Open HTTP/SSE Stream via HTTPTransport"]
    D --> E
    E --> F["Stream StreamEvents (TextDelta, ThinkingDelta, ToolCallDelta)"]
    F --> G["ToolCallAccumulator Stitches Partial JSON Fragments"]
    G --> H["Opaque Thinking Signatures Captured for Multi-Turn Replay"]
    H --> I{"Provider Overloaded / 5xx?"}
    I -- Yes --> J["Cooperative Jittered Retry (2s, 4s, 8s backoff)"]
    J --> E
    I -- No --> K["Emit model.stopped Event with Usage Metrics"]
      </pre>
    </div>
  </div>

  <!-- Flowchart 5.5: Gating & Tool Execution -->
  <div class="nx-flow-card" style="margin-top: 24px;">
    <div class="nx-flow-badge">FLOWCHART 5.5 · SECURITY & TOOL EXECUTION</div>
    <h3>Batch Permission Gating & Concurrency Planning</h3>
    <p>Two-tier security gating: path containment verification, atomic batch approvals, and parallel vs exclusive subprocess execution.</p>
    <div class="nx-diagram-canvas">
      <pre class="mermaid">
flowchart TD
    A["Batch of Tool Calls from Assistant Message"] --> B["PreToolUse Hooks Evaluated"]
    B --> C["PathGuard Validates Absolute Path Containment"]
    C --> D{"Path Escapes Workspace Boundary?"}
    D -- Yes --> E["Hard Deny (Violation Recorded in ToolResult)"]
    D -- No --> F{"Matches Auto-Approve Prefix / Rule?"}
    F -- Yes --> G["Mark Call as Auto-Approved"]
    F -- No --> H["Request Human Approval (ApprovalBroker)"]
    H --> I{"User Approves via Surface UI?"}
    I -- No --> J["Deny Batch Atomically (All Calls Cancelled)"]
    I -- Yes --> K["Mark Call as Approved"]
    G --> L["Concurrency Planner: Group Parallel vs Exclusive Tools"]
    K --> L
    L --> M["Run read-only tools concurrently; Run mutating tools exclusively"]
    M --> N["Subprocess Sandbox (Process Group SIGTERM/SIGKILL handles)"]
    N --> O["Capture Output into ToolExecutionResult (max 25k tokens capped)"]
      </pre>
    </div>
  </div>

  <!-- Flowchart 5.6: Durable Storage & Pure View Reduction Pipeline -->
  <div class="nx-flow-card" style="margin-top: 24px;">
    <div class="nx-flow-badge">FLOWCHART 5.6 · DURABILITY & REDUCTION</div>
    <h3>SQLite Persistence to Client Projection</h3>
    <p>The monotonic event sequence spine: WAL persistence, pure reducer fold, and zero-drift presentation projection to all 4 surfaces.</p>
    <div class="nx-diagram-canvas">
      <pre class="mermaid">
flowchart TD
    A["Event Generated in Core Loop"] --> B["Stamped with Monotonic seq Counter"]
    B --> C["SQLite Transaction: BEGIN IMMEDIATE in ~/.nexus/nexus.db"]
    C --> D["Write to records table (kind, ts, msgspec body)"]
    D --> E["Update completion_seq & last_seq in sessions table"]
    E --> F["Commit SQLite WAL Log (synchronous=FULL)"]
    F --> G["Pass Event into pure view/reduce.py: apply(view, event)"]
    G --> H["Produce New Immutable ConversationView"]
    H --> I["Schema-3 Section Delta & Transcript Suffix Projected to Ratatui TUI"]
    H --> J["Owned Presentation Snapshot Projected to GPUI Desktop"]
    H --> K["JSONL Stream Emitted to nexus run CLI"]
      </pre>
    </div>
  </div>
</section>

<!-- SECTION 06: SUBSYSTEMS & CAPABILITIES (HIGH TO LOW) -->
<section id="subsystems" class="nx-section">
  <div class="nx-section-header">
    <span class="nx-sec-num">06</span>
    <h2>Subsystems & Capabilities: Deep-Dive Details</h2>
    <p>Exhaustive breakdown of every major capability built into Nexus.</p>
  </div>

  <div class="nx-grid">
    <div class="nx-card">
      <div class="nx-card-badge">SUBSYSTEM 1</div>
      <h3>Multi-Model Routing & Tiers</h3>
      <p>Seamlessly routes requests across Anthropic Claude (3.7 Sonnet, Haiku), OpenAI (GPT-4o, o1, o3-mini), Google Gemini (2.0 Flash, 1.5 Pro), Ollama, and OpenRouter. Semantic tiers (<code>fast</code>, <code>balanced</code>, <code>reasoning</code>) decouple scripts from hardcoded model IDs. Supports opaque thinking signature capture and verbatim multi-turn replay.</p>
    </div>
    <div class="nx-card">
      <div class="nx-card-badge">SUBSYSTEM 2</div>
      <h3>Managed Tool Harness & Sandboxing</h3>
      <p>Built-in tools for file reading, writing, precise contiguous line replacements, ripgrep regex searching, git inspection, and sandboxed bash execution with interactive standard input piping. Long-running shell commands run as background tasks with <code>manage_task</code> handles.</p>
    </div>
    <div class="nx-card">
      <div class="nx-card-badge">SUBSYSTEM 3</div>
      <h3>Autonomous Subagents & Worktrees</h3>
      <p>Sessions can spawn child subagents (<code>invoke_subagent</code>) with dedicated sessions and isolated Git worktrees (<code>branch</code> or <code>share</code>), preventing parallel experiments from dirtying the main working tree. Inter-agent communication is handled via <code>send_message</code>.</p>
    </div>
    <div class="nx-card">
      <div class="nx-card-badge">SUBSYSTEM 4</div>
      <h3>Model Context Protocol (MCP)</h3>
      <p>First-class native MCP client support connecting to local stdio and remote SSE servers. Discovers external tools, prompts, and resources, exposing them directly into the agent's structured tool schema.</p>
    </div>
    <div class="nx-card">
      <div class="nx-card-badge">SUBSYSTEM 5</div>
      <h3>Skills & Hot-Reloading</h3>
      <p>Reusable markdown instructions and procedural playbooks under <code>.agents/skills/</code>. Editing skills, hooks, or <code>settings.toml</code> takes effect immediately in the next turn iteration without restarting the daemon.</p>
    </div>
    <div class="nx-card">
      <div class="nx-card-badge">SUBSYSTEM 6</div>
      <h3>Voice Dictation & Realtime Live Audio</h3>
      <p>Integrated local speech-to-text dictation via Whisper, alongside experimental full-duplex real-time bidirectional audio calls using OpenAI Realtime voice over WebSockets.</p>
    </div>
    <div class="nx-card">
      <div class="nx-card-badge">SUBSYSTEM 7</div>
      <h3>Durable Persistence & Replay Matrix</h3>
      <p>Shared SQLite database (<code>~/.nexus/nexus.db</code>) in WAL mode with <code>synchronous=FULL</code>. Writes in <code>BEGIN IMMEDIATE</code> prevent concurrency races across multiple workspace daemons. Crash recovery is deterministic because all views derive from the append-only event stream.</p>
    </div>
    <div class="nx-card">
      <div class="nx-card-badge">SUBSYSTEM 8</div>
      <h3>Multi-Surface Architecture</h3>
      <p>Four distinct surfaces connect to the same daemon: Native Ratatui TUI client (Rust presentation with schema-3 deltas), Rust GPUI desktop client (owned presentation snapshots), One-shot CLI & JSONL stream, and Web client.</p>
    </div>
  </div>
</section>

<!-- SECTION 07: CLI COMMANDS & OPERATIONAL REFERENCE -->
<section id="cli" class="nx-section">
  <div class="nx-section-header">
    <span class="nx-sec-num">07</span>
    <h2>CLI Commands & Operational Reference</h2>
    <p>Every terminal command available to developers and operators.</p>
  </div>

  <div class="nx-contracts-table">
    <table>
      <thead>
        <tr>
          <th>Command</th>
          <th>Surface / Mode</th>
          <th>Description & Usage</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><code>nexus chat</code></td>
          <td>Terminal UI (Ratatui)</td>
          <td>Launches the high-performance native terminal chat client. Requires a TTY. Connects to the workspace daemon automatically.</td>
        </tr>
        <tr>
          <td><code>nexus run "prompt"</code></td>
          <td>One-Shot CLI</td>
          <td>Executes a single prompt turn non-interactively and prints the output or streams JSONL events.</td>
        </tr>
        <tr>
          <td><code>nexus desktop</code></td>
          <td>Native Desktop App</td>
          <td>Launches the GPU-accelerated Rust GPUI desktop client receiving presentation snapshots from the daemon.</td>
        </tr>
        <tr>
          <td><code>nexus web</code></td>
          <td>Browser App</td>
          <td>Starts the plain HTML/CSS/JS web client over loopback WebSockets.</td>
        </tr>
        <tr>
          <td><code>nexus doctor</code></td>
          <td>Diagnostics</td>
          <td>Inspects workspace configuration, models, credentials, database health, and daemon socket connectivity.</td>
        </tr>
        <tr>
          <td><code>nexus daemon status|stop|restart|logs</code></td>
          <td>Daemon Management</td>
          <td>Inspects or manages the background workspace daemon process.</td>
        </tr>
      </tbody>
    </table>
  </div>
</section>

<!-- Footer -->
<footer class="nx-footer">
  <div class="nx-footer-inner">
    <div><strong>NEXUS</strong> — Context-Transparent Agent Harness</div>
    <div>Docs: <code>docs/architecture.md</code> · DB: <code>~/.nexus/nexus.db</code> · Socket: <code>~/.nexus/nexus.sock</code></div>
  </div>
</footer>
`;

// Additional styling for the complete document & Flowcharts
const DOCUMENT_CSS = `
/* Layout & Typography */
.nx-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px 0;
  border-bottom: 1px solid var(--anatomy-faint);
  margin-bottom: 48px;
}
.nx-brand {
  display: flex;
  align-items: center;
  gap: 10px;
}
.nx-logo {
  font-size: 22px;
  color: var(--anatomy-lit);
}
.nx-brand-title {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: .08em;
  color: var(--anatomy-strong);
}
.nx-brand-badge {
  font-size: 11px;
  font-family: var(--anatomy-mono);
  padding: 3px 8px;
  background: var(--anatomy-face);
  border: 1px solid var(--anatomy-lo);
  border-radius: 4px;
  color: var(--anatomy-mid);
}
.nx-nav {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
}
.nx-nav a {
  color: var(--anatomy-text);
  text-decoration: none;
  font-size: 13px;
  transition: color .15s ease;
}
.nx-nav a:hover {
  color: var(--anatomy-lit);
}

.nx-hero {
  margin-bottom: 64px;
}
.nx-eyebrow {
  font-family: var(--anatomy-mono);
  font-size: 12px;
  letter-spacing: .1em;
  color: var(--anatomy-mid);
  margin-bottom: 12px;
}
.nx-title {
  font-size: 38px;
  font-weight: 700;
  letter-spacing: -.02em;
  color: var(--anatomy-strong);
  margin: 0 0 16px 0;
}
.nx-lead {
  font-size: 17px;
  line-height: 1.6;
  color: var(--anatomy-text);
  max-width: 720px;
  margin: 0 0 24px 0;
}
.nx-quote {
  padding: 16px 20px;
  background: var(--anatomy-face);
  border-left: 3px solid var(--anatomy-lit);
  border-radius: 0 6px 6px 0;
  font-size: 14px;
  color: var(--anatomy-strong);
  line-height: 1.5;
}

.nx-section {
  margin-bottom: 80px;
}
.nx-section-header {
  margin-bottom: 28px;
}
.nx-sec-num {
  font-family: var(--anatomy-mono);
  font-size: 13px;
  color: var(--anatomy-mid);
  display: block;
  margin-bottom: 4px;
}
.nx-section-header h2 {
  font-size: 24px;
  font-weight: 600;
  color: var(--anatomy-strong);
  margin: 0 0 8px 0;
}
.nx-section-header p {
  color: var(--anatomy-text);
  font-size: 15px;
  margin: 0;
}

.nx-figure-wrap {
  background: var(--anatomy-card);
  border: 1px solid var(--anatomy-lo);
  border-radius: 8px;
  padding: 16px 20px 24px 20px;
}

.nx-controls-deck {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  padding: 16px 0;
  margin-top: 12px;
  border-top: 1px solid var(--anatomy-faint);
  border-bottom: 1px solid var(--anatomy-faint);
}
.nx-phase-pills {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.nx-pill {
  background: var(--anatomy-face);
  border: 1px solid var(--anatomy-lo);
  color: var(--anatomy-text);
  font-family: var(--anatomy-mono);
  font-size: 12px;
  padding: 6px 12px;
  border-radius: 4px;
  cursor: pointer;
  transition: all .15s ease;
}
.nx-pill:hover {
  background: var(--anatomy-shade-3);
  color: var(--anatomy-strong);
}
.nx-pill.active {
  background: var(--anatomy-lit-top);
  border-color: var(--anatomy-hi);
  color: var(--anatomy-lit);
  font-weight: 600;
}

.nx-nav-buttons {
  display: flex;
  gap: 8px;
}
.nx-btn {
  background: var(--anatomy-face);
  border: 1px solid var(--anatomy-lo);
  color: var(--anatomy-strong);
  font-size: 12px;
  padding: 6px 14px;
  border-radius: 4px;
  cursor: pointer;
  transition: all .15s ease;
}
.nx-btn:hover {
  background: var(--anatomy-shade-3);
  border-color: var(--anatomy-mid);
}
.nx-btn-primary {
  background: var(--anatomy-shade-3);
  border-color: var(--anatomy-hi);
  font-weight: 600;
}
.nx-btn-warn {
  border-color: #d97706;
  color: #fbbf24;
}
.nx-btn-danger {
  border-color: #b91c1c;
  color: #f87171;
}

.nx-inspector {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-top: 16px;
  padding: 16px;
  background: var(--anatomy-face);
  border: 1px solid var(--anatomy-faint);
  border-radius: 6px;
}
.nx-col-label {
  font-family: var(--anatomy-mono);
  font-size: 11px;
  color: var(--anatomy-mid);
  margin-bottom: 6px;
  letter-spacing: .06em;
}
.nx-col-body {
  font-size: 13px;
  line-height: 1.5;
  color: var(--anatomy-strong);
}
.nx-col-body code {
  font-family: var(--anatomy-mono);
  font-size: 12px;
  padding: 1px 4px;
  background: var(--anatomy-card);
  border-radius: 3px;
}

/* FSM Module Styles */
.nx-fsm-container {
  display: flex;
  flex-direction: column;
  gap: 24px;
}
.nx-fsm-diagram {
  background: var(--anatomy-card);
  border: 1px solid var(--anatomy-lo);
  border-radius: 8px;
  padding: 24px;
}
.nx-fsm-header-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--anatomy-faint);
}
.nx-fsm-title {
  font-family: var(--anatomy-mono);
  font-size: 12px;
  letter-spacing: .08em;
  color: var(--anatomy-strong);
}
.nx-fsm-status-badge {
  font-family: var(--anatomy-mono);
  font-size: 12px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: 4px;
  background: var(--anatomy-face);
  border: 1px solid var(--anatomy-hi);
  color: var(--anatomy-lit);
}
.nx-fsm-nodes-layout {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.nx-fsm-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.nx-fsm-node {
  background: var(--anatomy-face);
  border: 1px solid var(--anatomy-lo);
  border-radius: 6px;
  padding: 12px 16px;
  min-width: 140px;
  transition: all .2s ease;
  position: relative;
}
.nx-fsm-node.active {
  background: var(--anatomy-shade-3);
  border-color: var(--anatomy-lit);
  box-shadow: 0 0 12px rgba(255, 255, 255, 0.15);
}
.nx-fsm-node.active .nx-fsm-node-name {
  color: var(--anatomy-lit);
}
.nx-fsm-node-type {
  font-family: var(--anatomy-mono);
  font-size: 9px;
  letter-spacing: .06em;
  color: var(--anatomy-mid);
  margin-bottom: 4px;
}
.nx-fsm-node-name {
  font-size: 13px;
  font-weight: 700;
  color: var(--anatomy-strong);
  margin-bottom: 2px;
}
.nx-fsm-node-desc {
  font-size: 11px;
  color: var(--anatomy-muted);
}
.nx-fsm-arrow {
  font-family: var(--anatomy-mono);
  font-size: 10px;
  color: var(--anatomy-mid);
  white-space: nowrap;
}
.nx-fsm-terminals {
  margin-top: 8px;
  padding-top: 16px;
  border-top: 1px dashed var(--anatomy-faint);
}
.nx-node-terminal {
  border-style: dashed;
}
.nx-node-success.active {
  border-color: var(--anatomy-green);
  box-shadow: 0 0 12px rgba(107, 224, 160, 0.2);
}
.nx-node-warn.active {
  border-color: #f59e0b;
  box-shadow: 0 0 12px rgba(245, 158, 11, 0.2);
}
.nx-node-danger.active {
  border-color: var(--anatomy-red);
  box-shadow: 0 0 12px rgba(255, 107, 107, 0.2);
}

.nx-fsm-triggers {
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid var(--anatomy-faint);
}
.nx-trigger-label {
  font-family: var(--anatomy-mono);
  font-size: 11px;
  color: var(--anatomy-mid);
  margin-bottom: 10px;
  letter-spacing: .06em;
}
.nx-trigger-buttons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.nx-fsm-inspector-box {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-top: 20px;
  padding: 16px;
  background: var(--anatomy-face);
  border: 1px solid var(--anatomy-faint);
  border-radius: 6px;
}
.nx-fsm-metric {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.nx-metric-lbl {
  font-family: var(--anatomy-mono);
  font-size: 10px;
  color: var(--anatomy-mid);
  letter-spacing: .06em;
}
.nx-metric-val {
  font-family: var(--anatomy-mono);
  font-size: 12px;
  color: var(--anatomy-strong);
  word-break: break-all;
}

.nx-fsm-specs {
  background: var(--anatomy-card);
  border: 1px solid var(--anatomy-lo);
  border-radius: 8px;
  padding: 24px;
}
.nx-fsm-specs h3 {
  font-size: 17px;
  margin: 0 0 16px 0;
  color: var(--anatomy-strong);
}
.nx-code-block {
  background: var(--anatomy-face);
  border: 1px solid var(--anatomy-faint);
  border-radius: 6px;
  padding: 14px 18px;
  margin-bottom: 18px;
  overflow-x: auto;
}
.nx-code-block code {
  font-family: var(--anatomy-mono);
  font-size: 12px;
  line-height: 1.5;
  color: var(--anatomy-strong);
}
.nx-specs-text h4 {
  font-size: 14px;
  color: var(--anatomy-strong);
  margin: 0 0 10px 0;
}
.nx-specs-text ol {
  margin: 0;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--anatomy-text);
}
.nx-specs-text li {
  margin-bottom: 8px;
}

/* Flowchart & Diagram Cards */
.nx-flow-card {
  background: var(--anatomy-card);
  border: 1px solid var(--anatomy-lo);
  border-radius: 8px;
  padding: 24px;
}
.nx-flow-badge {
  font-family: var(--anatomy-mono);
  font-size: 10px;
  color: var(--anatomy-mid);
  letter-spacing: .08em;
  margin-bottom: 8px;
}
.nx-flow-card h3 {
  font-size: 18px;
  color: var(--anatomy-strong);
  margin: 0 0 8px 0;
}
.nx-flow-card p {
  font-size: 13px;
  color: var(--anatomy-text);
  margin: 0 0 16px 0;
}
.nx-diagram-canvas {
  position: relative;
  background: var(--anatomy-face);
  border: 1px solid var(--anatomy-faint);
  border-radius: 6px;
  padding: 20px;
  min-height: 180px;
  box-sizing: border-box;
  overflow: hidden;
  --pele-bg: var(--anatomy-card);
  --pele-fg: var(--anatomy-strong);
  --pele-muted: var(--anatomy-muted);
  --pele-line: var(--anatomy-mid);
  --pele-surface: var(--anatomy-card);
  --pele-surface-alt: var(--anatomy-shade-2);
  --pele-border: var(--anatomy-lo);
  --pele-accent: var(--anatomy-lit);
  --pele-radius: 6px;
  --pele-font: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --pele-font-mono: var(--anatomy-mono);
  --pele-error: var(--anatomy-red);
  --pele-zoom-top: 12px;
  --pele-zoom-right: 12px;
}
pre.mermaid {
  position: relative;
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 160px;
  width: 100%;
  margin: 0;
  padding: 8px;
  box-sizing: border-box;
  background: transparent;
  color: var(--anatomy-strong);
  font-family: var(--anatomy-mono);
  font-size: 12px;
  white-space: pre-wrap;
  overflow: hidden;
}
svg.pele {
  max-width: 100%;
  height: auto;
  border-radius: 4px;
}
.pele-zoom {
  --_bg: var(--anatomy-face) !important;
  --_fg: var(--anatomy-strong) !important;
  --_b: var(--anatomy-lo) !important;
  border-radius: 6px !important;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.4);
}
.pele-zoom button {
  transition: all .15s ease;
}
.pele-zoom button:hover:not([disabled]) {
  background: var(--anatomy-shade-3) !important;
  color: var(--anatomy-lit) !important;
}

.nx-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 20px;
}
.nx-card {
  background: var(--anatomy-card);
  border: 1px solid var(--anatomy-faint);
  border-radius: 6px;
  padding: 20px;
}
.nx-card-badge {
  font-family: var(--anatomy-mono);
  font-size: 10px;
  color: var(--anatomy-mid);
  letter-spacing: .08em;
  margin-bottom: 8px;
}
.nx-card h3 {
  font-size: 16px;
  color: var(--anatomy-strong);
  margin: 0 0 6px 0;
}
.nx-code-file {
  font-family: var(--anatomy-mono);
  font-size: 11px;
  color: var(--anatomy-hi);
  margin-bottom: 12px;
}
.nx-card p {
  font-size: 13px;
  line-height: 1.5;
  color: var(--anatomy-text);
  margin: 0;
}

.nx-layers-box {
  background: var(--anatomy-card);
  border: 1px solid var(--anatomy-lo);
  border-radius: 6px;
  overflow: hidden;
}
.nx-layer-row {
  display: flex;
  align-items: flex-start;
  gap: 20px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--anatomy-faint);
}
.nx-layer-row:last-child {
  border-bottom: none;
}
.nx-lvl {
  font-family: var(--anatomy-mono);
  font-weight: 700;
  font-size: 13px;
  color: var(--anatomy-lit);
  background: var(--anatomy-face);
  padding: 4px 8px;
  border-radius: 4px;
  border: 1px solid var(--anatomy-lo);
  min-width: 36px;
  text-align: center;
}
.nx-layer-info strong {
  color: var(--anatomy-strong);
  font-size: 14px;
}
.nx-layer-info code {
  font-family: var(--anatomy-mono);
  font-size: 12px;
  color: var(--anatomy-hi);
}
.nx-layer-info p {
  font-size: 13px;
  color: var(--anatomy-text);
  margin: 4px 0 0 0;
}

.nx-contracts-table {
  background: var(--anatomy-card);
  border: 1px solid var(--anatomy-lo);
  border-radius: 6px;
  overflow-x: auto;
}
.nx-contracts-table table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  text-align: left;
}
.nx-contracts-table th, .nx-contracts-table td {
  padding: 14px 18px;
  border-bottom: 1px solid var(--anatomy-faint);
}
.nx-contracts-table th {
  background: var(--anatomy-face);
  color: var(--anatomy-strong);
  font-weight: 600;
}
.nx-contracts-table tr:last-child td {
  border-bottom: none;
}
.nx-contracts-table td code {
  font-family: var(--anatomy-mono);
  font-size: 12px;
}

.nx-footer {
  border-top: 1px solid var(--anatomy-faint);
  padding: 32px 0 64px 0;
  color: var(--anatomy-muted);
  font-size: 13px;
}
.nx-footer-inner {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
}
.nx-footer code {
  font-family: var(--anatomy-mono);
  font-size: 11px;
}
`;

// Clean, Robust Client-Side Interactive Engine (NO external kitScript to avoid tokenizer collisions)
const liveScript = `
const P = ${JSON.stringify(P)};
const DIAL = ${JSON.stringify(DIAL)};
const DIAL_R = ${DIAL_R}, DIAL_TOP = ${DIAL_TOP};

const az = (P.azimuth ?? 45) * Math.PI / 180;
const el = (P.elevation ?? 30) * Math.PI / 180;
const sinA = Math.sin(az), cosA = Math.cos(az);
const sinE = Math.sin(el), cosE = Math.cos(el);
const kScale = P.scale ?? 1;

const STAGES = [
  {
    name: "IDLE",
    fsmNode: "node-idle",
    legal: "running (start_turn)",
    terminalRule: "ALLOWED (Non-terminal)",
    sqlite: "None (Awaiting Host Command)",
    seq: 104,
    tokens: 12400,
    stroke: 0,
    physical: "The Geneva turret rests at Station 1. The Chrono-Spine reel is locked with ratchet pawl engaged. Token dial needle rests at base standby mark (12.4k tokens).",
    software: "<strong>Workspace Daemon (UDS Loopback):</strong> Listening on <code>~/.nexus/nexus.sock</code>. Supervisor holds per-session FIFO queue. No active turn running."
  },
  {
    name: "ASSEMBLE",
    fsmNode: "node-running",
    legal: "awaiting_model, failed, cancelled",
    terminalRule: "ALLOWED (Non-terminal)",
    sqlite: "turn.started · seq 104",
    seq: 104,
    tokens: 28600,
    stroke: 2,
    physical: "Station 1's precision caliper closes to gauge context thickness. Tape guide rollers advance to read recent turn records. Needle advances to 28.6k.",
    software: "<strong>Context Manager (nexus/context/):</strong> Gathers system text, skills, hooks, and tool declarations. Caliper checks token budget and runs compaction if threshold exceeded."
  },
  {
    name: "STREAM",
    fsmNode: "node-awaiting-model",
    legal: "awaiting_tools, completed, failed, cancelled",
    terminalRule: "ALLOWED (Non-terminal)",
    sqlite: "model.started · text.delta · thinking.delta",
    seq: 104,
    tokens: 34200,
    stroke: 4,
    physical: "Station 2's crystal resonator vibrates with high-frequency acoustic pulses. Light pulses through the central shaft. Dial needle advances to 34.2k tokens.",
    software: "<strong>Model Provider (Anthropic / OpenAI / Gemini):</strong> Streams <code>TextDelta</code> and opaque <code>ThinkingDelta</code> chunks. <code>ToolCallAccumulator</code> stitches partial JSON in flight."
  },
  {
    name: "GATE",
    fsmNode: "node-awaiting-permission",
    legal: "awaiting_tools, cancelled",
    terminalRule: "ALLOWED (Gate Barrier Active)",
    sqlite: "permission.requested (status: pending)",
    seq: 104,
    tokens: 34200,
    stroke: 6,
    physical: "Station 3's mechanical safety shutter drops into place to inspect tool arguments. If approval is required, an interlock trip pin halts further carriage motion.",
    software: "<strong>Permission Gate (nexus/tools/permissions.py):</strong> Validates filesystem bounds, sandbox policies, and auto-approval prefixes before authorizing dispatch."
  },
  {
    name: "DISPATCH",
    fsmNode: "node-awaiting-tools",
    legal: "awaiting_model, completed, failed, cancelled",
    terminalRule: "ALLOWED (Non-terminal)",
    sqlite: "tool.requested · tool.result (iteration 1)",
    seq: 104,
    tokens: 49800,
    stroke: 13,
    physical: "Geneva turret indexes to Station 4. The tool actuator carriage slides forward along precision rails, probe extending to full stroke. Dial sweeps to 49.8k.",
    software: "<strong>Tool Dispatcher (nexus/tools/):</strong> Executes file mutation, bash process, or subagent worktree clutch. Captures stdout/stderr into <code>ToolExecutionResult</code>."
  },
  {
    name: "REDUCE",
    fsmNode: "node-reduce",
    legal: "idle, completed",
    terminalRule: "ALLOWED (Persistence Spur)",
    sqlite: "Event(seq: 105) appended to ~/.nexus/nexus.db",
    seq: 105,
    tokens: 51200,
    stroke: 1,
    physical: "The embossing hammer strikes the Chrono-Spine metallic tape, incrementing sequence counter to 105. Light refracts through the quartz prism into all 4 fibers.",
    software: "<strong>Pure Reducer (nexus/view/reduce.py):</strong> Event appended to SQLite. Pure reduction produces updated <code>ConversationView</code>; deltas projected to Ratatui & GPUI."
  }
];

let stageIndex = 0;
let autoTimer = null;

const carriageEl = document.querySelector('[data-part="tool-carriage"]');
const needleEl = document.querySelector('[data-part="needle"]');
const readoutEl = document.querySelector("[data-readout]");
const inspectPhysicalEl = document.getElementById("inspectPhysical");
const inspectSoftwareEl = document.getElementById("inspectSoftware");
const pills = document.querySelectorAll(".nx-pill");

// FSM elements
const fsmBadge = document.getElementById("fsmCurrentBadge");
const fsmStateName = document.getElementById("fsmStateName");
const fsmLegalTrans = document.getElementById("fsmLegalTrans");
const fsmTerminalRule = document.getElementById("fsmTerminalRule");
const fsmSqliteEvent = document.getElementById("fsmSqliteEvent");
const fsmNodes = document.querySelectorAll(".nx-fsm-node");

function renderNeedle(ratio) {
  const angle = ratio * Math.PI * 1.6 + 0.2;
  const [dx, dy] = DIAL;
  const tipR = DIAL_R - 3.8;
  const x1 = dx - Math.sin(angle) * 2.8;
  const y1 = dy + Math.cos(angle) * 2.8;
  const x2 = dx + Math.sin(angle) * tipR;
  const y2 = dy - Math.cos(angle) * tipR;
  
  const p1x = P.origin[0] + (x1 * sinA - y1 * cosA) * kScale;
  const p1y = P.origin[1] + ((x1 * cosA + y1 * sinA) * sinE - (DIAL_TOP + 0.3) * cosE) * kScale;
  const p2x = P.origin[0] + (x2 * sinA - y2 * cosA) * kScale;
  const p2y = P.origin[1] + ((x2 * cosA + y2 * sinA) * sinE - (DIAL_TOP + 0.3) * cosE) * kScale;
  
  if (needleEl) {
    needleEl.setAttribute("d", "M" + p1x.toFixed(1) + " " + p1y.toFixed(1) + "L" + p2x.toFixed(1) + " " + p2y.toFixed(1));
  }
}

function updateState(idx) {
  stageIndex = (idx + STAGES.length) % STAGES.length;
  const cur = STAGES[stageIndex];
  
  // Readout update
  if (readoutEl) {
    readoutEl.textContent = cur.name + " · SEQ " + cur.seq + " · " + (cur.tokens / 1000).toFixed(1) + "k TOKENS";
  }
  
  // Inspector update
  if (inspectPhysicalEl) inspectPhysicalEl.innerHTML = cur.physical;
  if (inspectSoftwareEl) inspectSoftwareEl.innerHTML = cur.software;
  
  // Needle update
  renderNeedle(cur.tokens / 100000);
  
  // Carriage position
  const dy = cur.stroke;
  const sx = -dy * cosA * kScale;
  const sy = dy * sinA * sinE * kScale;
  if (carriageEl) {
    carriageEl.setAttribute("transform", "translate(" + sx.toFixed(1) + " " + sy.toFixed(1) + ")");
  }
  
  // Update Pills
  pills.forEach((pill, i) => {
    if (i === stageIndex) {
      pill.classList.add("active");
    } else {
      pill.classList.remove("active");
    }
  });

  // Update FSM Graph Node
  fsmNodes.forEach(node => {
    if (node.id === cur.fsmNode) {
      node.classList.add("active");
    } else {
      node.classList.remove("active");
    }
  });

  // Update FSM Metrics
  if (fsmBadge) fsmBadge.textContent = "ACTIVE: " + cur.name;
  if (fsmStateName) fsmStateName.textContent = cur.name;
  if (fsmLegalTrans) fsmLegalTrans.textContent = cur.legal;
  if (fsmTerminalRule) fsmTerminalRule.textContent = cur.terminalRule;
  if (fsmSqliteEvent) fsmSqliteEvent.textContent = cur.sqlite;
}

// Custom trigger for terminal nodes
function setTerminalFSM(nodeId, name, legal, rule, sqlite, stroke, tokens) {
  fsmNodes.forEach(node => {
    if (node.id === nodeId) {
      node.classList.add("active");
    } else {
      node.classList.remove("active");
    }
  });
  if (fsmBadge) fsmBadge.textContent = "TERMINAL: " + name;
  if (fsmStateName) fsmStateName.textContent = name;
  if (fsmLegalTrans) fsmLegalTrans.textContent = legal;
  if (fsmTerminalRule) fsmTerminalRule.textContent = rule;
  if (fsmSqliteEvent) fsmSqliteEvent.textContent = sqlite;
  if (readoutEl) readoutEl.textContent = name + " · SEQ 105 · " + (tokens / 1000).toFixed(1) + "k TOKENS";
  renderNeedle(tokens / 100000);
}

// Attach listeners
document.getElementById("btnPrev")?.addEventListener("click", () => updateState(stageIndex - 1));
document.getElementById("btnNext")?.addEventListener("click", () => updateState(stageIndex + 1));

const autoBtn = document.getElementById("btnAuto");
autoBtn?.addEventListener("click", () => {
  if (autoTimer) {
    clearInterval(autoTimer);
    autoTimer = null;
    autoBtn.textContent = "Auto Tour";
    autoBtn.classList.remove("nx-btn-primary");
  } else {
    autoTimer = setInterval(() => updateState(stageIndex + 1), 2200);
    autoBtn.textContent = "Pause Tour ⏸";
    autoBtn.classList.add("nx-btn-primary");
  }
});

pills.forEach((pill) => {
  pill.addEventListener("click", () => {
    const p = parseInt(pill.getAttribute("data-phase") || "0", 10);
    updateState(p);
  });
});

// FSM Trigger Buttons
document.getElementById("fsmTrigStart")?.addEventListener("click", () => updateState(1));
document.getElementById("fsmTrigStream")?.addEventListener("click", () => updateState(2));
document.getElementById("fsmTrigNoTool")?.addEventListener("click", () => {
  setTerminalFSM("node-completed", "COMPLETED (end_turn)", "None (Terminal frozen)", "STRICTLY FORBIDDEN (TurnStateError)", "turn.completed(stop_reason: end_turn)", 0, 36400);
});
document.getElementById("fsmTrigToolReq")?.addEventListener("click", () => updateState(4));
document.getElementById("fsmTrigAskPerm")?.addEventListener("click", () => updateState(3));
document.getElementById("fsmTrigResolvePerm")?.addEventListener("click", () => updateState(4));
document.getElementById("fsmTrigToolDone")?.addEventListener("click", () => updateState(5));
document.getElementById("fsmTrigCancel")?.addEventListener("click", () => {
  setTerminalFSM("node-cancelled", "CANCELLED", "None (Terminal frozen)", "STRICTLY FORBIDDEN (TurnStateError)", "turn.cancelled · SIGTERM to tool group", 0, 35000);
});
document.getElementById("fsmTrigError")?.addEventListener("click", () => {
  setTerminalFSM("node-failed", "FAILED", "None (Terminal frozen)", "STRICTLY FORBIDDEN (TurnStateError)", "turn.failed (redacted error message)", 0, 35000);
});
document.getElementById("fsmTrigReset")?.addEventListener("click", () => updateState(0));

// Arrow key navigation
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight" || e.key === " ") {
    updateState(stageIndex + 1);
  } else if (e.key === "ArrowLeft") {
    updateState(stageIndex - 1);
  }
});

// Click on console plate advances state
document.querySelector(".iso-plate")?.addEventListener("click", (e) => {
  if (!e.target.closest("button") && !e.target.closest("a")) {
    updateState(stageIndex + 1);
  }
});

updateState(0);

// Initialize Pele Mermaid diagram rendering with automatic zoom and responsive container adaptation
function initPeleDiagrams() {
  if (typeof pele === "undefined") {
    setTimeout(initPeleDiagrams, 80);
    return;
  }
  const mounted = [];
  document.querySelectorAll("pre.mermaid").forEach((el) => {
    const raw = el.textContent.trim();
    try {
      const diagram = pele.mount(el, raw, {
        zoom: "auto",
        fontFamily: 'ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        fontFamilyMono: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
        fontSize: 13,
        autoDirection: true,
        directionBreakpoint: 640,
        onError: (err) => {
          console.error("Pele diagram error:", err);
        }
      });
      mounted.push(diagram);
    } catch (err) {
      if (typeof pele.PeleError !== "undefined" && err instanceof pele.PeleError) {
        el.textContent = "[Pele Error: " + err.code + "] " + err.message + "\\n\\n" + (err.snippet || "");
      } else {
        el.textContent = "Diagram error: " + err.message;
      }
    }
  });
  window.nexusDiagrams = mounted;
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initPeleDiagrams);
} else {
  initPeleDiagrams();
}
`;

const page = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Anatomy of Nexus — Master Architecture, FSM & Flowcharts</title>
  <style>
    ${k.ISO_CSS}
    * { box-sizing: border-box; }
    html { color-scheme: dark; scroll-behavior: smooth; }
    body {
      margin: 0;
      background: var(--anatomy-page);
      color: var(--anatomy-text);
      font: 14px/1.6 ui-sans-serif, system-ui, -apple-system, sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    .iso-page {
      width: min(960px, 100% - 32px);
      margin: 24px auto 96px auto;
    }
    ${DOCUMENT_CSS}
  </style>
  <script charset="utf-8" src="./pele.global.js"></script>
  <script>
    if (typeof pele === "undefined") {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/pele@1/dist/pele.global.js";
      s.charset = "utf-8";
      document.head.appendChild(s);
    }
  </script>
</head>
<body class="iso" data-theme="${theme}">
  <main class="iso-page">
    ${fullDocumentBody}
  </main>
  <script type="module">
    ${liveScript}
  </script>
</body>
</html>`;

const targetFile = join(dirname(fileURLToPath(import.meta.url)), "nexus_anatomy.html");
writeFileSync(targetFile, page, "utf-8");
console.log("wrote " + targetFile);
