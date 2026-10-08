//! Desktop palettes derived from the Ratatui client's `Palette` (rust/tui/src/render.rs):
//! the same neutral greys, warm brand accent and semantic hues, so both native
//! surfaces read as one product. Host colour tokens resolve through `Theme::resolve`.
use gpui::{point, px, rgb, rgba, BoxShadow, Hsla};
use std::sync::OnceLock;

/// Shared visual scale. Dimensions such as pane widths and icon strokes remain
/// independent geometry; typography, insets and corners use these tokens.
pub mod size {
    pub const CAPTION: f32 = 11.;
    pub const SMALL: f32 = 12.;
    pub const UI: f32 = 13.;
    pub const BODY: f32 = 15.;
    pub const TITLE: f32 = 20.;
    pub const BODY_LINE: f32 = 24.;
    pub const CODE_LINE: f32 = 20.;
    pub const READING: f32 = 720.;
}
pub mod space {
    pub const XS: f32 = 4.;
    pub const SM: f32 = 8.;
    pub const MD: f32 = 12.;
    pub const LG: f32 = 16.;
    pub const XL: f32 = 24.;
    pub const XXL: f32 = 32.;
}
pub mod radius {
    pub const CHIP: f32 = 4.;
    pub const CONTROL: f32 = 8.;
    pub const CARD: f32 = 12.;
    pub const PILL: f32 = 999.;
}
pub const CODE_FONTS: [&str; 3] = ["Monaspace Argon", "SF Mono", "Menlo"];
static CODE_FONT: OnceLock<&'static str> = OnceLock::new();

