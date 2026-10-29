//! Drawing helpers that work on a finished `Buffer` rather than on one widget.
//! They use only the public `Theme` surface (`color`, `opts`).

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier};

use crate::theme::{Theme, Tier};

fn mix(a: u8, b: u8) -> u8 {
    (u16::from(a) + u16::from(b)).div_ceil(2) as u8
}

fn toward(c: Color, canvas: (u8, u8, u8)) -> Option<Color> {
    match c {
        Color::Rgb(r, g, b) => Some(Color::Rgb(
            mix(r, canvas.0),
            mix(g, canvas.1),
            mix(b, canvas.2),
        )),
        _ => None,
    }
}

/// The scrim under a modal (DESIGN_PLAN.md D01): every cell's fg and bg move
/// 50% toward `surface.canvas`. Cells whose colours cannot be blended (indexed,
/// named, default), and every cell in ANSI-16, 256 and mono, get
/// `Modifier::DIM` instead.
pub fn scrim(theme: &Theme, buf: &mut Buffer, area: Rect) {
    let area = area.intersection(buf.area);
    let canvas = match (theme.opts.tier, theme.color("surface.canvas")) {
        (Tier::TrueColor, Some(Color::Rgb(r, g, b))) => Some((r, g, b)),
        _ => None,
    };
    for y in area.y..area.bottom() {
        for x in area.x..area.right() {
            let cell = &mut buf[(x, y)];
            let blended = canvas.and_then(|k| Some((toward(cell.fg, k)?, toward(cell.bg, k)?)));
            match blended {
                Some((fg, bg)) => {
                    cell.fg = fg;
                    cell.bg = bg;
                }
                None => {
                    cell.modifier.insert(Modifier::DIM);
                }
            }
        }
    }
}

/// The block of a modal (DESIGN_PLAN.md D13): `width` by `height` cells,
/// clamped to fit within 2 cells of every edge of `area` and to at most 70% of
/// its height, centred (`high`: a quarter of the way down, for the palette).
pub fn modal_rect(area: Rect, width: u16, height: u16, high: bool) -> Rect {
    let w = width.min(area.width.saturating_sub(4)).max(1);
    let cap = (u32::from(area.height) * 7 / 10) as u16;
    let h = height
        .min(cap)
        .min(area.height.saturating_sub(4))
        .max(8.min(area.height));
    let spare = area.height.saturating_sub(h);
    Rect::new(
        area.x + area.width.saturating_sub(w) / 2,
        area.y + if high { spare / 4 } else { spare / 2 },
        w,
        h,
    )
}

/// A toast's width (DESIGN_PLAN.md D15): 36 to 48 cells for `want`, within
/// the page column and two cells of its edges.
pub fn toast_width(want: usize, avail: u16) -> u16 {
    (want.clamp(36, 48) as u16)
        .min(avail.saturating_sub(2))
        .max(1)
}

/// The jump-to-latest pill (D15): `width` cells on the composer region's first
/// row (the live row above the card), right-aligned with the card's edge.
pub fn pill_rect(composer: Rect, width: u16) -> Rect {
    let width = width.min(composer.width.saturating_sub(4));
    Rect::new(
        composer.right().saturating_sub(2 + width),
        composer.y,
        width,
        1,
    )
}

/// The context meter (DESIGN_PLAN.md D11): `cells` hatch cells, the first
/// `ceil(cells * fraction)` painted with `filled` and the rest with `empty`.
/// Mono and ASCII use `/` for filled and `.` for empty cells so the two stay
/// distinguishable without colour. Returns how many cells were filled.
pub fn meter(
    theme: &Theme,
    buf: &mut Buffer,
    (x, y): (u16, u16),
    cells: u16,
    fraction: f64,
    filled: ratatui::style::Style,
    empty: ratatui::style::Style,
) -> u16 {
    let fraction = if fraction.is_finite() {
        fraction.clamp(0.0, 1.0)
    } else {
        0.0
    };
    let on = ((f64::from(cells) * fraction).ceil() as u16).min(cells);
    let plain = theme.opts.ascii || theme.mono();
    let (full_glyph, empty_glyph) = if plain { ("/", ".") } else { ("╱", "╱") };
    for i in 0..cells {
        let (glyph, style) = if i < on {
            (full_glyph, filled)
        } else {
            (empty_glyph, empty)
        };
        if x + i < buf.area.right() && y < buf.area.bottom() {
            buf.set_string(x + i, y, glyph, style);
        }
    }
    on
}

