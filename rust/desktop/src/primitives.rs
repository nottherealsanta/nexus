//! Small reusable GPUI building blocks for consistent desktop panels.
use crate::theme::Theme;
use gpui::prelude::*;
use gpui::*;

/// Accessible, compact section label for settings and grouped pickers.
pub fn section_label(label: impl Into<SharedString>, theme: Theme) -> impl IntoElement {
    let label: SharedString = label.into();
    div()
        .px_2()
        .py_1()
        .text_size(px(crate::theme::size::CAPTION))
        .font_weight(FontWeight::SEMIBOLD)
        .text_color(theme.muted)
        .child(label.to_string())
}

/// Reusable filled control with a visible hover and focus treatment.
pub fn control(label: impl Into<SharedString>, theme: Theme) -> impl IntoElement {
    let label: SharedString = label.into();
    div()
        .px_3()
        .py_2()
        .rounded(px(crate::theme::radius::CONTROL))
        .bg(theme.raised)
        .text_color(theme.text)
        .hover(move |style| style.bg(theme.accent_bg))
        .focus(move |style| style.border_1().border_color(theme.focus_ring))
        .child(label.to_string())
}

/// Convert a potentially large row collection into a bounded viewport window.
/// The returned range includes a small overscan for smooth row-by-row scrolling.
pub fn visible_range(
    total: usize,
    first: usize,
    viewport_rows: usize,
    overscan: usize,
) -> std::ops::Range<usize> {
    if total == 0 {
        return 0..0;
    }
    let start = first.saturating_sub(overscan).min(total);
    let end = first
        .saturating_add(viewport_rows)
        .saturating_add(overscan)
        .min(total);
    start..end.max(start)
}

#[cfg(test)]
mod tests {
    use super::visible_range;

    #[test]
    fn virtual_range_is_bounded_and_overscanned() {
        assert_eq!(visible_range(1_000, 100, 20, 3), 97..123);
        assert_eq!(visible_range(10, 0, 4, 2), 0..6);
        assert_eq!(visible_range(10, 9, 4, 2), 7..10);
        assert_eq!(visible_range(0, 0, 20, 2), 0..0);
    }
}
