//! Grok row grammar for Settings subpages (plan GROK_TUI_PLAN WS-C), the same look as the
//! root list in `root.rs`: `Title ───` headings, `▸ label … value` rows with the `element`
//! fill on the selected one, `on`/`off` toggles, `value ›` selects, ` / ` segmented
//! controls, link-style buttons, `▾ Name … status` groups, numbered model lists whose
//! ops show on the selected row only, and quiet tables. No accent fills anywhere.
//! Every focus id and hit part is the one the input handler already knows.
use super::draw::wrap;
use super::model::*;
use nexus_widgets::{
    fill, hit::Part, put, put_right, row_style, truncate, width, Theme, Ui,
};
use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
};
use unicode_segmentation::UnicodeSegmentation;

/// Top-to-bottom layout cursor.
pub struct Flow {
    pub x: u16,
    pub w: u16,
    pub y: u16,
}

impl Flow {
    pub fn rect(&mut self, h: u16) -> Rect {
        let r = Rect::new(self.x, self.y, self.w, h);
        self.y += h;
        r
    }
}

/// One styled run of a row's value with an optional mouse target.
struct Span {
    text: String,
    style: Style,
    part: Option<Part>,
}

fn sp(text: impl Into<String>, style: Style) -> Span {
    Span { text: text.into(), style, part: None }
}
fn sp_part(text: impl Into<String>, style: Style, part: &str) -> Span {
    Span { text: text.into(), style, part: Some(Part::Named(part.into())) }
}
fn spans_width(spans: &[Span]) -> u16 {
    spans.iter().map(|s| width(&s.text) as u16).sum()
}

fn text(t: &Theme) -> Style {
    Style::default().fg(t.text)
}
fn bold(t: &Theme) -> Style {
    text(t).add_modifier(Modifier::BOLD)
}
fn quiet(t: &Theme) -> Style {
    t.quiet_style()
}
fn success(t: &Theme) -> Style {
    Style::default().fg(t.success)
}

/// Draw spans so they end at `right` (exclusive), registering their hit parts.
fn put_spans_right(buf: &mut Buffer, ui: &mut Ui, id: &str, right: u16, y: u16, spans: &[Span], bg: Color) {
    let mut x = right.saturating_sub(spans_width(spans));
    for s in spans {
        let w = width(&s.text) as u16;
        put(buf, x, y, &s.text, s.style.bg(bg), w);
        if let Some(part) = &s.part {
            ui.hits.add(Rect::new(x, y, w, 1), id, part.clone());
        }
        x += w;
    }
}

/// `Title · sub ─────`: bold muted title, quiet sub-label, rule to the right edge.
pub fn heading(buf: &mut Buffer, ui: &Ui, f: &mut Flow, label: &str) {
    let t = ui.theme;
    let (title, sub) = match label.split_once(" · ") {
        Some((a, b)) => (a, Some(b)),
        None => (label, None),
    };
    let mut x = f.x;
    let end = f.x + f.w;
    x += put(
        buf,
        x,
        f.y,
        title,
        Style::default().fg(t.muted).add_modifier(Modifier::BOLD),
        end.saturating_sub(x),
    );
    if let Some(sub) = sub {
        x += put(buf, x, f.y, &format!(" · {sub}"), quiet(t), end.saturating_sub(x));
    }
    let rule = end.saturating_sub(x + 1) as usize;
    if rule > 0 {
        put(
            buf,
            x + 1,
            f.y,
            &ui.glyphs.rule.repeat(rule),
            Style::default().fg(t.border),
            rule as u16,
        );
    }
    f.y += 1;
}

/// A quiet note line (errors stay red; "warning" is not louder than any other note).
pub fn note(buf: &mut Buffer, ui: &Ui, f: &mut Flow, text_: &str, tone: &str) {
    let t = ui.theme;
    let st = if tone == "error" { Style::default().fg(t.error) } else { quiet(t) };
    for line in wrap(text_, f.w.saturating_sub(2) as usize, 4) {
        put(buf, f.x + 2, f.y, &line, st, f.w.saturating_sub(2));
        f.y += 1;
    }
}

