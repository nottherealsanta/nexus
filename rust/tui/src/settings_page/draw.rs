//! Draws a Settings page with the shared widget kit. Layout is a plain top-to-bottom
//! flow into an off-screen buffer (`scrolled`), so focus, hit rectangles and scrolling
//! share one geometry. Nothing the host sent is dropped: long text wraps, and clipped
//! lists announce `… N more`.
use super::input::PageState;
use super::model::*;
use crate::render::{toasts, Palette};
use nexus_widgets::{
    button_view, callout, controls::TOGGLE_W, feedback::progress, key_hints, lists::*, put,
    put_right, scrolled, segmented, segmented_view, segmented_width, select_popup, select_view,
    setting_row, stepper_view, stepper_width, table, tabs, toggle_view, truncate, width, Button,
    ButtonKind, Glyphs, Tab, Theme, Ui, SCOPE_W,
};
use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Modifier, Style},
    Frame,
};
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

struct Flow {
    x: u16,
    w: u16,
    y: u16,
}
impl Flow {
    fn rect(&mut self, h: u16) -> Rect {
        let r = Rect::new(self.x, self.y, self.w, h);
        self.y += h;
        r
    }
}

struct Ctx<'a> {
    edit: Option<(String, nexus_widgets::TextState)>,
    sections: &'a std::collections::HashMap<String, bool>,
    tables: usize,
}

fn control_width(c: &Control) -> u16 {
    match c {
        Control::Toggle { .. } => TOGGLE_W,
        Control::Segmented { options, .. } => {
            segmented_width(&options.iter().map(String::as_str).collect::<Vec<_>>())
        }
        Control::Select { options, value, .. } => {
            let longest = options
                .iter()
                .map(|(l, _)| width(l))
                .chain(std::iter::once(width(value)))
                .max()
                .unwrap_or(8);
            (longest as u16 + 6).clamp(14, 34)
        }
        Control::Stepper { display, .. } => stepper_width(display),
        Control::Text { .. } => 30,
        Control::Button(b) => nexus_widgets::button_width(&b.label),
        Control::Readout(v) => (width(v) as u16).clamp(1, 40),
    }
}

fn kind_of(v: &str) -> ButtonKind {
    match v {
        "primary" => ButtonKind::Primary,
        "danger" => ButtonKind::Danger,
        "ghost" => ButtonKind::Ghost,
        _ => ButtonKind::Secondary,
    }
}

