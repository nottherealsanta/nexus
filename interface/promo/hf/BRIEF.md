# Scene author brief (HyperFrames)

The promo is now built with **HyperFrames** (HTML → video), project `interface/promo/hf/`
(pinned `hyperframes@0.8.185`, run as `npx hyperframes …` from that folder). The old Remotion
version in `interface/promo/video/src/scenes/*.tsx` is **reference only** for timing, pacing and
ideas; it is being retired. Its hand-drawn UIs looked like old Nexus, so the user asked for the
**real, current Nexus (simulated)**: every UI on screen must now be a real capture.

Read first:
- `interface/promo/plan.md`: storyboard and the measured music map (master-timeline seconds).
- `hf/index.html`: the root, scene hosts and their `data-start`/`data-duration`, plus audio placement.
- HyperFrames contract: `hf/node_modules/hyperframes/dist/skills/hyperframes/SKILL.md`,
  `references/format.md`, `references/runtimes.md`, `references/motion.md`, and `npx hyperframes docs gsap`.
- Captures: `interface/promo/captures/nsh/manifest.json` and `captures/tui/manifest.json`, then READ the PNGs you
  plan to use. Copy what you use into `hf/assets/cap/nsh/` or `hf/assets/cap/tui/` (keep file names) and reference
  them as `assets/cap/...`. TUI SVGs are vector and preferred over PNGs for close-ups.

Rules
- Edit ONLY your own scene file(s) in `hf/compositions/` (named in your task) and add assets under `hf/assets/cap/`.
  Do not edit `index.html`, `assets/kit/kit.css`, other scenes, or anything outside `hf/`.
- Scene file shape (format.md): everything inside `<template>`; start with `<link rel="stylesheet" href="assets/kit/kit.css" />`
  (fonts Google Sans + Monaspace Argon, Nexus colour vars); root `<div id="root" data-composition-id="<id>" …>` styled as `#root { position:absolute; inset:0 }`;
  timed elements have `class="clip"`, `data-start`, `data-duration` (seconds local to the scene) and unique ids prefixed with the scene id;
  one paused GSAP timeline (`gsap` is global, loaded by the root) registered last as `window.__timelines["<id>"] = tl`.
  Use `fromTo` with absolute positions. Never animate a `.clip` element's transform/visibility: animate a child.
  Per-frame drawing (typewriters, counters, a moving selection) may use the `hf-seek` protocol in runtimes.md, deriving state from time alone.
  No Math.random (use a seeded mulberry32), no timers, no Date.
- Accuracy: show only real Nexus behaviour from the captures; captions use the wording in plan.md.
- Look: premium dark product film (Apple, Linear or Vercel launch style). Restraint, big confident Google Sans captions (one at a time),
  real UI framed large and crisp, slow eased 3D camera drifts (CSS perspective + GSAP rotationX/rotationY/z on a wrapper),
  punch-in zooms onto the exact region of a capture that matters, light accent glows, hard cuts exactly on the phrase times in plan.md.
  The TUI accent is `#e8a46e`. NSH Void is monochrome; agent colours are its only colour.
  Keep UI legible: at least 16 px of on-screen text height, and zoom into captures rather than showing them tiny.
- Verify: `npx hyperframes check --json` (fix errors, weigh warnings) and
  `npx hyperframes snapshot --at <master seconds,…> --describe false -o .hyperframes/shots-<id> --keep`. Snapshots use
  **master** time: your scene's start in index.html plus local time. READ the PNGs and iterate until every phrase looks polished.
  Never pass `--describe` without `false`: it would send frames to an external API.
- Final report (≤200 words): what each phrase shows, the captures used, files touched, known weaknesses.