/// Verb glyphs for tool rows (DESIGN_PLAN.md D06), as `(name, unicode, ascii)`.
/// Mirrors `[glyphs.verb]` in `interface/theme.toml` (a test compares them).
pub const VERBS: [(&str, &str, &str); 8] = [
    ("read", "→", ">"),
    ("edit", "←", "<"),
    ("shell", "$", "$"),
    ("search", "*", "*"),
    ("code", "λ", "L"),
    ("ask", "?", "?"),
    ("session", "↳", "^"),
    ("web", "↗", "^"),
];

/// The verb family of a tool name (a `VERBS` name), if it has one.
pub fn verb_family(tool: &str) -> Option<&'static str> {
    Some(match tool {
        "read" | "ls" => "read",
        "edit" | "write" | "patch" | "multiedit" => "edit",
        "bash" | "bash_output" | "bash_kill" => "shell",
        "grep" | "glob" | "search" | "find" => "search",
        "code" => "code",
        "ask" => "ask",
        "session" => "session",
        "web_search" | "web_fetch" => "web",
        _ => return None,
    })
}

/// The glyph in front of a tool row: its verb glyph, or a dot for a tool
/// without a verb. `ascii` picks the ASCII fallback.
pub fn verb_glyph(tool: &str, ascii: bool) -> &'static str {
    verb_family(tool)
        .and_then(|family| VERBS.iter().find(|(name, _, _)| *name == family))
        .map_or(if ascii { "-" } else { "·" }, |(_, uni, asc)| {
            if ascii {
                asc
            } else {
                uni
            }
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::{Mode, Options};

    #[test]
    fn verb_glyphs_match_the_theme_file_and_have_ascii_fallbacks() {
        use unicode_width::UnicodeWidthStr;
        let doc: toml::Table = include_str!("../../interface/theme.toml").parse().unwrap();
        let table = doc["glyphs"]["verb"].as_table().expect("[glyphs.verb]");
        assert_eq!(table.len(), VERBS.len());
        for (name, uni, asc) in VERBS {
            let row = table[name].as_table().unwrap();
            assert_eq!(row["unicode"].as_str(), Some(uni), "{name}");
            assert_eq!(row["ascii"].as_str(), Some(asc), "{name}");
            assert!(asc.is_ascii(), "{name}");
            assert_eq!(uni.width(), 1, "{name} is single width");
        }
        for tool in ["read", "edit", "write", "bash", "code", "ask", "session"] {
            assert!(verb_glyph(tool, true).is_ascii(), "{tool}");
            assert_eq!(verb_glyph(tool, false).width(), 1, "{tool}");
        }
        assert_eq!(verb_glyph("read", false), "→");
        assert_eq!(verb_glyph("read", true), ">");
        assert_eq!(verb_glyph("web_fetch", false), "↗");
        assert_eq!(verb_glyph("mystery", true), "-");
    }

    fn theme(tier: Tier) -> Theme {
        Theme::new(Options {
            tier,
            mode: Mode::Dark,
            ..Options::default()
        })
    }

    #[test]
    fn rgb_cell_blends_halfway_toward_the_canvas() {
        let t = theme(Tier::TrueColor);
        let Some(Color::Rgb(cr, cg, cb)) = t.color("surface.canvas") else {
            panic!("canvas is rgb in truecolor");
        };
        let mut buf = Buffer::empty(Rect::new(0, 0, 2, 1));
        buf[(0, 0)].set_fg(Color::Rgb(255, 255, 255));
        buf[(0, 0)].set_bg(Color::Rgb(100, 0, 40));
        scrim(&t, &mut buf, Rect::new(0, 0, 1, 1));
        let c = &buf[(0, 0)];
        assert_eq!(c.fg, Color::Rgb(mix(255, cr), mix(255, cg), mix(255, cb)));
        assert_eq!(c.bg, Color::Rgb(mix(100, cr), mix(0, cg), mix(40, cb)));
        assert!(!c.modifier.contains(Modifier::DIM));
        // outside the area: untouched
        assert_eq!(buf[(1, 0)].fg, Color::Reset);
    }

    #[test]
    fn indexed_cell_and_low_tiers_get_dim() {
        let t = theme(Tier::TrueColor);
        let mut buf = Buffer::empty(Rect::new(0, 0, 1, 1));
        buf[(0, 0)].set_fg(Color::Indexed(100));
        buf[(0, 0)].set_bg(Color::Indexed(30));
        let area = buf.area;
        scrim(&t, &mut buf, area);
        assert_eq!(buf[(0, 0)].fg, Color::Indexed(100));
        assert!(buf[(0, 0)].modifier.contains(Modifier::DIM));
        for tier in [Tier::Ansi256, Tier::Ansi16, Tier::Mono] {
            let t = theme(tier);
            let mut buf = Buffer::empty(Rect::new(0, 0, 1, 1));
            buf[(0, 0)].set_fg(Color::Rgb(1, 2, 3));
            buf[(0, 0)].set_bg(Color::Rgb(4, 5, 6));
            let area = buf.area;
            scrim(&t, &mut buf, area);
            assert_eq!(buf[(0, 0)].fg, Color::Rgb(1, 2, 3), "{tier:?}");
            assert!(buf[(0, 0)].modifier.contains(Modifier::DIM), "{tier:?}");
        }
    }
}

// ---------------------------------------------------------------------------
// Starting-context sections (DESIGN_PLAN.md D10, I9)
// ---------------------------------------------------------------------------

/// What a span of a context row is, so the new-session view and the timeline
/// each map it to their own styles.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CtxRole {
    /// The `◆` before a section title (muted).
    Mark,
    /// The section title (bold secondary).
    Title,
    /// The token count (secondary); estimated counts read `~N tok`.
    Tokens,
    /// The meta after the count and indents (muted).
    Meta,
    /// A preview line (muted).
    Preview,
    /// A clickable context switch that is on.
    ToggleOn(usize),
    /// A clickable context switch that is off.
    ToggleOff(usize),
    /// A switch that cannot be enabled until a new session.
    ToggleLocked(usize),
    /// The `Context total` label (bold secondary).
    TotalLabel,
    /// The total's figure (secondary).
    TotalFigure,
}

