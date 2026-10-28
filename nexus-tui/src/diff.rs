//! Diffs (DESIGN_PLAN.md D09), one renderer for the tool details of edit, write
//! and patch and for the Git diff view.
//!
//! A [`Diff`] is parsed from a unified diff (`git diff`), from the `apply_patch`
//! text of the patch tool, from an old/new string pair, or from new file
//! content. [`Diff::unified`] and [`Diff::split`] turn it into rows of exactly
//! `width` cells; [`Diff::render`] picks split when the column is at least
//! [`SPLIT_MIN`] cells wide (old Nexus' side-by-side layout).
//!
//! Tinted text is primary and context text is muted. The empty side of an add
//! or a delete in the split view is a hatch of `╱` in `border.default`; the
//! split rule `│` is `border.strong`.

use ratatui::style::{Color, Style};
use ratatui::text::Span;

use crate::text::{truncate, width, wrap_line};
use crate::theme::Theme;

/// The narrowest column that gets the split view.
pub const SPLIT_MIN: usize = 110;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Context,
    Add,
    Del,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    pub kind: Kind,
    /// Line number in the old file, when known.
    pub old: Option<usize>,
    /// Line number in the new file, when known.
    pub new: Option<usize>,
    pub text: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Hunk {
    /// `@@ -10,6 +10,13 @@ class Loader:` or an `apply_patch` anchor; may be empty.
    pub header: String,
    pub rows: Vec<Row>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct File {
    pub path: String,
    pub hunks: Vec<Hunk>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Diff {
    pub files: Vec<File>,
}

/// Styles for the rows, from the theme.
#[derive(Clone, Default)]
pub struct Look {
    text: Style,
    muted: Style,
    secondary: Style,
    add: Style,
    del: Style,
    add_tint: Style,
    del_tint: Style,
    rule: Style,
    hatch: Style,
    ascii: bool,
}

impl Look {
    pub fn from_theme(t: &Theme) -> Self {
        let fg = |name: &str| Style::default().fg(t.color(name).unwrap_or(Color::Reset));
        let tint = |name: &str| match t.color(name) {
            Some(color) => Style::default().bg(color),
            None => Style::default(),
        };
        Self {
            text: fg("text.primary"),
            muted: fg("text.muted"),
            secondary: fg("text.secondary"),
            add: fg("diff.add"),
            del: fg("diff.del"),
            add_tint: tint("diff.add_bg"),
            del_tint: tint("diff.del_bg"),
            rule: fg("border.strong"),
            hatch: fg("border.default"),
            ascii: t.opts.ascii,
        }
    }
}

fn parse_range(spec: &str) -> Option<(usize, usize)> {
    let spec = spec.get(1..)?;
    let mut parts = spec.split(',');
    let start = parts.next()?.parse().ok()?;
    let len = parts.next().map_or(Some(1), |n| n.parse().ok())?;
    Some((start, len))
}

/// `@@ -a,b +c,d @@ tail` as `((a, b), (c, d))`.
fn parse_hunk_header(line: &str) -> Option<((usize, usize), (usize, usize))> {
    let rest = line.strip_prefix("@@ ")?;
    let mut parts = rest.split_whitespace();
    let old = parse_range(parts.next().filter(|p| p.starts_with('-'))?)?;
    let new = parse_range(parts.next().filter(|p| p.starts_with('+'))?)?;
    Some((old, new))
}

fn strip_side(path: &str) -> &str {
    path.strip_prefix("a/")
        .or_else(|| path.strip_prefix("b/"))
        .unwrap_or(path)
}

impl Diff {
    /// A unified diff or an `apply_patch` patch. Unknown lines before a hunk
    /// (`index ...`, `new file mode ...`) are skipped.
    pub fn from_patch(patch: &str) -> Self {
        let mut diff = Diff::default();
        // Lines left in the current numeric hunk: (old, new).
        let mut left: Option<(usize, usize)> = None;
        let (mut old_no, mut new_no): (Option<usize>, Option<usize>) = (None, None);
        for line in patch.lines() {
            let counted = left.is_some_and(|(o, n)| o + n > 0);
            if !counted {
                if let Some(path) = line
                    .strip_prefix("*** Update File: ")
                    .or_else(|| line.strip_prefix("*** Add File: "))
                    .or_else(|| line.strip_prefix("*** Delete File: "))
                {
                    diff.files.push(File {
                        path: path.trim().to_owned(),
                        hunks: Vec::new(),
                    });
                    continue;
                }
                if line.starts_with("*** ") {
                    continue;
                }
                if let Some(rest) = line.strip_prefix("diff --git ") {
                    let path = rest.rsplit_once(" b/").map_or(rest, |(_, p)| p);
                    diff.files.push(File {
                        path: path.to_owned(),
                        hunks: Vec::new(),
                    });
                    continue;
                }
                if let Some(path) = line.strip_prefix("+++ ") {
                    let path = path.trim();
                    if diff.files.last().is_none_or(|f| f.hunks.is_empty()) {
                        if path != "/dev/null" {
                            match diff.files.last_mut() {
                                Some(file) => file.path = strip_side(path).to_owned(),
                                None => diff.files.push(File {
                                    path: strip_side(path).to_owned(),
                                    hunks: Vec::new(),
                                }),
                            }
                        }
                        continue;
                    }
                }
                if line.starts_with("--- ") && diff.files.last().is_none_or(|f| f.hunks.is_empty())
                {
                    continue;
                }
                if line.starts_with("@@") {
                    if diff.files.is_empty() {
                        diff.files.push(File::default());
                    }
                    let numbers = parse_hunk_header(line);
                    (old_no, new_no) =
                        numbers.map_or((None, None), |(o, n)| (Some(o.0), Some(n.0)));
                    left = numbers.map(|(o, n)| (o.1, n.1));
                    if let Some(file) = diff.files.last_mut() {
                        file.hunks.push(Hunk {
                            header: line.trim_end().to_owned(),
                            rows: Vec::new(),
                        });
                    }
                    continue;
                }
                if line.starts_with('\\') {
                    continue; // \ No newline at end of file
                }
            }
            let (kind, text) = match line.chars().next() {
                Some('+') => (Kind::Add, &line[1..]),
                Some('-') => (Kind::Del, &line[1..]),
                Some(' ') => (Kind::Context, &line[1..]),
                None => (Kind::Context, ""),
                _ => {
                    if !counted && diff.files.last().is_none_or(|f| f.hunks.is_empty()) {
                        continue; // header noise before the first hunk
                    }
                    (Kind::Context, line)
                }
            };
            if diff.files.is_empty() {
                diff.files.push(File::default());
            }
            let Some(file) = diff.files.last_mut() else {
                continue;
            };
            if file.hunks.is_empty() {
                // The first hunk of an `apply_patch` update may omit its `@@`.
                file.hunks.push(Hunk::default());
            }
            let (old, new) = (
                (kind != Kind::Add).then_some(old_no).flatten(),
                (kind != Kind::Del).then_some(new_no).flatten(),
            );
            if kind != Kind::Add {
                old_no = old_no.map(|n| n + 1);
            }
            if kind != Kind::Del {
                new_no = new_no.map(|n| n + 1);
            }
            if let Some((o, n)) = &mut left {
                if kind != Kind::Add {
                    *o = o.saturating_sub(1);
                }
                if kind != Kind::Del {
                    *n = n.saturating_sub(1);
                }
            }
            if let Some(hunk) = file.hunks.last_mut() {
                hunk.rows.push(Row {
                    kind,
                    old,
                    new,
                    text: text.to_owned(),
                });
            }
        }
        diff.files.retain(|f| !f.hunks.is_empty());
        diff
    }

    /// A replacement edit: every old line removed, then every new line added.
    /// The position in the file is not known, so the rows carry no numbers.
    pub fn from_replace(old: &str, new: &str) -> Self {
        let rows = old
            .lines()
            .map(|t| (Kind::Del, t))
            .chain(new.lines().map(|t| (Kind::Add, t)))
            .map(|(kind, text)| Row {
                kind,
                old: None,
                new: None,
                text: text.to_owned(),
            })
            .collect();
        Self::one(Hunk {
            header: String::new(),
            rows,
        })
    }

    /// A new file: every line added, numbered from 1.
    pub fn from_new(content: &str) -> Self {
        let rows = content
            .lines()
            .enumerate()
            .map(|(i, text)| Row {
                kind: Kind::Add,
                old: None,
                new: Some(i + 1),
                text: text.to_owned(),
            })
            .collect();
        Self::one(Hunk {
            header: String::new(),
            rows,
        })
    }

    fn one(hunk: Hunk) -> Self {
        Self {
            files: vec![File {
                path: String::new(),
                hunks: vec![hunk],
            }],
        }
    }

    pub fn is_empty(&self) -> bool {
        self.files
            .iter()
            .all(|f| f.hunks.iter().all(|h| h.rows.is_empty()))
    }

    /// `(added, removed)` lines.
    pub fn counts(&self) -> (usize, usize) {
        let mut counts = (0, 0);
        for row in self
            .files
            .iter()
            .flat_map(|f| &f.hunks)
            .flat_map(|h| &h.rows)
        {
            match row.kind {
                Kind::Add => counts.0 += 1,
                Kind::Del => counts.1 += 1,
                Kind::Context => {}
            }
        }
        counts
    }

    /// Split at [`SPLIT_MIN`] cells and wider, unified below.
    pub fn render(&self, w: usize, look: &Look) -> Vec<Vec<Span<'static>>> {
        if w >= SPLIT_MIN {
            self.split(w, look)
        } else {
            self.unified(w, look)
        }
    }

    /// Digits of the widest line number, or 0 when no row has one.
    fn digits(&self) -> usize {
        let top = self
            .files
            .iter()
            .flat_map(|f| &f.hunks)
            .flat_map(|h| &h.rows)
            .flat_map(|r| [r.old, r.new])
            .flatten()
            .max();
        top.map_or(0, |n| n.to_string().len().max(2))
    }

    /// The `⋯ @@ … @@` separator for a hunk, if it has one to show.
    fn separator(first: bool, hunk: &Hunk, look: &Look, w: usize) -> Option<Vec<Span<'static>>> {
        if first && hunk.header.is_empty() {
            return None;
        }
        let dots = if look.ascii { "..." } else { "⋯" };
        let text = if hunk.header.is_empty() {
            dots.to_owned()
        } else {
            format!("{dots} {}", hunk.header)
        };
        let ell = if look.ascii { "..." } else { "…" };
        Some(vec![Span::styled(truncate(&text, w, ell), look.muted)])
    }

    /// The file header row: `path  +12 −3`.
    fn file_header(file: &File, look: &Look) -> Vec<Span<'static>> {
        let (mut add, mut del) = (0, 0);
        for row in file.hunks.iter().flat_map(|h| &h.rows) {
            match row.kind {
                Kind::Add => add += 1,
                Kind::Del => del += 1,
                Kind::Context => {}
            }
        }
        let minus = if look.ascii { "-" } else { "−" };
        vec![
            Span::styled(file.path.clone(), look.secondary),
            Span::raw("  "),
            Span::styled(format!("+{add}"), look.add),
            Span::raw(" "),
            Span::styled(format!("{minus}{del}"), look.del),
        ]
    }

    /// Hunks as `(file index, first hunk of the file, hunk)`, with a blank row
    /// and a file header between files when there is more than one.
    fn each(
        &self,
        look: &Look,
        w: usize,
        out: &mut Vec<Vec<Span<'static>>>,
        mut hunk: impl FnMut(&Hunk, &mut Vec<Vec<Span<'static>>>),
    ) {
        let many = self.files.len() > 1;
        for (fi, file) in self.files.iter().enumerate() {
            if many {
                if fi > 0 {
                    out.push(Vec::new());
                }
                out.push(Self::file_header(file, look));
            }
            for (hi, h) in file.hunks.iter().enumerate() {
                if hi > 0 {
                    out.push(Vec::new());
                }
                if let Some(sep) = Self::separator(hi == 0 && !many, h, look, w) {
                    out.push(sep);
                }
                hunk(h, out);
            }
        }
    }

    /// One column: a gutter with the new line numbers, then `+` or `-`, then the
    /// text on the tint. Context rows are muted. Every row is `w` cells.
    pub fn unified(&self, w: usize, look: &Look) -> Vec<Vec<Span<'static>>> {
        let digits = self.digits();
        let gutter = if digits > 0 { digits + 1 } else { 0 };
        let text_w = w.saturating_sub(gutter + 2).max(1);
        let mut out = Vec::new();
        self.each(look, w, &mut out, |hunk, out| {
            for row in &hunk.rows {
                let (tint, mark, fg, mark_style) = match row.kind {
                    Kind::Add => (look.add_tint, "+", look.text, look.add),
                    Kind::Del => (look.del_tint, "-", look.text, look.del),
                    Kind::Context => (Style::default(), " ", look.muted, look.muted),
                };
                for (i, part) in wrap_line(&row.text.replace('\t', "    "), text_w)
                    .into_iter()
                    .enumerate()
                {
                    let number = match (i, row.new) {
                        (0, Some(n)) => format!("{n:>digits$} "),
                        _ => " ".repeat(gutter),
                    };
                    let mark = if i == 0 { mark } else { " " };
                    let used = gutter + 2 + width(&part);
                    out.push(vec![
                        Span::styled(number, look.muted.patch(tint)),
                        Span::styled(format!("{mark} "), mark_style.patch(tint)),
                        Span::styled(part, fg.patch(tint)),
                        Span::styled(" ".repeat(w.saturating_sub(used)), tint),
                    ]);
                }
            }
        });
        out
    }

    /// Old side | new side. Removed lines tint the left, added lines tint the
    /// right, and the empty side of an add or delete is hatched. Long lines wrap
    /// inside their column. Every row is `w` cells.
    pub fn split(&self, w: usize, look: &Look) -> Vec<Vec<Span<'static>>> {
        let digits = self.digits();
        // number, space, mark, space
        let gutter = if digits > 0 { digits + 3 } else { 2 };
        let left_w = w.saturating_sub(1) / 2;
        let right_w = w.saturating_sub(1) - left_w;
        let hatch_glyph = if look.ascii { "/" } else { "╱" };
        let rule = if look.ascii { "|" } else { "│" };
        let mut out = Vec::new();
        self.each(look, w, &mut out, |hunk, out| {
            let rows = &hunk.rows;
            let mut i = 0;
            while i < rows.len() {
                if rows[i].kind == Kind::Context {
                    let r = &rows[i];
                    push_pair(
                        out,
                        Some((r, number(r.old, digits))),
                        Some((r, number(r.new, digits))),
                        (left_w, right_w),
                        gutter,
                        (look, hatch_glyph, rule),
                    );
                    i += 1;
                    continue;
                }
                // A run of removals, then a run of additions, pairs line by line.
                let dels: Vec<&Row> = rows[i..]
                    .iter()
                    .take_while(|r| r.kind == Kind::Del)
                    .collect();
                i += dels.len();
                let adds: Vec<&Row> = rows[i..]
                    .iter()
                    .take_while(|r| r.kind == Kind::Add)
                    .collect();
                i += adds.len();
                for k in 0..dels.len().max(adds.len()) {
                    push_pair(
                        out,
                        dels.get(k).map(|r| (*r, number(r.old, digits))),
                        adds.get(k).map(|r| (*r, number(r.new, digits))),
                        (left_w, right_w),
                        gutter,
                        (look, hatch_glyph, rule),
                    );
                }
            }
        });
        out
    }
}

