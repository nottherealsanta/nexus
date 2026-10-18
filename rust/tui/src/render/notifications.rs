//! Notifications: the client-side history of shown toasts, the top-bar counter and the
//! drawer that lists them (design overhaul step 10, plate P-TST).
//!
//! The host only sends the newest bounded toast list; this module keeps the last 50
//! toasts it saw (newest first). The history is visual client state, not durable: it
//! starts empty after a reconnect. The counter is the number of toasts recorded since the
//! drawer was last opened. Opening is local (click on the counter, `/notifications`
//! through `notifications_open_seq`, or `Ctrl+X Shift+N`); no host round trip.
use super::Palette;
use crate::bridge::Snapshot;
use nexus_widgets::Glyphs;
use ratatui::{
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
    Frame,
};
use std::{
    collections::VecDeque,
    time::{Duration, Instant},
};
use unicode_width::UnicodeWidthStr;

/// Most toasts kept.
pub const HISTORY: usize = 50;
/// Outer width of the drawer, border included.
pub const DRAWER_WIDTH: u16 = 46;
/// The counter pops for 4 frames when it changes.
const POP: Duration = Duration::from_millis(4 * 80);
/// One row drops in per 2 frames.
const ROW_DROP: Duration = Duration::from_millis(2 * 80);
/// Ages are shown to the second, so an open drawer redraws about once a second.
const AGE_REDRAW: Duration = Duration::from_secs(1);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Info,
    Success,
    Warning,
    Error,
}

