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
pub mod annotate;
pub mod api;
pub mod app;
mod audio_output;
pub mod code_editor;
pub mod completion;
pub mod composer;
pub mod dictation;
pub mod diff;
pub mod discovery;
pub mod editor;
pub mod frames;
pub mod images;
pub mod indicator;
pub mod inspector;
pub mod keys;
pub mod layout;
pub mod live;
pub mod manager;
pub mod manifest;
pub mod markdown;
pub mod mermaid;
pub use nexus_mermaid::text as mermaid_text;
pub mod paint;
pub mod partial;
pub mod prefs;
pub mod program_status;
pub mod reduce;
pub mod render;
pub mod selection;
pub mod speech;
pub mod splash;
pub mod stage;
pub mod state;
pub mod stats;
pub mod sticky;
pub mod svg;
pub mod sync;
pub mod term;
pub mod terminal_api;
pub mod terminal_panel;
pub mod terminal_screen;
pub mod text;
pub mod theme;
pub mod timeline;
pub mod transport;
pub mod ui;
pub mod usage;
