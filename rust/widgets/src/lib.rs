//! Nexus widget kit: stateless Ratatui components plus small state structs.
//!
//! Contract (plans/done/RATATUI_DESIGN_MOCKUPS_PLAN.md §3): components render into a
//! `Buffer`, register their focus stop and hit rectangle on the shared [`Ui`], and
//! return a [`Response`]. They never mutate application state; the owner maps
//! [`keys::Intent`] values to host operations. Hover and focus change colour only,
//! never geometry. Everything is bounded.
pub mod anim;
pub mod components;
pub mod focus;
pub mod glyphs;
pub mod hit;
pub mod keys;
pub mod layout;
pub mod theme;
pub mod ui;

pub use components::*;
pub use components::{controls, feedback, inputs, lists, scroll};
pub use focus::{FocusRing, Region};
pub use glyphs::Glyphs;
pub use hit::HitMap;
pub use keys::Intent;
pub use theme::Theme;
pub use ui::{Response, Ui};