/// Quiet description lines under a selected row.
fn description(buf: &mut Buffer, ui: &Ui, f: &mut Flow, lines: &[String]) {
    for line in lines {
        put(buf, f.x + 4, f.y, line, quiet(ui.theme), f.w.saturating_sub(4));
        f.y += 1;
    }
}

fn desc_lines(desc: &str, w: u16) -> Vec<String> {
    if desc.is_empty() {
        Vec::new()
    } else {
        wrap(desc, w.saturating_sub(5) as usize, 3)
    }
}

/// Create-style buttons read as `+ New file` in green.
fn is_create(label: &str) -> bool {
    label.starts_with('+') || label.starts_with("New ") || label.starts_with("Add ")
}

fn button_spans(t: &Theme, label: &str, variant: &str) -> Vec<Span> {
    if is_create(label) {
        let l = if label.starts_with('+') { label.to_string() } else { format!("+ {label}") };
        vec![sp(l, success(t))]
    } else if variant == "danger" {
        vec![sp(format!("{label} ›"), Style::default().fg(t.error))]
    } else {
        vec![sp(format!("{label} ›"), quiet(t))]
    }
}

fn segmented_spans(t: &Theme, options: &[String], active: usize, focused: bool) -> Vec<Span> {
    let mut out = Vec::new();
    for (i, o) in options.iter().enumerate() {
        if i > 0 {
            out.push(sp(" / ", quiet(t)));
        }
        let mut st = if i == active { bold(t) } else { quiet(t) };
        if focused && i == active {
            st = st.add_modifier(Modifier::UNDERLINED);
        }
        out.push(sp_part(o.clone(), st, &format!("segment:{i}")));
    }
    out
}

fn control_spans(ui: &Ui, c: &Control, focused: bool) -> Vec<Span> {
    let t = ui.theme;
    match c {
        Control::Toggle { on, locked, .. } => {
            if !locked.is_empty() {
                vec![sp("locked", quiet(t))]
            } else if *on {
                vec![sp("on", text(t))]
            } else {
                vec![sp("off", quiet(t))]
            }
        }
        Control::Segmented { options, active, .. } => segmented_spans(t, options, *active, focused),
        Control::Select { value, .. } => vec![sp_part(format!("{} ›", truncate(value, 30, ui.glyphs.ellipsis)), text(t), "control")],
        Control::Stepper { display, .. } => vec![
            sp_part("‹ ", quiet(t), "dec"),
            sp(display.clone(), text(t)),
            sp_part(" ›", quiet(t), "inc"),
        ],
        Control::Text { value, secret, placeholder, .. } => {
            if value.is_empty() {
                vec![sp_part(format!("{} ›", truncate(placeholder, 28, ui.glyphs.ellipsis)), quiet(t), "control")]
            } else {
                let shown = if *secret { "•".repeat(value.graphemes(true).count().min(24)) } else { truncate(value, 28, ui.glyphs.ellipsis) };
                vec![sp_part(format!("{shown} ›"), text(t), "control")]
            }
        }
        Control::Button(b) => button_spans(t, &b.label, &b.variant),
        Control::Readout(v) => vec![sp(truncate(v, 40, ui.glyphs.ellipsis), quiet(t))],
    }
}