/// One section of the starting context.
#[derive(Debug, Clone)]
pub struct CtxSection {
    pub title: String,
    /// Tokens the section costs; `None` when unknown.
    pub tokens: Option<u64>,
    /// `· meta` after the count (without the separator).
    pub meta: String,
    pub body: CtxBody,
}

/// A tool, skill or MCP server switch in the starting context.
#[derive(Debug, Clone)]
pub struct CtxItem {
    /// Index in the context's combined switch list.
    pub index: usize,
    /// Plain disabled_tools key: tool name, `skill:<name>` or `mcp:<server>`.
    pub key: String,
    /// Visible name and optional short description/status.
    pub label: String,
    pub on: bool,
    pub locked: bool,
}

#[derive(Debug, Clone)]
pub enum CtxBody {
    /// One preview line, clipped with an ellipsis.
    Preview(String),
    /// An inventory whose rows can be switched on or off.
    Inventory(Vec<CtxItem>),
    None,
}

/// How the rows are laid out.
pub struct CtxLayout<'a> {
    pub width: usize,
    pub sep: &'a str,
    pub ell: &'a str,
    pub ascii: bool,
    /// Counts are estimates (`~`).
    pub estimated: bool,
    /// The model's context window, for the total's share.
    pub window: Option<u64>,
    /// Lay the tools out in up to four columns instead of one per row. The
    /// session context keeps one per row: its lines carry a single switch.
    pub columns: bool,
}

pub type CtxRow = Vec<(String, CtxRole)>;

/// Group digits: `17580` is `17,580`.
pub fn thousands(n: u64) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// `17.6k` for a context figure; plain digits below a thousand.
pub fn compact(n: u64) -> String {
    if n >= 1000 {
        format!("{:.1}k", n as f64 / 1000.0)
    } else {
        n.to_string()
    }
}

/// Rows of file text in a file view `height` rows high: the view has a header
/// row, a blank row under it (when there is room) and the search row at the
/// bottom, which is always reserved so the text does not shift (DESIGN_PLAN.md D16).
pub fn file_page(height: u16) -> usize {
    usize::from(if height >= 8 {
        height - 3
    } else {
        height.saturating_sub(2)
    })
}