fn draw_row(buf: &mut Buffer, ui: &mut Ui, f: &mut Flow, r: &Row, cx: &Ctx) {
    let rid = row_id(&r.id);
    let cw = control_width(&r.control);
    let rect = f.rect(setting_row_height(
        f.w,
        &r.label,
        &r.description,
        &r.scope,
        cw,
    ));
    let editing = cx
        .edit
        .as_ref()
        .filter(|(id, _)| *id == rid)
        .map(|(_, t)| t.clone());
    setting_row(
        buf,
        ui,
        rect,
        &rid,
        &r.label,
        &r.description,
        &r.scope,
        cw,
        |b, ui, c, focused| {
            let x = c.x + c.width - cw.min(c.width);
            match &r.control {
                Control::Toggle { on, locked, .. } => {
                    toggle_view(b, ui, x, c.y, *on, !locked.is_empty(), focused)
                }
                Control::Segmented {
                    options, active, ..
                } => {
                    let opts: Vec<&str> = options.iter().map(String::as_str).collect();
                    segmented_view(
                        b,
                        ui,
                        x,
                        c.y,
                        &rid,
                        &opts,
                        *active,
                        focused.then_some(*active),
                    );
                }
                Control::Select { value, .. } => {
                    select_view(b, ui, x, c.y, cw.min(c.width), value, focused, 0.0);
                    ui.hits.add(
                        Rect::new(x, c.y, cw.min(c.width), 1),
                        &rid,
                        nexus_widgets::hit::Part::Named("control".into()),
                    );
                }
                Control::Stepper { display, .. } => {
                    stepper_view(b, ui, x, c.y, &rid, display, focused)
                }
                Control::Text {
                    value,
                    secret,
                    placeholder,
                    ..
                } => {
                    let t = ui.theme;
                    let w = cw.min(c.width);
                    let bg = if focused || editing.is_some() {
                        t.element_hi
                    } else {
                        t.element
                    };
                    let st = Style::default().fg(t.text).bg(bg);
                    nexus_widgets::fill(b, Rect::new(x, c.y, w, 1), st);
                    let shown = match &editing {
                        Some(e) if *secret => "•".repeat(e.value.graphemes(true).count()),
                        Some(e) => e.value.clone(),
                        None if value.is_empty() => String::new(),
                        None if *secret => "•".repeat(value.graphemes(true).count().min(24)),
                        None => value.clone(),
                    };
                    if shown.is_empty() && editing.is_none() {
                        put(
                            b,
                            x + 1,
                            c.y,
                            &truncate(
                                placeholder,
                                w.saturating_sub(2) as usize,
                                ui.glyphs.ellipsis,
                            ),
                            t.dim().bg(bg),
                            w.saturating_sub(2),
                        );
                    } else {
                        put(
                            b,
                            x + 1,
                            c.y,
                            &truncate(&shown, w.saturating_sub(2) as usize, ui.glyphs.ellipsis),
                            st,
                            w.saturating_sub(2),
                        );
                    }
                    if let Some(e) = &editing {
                        let cur = x + 1 + (e.cursor as u16).min(w.saturating_sub(3));
                        b[(cur, c.y)].set_style(st.add_modifier(Modifier::REVERSED));
                    }
                    ui.hits.add(
                        Rect::new(x, c.y, w, 1),
                        &rid,
                        nexus_widgets::hit::Part::Named("control".into()),
                    );
                }
                Control::Button(btn) => {
                    let kind = kind_of(&btn.variant);
                    button_view(
                        b,
                        ui,
                        x,
                        c.y,
                        &Button::new(&rid, &btn.label, kind),
                        focused,
                        0.0,
                    );
                }
                Control::Readout(v) => {
                    put(
                        b,
                        x,
                        c.y,
                        &truncate(v, c.width as usize, ui.glyphs.ellipsis),
                        ui.theme.dim(),
                        c.width,
                    );
                }
            }
        },
    );
    if !r.error.is_empty() {
        let e = f.rect(1);
        put(
            buf,
            e.x + 4,
            e.y,
            &truncate(&format!("! {}", r.error), e.w_minus(5), ui.glyphs.ellipsis),
            Style::default().fg(ui.theme.error),
            e.width,
        );
    }
}

trait WMinus {
    fn w_minus(&self, n: u16) -> usize;
}
impl WMinus for Rect {
    fn w_minus(&self, n: u16) -> usize {
        self.width.saturating_sub(n) as usize
    }
}

