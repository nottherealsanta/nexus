//! Draws a Settings page with the shared widget kit. Layout is a plain top-to-bottom
//! flow into an off-screen buffer (`scrolled`), so focus, hit rectangles and scrolling
//! share one geometry. Nothing the host sent is dropped: long text wraps, and clipped
//! lists announce `… N more`.
use super::grok::{self, Flow};
use super::input::PageState;
use super::model::*;
use crate::render::{toasts, Palette};
use nexus_widgets::{
    callout, feedback::progress, key_hints, put, scrolled, select_popup, tabs, truncate, width,
    Glyphs, Tab, Theme, Ui,
};
use ratatui::{buffer::Buffer, layout::Rect, style::Style, Frame};
use unicode_segmentation::UnicodeSegmentation;

/// Greedy word wrap into at most `max_lines`; the last line ends with an ellipsis if cut.
pub fn wrap(text: &str, width_: usize, max_lines: usize) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    let mut cur = String::new();
    for word in text.split_whitespace() {
        let w = width(word);
        if !cur.is_empty() && width(&cur) + 1 + w > width_ {
            lines.push(std::mem::take(&mut cur));
        }
        if w > width_ {
            // An oversized token splits at grapheme boundaries instead of overflowing.
            for g in word.graphemes(true) {
                if width(&cur) + width(g) > width_ {
                    lines.push(std::mem::take(&mut cur));
                }
                cur.push_str(g);
            }
            continue;
        }
        if !cur.is_empty() {
            cur.push(' ');
        }
        cur.push_str(word);
    }
    if !cur.is_empty() {
        lines.push(cur);
    }
    if lines.len() > max_lines {
        lines.truncate(max_lines);
        if let Some(last) = lines.last_mut() {
            *last = truncate(&format!("{last}…"), width_, "…");
            if !last.ends_with('…') {
                last.push('…');
            }
        }
    }
    lines
}

struct Ctx<'a> {
    edit: Option<(String, nexus_widgets::TextState)>,
    sections: &'a std::collections::HashMap<String, bool>,
    tables: usize,
}

fn draw_blocks(buf: &mut Buffer, ui: &mut Ui, f: &mut Flow, blocks: &[Block], cx: &mut Ctx) {
    for block in blocks {
        match block {
            Block::Heading(text) => grok::heading(buf, ui, f, text),
            Block::Note { text, tone } => grok::note(buf, ui, f, text, tone),
            Block::Gap => f.y += 1,
            Block::Row(r) => {
                let rid = row_id(&r.id);
                let editing = cx
                    .edit
                    .as_ref()
                    .filter(|(id, _)| *id == rid)
                    .map(|(_, t)| t);
                grok::row(buf, ui, f, r, editing);
            }
            Block::Tabs(tb) => {
                let items: Vec<Tab> = tb
                    .items
                    .iter()
                    .map(|(l, b)| Tab { label: l, badge: b })
                    .collect();
                let rect = f.rect(2);
                tabs(buf, ui, rect, &tabs_id(&tb.id), &items, tb.active);
            }
            Block::Ordered(o) => grok::ordered(buf, ui, f, o),
            Block::Buttons(bs) => grok::buttons(buf, ui, f, bs),
            Block::Section(sec) => {
                let open = cx.sections.get(&sec.id).copied().unwrap_or(sec.open);
                grok::section(buf, ui, f, sec, open);
                if open {
                    draw_blocks(buf, ui, f, &sec.blocks, cx);
                    f.y += 1;
                }
            }
            Block::Table { cols, rows } => {
                cx.tables += 1;
                grok::table(buf, ui, f, &format!("tbl:{}", cx.tables), cols, rows);
            }
            Block::Progress { fraction, label } => {
                let rect = f.rect(1);
                progress(
                    buf,
                    ui,
                    Rect::new(rect.x + 2, rect.y, rect.width.saturating_sub(2), 1),
                    *fraction,
                    label,
                );
            }
            Block::Callout {
                level,
                text,
                action,
            } => {
                let rect = f.rect(1);
                let id = co_id(text);
                callout(
                    buf,
                    ui,
                    rect,
                    &id,
                    toasts::level_of(level),
                    text,
                    action.as_ref().map(|a| a.label.as_str()).unwrap_or(""),
                );
            }
        }
    }
}