/// The Nexus splash logo (DESIGN_PLAN.md D18): an `N` of `_/` strokes, five
/// rows. Shared by the connecting screen and the empty workspace, in the
/// accent colour (bold in mono). ASCII mode needs no fallback.
pub const LOGO: [&str; 5] = [
    "    _/      _/",
    "   _/_/    _/",
    "  _/  _/  _/",
    " _/    _/_/",
    "_/      _/",
];

/// The logo's width in cells: its widest row. The block is centred by this
/// width, not row by row, so the strokes keep their shape.
pub fn logo_width() -> usize {
    LOGO.iter().map(|row| row.len()).max().unwrap_or(0)
}

/// The column, within `width` cells, where the logo block starts when centred.
pub fn logo_x(width: usize) -> usize {
    width.saturating_sub(logo_width()) / 2
}

/// A breadcrumb that keeps its end: `parts` joined by `sep`, and when that is
/// wider than `max` cells the earliest parts become one `… ›` (the ellipsis
/// and the separator) and, if the last part alone is still too wide, its start
/// is cut (`…tail`).
pub fn crumb_left(parts: &[String], sep: &str, ell: &str, max: usize) -> String {
    use crate::text::{truncate_start, width};
    let full = parts.join(sep);
    if width(&full) <= max || parts.len() < 2 {
        return truncate_start(&full, max, ell);
    }
    let lead = format!("{ell}{sep}");
    let mut keep = parts.len();
    while keep > 1 {
        keep -= 1;
        let tail = parts[parts.len() - keep..].join(sep);
        if width(&lead) + width(&tail) <= max {
            return format!("{lead}{tail}");
        }
    }
    // Only the last part is left, and it does not fit after the lead.
    truncate_start(&parts[parts.len() - 1], max, ell)
}

/// The first meaningful line of a system prompt: not blank, not a heading and
/// not an HTML comment. The preview shows only text the model receives.
pub fn first_prompt_line(prompt: &str) -> String {
    prompt
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty() && !l.starts_with('#') && !l.starts_with("<!--"))
        .unwrap_or("")
        .to_owned()
}

/// The rows of the open context: one per section header, preview or inventory
/// item, and a blank row between sections, then the total row. Widths count
/// terminal cells; every row fits in `layout.width`, with clipped text ending
/// in an ellipsis.
pub fn context_rows(sections: &[CtxSection], total: u64, layout: &CtxLayout) -> Vec<CtxRow> {
    let mut rows: Vec<CtxRow> = Vec::new();
    let tilde = if layout.estimated { "~" } else { "" };
    for (i, section) in sections.iter().enumerate() {
        if i > 0 {
            rows.push(Vec::new());
        }
        let mut head = vec![(section.title.clone(), CtxRole::Title)];
        if let CtxBody::Inventory(items) = &section.body {
            head.push((format!(" [{}]", items.len()), CtxRole::Meta));
            let off = items.iter().filter(|item| !item.on).count();
            if off > 0 {
                head.push((format!(" ({off} off)"), CtxRole::Meta));
            }
        }
        if let Some(tokens) = section.tokens {
            head.push((
                format!(" {tilde}{}", compact(tokens).replace('k', "K")),
                CtxRole::Tokens,
            ));
        }
        if section.tokens.is_none() && section.title != "Workspace tools" {
            let empty_inventory =
                matches!(&section.body, CtxBody::Inventory(items) if items.is_empty());
            if empty_inventory || !matches!(&section.body, CtxBody::Inventory(_)) {
                head.push((format!("  {}", section.meta), CtxRole::Meta));
            }
        }
        rows.push(clip_row(head, layout));
        if let CtxBody::Inventory(items) = &section.body {
            if layout.columns && section.title == "Tools" {
                grid_rows(items, layout, &mut rows);
                continue;
            }
            for item in items {
                let marker = switch_marker(item.on, layout.ascii);
                let marker = format!("  {marker}");
                let mut label = item.label.clone();
                if item.locked {
                    label.push_str(" · new session");
                }
                let role = if item.locked {
                    CtxRole::ToggleLocked(item.index)
                } else if item.on {
                    CtxRole::ToggleOn(item.index)
                } else {
                    CtxRole::ToggleOff(item.index)
                };
                rows.push(clip_row(
                    vec![(marker, CtxRole::Meta), (label, role)],
                    layout,
                ));
            }
        }
    }
    if !sections.is_empty() {
        rows.push(Vec::new());
    }
    let mut totals: CtxRow = vec![
        ("total".to_owned(), CtxRole::TotalLabel),
        (
            format!("  {tilde}{} tok", thousands(total)),
            CtxRole::TotalFigure,
        ),
    ];
    if let Some(w) = layout.window.filter(|&w| w > 0) {
        let pct = total as f64 / w as f64 * 100.0;
        let share = if pct > 0.0 && pct < 0.1 {
            "<0.1%".to_owned()
        } else {
            format!("{pct:.1}%")
        };
        totals.push((
            format!("{}{share} of the window", layout.sep),
            CtxRole::Meta,
        ));
    }
    rows.push(clip_row(totals, layout));
    rows
}