fn draw_blocks(buf: &mut Buffer, ui: &mut Ui, f: &mut Flow, blocks: &[Block], cx: &mut Ctx) {
    let t = ui.theme;
    for block in blocks {
        match block {
            Block::Heading(text) => {
                put(
                    buf,
                    f.x,
                    f.y,
                    text,
                    t.strong(Style::default().fg(t.muted).add_modifier(Modifier::BOLD)),
                    f.w,
                );
                f.y += 1;
            }
            Block::Note { text, tone } => {
                let st = match tone.as_str() {
                    "warning" => Style::default().fg(t.warning),
                    "error" => Style::default().fg(t.error),
                    _ => t.dim(),
                };
                for line in wrap(text, f.w.saturating_sub(2) as usize, 4) {
                    put(buf, f.x + 2, f.y, &line, st, f.w.saturating_sub(2));
                    f.y += 1;
                }
            }
            Block::Gap => f.y += 1,
            Block::Row(r) => draw_row(buf, ui, f, r, cx),
            Block::Tabs(tb) => {
                let items: Vec<Tab> = tb
                    .items
                    .iter()
                    .map(|(l, b)| Tab { label: l, badge: b })
                    .collect();
                let rect = f.rect(2);
                tabs(buf, ui, rect, &tabs_id(&tb.id), &items, tb.active);
            }
            Block::Ordered(o) => {
                let items: Vec<OrderedItem> = o
                    .items
                    .iter()
                    .map(|i| OrderedItem {
                        label: &i.label,
                        tag: &i.tag,
                        note: &i.note,
                    })
                    .collect();
                let rect = f.rect(ordered_list_height(items.len(), f.w));
                let add = if o.editable { o.add_label.as_str() } else { "" };
                ordered_list(
                    buf,
                    ui,
                    rect,
                    &ord_id(&o.id),
                    &items,
                    if add.is_empty() { "Add…" } else { add },
                );
            }
            Block::Buttons(bs) => {
                let rect = f.rect(1);
                let mut x = rect.x + 2;
                if !bs.label.is_empty() {
                    x += put(buf, x, rect.y, &bs.label, t.dim(), rect.width) + 2;
                }
                for (k, item) in bs.items.iter().enumerate() {
                    let id = btn_id(&bs.id, k);
                    let b = Button::new(&id, &item.label, kind_of(&item.variant));
                    x += nexus_widgets::button(buf, ui, x, rect.y, &b).rect.width + 1;
                }
            }
            Block::Section(sec) => {
                let open = cx.sections.get(&sec.id).copied().unwrap_or(sec.open);
                let rect = f.rect(1);
                let color = match sec.tone.as_str() {
                    "ok" | "success" => Some(t.success),
                    "warning" => Some(t.warning),
                    "error" => Some(t.error),
                    _ => None,
                };
                section(
                    buf,
                    ui,
                    rect,
                    &sec_id(&sec.id),
                    &sec.title,
                    &sec.summary,
                    color,
                    open,
                );
                if open {
                    draw_blocks(buf, ui, f, &sec.blocks, cx);
                    f.y += 1;
                }
            }
            Block::Table { cols, rows } => {
                cx.tables += 1;
                let c: Vec<(&str, u16)> = cols.iter().map(|(n, w)| (n.as_str(), *w)).collect();
                let rect = f.rect(rows.len() as u16 + 2);
                table(
                    buf,
                    ui,
                    rect,
                    &format!("tbl:{}", cx.tables),
                    &c,
                    rows,
                    usize::MAX,
                    0,
                );
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
    let theme: Theme = toasts::theme(p, light);
    let glyphs = Glyphs::from_env();
    let mut ui = Ui::new(&theme, &glyphs);
    if !st.focus.is_empty() {
        ui.focus.set(&st.focus);
    }
    ui.begin_frame();
    let buf = frame.buffer_mut();
    let t = &theme;
    // Header: title (and scope on pages that can differ per project), then the intro.
    put(
        buf,
        area.x + 2,
        area.y,
        &page.title,
        Style::default().fg(t.text).add_modifier(Modifier::BOLD),
        area.width.saturating_sub(4),
    );
    if let Some(sc) = &page.scope {
        let opts: Vec<&str> = sc.options.iter().map(String::as_str).collect();
        let w = segmented_width(&opts);
        let x = area.x + area.width.saturating_sub(w + 2);
        put(buf, x.saturating_sub(7), area.y, "Scope", t.dim(), 6);
        segmented(buf, &mut ui, x, area.y, "scope", &opts, sc.value);
    }
    let mut y = area.y + 1;
    for line in wrap(&page.intro, area.width.saturating_sub(5) as usize, 2) {
        put(
            buf,
            area.x + 2,
            y,
            &line,
            t.dim(),
            area.width.saturating_sub(4),
        );
        y += 1;
    }
    y += 1;
    let footer_h = 2u16;
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
            x: 2,
            w: r.width.saturating_sub(3),
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
    // Footer: where it is saved, then the keys.
    put(
        buf,
        area.x + 2,
        area.bottom().saturating_sub(2),
        &truncate(&page.footer, area.width.saturating_sub(4) as usize, "…"),
        t.dim(),
        area.width.saturating_sub(4),
    );
    key_hints(
        buf,
        &ui,
        Rect::new(area.x, area.bottom().saturating_sub(1), area.width, 1),
        &[
            ("↑↓", "move"),
            ("←→", "change"),
            ("Space", "toggle"),
            ("Enter", "open"),
            ("Alt+↑↓", "reorder"),
            ("Del", "remove"),
            ("Esc", "close"),
        ],
    );
    ui.end_frame();
    st.focus = ui.focus.current().unwrap_or("").to_string();
    st.order = ui.focus.order().to_vec();
    st.hits = std::mem::take(&mut ui.hits);
    let _ = (put_right, SCOPE_W);
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
            "Models",
            "DEFAULT",
            "Titles",
            "ON",
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