/// Keys that do something on this page (Grok footer), ending with how to go back.
fn page_hints(blocks: &[Block]) -> Vec<(&'static str, &'static str)> {
    #[derive(Default)]
    struct F {
        change: bool,
        toggle: bool,
        open: bool,
        reorder: bool,
        tabs: bool,
    }
    fn scan(blocks: &[Block], f: &mut F) {
        for b in blocks {
            match b {
                Block::Row(r) => row(r, f),
                Block::Tabs(_) => f.tabs = true,
                Block::Ordered(o) => {
                    f.open = true;
                    f.reorder |= o.editable;
                    o.controls.iter().for_each(|r| row(r, f));
                }
                Block::Buttons(_)
                | Block::Callout {
                    action: Some(_), ..
                } => f.open = true,
                Block::Section(sec) => {
                    f.open |= sec.collapsible;
                    scan(&sec.blocks, f);
                }
                _ => {}
            }
        }
    }
    fn row(r: &Row, f: &mut F) {
        match &r.control {
            Control::Toggle { .. } => f.toggle = true,
            Control::Segmented { .. } | Control::Stepper { .. } => f.change = true,
            Control::Select { .. } | Control::Text { .. } | Control::Button(_) => f.open = true,
            Control::Readout(_) => {}
        }
    }
    let mut f = F::default();
    scan(blocks, &mut f);
    let mut out = vec![("↑/↓", "nav")];
    if f.change {
        out.push(("←/→", "change"));
    }
    if f.toggle {
        out.push(("Space", "toggle"));
    }
    if f.open {
        out.push(("Enter", "open"));
    }
    if f.reorder {
        out.push(("Alt+↑/↓", "reorder"));
        out.push(("Del", "remove"));
    }
    if f.tabs {
        out.push(("Ctrl+PgUp/PgDn", "tab"));
    }
    out.push(("←/Esc", "back"));
    out
}