/// A switch's marker: a full circle when on, an empty one when off.
fn switch_marker(on: bool, ascii: bool) -> &'static str {
    match (on, ascii) {
        (true, false) => "\u{25cf} ",
        (false, false) => "\u{25cb} ",
        (true, true) => "(*) ",
        (false, true) => "( ) ",
    }
}

/// Switch cells in columns: `● name` padded to the widest, as many columns
/// as fit up to four (at least one).
fn grid_rows(items: &[CtxItem], layout: &CtxLayout, rows: &mut Vec<CtxRow>) {
    use crate::text::width;
    let label = |item: &CtxItem| {
        if item.locked {
            format!("{} \u{b7} new session", item.label)
        } else {
            item.label.clone()
        }
    };
    let mw = width(switch_marker(true, layout.ascii));
    let cell = items.iter().map(|i| width(&label(i))).max().unwrap_or(0) + 2 + mw + 2;
    let columns = (layout.width / cell.max(1)).clamp(1, 4);
    for chunk in items.chunks(columns) {
        let mut row: CtxRow = Vec::new();
        for item in chunk {
            let text = label(item);
            let role = if item.locked {
                CtxRole::ToggleLocked(item.index)
            } else if item.on {
                CtxRole::ToggleOn(item.index)
            } else {
                CtxRole::ToggleOff(item.index)
            };
            let pad = cell - 2 - mw - width(&text);
            row.push((
                format!("  {}", switch_marker(item.on, layout.ascii)),
                CtxRole::Meta,
            ));
            row.push((text, role));
            // The cell's hover reaches 2 cells past the name, as it does before the marker.
            row.push(("  ".to_owned(), CtxRole::Meta));
            row.push((" ".repeat(pad.saturating_sub(2)), CtxRole::Meta));
        }
        rows.push(clip_row(row, layout));
    }
}

/// Clip a row to the layout width with the ellipsis (never silently).
fn clip_row(mut row: CtxRow, layout: &CtxLayout) -> CtxRow {
    use crate::text::{truncate, width};
    let mut used = 0;
    for i in 0..row.len() {
        let w = width(&row[i].0);
        if used + w > layout.width {
            row[i].0 = truncate(&row[i].0, layout.width.saturating_sub(used), layout.ell);
            row.truncate(i + 1);
            break;
        }
        used += w;
    }
    row
}

#[cfg(test)]
mod ctx_tests {
    use super::*;