pub fn init_code_font(names: &[String]) {
    CODE_FONT.get_or_init(|| choose_code_font(names));
}
fn choose_code_font(names: &[String]) -> &'static str {
    CODE_FONTS
        .into_iter()
        .find(|family| names.iter().any(|name| name == family))
        .unwrap_or("Menlo")
}
pub fn code_font() -> &'static str {
    CODE_FONT.get().copied().unwrap_or("Menlo")
}
#[derive(Clone, Copy)]
pub struct Theme {
    pub background: Hsla,
    pub sidebar: Hsla,
    pub surface: Hsla,
    pub raised: Hsla,
    pub border: Hsla,
    pub text: Hsla,
    pub muted: Hsla,
    pub text_tertiary: Hsla,
    pub hover: Hsla,
    pub pressed: Hsla,
    pub separator: Hsla,
    pub shadow: Hsla,
    pub accent: Hsla,
    pub accent_bg: Hsla,
    pub green: Hsla,
    pub red: Hsla,
    pub amber: Hsla,
    pub diff_add: Hsla,
    pub diff_add_bg: Hsla,
    pub diff_remove: Hsla,
    pub diff_remove_bg: Hsla,
    pub focus_ring: Hsla,
    pub disabled: Hsla,
    /// The TUI's warm accent: active markers, default block titles and links.
    pub brand: Hsla,
    pub blue: Hsla,
    pub purple: Hsla,
    pub cyan: Hsla,
}
impl Theme {
    pub fn popover_shadow(self) -> Vec<BoxShadow> {
        vec![BoxShadow {
            color: self.shadow,
            offset: point(px(0.), px(4.)),
            blur_radius: px(16.),
            spread_radius: px(0.),
        }]
    }
    pub fn sheet_shadow(self) -> Vec<BoxShadow> {
        vec![BoxShadow {
            color: self.shadow,
            offset: point(px(0.), px(8.)),
            blur_radius: px(32.),
            spread_radius: px(0.),
        }]
    }
    pub fn new(light: bool) -> Self {
        let c = |v| rgb(v).into();
        if light {
            Self {
                background: c(0xffffff),
                sidebar: c(0xf5f5f4),
                surface: c(0xf5f5f4),
                raised: c(0xececea),
                border: c(0xdcdcd8),
                text: c(0x1b1b1b),
                muted: c(0x555555),
                text_tertiary: c(0x8a8a8a),
                hover: c(0xececea),
                pressed: c(0xe2e2df),
                separator: c(0xe6e6e3),
                shadow: rgba(0x1b1b1b24).into(),
                accent: c(0x2b2b2b),
                accent_bg: c(0xe8e8e5),
                green: c(0x268044),
                red: c(0xc23a4a),
                amber: c(0xa86200),
                diff_add: c(0x1f6b39),
                diff_add_bg: c(0xdcf5e2),
                diff_remove: c(0xa8303f),
                diff_remove_bg: c(0xfde2e4),
                focus_ring: c(0x2f6fd6),
                disabled: c(0xb9b9b4),
                brand: c(0xc8672f),
                blue: c(0x2f6fd6),
                purple: c(0x7a52c7),
                cyan: c(0x0e7490),
            }
        } else {
            Self {
                background: c(0x0b0b0b),
                sidebar: c(0x111111),
                surface: c(0x141414),
                raised: c(0x1e1e1e),
                border: c(0x2c2c2c),
                text: c(0xeeeeee),
                muted: c(0xa3a3a3),
                text_tertiary: c(0x6f6f6f),
                hover: c(0x1e1e1e),
                pressed: c(0x282828),
                separator: c(0x222222),
                shadow: rgba(0x00000070).into(),
                accent: c(0xe0e0e0),
                accent_bg: c(0x262626),
                green: c(0x7fd88f),
                red: c(0xe06c75),
                amber: c(0xf5a742),
                diff_add: c(0x9be3a8),
                diff_add_bg: c(0x183824),
                diff_remove: c(0xf0959c),
                diff_remove_bg: c(0x3e1b21),
                focus_ring: c(0x5c9cf5),
                disabled: c(0x5a5a5a),
                brand: c(0xfab283),
                blue: c(0x5c9cf5),
                purple: c(0x9d7cd8),
                cyan: c(0x56d4dd),
            }
        }
    }
    /// Host colour tokens (`$nx-blue`, agent hex colours) as the TUI resolves them
    /// (`transcript::color`); anything unrecognised keeps `fallback`.
    pub fn resolve(self, value: &str, fallback: Hsla) -> Hsla {
        match value {
            "$nx-blue" => return self.blue,
            "$nx-accent" => return self.brand,
            "$nx-purple" => return self.purple,
            "$nx-success" => return self.green,
            "$nx-warning" => return self.amber,
            "$nx-cyan" => return self.cyan,
            "$nx-label-neutral" => return self.text_tertiary,
            _ => {}
        }
        value
            .strip_prefix('#')
            .filter(|hex| hex.len() == 6)
            .and_then(|hex| u32::from_str_radix(hex, 16).ok())
            .map(|n| rgb(n).into())
            .unwrap_or(fallback)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rgb_value(color: Hsla) -> [u8; 3] {
        let rgba: gpui::Rgba = color.into();
        [
            (rgba.r * 255.) as u8,
            (rgba.g * 255.) as u8,
            (rgba.b * 255.) as u8,
        ]
    }

    #[test]
    fn code_font_prefers_installed_families_in_order() {
        assert_eq!(
            choose_code_font(&["Menlo".into(), "SF Mono".into(), "Monaspace Argon".into()]),
            "Monaspace Argon"
        );
        assert_eq!(
            choose_code_font(&["Menlo".into(), "SF Mono".into()]),
            "SF Mono"
        );
        assert_eq!(choose_code_font(&[]), "Menlo");
    }

    #[test]
    fn dark_canvas_retains_near_black_and_palettes_are_opaque() {
        let dark = Theme::new(false);
        assert_eq!(rgb_value(dark.background), [11, 11, 11]);
        assert_eq!(dark.background.a, 1.0);
        let light = Theme::new(true);
        assert_eq!(light.background.a, 1.0);
    }

    #[test]
    fn diff_states_have_distinct_tinted_semantics() {
        let dark = Theme::new(false);
        let light = Theme::new(true);
        assert_ne!(dark.diff_add_bg, dark.surface);
        assert_ne!(dark.diff_remove_bg, dark.surface);
        assert_ne!(dark.diff_add, dark.diff_remove);
        assert_ne!(light.diff_add_bg, light.diff_remove_bg);
    }

    #[test]
    fn host_colour_tokens_resolve_like_the_tui() {
        let dark = Theme::new(false);
        assert_eq!(rgb_value(dark.brand), [250, 178, 131]);
        assert_eq!(dark.resolve("$nx-blue", dark.text), dark.blue);
        assert_eq!(dark.resolve("$nx-accent", dark.text), dark.brand);
        assert_eq!(rgb_value(dark.resolve("#ff8000", dark.text)), [255, 128, 0]);
        assert_eq!(dark.resolve("", dark.muted), dark.muted);
        assert_eq!(dark.resolve("#zz", dark.muted), dark.muted);
    }
}