/// One setting row: `▸ label … value`, description under it when selected.
pub fn row(
    buf: &mut Buffer,
    ui: &mut Ui,
    f: &mut Flow,
    r: &Row,
    edit: Option<&nexus_widgets::TextState>,
) {
    let rid = row_id(&r.id);
    let t = ui.theme;
    let focused = ui.focus.is(&rid);
    let spans = if edit.is_some() { Vec::new() } else { control_spans(ui, &r.control, focused) };
    let cw = if edit.is_some() { 30 } else { spans_width(&spans) };
    let scope_w = if r.scope.is_empty() { 0 } else { width(&r.scope) as u16 + 3 };
    let stack = width(&r.label) as u16 + 4 + scope_w + cw + 2 > f.w;
    let h = 1 + u16::from(stack);
    let rect = Rect::new(f.x, f.y, f.w, h);
    let resp = ui.stop(&rid, rect);
    let st = row_style(ui, &rid, resp.focused, t.bg);
    fill(buf, rect, st);
    let bg = st.bg.unwrap_or(t.bg);
    put(buf, f.x, f.y, "▸ ", quiet(t).bg(bg), 2);
    let label_st = if resp.focused { st.add_modifier(Modifier::BOLD) } else { st };
    let lw = if stack { f.w.saturating_sub(4 + scope_w) } else { f.w.saturating_sub(cw + 5 + scope_w) };
    let used = put(buf, f.x + 2, f.y, &truncate(&r.label, lw as usize, ui.glyphs.ellipsis), label_st, lw);
    if scope_w > 0 {
        put(buf, f.x + 2 + used, f.y, &format!(" · {}", r.scope), quiet(t).bg(bg), scope_w);
    }
    let vy = f.y + u16::from(stack);
    let right = f.x + f.w - 1;
    if let Some(e) = edit {
        let secret = matches!(&r.control, Control::Text { secret: true, .. });
        let x = right.saturating_sub(30);
        let est = Style::default().fg(t.text).bg(t.element_hi);
        fill(buf, Rect::new(x, vy, 30, 1), est);
        let shown = if secret { "•".repeat(e.value.graphemes(true).count()) } else { e.value.clone() };
        put(buf, x + 1, vy, &truncate(&shown, 27, ui.glyphs.ellipsis), est, 28);
        let cur = x + 1 + (e.cursor as u16).min(27);
        buf[(cur, vy)].set_style(est.add_modifier(Modifier::REVERSED));
        ui.hits.add(Rect::new(x, vy, 30, 1), &rid, Part::Named("control".into()));
    } else {
        put_spans_right(buf, ui, &rid, right, vy, &spans, bg);
    }
    f.y += h;
    if !r.error.is_empty() {
        put(
            buf,
            f.x + 4,
            f.y,
            &truncate(&format!("! {}", r.error), f.w.saturating_sub(5) as usize, ui.glyphs.ellipsis),
            Style::default().fg(t.error),
            f.w.saturating_sub(4),
        );
        f.y += 1;
    }
    if resp.focused {
        description(buf, ui, f, &desc_lines(&r.description, f.w));
    }
}

/// A row of link-style buttons, one line each.
pub fn buttons(buf: &mut Buffer, ui: &mut Ui, f: &mut Flow, bs: &Buttons) {
    let t = ui.theme;
    for (k, item) in bs.items.iter().enumerate() {
        let id = btn_id(&bs.id, k);
        let rect = Rect::new(f.x, f.y, f.w, 1);
        let resp = ui.stop(&id, rect);
        let st = row_style(ui, &id, resp.focused, t.bg);
        fill(buf, rect, st);
        let bg = st.bg.unwrap_or(t.bg);
        let spans = button_spans(t, &item.label, &item.variant);
        let create = is_create(&item.label);
        let weight = if resp.focused { Modifier::BOLD } else { Modifier::empty() };
        if create && bs.label.is_empty() {
            put(buf, f.x, f.y, &spans[0].text, spans[0].style.bg(bg).add_modifier(weight), f.w);
        } else if bs.label.is_empty() {
            put(buf, f.x, f.y, "▸ ", quiet(t).bg(bg), 2);
            let label = if item.variant == "danger" { Style::default().fg(t.error) } else { text(t) };
            let lw = f.w.saturating_sub(6);
            put(buf, f.x + 2, f.y, &truncate(&item.label, lw as usize, ui.glyphs.ellipsis), label.bg(bg).add_modifier(weight), lw);
            put_right(buf, f.x + f.w - 1, f.y, "›", quiet(t).bg(bg));
        } else {
            put(buf, f.x, f.y, "▸ ", quiet(t).bg(bg), 2);
            put(buf, f.x + 2, f.y, &truncate(&bs.label, 20, ui.glyphs.ellipsis), st.add_modifier(weight), 20);
            put_spans_right(buf, ui, &id, f.x + f.w - 1, f.y, &spans, bg);
        }
        f.y += 1;
    }
}