fn number(n: Option<usize>, digits: usize) -> String {
    match n {
        Some(n) => format!("{n:>digits$}"),
        None => " ".repeat(digits),
    }
}

/// One side of a split row, wrapped: its rows, each exactly `side_w` cells.
fn side(
    cell: Option<(&Row, String)>,
    side_w: usize,
    gutter: usize,
    look: &Look,
) -> (Vec<Vec<Span<'static>>>, Style) {
    let Some((row, number)) = cell else {
        return (Vec::new(), Style::default());
    };
    let (tint, mark, fg, mark_style) = match row.kind {
        Kind::Add => (look.add_tint, "+", look.text, look.add),
        Kind::Del => (look.del_tint, "-", look.text, look.del),
        Kind::Context => (Style::default(), " ", look.muted, look.muted),
    };
    let text_w = side_w.saturating_sub(gutter).max(1);
    let rows = wrap_line(&row.text.replace('\t', "    "), text_w)
        .into_iter()
        .enumerate()
        .map(|(i, part)| {
            let lead = if gutter > 2 {
                if i == 0 {
                    format!("{number} ")
                } else {
                    " ".repeat(number.len() + 1)
                }
            } else {
                String::new()
            };
            let mark = if i == 0 { mark } else { " " };
            let used = lead.len() + 2 + width(&part);
            vec![
                Span::styled(lead, look.muted.patch(tint)),
                Span::styled(format!("{mark} "), mark_style.patch(tint)),
                Span::styled(part, fg.patch(tint)),
                Span::styled(" ".repeat(side_w.saturating_sub(used)), tint),
            ]
        })
        .collect();
    (rows, tint)
}

