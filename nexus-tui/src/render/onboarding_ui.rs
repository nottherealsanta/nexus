//! The first-run tour (plans/52), drawn over the whole window. NSH draws the same steps in
//! `nexus-nsh/src/ui/onboarding.tsx`; the wording comes from `interface/manifest.toml`.

use super::*;
use crate::onboarding::{agent_model_line, agent_models, fill_keys, root_agent, Tour};
use crate::ui::OnboardingTarget as T;

/// The widest the text column grows; narrower windows use the whole width less a margin.
const COLUMN: u16 = 76;

#[allow(clippy::too_many_arguments)]
fn button(
    cx: &Cx,
    buf: &mut Buffer,
    hits: &mut HitMap,
    x: u16,
    y: u16,
    label: &str,
    token: &str,
    focused: bool,
    target: T,
) -> u16 {
    let text = format!("[ {label} ]");
    // The focused button takes the gray selected fill, as every other control does.
    let mut style = cx.st(
        token,
        if focused {
            "interactive.row_selected"
        } else {
            CANVAS
        },
    );
    if focused {
        style = style.add_modifier(Modifier::BOLD);
    }
    let end = put(buf, x, y, &text, style, width(&text) as u16);
    hits.add(Rect::new(x, y, end - x, 1), Target::Onboarding(target));
    end
}

/// One wrapped paragraph; returns the next free row.
#[allow(clippy::too_many_arguments)]
fn paragraph(
    cx: &Cx,
    buf: &mut Buffer,
    x: u16,
    mut y: u16,
    w: u16,
    text: &str,
    token: &str,
    limit: u16,
) -> u16 {
    for line in crate::text::wrap(text, w as usize) {
        if y >= limit {
            return y;
        }
        put(buf, x, y, &line, cx.st(token, CANVAS), w);
        y += 1;
    }
    y
}