/// `▾ Name … status`, the caret quiet, status coloured by tone (green/red, else quiet).
pub fn section(buf: &mut Buffer, ui: &mut Ui, f: &mut Flow, sec: &Section, open: bool) {
    let t = ui.theme;
    let id = sec_id(&sec.id);
    let rect = Rect::new(f.x, f.y, f.w, 1);
    let resp = ui.stop(&id, rect);
    let st = row_style(ui, &id, resp.focused, t.bg);
    fill(buf, rect, st);
    let bg = st.bg.unwrap_or(t.bg);
    let caret = if open { "▾ " } else { "▸ " };
    put(buf, f.x, f.y, caret, quiet(t).bg(bg), 2);
    let label_st = if resp.focused { st.add_modifier(Modifier::BOLD) } else { st };
    let sw = width(&sec.summary) as u16;
    let lw = f.w.saturating_sub(sw + 5);
    put(buf, f.x + 2, f.y, &truncate(&sec.title, lw as usize, ui.glyphs.ellipsis), label_st, lw);
    if sw > 0 {
        let color = match sec.tone.as_str() {
            "ok" | "success" => Some(t.success),
            "error" => Some(t.error),
            _ => None,
        };
        let sst = color.map_or_else(|| quiet(t), |c| Style::default().fg(c)).bg(bg);
        let room = f.w.saturating_sub(5 + width(&sec.title) as u16);
        put_right(buf, f.x + f.w - 1, f.y, &truncate(&sec.summary, room as usize, ui.glyphs.ellipsis), sst);
    }
    f.y += 1;
}

