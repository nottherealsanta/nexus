//! One-page Settings (plan RATATUI_DESIGN_MOCKUPS_PLAN §9.3): the host sends a typed
//! page, this module renders it with the widget kit and handles its keys and mouse.
pub mod draw;
pub mod grok;
pub mod input;
pub mod model;
pub mod root;

pub use draw::draw;
pub use input::{click, hover, key, Act, PageState};
pub use model::Page;
