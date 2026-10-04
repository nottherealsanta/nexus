//! Embedded Lucide stroke icons and restrained chamfer geometry.
//! No Apple-only glyphs or runtime asset paths; see assets/LICENSE-LUCIDE.
use crate::theme::Theme;
use gpui::{prelude::*, *};
use std::borrow::Cow;
const ICONS: &[(&str, &[u8])] = &[
    (
        "icons/arrow-down.svg",
        include_bytes!("../assets/icons/arrow-down.svg"),
    ),
    (
        "icons/arrow-left.svg",
        include_bytes!("../assets/icons/arrow-left.svg"),
    ),
    (
        "icons/arrow-up.svg",
        include_bytes!("../assets/icons/arrow-up.svg"),
    ),
    (
        "icons/brain.svg",
        include_bytes!("../assets/icons/brain.svg"),
    ),
    (
        "icons/check.svg",
        include_bytes!("../assets/icons/check.svg"),
    ),
    (
        "icons/chevron-down.svg",
        include_bytes!("../assets/icons/chevron-down.svg"),
    ),
    (
        "icons/chevron-right.svg",
        include_bytes!("../assets/icons/chevron-right.svg"),
    ),
    (
        "icons/circle-dot.svg",
        include_bytes!("../assets/icons/circle-dot.svg"),
    ),
    (
        "icons/circle-help.svg",
        include_bytes!("../assets/icons/circle-help.svg"),
    ),
    ("icons/code.svg", include_bytes!("../assets/icons/code.svg")),
    ("icons/cpu.svg", include_bytes!("../assets/icons/cpu.svg")),
    (
        "icons/ellipsis.svg",
        include_bytes!("../assets/icons/ellipsis.svg"),
    ),
    (
        "icons/file-text.svg",
        include_bytes!("../assets/icons/file-text.svg"),
    ),
    (
        "icons/folder.svg",
        include_bytes!("../assets/icons/folder.svg"),
    ),
    (
        "icons/git-branch.svg",
        include_bytes!("../assets/icons/git-branch.svg"),
    ),
    (
        "icons/message-square.svg",
        include_bytes!("../assets/icons/message-square.svg"),
    ),
    ("icons/mic.svg", include_bytes!("../assets/icons/mic.svg")),
    (
        "icons/network.svg",
        include_bytes!("../assets/icons/network.svg"),
    ),
    (
        "icons/panel-left.svg",
        include_bytes!("../assets/icons/panel-left.svg"),
    ),
    (
        "icons/panel-right.svg",
        include_bytes!("../assets/icons/panel-right.svg"),
    ),
    (
        "icons/paperclip.svg",
        include_bytes!("../assets/icons/paperclip.svg"),
    ),
    ("icons/plus.svg", include_bytes!("../assets/icons/plus.svg")),
    (
        "icons/refresh-cw.svg",
        include_bytes!("../assets/icons/refresh-cw.svg"),
    ),
    (
        "icons/search.svg",
        include_bytes!("../assets/icons/search.svg"),
    ),
    (
        "icons/settings-2.svg",
        include_bytes!("../assets/icons/settings-2.svg"),
    ),
    (
        "icons/shield-check.svg",
        include_bytes!("../assets/icons/shield-check.svg"),
    ),
    (
        "icons/square.svg",
        include_bytes!("../assets/icons/square.svg"),
    ),
    (
        "icons/terminal.svg",
        include_bytes!("../assets/icons/terminal.svg"),
    ),
    (
        "icons/triangle-alert.svg",
        include_bytes!("../assets/icons/triangle-alert.svg"),
    ),
    (
        "icons/volume-2.svg",
        include_bytes!("../assets/icons/volume-2.svg"),
    ),
    ("icons/x.svg", include_bytes!("../assets/icons/x.svg")),
];
pub struct Assets;
impl AssetSource for Assets {
    fn load(&self, path: &str) -> Result<Option<Cow<'static, [u8]>>> {
        Ok(ICONS
            .iter()
            .find(|(name, _)| *name == path)
            .map(|(_, bytes)| Cow::Borrowed(*bytes)))
    }
    fn list(&self, path: &str) -> Result<Vec<SharedString>> {
        Ok(ICONS
            .iter()
            .filter(|(name, _)| name.starts_with(path))
            .map(|(name, _)| SharedString::from(*name))
            .collect())
    }
}
pub fn icon(name: &'static str, color: Hsla) -> Svg {
    svg()
        .path(format!("icons/{name}.svg"))
        .size(px(15.))
        .flex_shrink_0()
        .text_color(color)
}
/// Uniform icon slots retain visible labels for unfamiliar commands.
pub fn button_label(label: &str) -> (Option<&'static str>, &str) {
    match label {
        "+" => (Some("plus"), ""),
        "×" => (Some("x"), ""),
        "←" => (Some("arrow-left"), ""),
        "···" => (Some("ellipsis"), ""),
        "⚙" => (Some("settings-2"), ""),
        "Attach" => (Some("paperclip"), label),
        "Dictate" | "Stop dictation" => (Some("mic"), label),
        "Listen" => (Some("volume-2"), label),
        "Send" => (Some("arrow-up"), label),
        "Stop" => (Some("square"), label),
        "Sessions" => (Some("panel-left"), label),
        "Details" => (Some("panel-right"), label),
        "Inspect workspace" => (Some("folder"), label),
        "Review changes" => (Some("git-branch"), label),
        "Plan implementation" => (Some("terminal"), label),
        _ if label.starts_with("New conversation") => (Some("plus"), label),
        _ if label.starts_with("Commands") => (Some("search"), label),
        _ if label.starts_with("Settings") => (Some("settings-2"), label),
        _ if label.starts_with("Jump to latest") => (Some("arrow-down"), label),
        _ => (None, label),
    }
}
/// A single 45-degree cut marks a raised decision/composer surface.
/// It is structural, never a status decoration, and follows the theme boundary.
pub fn corner(t: Theme) -> impl IntoElement {
    canvas(
        |_, _, _| (),
        move |bounds, _, window, _| {
            let mut cut = PathBuilder::fill();
            cut.move_to(bounds.origin);
            cut.line_to(bounds.top_right());
            cut.line_to(bounds.bottom_right());
            cut.close();
            if let Ok(path) = cut.build() {
                window.paint_path(path, t.background);
            }
            let mut edge = PathBuilder::stroke(px(1.));
            edge.move_to(bounds.origin);
            edge.line_to(bounds.bottom_right());
            if let Ok(path) = edge.build() {
                window.paint_path(path, t.border);
            }
        },
    )
    .absolute()
    .top_0()
    .right_0()
    .size(px(10.))
}