/// Draw `page` into `area`. Focus, focus order and hit rectangles are written back to `st`.
pub fn draw(
    frame: &mut Frame,
    area: Rect,
    page: &Page,
    st: &mut PageState,
    p: &Palette,
    light: bool,
) {
    st.reset_for(&page.area);
    let mut theme: Theme = toasts::theme(p, light);
    // Grok row grammar: the focused row is the `element` fill with bold text, no accent bar.
    theme.focus_bg = p.element;
    let mut glyphs = Glyphs::from_env();
    glyphs.focus_bar = " ";
    let mut ui = Ui::new(&theme, &glyphs);
    if !st.focus.is_empty() {
        ui.focus.set(&st.focus);
    }
    ui.begin_frame();
    if st.popup.is_none() && st.edit.is_none() {
        if let Some(id) = &st.hover {
            ui.hover.insert(id.clone(), 1.0);
        }
    }
    let buf = frame.buffer_mut();
    let t = &theme;
    // Header: the page title lives in the modal frame (`Settings › Area`); only the scope
    // switch (pages that can differ per project) and the intro are drawn here.
    if let Some(sc) = &page.scope {
        let (w, x) = (0, 0);
        let _ = (w, x);
        grok::scope(buf, &mut ui, area, sc);
    }
    let mut y = area.y + u16::from(page.scope.is_some());
    for line in wrap(&page.intro, area.width.saturating_sub(5) as usize, 2) {
        put(buf, area.x, y, &line, t.quiet_style(), area.width);
        y += 1;
    }
    y += 1;
    // Footer: the saved-where line, then only the keys that work on this page.
    let hint_rows =
        crate::settings_page::root::hint_lines(&page_hints(&page.blocks), area.width as usize, p);
    let footer_h = 1 + hint_rows.len() as u16;
    let body = Rect::new(
        area.x,
        y,
        area.width,
        area.bottom().saturating_sub(y + footer_h),
    );
    let mut scroll = st.scroll;
    let mut cx = Ctx {
        edit: st.edit.as_ref().map(|e| (e.id.clone(), e.text.clone())),
        sections: &st.sections,
        tables: 0,
    };
    let blocks = &page.blocks;
    scrolled(buf, &mut ui, body, &mut scroll, |b, ui, r| {
        let mut f = Flow {
            x: 0,
            w: r.width,
            y: 0,
        };
        draw_blocks(b, ui, &mut f, blocks, &mut cx);
        f.y + 1
    });
    st.scroll = scroll;
    if let Some(popup) = &st.popup {
        if let Some(Target::Row(Row {
            control: Control::Select { options, value, .. },
            ..
        })) = page.find(&popup.id)
        {
            if let Some(anchor) = ui.hits.rect_of_part(&popup.id, "control") {
                // The value is quiet text now, so widen the anchor to fit the longest option.
                let longest = options
                    .iter()
                    .map(|(l, _)| width(l) as u16)
                    .max()
                    .unwrap_or(0)
                    + 4;
                let grow = longest.saturating_sub(anchor.width);
                let anchor = Rect::new(
                    anchor.x.saturating_sub(grow),
                    anchor.y,
                    anchor.width + grow,
                    anchor.height,
                );
                let labels: Vec<&str> = options.iter().map(|(l, _)| l.as_str()).collect();
                let current = options.iter().position(|(l, _)| l == value).unwrap_or(0);
                select_popup(
                    buf,
                    &mut ui,
                    area,
                    anchor,
                    &format!("popup:{}", popup.id),
                    &labels,
                    current,
                    popup.highlighted,
                    8,
                );
            }
        }
    }
    grok::tip(
        buf,
        &ui,
        area,
        area.bottom().saturating_sub(footer_h),
        &page.footer,
    );
    let _ = (key_hints, truncate, width);
    frame.render_widget(
        ratatui::widgets::Paragraph::new(hint_rows.clone()).style(Style::default().bg(p.dialog)),
        Rect::new(
            area.x,
            area.bottom().saturating_sub(hint_rows.len() as u16),
            area.width,
            hint_rows.len() as u16,
        ),
    );
    ui.end_frame();
    st.focus = ui.focus.current().unwrap_or("").to_string();
    st.order = ui.focus.order().to_vec();
    st.hits = std::mem::take(&mut ui.hits);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::settings_page::model::tests::sample;
    use ratatui::{backend::TestBackend, Terminal};

    fn render(page: &Page, st: &mut PageState, w: u16, h: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
        let p = Palette::new(false);
        terminal
            .draw(|f| draw(f, f.area(), page, st, &p, false))
            .unwrap();
        let buf = terminal.backend().buffer();
        (0..h)
            .map(|y| {
                (0..w)
                    .map(|x| buf[(x, y)].symbol().to_string())
                    .collect::<String>()
                    + "\n"
            })
            .collect()
    }

    #[test]
    fn the_page_shows_every_control_with_text_not_only_colour() {
        let page = Page::from_value(&sample());
        let mut st = PageState::default();
        let screen = render(&page, &mut st, 100, 30);
        for want in [
            "DEFAULT",
            "Titles",
            "on",
            "Effort",
            "Low",
            "High",
            "Device",
            "auto",
            "in use",
            "skipped",
            "no key",
            "Add model",
            "OpenAI",
            "unsupported settings block",
        ] {
            assert!(screen.contains(want), "{want} missing:\n{screen}");
        }
        assert!(screen.contains("Saved in ~/.nexus/config.toml"), "{screen}");
    }

    #[test]
    fn hover_tints_enabled_rows_only_and_stays_distinct_from_focus() {
        use crate::settings_page::input::hover;
        let value = serde_json::json!({"area": "x", "blocks": [
            {"t": "row", "id": "a", "label": "A", "control": {"c": "toggle", "on": true, "locked": "", "operation": {"kind": "a"}}},
            {"t": "row", "id": "b", "label": "B", "control": {"c": "toggle", "on": true, "locked": "managed", "operation": {"kind": "b"}}},
            {"t": "row", "id": "c", "label": "C", "control": {"c": "readout", "value": "ro"}}
        ]});
        let page = Page::from_value(&value);
        let mut st = PageState::default();
        render(&page, &mut st, 80, 20);
        let at = |st: &PageState, id: &str| {
            let r = st.hits.rect_of(id).unwrap();
            (r.x + 3, r.y)
        };
        let bg = |st: &mut PageState, id: &str| {
            let mut t = Terminal::new(TestBackend::new(80, 20)).unwrap();
            let p = Palette::new(false);
            t.draw(|f| draw(f, f.area(), &page, st, &p, false)).unwrap();
            let r = st.hits.rect_of(id).unwrap();
            t.backend().buffer()[(r.x + 3, r.y)].bg
        };
        let (a, b, c) = (at(&st, "row:a"), at(&st, "row:b"), at(&st, "row:c"));
        assert_eq!(st.focus, "row:a");
        let focused = bg(&mut st, "row:b");
        let idle = bg(&mut st, "row:b");
        assert_eq!(focused, idle);
        assert!(
            hover(&page, &mut st, Some(b)) == false,
            "locked row never hovers"
        );
        assert!(
            !hover(&page, &mut st, Some(c)),
            "read-only row never hovers"
        );
        assert!(hover(&page, &mut st, Some(a)));
        assert!(
            !hover(&page, &mut st, Some(a)),
            "unchanged target is not a redraw"
        );
        st.focus = "row:b".into();
        let hovered_unfocused = bg(&mut st, "row:a");
        st.hover = None;
        let plain = bg(&mut st, "row:a");
        assert_ne!(hovered_unfocused, plain, "hover changes the row");
        assert_ne!(
            hovered_unfocused,
            bg(&mut st, "row:b"),
            "hover differs from focus"
        );
        // A popup/edit invalidates hover, and leaving the page clears it.
        hover(&page, &mut st, Some(a));
        st.edit = Some(crate::settings_page::input::Edit {
            id: "row:a".into(),
            text: Default::default(),
        });
        assert!(hover(&page, &mut st, Some(a)));
        assert!(st.hover.is_none());
        st.edit = None;
        hover(&page, &mut st, Some(a));
        assert!(hover(&page, &mut st, None));
    }

    #[test]
    fn drawing_registers_one_focus_stop_per_control_and_hits_for_the_mouse() {
        let page = Page::from_value(&sample());
        let mut st = PageState::default();
        render(&page, &mut st, 100, 40);
        assert_eq!(
            &st.order[..4],
            &["row:titles", "row:eff", "row:dev", "tabs:tiers"]
        );
        assert!(
            st.order.contains(&"ord:tier:low:1".to_string())
                && st.order.contains(&"ord:tier:low:add".to_string())
        );
        assert!(
            !st.order.contains(&"btn:act:0".to_string()),
            "a closed section hides its stops"
        );
        assert_eq!(
            st.focus, "row:titles",
            "the first stop holds focus until the user moves it"
        );
        assert!(st.hits.rect_of("row:eff").is_some());
    }

    #[test]
    fn inline_effort_is_left_of_arrows_and_models_are_two_rows_at_any_width() {
        let value = serde_json::json!({"area": "models", "blocks": [{
            "t": "ordered", "id": "tier:low", "items": [
                ["antigravity/gemini-3.1-flash-high", "in use", "subagents only"],
                ["codex/gpt-6-luna", "fallback", ""]
            ], "controls": [
                {"t": "row", "id": "effort:0", "control": {"c": "select", "value": "Default", "options": [["Default", null], ["High", "high"]], "operation": {"kind": "effort"}}},
                {"t": "row", "id": "effort:1", "control": {"c": "select", "value": "High", "options": [["High", "high"]], "operation": {"kind": "effort"}}}
            ], "operation": {"kind": "tier"}
        }]});
        let page = Page::from_value(&value);
        for width in [60, 100, 150] {
            let mut st = PageState::default();
            st.focus = "ord:tier:low:0".into();
            render(&page, &mut st, width, 24); // the first draw of an area resets state
            st.focus = "ord:tier:low:0".into();
            render(&page, &mut st, width, 24);
            let first = st.hits.rect_of("ord:tier:low:0").unwrap();
            let second = st.hits.rect_of("ord:tier:low:1").unwrap();
            let effort = st.hits.rect_of("row:effort:0").unwrap();
            let screen = render(&page, &mut st, width, 24);
            assert_eq!(first.height, 1);
            assert_eq!(
                second.y - first.y,
                2,
                "the selected row's note takes a line"
            );
            assert_eq!(effort.y, first.y);
            assert_eq!(effort.right(), first.right() - 11);
            assert!(
                st.hits.rect_of("row:effort:1").is_none(),
                "effort shows on the selected row only"
            );
            assert!(screen.contains("Default"));
            st.focus = "row:effort:0".into();
            assert_eq!(
                crate::settings_page::input::key(
                    &page,
                    &mut st,
                    crossterm::event::KeyEvent::new(
                        crossterm::event::KeyCode::Enter,
                        crossterm::event::KeyModifiers::NONE
                    )
                ),
                crate::settings_page::input::Act::Done
            );
            assert!(render(&page, &mut st, width, 24).contains("High"));
            st.popup = None;
            render(&page, &mut st, width, 24);
            assert_eq!(
                crate::settings_page::input::click(&page, &mut st, effort.x, effort.y),
                crate::settings_page::input::Act::Done
            );
            assert_eq!(st.popup.as_ref().unwrap().id, "row:effort:0");
            crate::settings_page::input::key(
                &page,
                &mut st,
                crossterm::event::KeyEvent::new(
                    crossterm::event::KeyCode::Down,
                    crossterm::event::KeyModifiers::NONE,
                ),
            );
            assert_eq!(
                crate::settings_page::input::key(
                    &page,
                    &mut st,
                    crossterm::event::KeyEvent::new(
                        crossterm::event::KeyCode::Enter,
                        crossterm::event::KeyModifiers::NONE
                    )
                ),
                crate::settings_page::input::Act::Send(
                    serde_json::json!({"kind": "effort", "value": "high"})
                )
            );
        }
    }

    #[test]
    fn a_closed_section_opens_and_exposes_its_buttons() {
        let page = Page::from_value(&sample());
        let mut st = PageState::default();
        st.area = "models".into(); // a draw for a new area starts with fresh state
        st.sections.insert("prov".into(), true);
        let screen = render(&page, &mut st, 100, 60);
        assert!(screen.contains("Disconnect"), "{screen}");
        assert!(st.order.contains(&"btn:act:0".to_string()));
    }

    #[test]
    fn a_select_popup_lists_the_options_over_the_page() {
        let page = Page::from_value(&sample());
        let mut st = PageState::default();
        render(&page, &mut st, 100, 40);
        st.focus = "row:dev".into();
        st.popup = Some(crate::settings_page::input::Popup {
            id: "row:dev".into(),
            highlighted: 1,
        });
        let screen = render(&page, &mut st, 100, 40);
        assert!(screen.contains("cpu"), "{screen}");
    }

    #[test]
    fn long_pages_scroll_to_keep_focus_visible_and_narrow_widths_never_overflow() {
        let mut v = sample();
        let blocks = v["blocks"].as_array_mut().unwrap();
        for i in 0..60 {
            blocks.push(serde_json::json!({"t": "row", "id": format!("r{i}"), "label": format!("Row number {i}"), "control": {"c": "toggle", "on": false, "operation": {"kind": "x"}}}));
        }
        let page = Page::from_value(&v);
        let mut st = PageState::default();
        render(&page, &mut st, 80, 24);
        st.focus = "row:r55".into();
        let screen = render(&page, &mut st, 80, 24);
        assert!(
            screen.contains("Row number 55"),
            "focus scrolled into view:\n{screen}"
        );
        assert!(st.scroll.offset > 0);
        assert!(screen.lines().all(|l| l.chars().count() <= 80));
    }

    #[test]
    fn wrap_breaks_on_words_splits_oversized_tokens_and_announces_cuts() {
        assert_eq!(
            wrap("one two three four", 9, 4),
            vec!["one two", "three", "four"]
        );
        let long = wrap(&"x".repeat(25), 10, 4);
        assert_eq!(long.len(), 3);
        assert!(long.iter().all(|l| width(l) <= 10));
        let cut = wrap("a b c d e f g h i j", 3, 2);
        assert_eq!(cut.len(), 2);
        assert!(cut[1].ends_with('…'));
    }
}