/// Numbered model list. Ops (effort picker, ↑ ↓ ✕) show on the selected or hovered row.
pub fn ordered(buf: &mut Buffer, ui: &mut Ui, f: &mut Flow, o: &Ordered) {
    let t = ui.theme;
    let g = ui.glyphs;
    let oid = ord_id(&o.id);
    let label_w = o
        .items
        .iter()
        .map(|i| width(&i.label))
        .max()
        .unwrap_or(0)
        .min(f.w.saturating_sub(24) as usize);
    let right = f.x + f.w - 1;
    let mut eff_at: Vec<Option<u16>> = vec![None; o.items.len()];
    let mut ys: Vec<u16> = Vec::new();
    for (i, it) in o.items.iter().enumerate() {
        let rid = format!("{oid}:{i}");
        let eff = o.controls.get(i);
        let eff_id = eff.map(|r| row_id(&r.id));
        let focused = ui.focus.is(&rid);
        let eff_focused = eff_id.as_deref().is_some_and(|id| ui.focus.is(id));
        let hovered = ui.hover_of(&rid) > 0.5;
        let show_ops = focused || eff_focused || hovered;
        ys.push(f.y);
        let rect = Rect::new(f.x, f.y, f.w, 1);
        let resp = ui.stop(&rid, rect);
        let st = row_style(ui, &rid, resp.focused || eff_focused, t.bg);
        fill(buf, rect, st);
        let bg = st.bg.unwrap_or(t.bg);
        put(buf, f.x, f.y, &(i + 1).to_string(), quiet(t).bg(bg), 2);
        // Ops, right to left: ✕ ↓ ↑ then the effort value.
        let mut ops_left = right + 1;
        if show_ops {
            if o.editable {
                ops_left = right.saturating_sub(8);
                for (k, (name, gl)) in [("up", g.up), ("down", g.down), ("remove", "✕")].iter().enumerate() {
                    let x = ops_left + k as u16 * 3;
                    let s = if *name == "remove" { Style::default().fg(t.error) } else { quiet(t) };
                    put(buf, x, f.y, &format!(" {gl} "), s.bg(bg), 3);
                    ui.hits.add(Rect::new(x, f.y, 3, 1), &rid, Part::Named((*name).into()));
                }
            }
            if let Some(Control::Select { value, .. }) = eff.map(|r| &r.control) {
                let txt = format!("{} ›", truncate(value, 20, g.ellipsis));
                let w = width(&txt) as u16;
                let x = ops_left.saturating_sub(w + 2);
                eff_at[i] = Some(x);
                ops_left = x;
            }
        }
        let tag = if !it.tag.is_empty() {
            it.tag.as_str()
        } else if i == 0 {
            "in use"
        } else {
            "fallback"
        };
        let lw = (label_w as u16).min(ops_left.saturating_sub(f.x + 5));
        let label_st = if resp.focused { st.add_modifier(Modifier::BOLD) } else { st };
        put(buf, f.x + 3, f.y, &truncate(&it.label, lw as usize, g.ellipsis), label_st, lw);
        let tx = f.x + 3 + label_w as u16 + 2;
        let room = ops_left.saturating_sub(tx + 1);
        let tag_st = if tag == "in use" { success(t) } else { quiet(t) }.bg(bg);
        let used = put(buf, tx, f.y, &truncate(tag, room as usize, g.ellipsis), tag_st, room);
        if !it.note.is_empty() && !focused {
            let nroom = room.saturating_sub(used);
            put(buf, tx + used, f.y, &truncate(&format!(" · {}", it.note), nroom as usize, g.ellipsis), quiet(t).bg(bg), nroom);
        }
        f.y += 1;
        if focused && !it.note.is_empty() {
            for line in wrap(&it.note, f.w.saturating_sub(7) as usize, 2) {
                put(buf, f.x + 3, f.y, &line, quiet(t), f.w.saturating_sub(3));
                f.y += 1;
            }
        }
    }
    // The add row.
    let aid = format!("{oid}:add");
    let rect = Rect::new(f.x, f.y, f.w, 1);
    let resp = ui.stop(&aid, rect);
    let st = row_style(ui, &aid, resp.focused, t.bg);
    fill(buf, rect, st);
    let label = if o.add_label.is_empty() { "Add…" } else { o.add_label.as_str() };
    let label = if label.starts_with('+') { label.to_string() } else { format!("+ {label}") };
    let weight = if resp.focused { Modifier::BOLD } else { Modifier::empty() };
    put(buf, f.x + 3, f.y, &label, success(t).bg(st.bg.unwrap_or(t.bg)).add_modifier(weight), f.w.saturating_sub(3));
    f.y += 1;
    // Effort pickers register after the list (focus order is unchanged) and draw at their row.
    for (i, row) in o.controls.iter().take(o.items.len()).enumerate() {
        let id = row_id(&row.id);
        let Some(x) = eff_at[i] else {
            ui.focus.register(&id);
            continue;
        };
        let Control::Select { value, .. } = &row.control else {
            ui.focus.register(&id);
            continue;
        };
        let y = ys[i];
        let txt = format!("{} ›", truncate(value, 20, g.ellipsis));
        let w = width(&txt) as u16;
        let resp = ui.stop(&id, Rect::new(x, y, w, 1));
        let mut s = text(t);
        if resp.focused {
            s = s.add_modifier(Modifier::BOLD | Modifier::UNDERLINED);
        }
        // Keep the row fill behind the value.
        let bg = buf[(x, y)].bg;
        put(buf, x, y, &txt, s.bg(bg), w);
        ui.hits.add(Rect::new(x, y, w, 1), &id, Part::Named("control".into()));
    }
}