fn push_pair(
    out: &mut Vec<Vec<Span<'static>>>,
    left: Option<(&Row, String)>,
    right: Option<(&Row, String)>,
    (left_w, right_w): (usize, usize),
    gutter: usize,
    (look, hatch_glyph, rule): (&Look, &str, &str),
) {
    let (l, l_tint) = side(left, left_w, gutter, look);
    let (r, r_tint) = side(right, right_w, gutter, look);
    let height = l.len().max(r.len()).max(1);
    let hatch = |w: usize| vec![Span::styled(hatch_glyph.repeat(w), look.hatch)];
    // An empty side is all hatch; a wrapped side that ended is blank tint.
    let fill = |rows: &[Vec<Span<'static>>], k: usize, w: usize, tint: Style| match (
        rows.get(k),
        rows.is_empty(),
    ) {
        (Some(row), _) => row.clone(),
        (None, true) => hatch(w),
        (None, false) => vec![Span::styled(" ".repeat(w), tint)],
    };
    for k in 0..height {
        let mut spans = fill(&l, k, left_w, l_tint);
        spans.push(Span::styled(rule.to_owned(), look.rule));
        spans.extend(fill(&r, k, right_w, r_tint));
        out.push(spans);
    }
}

/// Row text without styles, for tests and plain output.
pub fn plain(row: &[Span<'static>]) -> String {
    row.iter().map(|s| s.content.as_ref()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::{Options, Theme};

    const PATCH: &str = "diff --git a/src/a.py b/src/a.py\nindex 1..2 100644\n--- a/src/a.py\n+++ b/src/a.py\n@@ -10,3 +10,4 @@ class A:\n     keep\n-    old\n+    new\n+    more\n     tail\n@@ -40,1 +41,1 @@ def f():\n-x\n+y\n";

    fn look() -> Look {
        Look::from_theme(&Theme::new(Options::default()))
    }

    fn text(rows: &[Vec<Span<'static>>]) -> Vec<String> {
        rows.iter().map(|r| plain(r)).collect()
    }

    #[test]
    fn parses_unified_and_apply_patch_text() {
        let diff = Diff::from_patch(PATCH);
        assert_eq!(diff.files.len(), 1);
        assert_eq!(diff.files[0].path, "src/a.py");
        assert_eq!(diff.files[0].hunks.len(), 2);
        assert_eq!(diff.counts(), (3, 2));
        let rows = &diff.files[0].hunks[0].rows;
        assert_eq!((rows[0].old, rows[0].new), (Some(10), Some(10)));
        assert_eq!((rows[1].old, rows[1].new), (Some(11), None));
        assert_eq!((rows[2].old, rows[2].new), (None, Some(11)));
        assert_eq!((rows[4].old, rows[4].new), (Some(12), Some(13)));
        let ap = Diff::from_patch(
            "*** Begin Patch\n*** Update File: a.py\n@@ def f():\n a\n-b\n+c\n*** Add File: n.txt\n+one\n*** End Patch",
        );
        assert_eq!(ap.files.len(), 2);
        assert_eq!(ap.files[0].hunks[0].header, "@@ def f():");
        assert_eq!(ap.files[0].hunks[0].rows[0].new, None);
        assert_eq!(ap.files[1].path, "n.txt");
        assert_eq!(ap.counts(), (2, 1));
    }

    #[test]
    fn unified_shows_new_numbers_marks_and_hunk_separators() {
        let rows = text(&Diff::from_patch(PATCH).unified(60, &look()));
        assert_eq!(rows[0], "⋯ @@ -10,3 +10,4 @@ class A:");
        assert!(
            rows[1].starts_with("10 ") && rows[1].contains("keep"),
            "{:?}",
            rows[1]
        );
        assert!(rows[2].starts_with("   - "), "{:?}", rows[2]);
        assert!(rows[3].starts_with("11 +     new"), "{:?}", rows[3]);
        assert!(rows.iter().any(|r| r.starts_with("⋯ @@ -40,1 +41,1 @@")));
        assert!(
            rows.iter()
                .filter(|r| r.contains("  "))
                .all(|r| width(r) == 60),
            "{rows:?}"
        );
    }

    #[test]
    fn split_starts_at_110_cells_and_unified_below() {
        let diff = Diff::from_patch(PATCH);
        let l = look();
        let below = diff.render(109, &l);
        let at = diff.render(110, &l);
        assert_eq!(text(&below), text(&diff.unified(109, &l)));
        assert_eq!(text(&at), text(&diff.split(110, &l)));
        assert!(!text(&below).iter().any(|r| r.contains('│')));
        assert!(text(&at)
            .iter()
            .filter(|r| r.contains("   "))
            .all(|r| r.contains('│')));
    }

    #[test]
    fn split_hatches_the_empty_side_and_pairs_runs() {
        let diff = Diff::from_patch(PATCH);
        let l = look();
        let rows = diff.split(110, &l);
        let t = text(&rows);
        // `old` pairs with `new`; `more` has nothing on the left: hatched.
        let new = t.iter().position(|r| r.contains("+     new")).unwrap();
        assert!(t[new].contains("-     old"), "{}", t[new]);
        let more = t.iter().position(|r| r.contains("+     more")).unwrap();
        let left: String = t[more].chars().take(54).collect();
        assert_eq!(left, "╱".repeat(54));
        // The hatch is border.default, the split rule is border.strong.
        let hatch = &rows[more][0];
        assert_eq!(hatch.style, l.hatch);
        assert_eq!(rows[more][1].content, "│");
        assert_eq!(rows[more][1].style, l.rule);
        // Every row fills the column exactly.
        assert!(
            t.iter()
                .filter(|r| r.contains('│'))
                .all(|r| width(r) == 110),
            "{t:?}"
        );
    }

    #[test]
    fn wide_cjk_lines_wrap_inside_their_split_column() {
        let old = "旧".repeat(60);
        let new = "新".repeat(60);
        let diff = Diff::from_replace(&old, &new);
        let l = look();
        let rows = diff.split(110, &l);
        let t = text(&rows);
        assert!(t.len() >= 2, "wraps onto more rows: {t:?}");
        for r in &t {
            assert_eq!(width(r), 110, "{r:?}");
            assert_eq!(r.matches('│').count(), 1, "{r:?}");
        }
        let all: String = t.concat();
        assert_eq!(all.matches('旧').count(), 60);
        assert_eq!(all.matches('新').count(), 60);
        let unified = diff.unified(40, &l);
        assert!(text(&unified).iter().all(|r| width(r) == 40));
    }

    #[test]
    fn tint_is_on_the_changed_side_only() {
        let l = look();
        let rows = Diff::from_replace("a", "b").split(110, &l);
        // left: removed (tinted), right: added (tinted)
        assert_eq!(rows[0][0].style.bg, l.del_tint.bg);
        assert!(rows[0].iter().any(|s| s.style.bg == l.add_tint.bg));
        let ctx = Diff::from_patch("@@ -1,1 +1,1 @@\n same\n").split(110, &l);
        assert!(ctx[1].iter().all(|s| s.style.bg.is_none()));
    }

    #[test]
    fn multi_file_diffs_get_file_headers() {
        let rows = text(
            &Diff::from_patch("*** Update File: a.py\n-x\n+y\n*** Add File: b.py\n+z\n")
                .unified(40, &look()),
        );
        assert_eq!(rows[0].trim_end(), "a.py  +1 −1");
        assert!(rows.iter().any(|r| r.trim_end() == "b.py  +1 −0"));
    }
}
