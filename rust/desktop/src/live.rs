//! GPT Live in the desktop shell: the call bar above the composer and the timeline card.
//!
//! Python owns every fact (`nexus/ui/ratatui/live.py`, `nexus/ui_support/live_card.py`);
//! the snapshot carries `live` (the bar) and `live` blocks (card rows as
//! `tone TAB label TAB text` lines in `detail`). This module only formats and draws, in
//! the same words and order as the terminal client (`rust/tui/src/live_bar.rs`).
use super::*;
use crate::bridge::LiveBar;

const METER_CELLS: usize = 6;

/// `mm:ss`, or `h:mm:ss` past an hour.
pub fn clock(seconds: u64) -> String {
    if seconds >= 3600 {
        format!("{}:{:02}:{:02}", seconds / 3600, seconds / 60 % 60, seconds % 60)
    } else {
        format!("{:02}:{:02}", seconds / 60, seconds % 60)
    }
}

/// Seconds since the call began; 0 while connecting.
pub fn elapsed(live: &LiveBar, now_epoch: f64) -> u64 {
    if live.started <= 0.0 {
        0
    } else {
        (now_epoch - live.started).max(0.0) as u64
    }
}

/// An RMS level (0..1) as a short bar; speech RMS is small, so the scale is a square root.
pub fn meter(level: f64) -> String {
    let filled = ((level.clamp(0.0, 1.0).sqrt() * METER_CELLS as f64).round() as usize).min(METER_CELLS);
    format!("{}{}", "▰".repeat(filled), "▱".repeat(METER_CELLS - filled))
}

pub fn now_epoch() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// `Live · model · phase · mm:ss[ · in session]`
pub fn headline(live: &LiveBar, now: f64) -> String {
    let mut head = String::from("Live");
    if !live.model.is_empty() {
        head.push_str(&format!(" · {}", live.model));
    }
    if !live.phase.is_empty() {
        head.push_str(&format!(" · {}", live.phase));
    }
    head.push_str(&format!(" · {}", clock(elapsed(live, now))));
    if !live.elsewhere.is_empty() {
        head.push_str(&format!(" · in {}", live.elsewhere));
    }
    head
}

/// One parsed card row: (tone, label, text).
pub fn card_rows(detail: &str) -> Vec<(&str, &str, &str)> {
    detail
        .lines()
        .map(|row| {
            let mut parts = row.splitn(3, '\t');
            (
                parts.next().unwrap_or(""),
                parts.next().unwrap_or(""),
                parts.next().unwrap_or(""),
            )
        })
        .collect()
}

impl Desktop {
    /// The call bar: `● Live · model · phase · mm:ss`, meters, MUTED badge, stop hint.
    pub(crate) fn live_bar(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        let live = &self.snapshot.live;
        let pulse = live.state == "starting" || live.state == "stopping" || now_epoch() as u64 % 2 == 0;
        div()
            .id("live-bar")
            .debug_selector(|| "live-bar".into())
            .px_3()
            .py_2()
            .rounded(px(crate::theme::radius::CONTROL))
            .bg(t.accent_bg)
            .flex()
            .flex_wrap()
            .items_center()
            .gap_3()
            .text_size(px(crate::theme::size::SMALL))
            .child(div().text_color(if pulse { t.red } else { t.text_tertiary }).child("●"))
            .child(
                div()
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(t.text)
                    .child(headline(live, now_epoch())),
            )
            .child(
                div()
                    .font_family(theme::code_font())
                    .text_color(t.muted)
                    .child(format!("mic {} spk {}", meter(live.level_in), meter(live.level_out))),
            )
            .when(live.muted, |d| {
                d.child(
                    div()
                        .px_2()
                        .rounded(px(crate::theme::radius::CHIP))
                        .bg(t.amber)
                        .text_color(t.background)
                        .font_weight(FontWeight::BOLD)
                        .child("MUTED"),
                )
            })
            .child(
                self.button("live-stop", "/live stop", json!({"type":"command","text":"/live stop"}), cx)
                    .px_2()
                    .text_color(t.text_tertiary),
            )
            .into_any_element()
    }