pub(super) fn draw_onboarding(cx: &Cx, buf: &mut Buffer, area: Rect, hits: &mut HitMap) {
    let a = cx.app;
    let tour = Tour::of(&a.manifest);
    let state = &a.ui.onboarding;
    let step = tour.current(state);
    clear(buf, area, cx.st(PRIMARY, CANVAS));
    hits.add(area, Target::Swallow);
    if area.width < 24 || area.height < 8 {
        put(
            buf,
            area.x,
            area.y,
            "Window too small",
            cx.st(MUTED, CANVAS),
            area.width,
        );
        return;
    }
    let w = COLUMN.min(area.width.saturating_sub(4));
    let x = area.x + area.width.saturating_sub(w) / 2;

    // Header: the wordmark, the progress dots, the count and Skip.
    put(
        buf,
        area.x + 2,
        area.y + 1,
        "nexus",
        cx.st(PRIMARY, CANVAS).add_modifier(Modifier::BOLD),
        5,
    );
    let (on, past, later) = if cx.t.opts.ascii {
        ("O", "o", ".")
    } else {
        ("\u{25cf}", "\u{25cf}", "\u{25cb}")
    };
    let dots_w = (tour.step.len() * 2).saturating_sub(1) as u16;
    let dx = area.x + area.width.saturating_sub(dots_w) / 2;
    for i in 0..tour.step.len() {
        let (glyph, token) = if i == state.step {
            (on, ACCENT)
        } else if i < state.step {
            (past, SECONDARY)
        } else {
            (later, MUTED)
        };
        put(
            buf,
            dx + i as u16 * 2,
            area.y + 1,
            glyph,
            cx.st(token, CANVAS),
            1,
        );
    }
    let count = format!("{} / {}", state.step + 1, tour.step.len());
    let skip_x = area.right().saturating_sub(8 + 2);
    put(
        buf,
        skip_x.saturating_sub(width(&count) as u16 + 2),
        area.y + 1,
        &count,
        cx.st(MUTED, CANVAS),
        width(&count) as u16,
    );
    button(
        cx,
        buf,
        hits,
        skip_x,
        area.y + 1,
        "Skip",
        SECONDARY,
        false,
        T::Skip,
    );

    // Footer: Back, the key hints, Next or Finish.
    let foot = area.bottom().saturating_sub(2);
    let last = tour.is_last(state);
    if state.step == 0 {
        put(buf, x, foot, "[ Back ]", cx.st(MUTED, CANVAS), 8);
    } else {
        button(cx, buf, hits, x, foot, "Back", PRIMARY, false, T::Back);
    }
    let next_label = if last { "Finish" } else { "Next" };
    let next_w = width(next_label) as u16 + 4;
    button(
        cx,
        buf,
        hits,
        x + w.saturating_sub(next_w),
        foot,
        next_label,
        ACCENT,
        !tour.has_rows(state),
        T::Next,
    );
    let hints = tour.hints(state);
    put(
        buf,
        x,
        foot.saturating_sub(1),
        &truncate(hints, w as usize, cx.ell()),
        cx.st(MUTED, CANVAS),
        w,
    );

    // Body.
    let top = area.y + 3;
    let limit = foot.saturating_sub(2);
    put(
        buf,
        x,
        top,
        &truncate(&step.title, w as usize, cx.ell()),
        cx.st(PRIMARY, CANVAS).add_modifier(Modifier::BOLD),
        w,
    );
    let mut y = top + 2;
    for line in &step.lines {
        y = paragraph(
            cx,
            buf,
            x,
            y,
            w,
            &fill_keys(&a.manifest, line),
            SECONDARY,
            limit,
        );
        y += 1;
    }
    match step.kind.as_str() {
        "provider" => y = draw_providers(cx, buf, hits, x, y, w, limit),
        "agents" => y = draw_agents(cx, buf, hits, x, y, w, limit),
        "search" => y = draw_search(cx, buf, hits, x, y, w, limit),
        _ => {}
    }
    if step.kind == "done" && a.current_project().is_none() {
        y = paragraph(
            cx,
            buf,
            x,
            y,
            w,
            &tour.finish_no_project,
            "status.warning",
            limit,
        );
    }
    if let Some(tip) = &step.tip {
        let tip = format!(
            "{} {}",
            if cx.t.opts.ascii { "i" } else { "\u{24d8}" },
            fill_keys(&a.manifest, tip)
        );
        paragraph(cx, buf, x, (y + 1).min(limit), w, &tip, MUTED, limit);
    }

    if let Some(id) = a.ui.provider_detail.as_deref() {
        draw_provider_detail(cx, buf, area, hits, id);
    }
}

fn row_style(cx: &Cx, buf: &mut Buffer, x: u16, y: u16, w: u16, selected: bool) -> Style {
    if selected {
        return row_band(cx, buf, Rect::new(x, y, w, 1), PRIMARY, true);
    }
    cx.st(PRIMARY, CANVAS)
}

/// The search step: SearXNG's state from Core (starting, running, stopped, an error), then Retry. Core starts it in
/// the background; the tour never waits, and Next continues without it.
fn draw_search(
    cx: &Cx,
    buf: &mut Buffer,
    hits: &mut HitMap,
    x: u16,
    mut y: u16,
    w: u16,
    limit: u16,
) -> u16 {
    let a = cx.app;
    let tour = Tour::of(&a.manifest);
    let state = if a.ui.search_starting {
        "starting"
    } else {
        a.ui.search_service
            .as_ref()
            .and_then(|s| s["state"].as_str())
            .unwrap_or(if a.ui.search_service_error.is_some() {
                "unavailable"
            } else {
                "loading"
            })
    };
    let (glyph, token) = match state {
        "running" => (cx.t.status("done").0, "status.success"),
        "starting" => (cx.t.status("running").0, ACCENT),
        "unavailable" => (cx.t.status("error").0, "status.error"),
        _ => ("\u{25cb}", MUTED),
    };
    for (i, line) in a.search_status_lines().iter().enumerate() {
        if y >= limit {
            return y;
        }
        if i == 0 {
            put(
                buf,
                x,
                y,
                &format!("{glyph} {line}"),
                cx.st(token, CANVAS),
                w,
            );
        } else {
            let style = cx.st(if i == 1 { SECONDARY } else { MUTED }, CANVAS);
            put(
                buf,
                x + 2,
                y,
                &truncate(line, w.saturating_sub(2) as usize, cx.ell()),
                style,
                w.saturating_sub(2),
            );
        }
        y += 1;
    }
    y += 1;
    if y < limit {
        let enabled = a.search_start_reason().is_none();
        button(
            cx,
            buf,
            hits,
            x,
            y,
            &tour.search_retry,
            if enabled { ACCENT } else { MUTED },
            enabled,
            T::Retry,
        );
        y += 1;
    }
    y
}