    fn layout(width: usize) -> CtxLayout<'static> {
        CtxLayout {
            width,
            sep: " \u{b7} ",
            ell: "\u{2026}",
            ascii: false,
            estimated: true,
            window: Some(200_000),
            columns: false,
        }
    }

    fn text(rows: &[CtxRow]) -> Vec<String> {
        rows.iter()
            .map(|r| r.iter().map(|(t, _)| t.as_str()).collect())
            .collect()
    }

    #[test]
    fn the_logo_is_five_rows_of_one_width_block_and_centres_by_the_block() {
        assert_eq!(LOGO.len(), 5);
        assert_eq!(
            LOGO,
            [
                "    _/      _/",
                "   _/_/    _/",
                "  _/  _/  _/",
                " _/    _/_/",
                "_/      _/"
            ]
        );
        assert!(
            LOGO.iter().all(|row| row.is_ascii()),
            "ASCII needs no fallback"
        );
        assert_eq!(logo_width(), 14);
        // the block, not each row, is centred: 120 columns leave 53 each side
        assert_eq!(logo_x(120), 53);
        assert_eq!(logo_x(121), 53);
        assert_eq!(logo_x(14), 0);
        assert_eq!(logo_x(10), 0, "too narrow: starts at the edge");
    }

    #[test]
    fn a_crumb_keeps_its_end() {
        let p = |v: &[&str]| v.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let sep = " \u{203a} ";
        let two = p(&["root", "child"]);
        assert_eq!(crumb_left(&two, sep, "\u{2026}", 40), "root \u{203a} child");
        // the earliest parts give way first
        let three = p(&["root", "middle", "child"]);
        assert_eq!(
            crumb_left(&three, sep, "\u{2026}", 14),
            "\u{2026} \u{203a} child"
        );
        assert_eq!(
            crumb_left(&three, sep, "\u{2026}", 20),
            "\u{2026} \u{203a} middle \u{203a} child"
        );
        // a last part that is too long loses its start
        assert_eq!(
            crumb_left(&p(&["abcdefghij"]), sep, "\u{2026}", 6),
            "\u{2026}fghij"
        );
        assert_eq!(crumb_left(&two, sep, "\u{2026}", 5), "child");
        assert_eq!(crumb_left(&two, sep, "\u{2026}", 4), "\u{2026}ild");
        // ASCII ellipsis
        assert_eq!(crumb_left(&three, " > ", "...", 12), "... > child");
        for max in 0..30 {
            assert!(
                crate::text::width(&crumb_left(&three, sep, "\u{2026}", max)) <= max.max(1),
                "{max}"
            );
        }
    }

    #[test]
    fn a_file_page_reserves_the_header_blank_and_search_rows() {
        assert_eq!(file_page(36), 33);
        assert_eq!(file_page(8), 5);
        assert_eq!(file_page(7), 5, "short views drop the blank row");
        assert_eq!(file_page(2), 0);
        assert_eq!(file_page(0), 0);
    }

    #[test]
    fn the_prompt_preview_is_the_first_meaningful_prompt_line() {
        assert_eq!(
            first_prompt_line("\n# Harness\n<!-- note -->\n  You are an agent.\nNext"),
            "You are an agent."
        );
        assert_eq!(first_prompt_line("# Only a heading"), "");
    }

    #[test]
    fn digits_group_and_compact() {
        assert_eq!(thousands(17_580), "17,580");
        assert_eq!(thousands(12), "12");
        assert_eq!(compact(17_580), "17.6k");
        assert_eq!(compact(980), "980");
    }

    #[test]
    fn context_rows_show_tool_states_and_count() {
        let s = CtxSection {
            title: "Tools".into(),
            tokens: Some(100),
            meta: String::new(),
            body: CtxBody::Inventory(vec![CtxItem {
                index: 0,
                key: "read".into(),
                label: "read".into(),
                on: true,
                locked: false,
            }]),
        };
        let rows = context_rows(&[s], 100, &layout(60));
        let t = text(&rows);
        assert!(t.iter().any(|l| l.contains("Tools [1] ~100")), "{t:?}");
        assert!(t.iter().any(|l| l.contains("\u{25cf} read")), "{t:?}");
    }
}

/// The palette heading for a manifest `category` (DESIGN_PLAN.md D13): the
/// session categories share one heading and so do model and agent; every
/// other category is its own name, capitalised (the modal uppercases it).
pub fn palette_group(category: &str) -> String {
    match category {
        "session" | "sessions" => "Session".to_owned(),
        "model" | "agent" => "Model and agent".to_owned(),
        other => {
            let mut c = other.chars();
            c.next()
                .map(|f| f.to_uppercase().chain(c).collect())
                .unwrap_or_default()
        }
    }
}

/// Where a palette group sorts when the query is empty: the session first,
/// then the model, and the app-wide commands last, as in the mock-up.
pub fn palette_group_rank(category: &str) -> usize {
    const ORDER: [&str; 21] = [
        "session",
        "project",
        "model",
        "composer",
        "tools",
        "timeline",
        "tool",
        "waiting",
        "navigation",
        "files",
        "editor",
        "git",
        "copy",
        "inspect",
        "notifications",
        "connection",
        "auth",
        "appearance",
        "dictation",
        "app",
        "",
    ];
    let key = match category {
        "sessions" => "session",
        "agent" => "model",
        other => other,
    };
    ORDER
        .iter()
        .position(|c| *c == key)
        .unwrap_or(ORDER.len() - 1)
}
