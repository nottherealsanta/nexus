//! Independently tuned desktop palettes; semantic colors are shared across views.
use gpui::{rgb, Hsla};
#[derive(Clone, Copy)]
pub struct Theme {
    pub background: Hsla,
    pub sidebar: Hsla,
    pub surface: Hsla,
    pub raised: Hsla,
    pub border: Hsla,
    pub text: Hsla,
    pub muted: Hsla,
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
}
impl Theme {
    pub fn new(light: bool) -> Self {
        let c = |v| rgb(v).into();
        if light {
            Self {
                background: c(0xf7f8fa),
                sidebar: c(0xf1f2f5),
                surface: c(0xffffff),
                raised: c(0xebeef2),
                border: c(0xe1e4e8),
                text: c(0x252b33),
                muted: c(0x606c79),
                accent: c(0x41464f),
                accent_bg: c(0xe9ebef),
                green: c(0x267e58),
                red: c(0xb84949),
                amber: c(0x946716),
                diff_add: c(0x176b43),
                diff_add_bg: c(0xe8f5ec),
                diff_remove: c(0xa33c42),
                diff_remove_bg: c(0xfbeaec),
                focus_ring: c(0x4c7dff),
                disabled: c(0xa6abb3),
            }
        } else {
            Self {
                background: c(0x0b0b0b),
                sidebar: c(0x101113),
                surface: c(0x17181b),
                raised: c(0x24262b),
                border: c(0x303238),
                text: c(0xe8ebef),
                muted: c(0x969ca6),
                accent: c(0xd7dae0),
                accent_bg: c(0x25272c),
                green: c(0x91c9a4),
                red: c(0xf08c98),
                amber: c(0xe2bd80),
                diff_add: c(0x8ed5a5),
                diff_add_bg: c(0x14281d),
                diff_remove: c(0xf09a9e),
                diff_remove_bg: c(0x301a1d),
                focus_ring: c(0x789cff),
                disabled: c(0x666a72),
            }
        }
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
}