fn draw_providers(
    cx: &Cx,
    buf: &mut Buffer,
    hits: &mut HitMap,
    x: u16,
    mut y: u16,
    w: u16,
    limit: u16,
) -> u16 {
    let a = cx.app;
    let tour = Tour::of(&a.manifest);
    let done = a.onboarding_connected();
    let mark = if done {
        cx.t.status("done").0.to_owned()
    } else {
        "\u{25cb}".to_owned()
    };
    put(
        buf,
        x,
        y,
        &format!("{mark} {}", tour.quest_provider),
        cx.st(if done { "status.success" } else { PRIMARY }, CANVAS)
            .add_modifier(Modifier::BOLD),
        w,
    );
    y += 2;
    let providers = a.onboarding_providers();
    if providers.is_empty() {
        put(
            buf,
            x,
            y,
            "Loading providers\u{2026}",
            cx.st(MUTED, CANVAS),
            w,
        );
        return y + 1;
    }
    // Keep the selected row on screen: start at the first row from which it still fits.
    let room = limit.saturating_sub(y) as usize;
    let first = a.ui.onboarding.row.saturating_sub(room.saturating_sub(1));
    for (i, p) in providers.iter().enumerate().skip(first) {
        if y >= limit {
            break;
        }
        let selected = i == a.ui.onboarding.row;
        let id = p["id"].as_str().unwrap_or_default();
        let connected = p["connected"].as_bool().unwrap_or(false);
        let style = row_style(cx, buf, x, y, w, selected);
        let label = p["label"].as_str().unwrap_or(id);
        let pending =
            a.ui.provider_logins
                .get(id)
                .and_then(|l| l["status"].as_str())
                == Some("pending");
        let status = if pending {
            "sign-in pending \u{b7} waiting for you".to_owned()
        } else {
            a.provider_status_text(p)
        };
        let value = format!(
            "{status} \u{b7} {}",
            if connected { "manage" } else { "connect" }
        );
        let vw = (width(&value) as u16).min(w.saturating_sub(14));
        let lw = w.saturating_sub(vw + 4);
        put(
            buf,
            x + 2,
            y,
            &truncate(label, lw as usize, cx.ell()),
            style,
            lw,
        );
        let vtoken = if pending {
            "status.warning"
        } else if connected {
            "status.success"
        } else {
            MUTED
        };
        let mut vstyle = cx.st(
            vtoken,
            if selected {
                "interactive.row_selected"
            } else {
                CANVAS
            },
        );
        if selected {
            vstyle = vstyle.add_modifier(Modifier::BOLD);
        }
        put(
            buf,
            x + w - vw,
            y,
            &truncate(&value, vw as usize, cx.ell()),
            vstyle,
            vw,
        );
        hits.add(Rect::new(x, y, w, 1), Target::Onboarding(T::Row(i)));
        y += 1;
    }
    y
}

