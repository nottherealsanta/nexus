//! Shot registry. One module per decision; ids are `dNN-name`, matching
//! DESIGN_PLAN.md and the gallery.
pub mod alt;
pub mod common;
pub mod d02_shell;
pub mod d01_tokens;
pub mod d03_sessions;
pub mod d04_user;
pub mod d05_footer;
pub mod d06_groups;
pub mod d07_reasoning;
pub mod d08_markdown;
pub mod d09_diff;
pub mod d10_context;
pub mod d11_composer;
pub mod d13_overlays;
pub mod d14_waiting;
pub mod d15_alerts;
pub mod d16_files;
pub mod d17_narrow;
pub mod d18_states;

use crate::kit::P;
use crate::theme::Mode;

pub struct Shot {
    pub id: &'static str,
    pub caption: &'static str,
    pub w: u16,
    pub h: u16,
    pub modes: &'static [Mode],
    /// Also write an ASCII-glyph variant (dark).
    pub ascii: bool,
    pub draw: fn(&mut P),
}

pub const DL: &[Mode] = &[Mode::Dark, Mode::Light];
pub const DLM: &[Mode] = &[Mode::Dark, Mode::Light, Mode::Mono];
pub const D: &[Mode] = &[Mode::Dark];

pub fn all() -> Vec<Shot> {
    let mut v = Vec::new();
    v.extend(d01_tokens::shots());
    v.extend(d02_shell::shots());
    v.extend(d03_sessions::shots());
    v.extend(d04_user::shots());
    v.extend(d05_footer::shots());
    v.extend(d06_groups::shots());
    v.extend(d07_reasoning::shots());
    v.extend(d08_markdown::shots());
    v.extend(d09_diff::shots());
    v.extend(d10_context::shots());
    v.extend(d11_composer::shots());
    v.extend(d13_overlays::shots());
    v.extend(d14_waiting::shots());
    v.extend(d15_alerts::shots());
    v.extend(d16_files::shots());
    v.extend(d17_narrow::shots());
    v.extend(d18_states::shots());
    v.extend(alt::shots());
    v
}