impl Kind {
    pub fn of(level: &str) -> Self {
        match level {
            "success" => Kind::Success,
            "warning" => Kind::Warning,
            "error" => Kind::Error,
            _ => Kind::Info,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Entry {
    pub kind: Kind,
    pub text: String,
    pub at: Instant,
}

#[derive(Default)]
pub struct Notifications {
    entries: VecDeque<Entry>,
    /// Ids already recorded (a snapshot resends the host's whole list).
    ids: VecDeque<u64>,
    unseen: usize,
    open: bool,
    opened_at: Option<Instant>,
    pop_until: Option<Instant>,
    /// Drawer rectangle of the last frame, for click-outside tests.
    panel: Option<Rect>,
    /// An animation was in flight at the last `tick` (one more redraw settles it).
    moving: bool,
    last_age_redraw: Option<Instant>,
    /// Last `notifications_open_seq` from the host; `None` until the first snapshot.
    seq: Option<u64>,
}

/// Counter text in the top bar: ` ◔ 3 ` or, while it pops, `(◔ 3)`. Same width either way.
pub fn counter_text(count: usize, pop: bool) -> String {
    let glyph = if Glyphs::from_env().ascii { "o" } else { "◔" };
    let n = if count > 99 {
        "99+".to_string()
    } else {
        count.to_string()
    };
    if pop {
        format!("({glyph} {n})")
    } else {
        format!(" {glyph} {n} ")
    }
}

/// `now`, `12s`, `4m`, `3h`, `2d`.
pub fn age(elapsed: Duration) -> String {
    let s = elapsed.as_secs();
    match s {
        0..=4 => "now".into(),
        5..=59 => format!("{s}s"),
        60..=3599 => format!("{}m", s / 60),
        3600..=86_399 => format!("{}h", s / 3600),
        _ => format!("{}d", s / 86_400),
    }
}

impl Notifications {
    /// Record one shown toast once. Returns true when it was new.
    pub fn record(&mut self, id: u64, level: &str, title: &str, body: &str, now: Instant) -> bool {
        if self.ids.contains(&id) {
            return false;
        }
        self.ids.push_back(id);
        if self.ids.len() > HISTORY * 4 {
            self.ids.pop_front();
        }
        let text = if body.trim().is_empty() {
            title.to_string()
        } else {
            format!("{title} · {body}")
        };
        let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
        self.entries.push_front(Entry {
            kind: Kind::of(level),
            text,
            at: now,
        });
        self.entries.truncate(HISTORY);
        if !self.open {
            self.unseen += 1;
            self.pop_until = Some(now + POP);
        }
        true
    }

    #[allow(dead_code)]
    pub fn len(&self) -> usize {
        self.entries.len()
    }
    #[allow(dead_code)]
    pub fn unseen(&self) -> usize {
        self.unseen
    }
    pub fn is_open(&self) -> bool {
        self.open
    }
    #[cfg(test)]
    pub fn entries(&self) -> impl Iterator<Item = &Entry> {
        self.entries.iter()
    }

    /// Opening shows the history and resets the counter.
    pub fn open(&mut self, now: Instant) {
        self.open = true;
        self.opened_at = Some(now);
        self.unseen = 0;
        self.pop_until = None;
        self.last_age_redraw = None;
    }
    pub fn close(&mut self) {
        self.open = false;
        self.opened_at = None;
        self.panel = None;
    }
    pub fn toggle(&mut self, now: Instant) {
        if self.open {
            self.close();
        } else {
            self.open(now);
        }
    }

    /// Follow the host's `/notifications` counter: the first value is the baseline (a
    /// reconnect replays it and must not open the drawer), later changes open it.
    pub fn observe_seq(&mut self, seq: u64, now: Instant) -> bool {
        match self.seq.replace(seq) {
            Some(previous) if previous != seq => {
                if !self.open {
                    self.open(now);
                }
                true
            }
            _ => false,
        }
    }

    pub fn popping(&self, now: Instant, animate: bool) -> bool {
        animate && self.pop_until.is_some_and(|until| now < until)
    }

    /// Give the top bar what it draws (the layout and hit test read the snapshot).
    pub fn sync(&self, s: &mut Snapshot, now: Instant, animate: bool) {
        s.notifications = Some(self.unseen);
        s.notifications_pop = self.popping(now, animate);
    }

    /// Drawer rows revealed so far: all of them when animations are off.
    pub fn revealed(&self, now: Instant, animate: bool, capacity: usize) -> usize {
        let total = self.entries.len().min(capacity);
        let Some(opened) = self.opened_at.filter(|_| animate) else {
            return total;
        };
        let steps = now.saturating_duration_since(opened).as_millis() / ROW_DROP.as_millis();
        (steps as usize + 1).min(total)
    }

    /// Advance transients. True when a redraw is due.
    pub fn tick(&mut self, now: Instant, animate: bool) -> bool {
        let pop = self.popping(now, animate);
        let dropping = animate
            && self.open
            && self.opened_at.is_some_and(|opened| {
                now.saturating_duration_since(opened)
                    < ROW_DROP * (self.entries.len().min(HISTORY) as u32 + 1)
            });
        let age_due = self.open
            && self
                .last_age_redraw
                .map_or(true, |at| now.saturating_duration_since(at) >= AGE_REDRAW);
        if age_due {
            self.last_age_redraw = Some(now);
        }
        let was = std::mem::replace(&mut self.moving, pop || dropping);
        pop || dropping || was || age_due
    }

    pub fn in_panel(&self, x: u16, y: u16) -> bool {
        self.panel.is_some_and(|r| r.contains((x, y).into()))
    }

    /// Draw the drawer under the top bar's right edge. `bar` is the top-bar rectangle.
    pub fn draw(&mut self, frame: &mut Frame, p: &Palette, bar: Rect, now: Instant, animate: bool) {
        self.panel = None;
        if !self.open || bar.height == 0 {
            return;
        }
        let area = frame.area();
        let width = DRAWER_WIDTH.min(bar.width);
        let y = bar.y + 1;
        // Border rows plus the header row are fixed.
        let room = area.bottom().saturating_sub(y);
        if width < 12 || room < 4 {
            return;
        }
        let capacity = usize::from(room - 3);
        let shown = self.revealed(now, animate, capacity);
        let rows = shown.max(1) as u16;
        let rect = Rect::new(bar.right() - width, y, width, rows + 3);
        let inner = usize::from(width - 2);
        let glyphs = Glyphs::from_env();

        let header = crate::transcript::truncate(
            &format!(" Notifications  {} · Esc closes", self.entries.len()),
            inner,
        );
        let mut lines = vec![Line::from(Span::styled(
            format!("{header:<inner$}"),
            Style::default().fg(p.text).add_modifier(Modifier::BOLD),
        ))];
        if self.entries.is_empty() {
            lines.push(Line::styled(
                format!("{:<inner$}", " Nothing yet"),
                Style::default().fg(p.quiet),
            ));
        }
        for entry in self.entries.iter().take(shown) {
            let (glyph, color) = match entry.kind {
                Kind::Info => (glyphs.info, p.blue),
                Kind::Success => (glyphs.ok, p.success),
                Kind::Warning => (glyphs.warn, p.warning),
                Kind::Error => (glyphs.err, p.error),
            };
            let age = age(now.saturating_duration_since(entry.at));
            // " g " + text + "  " + age + " "
            let text_width = inner.saturating_sub(3 + 2 + age.width() + 1);
            let text = crate::transcript::truncate(&entry.text, text_width);
            let pad = inner.saturating_sub(3 + text.width() + 2 + age.width() + 1);
            lines.push(Line::from(vec![
                Span::styled(format!(" {glyph} "), Style::default().fg(color)),
                Span::styled(text, Style::default().fg(p.text)),
                Span::raw(" ".repeat(pad + 2)),
                Span::styled(age, Style::default().fg(p.quiet)),
                Span::raw(" "),
            ]));
        }
        frame.render_widget(Clear, rect);
        frame.render_widget(
            Paragraph::new(lines)
                .style(Style::default().bg(p.dialog).fg(p.text))
                .block(
                    Block::default()
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(p.border_strong).bg(p.dialog)),
                ),
            rect,
        );
        self.panel = Some(rect);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fill(n: &mut Notifications, count: u64, now: Instant) {
        for id in 0..count {
            n.record(id, "info", &format!("Toast {id}"), "", now);
        }
    }

    #[test]
    fn history_is_bounded_newest_first_and_deduped_by_id() {
        let mut n = Notifications::default();
        let t0 = Instant::now();
        fill(&mut n, 80, t0);
        assert_eq!(n.len(), HISTORY);
        assert_eq!(n.entries().next().unwrap().text, "Toast 79");
        assert_eq!(n.entries().last().unwrap().text, "Toast 30");
        assert!(
            !n.record(79, "info", "Toast 79", "", t0),
            "the same id is recorded once"
        );
        assert_eq!(n.len(), HISTORY);
    }

    #[test]
    fn counter_counts_since_the_drawer_was_opened_and_pops_on_change() {
        let mut n = Notifications::default();
        let t0 = Instant::now();
        fill(&mut n, 3, t0);
        assert_eq!(n.unseen(), 3);
        assert!(n.popping(t0 + Duration::from_millis(200), true));
        assert!(
            !n.popping(t0 + Duration::from_millis(400), true),
            "pop lasts ~4 frames"
        );
        assert!(!n.popping(t0, false), "animations off: no pop");
        n.open(t0);
        assert_eq!(n.unseen(), 0, "opening resets the counter");
        n.record(100, "error", "Boom", "HTTP 503", t0);
        assert_eq!(n.unseen(), 0, "an open drawer already shows it");
        n.close();
        n.record(101, "info", "More", "", t0);
        assert_eq!(n.unseen(), 1);
        assert_eq!(n.entries().nth(1).unwrap().text, "Boom · HTTP 503");
    }

    #[test]
    fn counter_text_pops_in_parentheses_at_the_same_width() {
        assert_eq!(counter_text(3, false), " ◔ 3 ");
        assert_eq!(counter_text(3, true), "(◔ 3)");
        assert_eq!(counter_text(250, false), " ◔ 99+ ");
        assert_eq!(
            counter_text(7, false).width(),
            counter_text(7, true).width()
        );
    }

    #[test]
    fn rows_drop_in_two_frames_apiece_and_show_all_without_animation() {
        let mut n = Notifications::default();
        let t0 = Instant::now();
        fill(&mut n, 10, t0);
        n.open(t0);
        assert_eq!(n.revealed(t0, true, 20), 1);
        assert_eq!(n.revealed(t0 + ROW_DROP, true, 20), 2);
        assert_eq!(n.revealed(t0 + ROW_DROP * 4, true, 20), 5);
        assert_eq!(n.revealed(t0 + Duration::from_secs(60), true, 20), 10);
        assert_eq!(n.revealed(t0, false, 20), 10);
        assert_eq!(
            n.revealed(t0 + Duration::from_secs(60), true, 4),
            4,
            "bounded by the room"
        );
    }

    #[test]
    fn tick_keeps_redrawing_while_a_transient_runs_then_settles() {
        let mut n = Notifications::default();
        let t0 = Instant::now();
        assert!(!n.tick(t0, true), "idle");
        fill(&mut n, 1, t0);
        assert!(
            n.tick(t0 + Duration::from_millis(80), true),
            "pop in flight"
        );
        let later = t0 + Duration::from_secs(1);
        assert!(n.tick(later, true), "one more frame settles the pop");
        assert!(!n.tick(later + Duration::from_millis(80), true), "settled");
    }

    #[test]
    fn host_counter_baseline_does_not_open_but_a_change_does() {
        let mut n = Notifications::default();
        let t0 = Instant::now();
        assert!(!n.observe_seq(4, t0), "first value is a baseline");
        assert!(!n.observe_seq(4, t0));
        assert!(!n.is_open());
        assert!(n.observe_seq(5, t0), "/notifications bumped it");
        assert!(n.is_open());
        n.close();
        assert!(!n.observe_seq(5, t0), "an unchanged counter never reopens");
    }

    #[test]
    fn age_labels() {
        assert_eq!(age(Duration::from_secs(2)), "now");
        assert_eq!(age(Duration::from_secs(12)), "12s");
        assert_eq!(age(Duration::from_secs(240)), "4m");
        assert_eq!(age(Duration::from_secs(3 * 3600)), "3h");
        assert_eq!(age(Duration::from_secs(2 * 86_400)), "2d");
    }

    fn draw_rows(n: &mut Notifications, width: u16, height: u16, now: Instant) -> Vec<String> {
        let backend = ratatui::backend::TestBackend::new(width, height);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| {
                let bar = Rect::new(0, 0, width, 3);
                n.draw(frame, &Palette::new(false), bar, now, false);
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        (0..height)
            .map(|y| (0..width).map(|x| buffer[(x, y)].symbol()).collect())
            .collect()
    }

    #[test]
    fn drawer_lists_rows_under_the_bar_and_a_click_outside_is_detected() {
        let mut n = Notifications::default();
        let t0 = Instant::now();
        n.record(1, "error", "Could not reach OpenAI", "HTTP 503", t0);
        n.record(2, "success", "Copied", "", t0);
        assert!(draw_rows(&mut n, 100, 20, t0)
            .iter()
            .all(|row| !row.contains("Notifications")));
        n.open(t0);
        let rows = draw_rows(&mut n, 100, 20, t0);
        assert!(rows[1].contains("┌"), "{:?}", rows[1]);
        assert!(
            rows[2].contains("Notifications  2 · Esc closes"),
            "{:?}",
            rows[2]
        );
        assert!(
            rows[3].contains("Copied") && rows[3].contains("now"),
            "newest first: {:?}",
            rows[3]
        );
        assert!(
            rows[4].contains("Could not reach OpenAI · HTTP 503"),
            "{:?}",
            rows[4]
        );
        let left = rows[2].find('│').unwrap();
        assert_eq!(
            left,
            100 - usize::from(DRAWER_WIDTH),
            "anchored to the right edge"
        );
        assert!(n.in_panel(99 - 3, 3));
        assert!(!n.in_panel(2, 3));
        n.close();
        assert!(!n.in_panel(99 - 3, 3));
    }

    #[test]
    fn drawer_survives_tiny_and_empty_areas() {
        let mut n = Notifications::default();
        let t0 = Instant::now();
        fill(&mut n, 60, t0);
        n.open(t0);
        for width in 1..60 {
            for height in 1..12 {
                let _ = draw_rows(&mut n, width, height, t0 + Duration::from_secs(30));
            }
        }
        let mut empty = Notifications::default();
        empty.open(t0);
        assert!(draw_rows(&mut empty, 80, 10, t0)[3].contains("Nothing yet"));
    }
}