/// Key/value style table: header row quiet, keys column bold, rows selectable.
pub fn table(
    buf: &mut Buffer,
    ui: &mut Ui,
    f: &mut Flow,
    id: &str,
    cols: &[(String, u16)],
    rows: &[Vec<String>],
) {
    let t = ui.theme;
    let mut fixed: Vec<u16> = cols.iter().map(|c| c.1).collect();
    let mut shown: Vec<usize> = (0..cols.len()).collect();
    let flex_of = |shown: &[usize], fixed: &[u16]| {
        let flexes = shown.iter().filter(|&&i| fixed[i] == 0).count().max(1) as u16;
        let used: u16 = shown.iter().map(|&i| fixed[i]).sum::<u16>() + shown.len() as u16 + 2;
        f.w.saturating_sub(used) / flexes
    };
    while flex_of(&shown, &fixed) < 20 && shown.len() > 1 {
        match shown.iter().copied().filter(|&i| fixed[i] > 8).max_by_key(|&i| fixed[i]) {
            Some(i) => fixed[i] -= 1,
            None => {
                shown.pop();
            }
        }
    }
    let flex = flex_of(&shown, &fixed).max(4);
    let widths: Vec<u16> = shown.iter().map(|&i| if fixed[i] == 0 { flex } else { fixed[i] }).collect();
    let mut x = f.x + 2;
    for (&i, w) in shown.iter().zip(&widths) {
        put(buf, x, f.y, &cols[i].0, quiet(t), *w);
        x += w + 1;
    }
    f.y += 1;
    for (n, row) in rows.iter().enumerate() {
        let rid = format!("{id}:{n}");
        let rect = Rect::new(f.x, f.y, f.w, 1);
        let resp = ui.stop(&rid, rect);
        let st = row_style(ui, &rid, resp.focused, t.bg);
        fill(buf, rect, st);
        let bg = st.bg.unwrap_or(t.bg);
        put(buf, f.x, f.y, "▸ ", quiet(t).bg(bg), 2);
        let mut x = f.x + 2;
        for (&c, w) in shown.iter().zip(&widths) {
            let cell = row.get(c).map(String::as_str).unwrap_or("");
            let s = if cols[c].0 == "Keys" {
                bold(t)
            } else if c == 0 {
                if resp.focused { st.add_modifier(Modifier::BOLD) } else { st }
            } else {
                quiet(t)
            };
            put(buf, x, f.y, &truncate(cell, *w as usize, ui.glyphs.ellipsis), s.bg(bg), *w);
            x += w + 1;
        }
        f.y += 1;
    }
}

/// Centered italic quiet `Tip · …` line.
pub fn tip(buf: &mut Buffer, ui: &Ui, area: Rect, y: u16, footer: &str) {
    if footer.is_empty() {
        return;
    }
    let s = truncate(&format!("Tip · {footer}"), area.width.saturating_sub(4) as usize, "…");
    let w = width(&s) as u16;
    let x = area.x + area.width.saturating_sub(w) / 2;
    put(buf, x, y, &s, quiet(ui.theme).add_modifier(Modifier::ITALIC), w);
}

/// Page scope switch (`Scope  Global / Project`), quiet, top right.
pub fn scope(buf: &mut Buffer, ui: &mut Ui, area: Rect, sc: &Scope) {
    let t = ui.theme;
    let focused = ui.focus.is("scope");
    let mut spans = vec![sp("Scope  ", quiet(t))];
    spans.extend(segmented_spans(t, &sc.options, sc.value, focused));
    let w = spans_width(&spans);
    let x = area.x + area.width.saturating_sub(w + 1);
    ui.stop("scope", Rect::new(x, area.y, w, 1));
    put_spans_right(buf, ui, "scope", x + w, area.y, &spans, t.bg);
}
