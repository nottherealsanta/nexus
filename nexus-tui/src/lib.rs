//! Nexus 0.10 terminal client.
//!
//! Layers, bottom to top (each depends only on the ones above it in this list):
//! `transport` (HTTP + SSE) -> `api` (typed endpoints) -> `reduce` (client
//! model) -> `discovery` / `sync` (connection supervisor) -> `state` (view
//! state). The shell on top: `manifest` and `theme` (shared interface data,
//! embedded at build time), `ui` (view-state types), `layout` (geometry and
//! hit-testing), `app` (focus, commands, input), `actions` (API mutations),
//! `render` (drawing) and `term` (terminal hygiene).

pub mod actions;
pub mod api;
pub mod app;
pub mod completion;
pub mod composer;
pub mod discovery;
pub mod editor;
pub mod keys;
pub mod layout;
pub mod manifest;
pub mod markdown;
pub mod mermaid;
pub mod mermaid_text;
pub mod prefs;
pub mod reduce;
pub mod render;
pub mod state;
pub mod sync;
pub mod term;
pub mod text;
pub mod theme;
pub mod timeline;
pub mod transport;
pub mod ui;
