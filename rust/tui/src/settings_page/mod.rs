//! One-page Settings (plan RATATUI_DESIGN_MOCKUPS_PLAN §9.3): the host sends a typed
//! page, this module renders it with the widget kit and handles its keys and mouse.
pub mod draw;
pub mod input;
pub mod model;

pub use draw::draw;
pub use input::{click, key, Act, PageState};
pub use model::Page;