fn draw_agents(
    cx: &Cx,
    buf: &mut Buffer,
    hits: &mut HitMap,
    x: u16,
    mut y: u16,
    w: u16,
    limit: u16,
) -> u16 {
    let a = cx.app;
    let tour = Tour::of(&a.manifest);
    if !a.onboarding_connected() {
        y = paragraph(cx, buf, x, y, w, &tour.no_provider, "status.warning", limit);
        y += 1;
    }
    // Each row is its line, up to two detail lines and a gap; start at the first row from which the selected one fits.
    let height = |agent: &crate::onboarding::AgentRow| {
        let detail = format!("{} \u{2014} {}", agent.role, agent.hint);
        let effort =
            !agent.is_root() && !agent_models(a.state.config.as_ref(), &agent.name).is_empty();
        1 + usize::from(effort)
            + crate::text::wrap(&detail, w.saturating_sub(2) as usize)
                .len()
                .min(2)
            + 1
    };
    let room = limit.saturating_sub(y) as usize;
    let selected_at = a.ui.onboarding.row.min(tour.agent.len().saturating_sub(1));
    let mut first = 0;
    while first < selected_at
        && tour.agent[first..=selected_at]
            .iter()
            .map(height)
            .sum::<usize>()
            > room + 1
    {
        first += 1;
    }
    for (i, agent) in tour.agent.iter().enumerate().skip(first) {
        if y + 1 >= limit {
            break;
        }
        let selected = i == a.ui.onboarding.row;
        let style = row_style(cx, buf, x, y, w, selected);
        let name = if agent.is_root() {
            "Default agent"
        } else {
            agent.name.as_str()
        };
        let (value, effort) = if agent.is_root() {
            (root_agent(a.state.config.as_ref(), &agent.options), None)
        } else {
            let models = agent_models(a.state.config.as_ref(), &agent.name);
            let effort = models.first().map(|model| {
                let value = a
                    .agent_effort(&agent.name, model)
                    .unwrap_or_else(|| a.effort_default_label(&agent.name));
                let editable = !a.model_efforts(model).is_empty()
                    || a.agent_effort(&agent.name, model).is_some();
                (
                    a.manifest.ui_text("effort.row", &[("value", &value)]),
                    editable,
                )
            });
            (
                agent_model_line(a.state.config.as_ref(), &agent.name),
                effort,
            )
        };
        let vw = (width(&value) as u16).min(w.saturating_sub(16));
        let lw = w.saturating_sub(vw + 4);
        put(
            buf,
            x + 2,
            y,
            &truncate(name, lw as usize, cx.ell()),
            style,
            lw,
        );
        let unset = !agent.is_root() && value == "the session's model";
        let mut vstyle = cx.st(
            if unset { MUTED } else { "status.success" },
            if selected {
                "interactive.row_selected"
            } else {
                CANVAS
            },
        );
        if selected {
            vstyle = vstyle.add_modifier(Modifier::BOLD);
        }
        put(
            buf,
            x + w - vw,
            y,
            &truncate(&value, vw as usize, cx.ell()),
            vstyle,
            vw,
        );
        hits.add(Rect::new(x, y, w, 1), Target::Onboarding(T::Row(i)));
        y += 1;
        if let Some((effort, editable)) = effort {
            if y < limit {
                put(
                    buf,
                    x + 2,
                    y,
                    &effort,
                    cx.st(if editable { SECONDARY } else { MUTED }, CANVAS),
                    w.saturating_sub(2),
                );
                if editable {
                    hits.add(Rect::new(x, y, w, 1), Target::Onboarding(T::Effort(i)));
                }
                y += 1;
            }
        }
        let detail = format!("{} \u{2014} {}", agent.role, agent.hint);
        for line in crate::text::wrap(&detail, w.saturating_sub(2) as usize)
            .into_iter()
            .take(2)
        {
            if y >= limit {
                break;
            }
            put(
                buf,
                x + 2,
                y,
                &line,
                cx.st(MUTED, CANVAS),
                w.saturating_sub(2),
            );
            y += 1;
        }
        y += 1;
    }
    y
}
