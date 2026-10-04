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
}
impl Theme {
    pub fn new(light: bool) -> Self {
        let c = |v| rgb(v).into();
        if light {
            Self {
                background: c(0xf7f8fa),
                sidebar: c(0xf7f8fa),
                surface: c(0xffffff),
                raised: c(0xe9edf2),
                border: c(0xd5dbe1),
                text: c(0x252b33),
                muted: c(0x606c79),
                accent: c(0x246f83),
                accent_bg: c(0xdeeff4),
                green: c(0x267e58),
                red: c(0xb84949),
                amber: c(0x946716),
            }
        } else {
            Self {
                background: c(0x0b0b0b),
                sidebar: c(0x0b0b0b),
                surface: c(0x101113),
                raised: c(0x14161a),
                border: c(0x292c32),
                text: c(0xe8ebef),
                muted: c(0xa0a6af),
                accent: c(0x69d8e5),
                accent_bg: c(0x09191d),
                green: c(0x91c9a4),
                red: c(0xf08c98),
                amber: c(0xe2bd80),
            }
        }
    }
}