    /// The Live timeline card; every row toggles the card (`operation`).
    pub(crate) fn live_card(&self, block: &Content, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        let op = block.operation.clone();
        let rail = t.resolve(&block.color, t.purple);
        let status_color = if block.status == "ended" { t.text_tertiary } else { t.green };
        div()
            .id(SharedString::from(block.id.clone()))
            .debug_selector(|| "live-card".into())
            .w_full()
            .bg(t.surface)
            .rounded(px(crate::theme::radius::CHIP))
            .border_l_2()
            .border_color(rail)
            .px_4()
            .py_2()
            .flex()
            .flex_col()
            .gap_1()
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap_2()
                    .child(icons::icon(
                        if block.collapsed { "chevron-right" } else { "chevron-down" },
                        t.text_tertiary,
                    ))
                    .child(
                        div()
                            .px_2()
                            .rounded(px(crate::theme::radius::CHIP))
                            .bg(rail)
                            .text_color(t.background)
                            .font_weight(FontWeight::BOLD)
                            .text_size(px(crate::theme::size::CAPTION))
                            .child("Live"),
                    )
                    .child(div().text_color(t.text).child(block.title.clone()))
                    .when(!block.status.is_empty(), |d| {
                        d.child(div().text_color(status_color).child(format!("· {}", block.status)))
                    }),
            )
            .children(card_rows(&block.detail).into_iter().map(|(tone, label, text)| {
                let (label_color, text_color) = match tone {
                    "you" => (t.muted, t.text),
                    "voice" => (t.blue, t.text),
                    "caption" => (t.text_tertiary, t.text_tertiary),
                    "sent" => (t.accent, t.text),
                    "told" => (t.cyan, t.muted),
                    "bad" => (t.red, t.red),
                    "note" => (t.text_tertiary, t.text_tertiary),
                    _ => (t.text_tertiary, t.muted),
                };
                div()
                    .flex()
                    .items_start()
                    .gap_3()
                    .text_size(px(crate::theme::size::UI))
                    .child(
                        div()
                            .w(px(150.))
                            .flex_shrink_0()
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(label_color)
                            .child(label.to_owned()),
                    )
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .text_color(text_color)
                            .when(tone == "caption", |d| d.italic())
                            .child(text.to_owned()),
                    )
            }))
            .on_click(cx.listener(move |this, _, w, cx| {
                if let Some(op) = &op {
                    this.dispatch(json!({"type":"operation","operation":op}), w, cx);
                }
            }))
            .into_any_element()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bar() -> LiveBar {
        LiveBar {
            active: true,
            state: "active".into(),
            phase: "listening".into(),
            model: "gpt-live-1-codex".into(),
            started: 1000.0,
            level_in: 0.25,
            ..Default::default()
        }
    }
    #[test]
    fn clock_and_elapsed() {
        assert_eq!(clock(161), "02:41");
        assert_eq!(clock(3725), "1:02:05");
        let mut live = bar();
        assert_eq!(elapsed(&live, 1161.0), 161);
        live.started = 0.0;
        assert_eq!(elapsed(&live, 1161.0), 0);
        live.started = 2000.0;
        assert_eq!(elapsed(&live, 1161.0), 0);
    }
    #[test]
    fn meter_is_bounded() {
        assert_eq!(meter(0.0), "▱▱▱▱▱▱");
        assert_eq!(meter(1.0), "▰▰▰▰▰▰");
        assert_eq!(meter(9.0).chars().count(), METER_CELLS);
    }
    #[test]
    fn headline_names_model_phase_clock_and_other_session() {
        let mut live = bar();
        assert_eq!(headline(&live, 1161.0), "Live · gpt-live-1-codex · listening · 02:41");
        live.elsewhere = "work".into();
        assert!(headline(&live, 1161.0).ends_with("· in work"));
    }
    #[test]
    fn card_rows_split_tone_label_text_and_keep_tabs_in_text() {
        let rows = card_rows("you\tYou\tcheck\nbad\tEnded\tuser · 02:41\nnote\t\ta\tb");
        assert_eq!(rows[0], ("you", "You", "check"));
        assert_eq!(rows[1], ("bad", "Ended", "user · 02:41"));
        assert_eq!(rows[2], ("note", "", "a\tb"));
    }
    #[test]
    fn live_snapshot_fields_decode_from_the_desktop_wire() {
        let value = json!({"live": {"active": true, "state": "active", "phase": "muted", "muted": true,
            "model": "m", "started": 5.0, "in": 0.5, "out": 0.1, "elsewhere": ""}});
        let snapshot: Snapshot = serde_json::from_value(value).unwrap();
        assert!(snapshot.live.active && snapshot.live.muted);
        assert_eq!(snapshot.live.level_in, 0.5);
    }
}
