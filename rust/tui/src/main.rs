//! Presentation-only terminal client. Python owns all host commands/reduction.
mod bridge;
mod copy_button;
mod disclosure;
mod editor;
mod input;
mod local_ui;
mod markdown;
mod render;
mod settings_page;
mod trace;
mod transcript;
use bridge::Snapshot;
use crossterm::{
    event::{
        self, Event, KeyCode, KeyModifiers, KeyboardEnhancementFlags, MouseEventKind,
        PopKeyboardEnhancementFlags, PushKeyboardEnhancementFlags,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use editor::Editor;
use input::{action, base64, edit, pick, save, send, MAX_DRAFT};
use ratatui::{backend::CrosstermBackend, Terminal};
use serde_json::json;
use std::{
    collections::HashMap,
    io::{self, BufRead, Write},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};
/// A row's click operation. Header rows carry one range per chip, so a click
/// opens the chip under the column; the keyboard (no column) opens the menu.
fn resolve_operation(
    operation: &Option<serde_json::Value>,
    column: Option<usize>,
) -> Option<serde_json::Value> {
    let operation = copy_button::inner(operation)?;
    if operation["kind"] != "context_chips" {
        return Some(operation.clone());
    }
    let Some(column) = column else {
        return Some(json!({"kind":"context_menu"}));
    };
    operation["chips"].as_array()?.iter().find_map(|chip| {
        let (start, end) = (
            chip["start"].as_u64()? as usize,
            chip["end"].as_u64()? as usize,
        );
        (column >= start && column < end).then(|| chip["operation"].clone())
    })
}

/// Edit distance with adjacent swaps, so `mew` is one step from `new`.
fn edit_distance(a: &str, b: &str) -> usize {
    let (a, b): (Vec<char>, Vec<char>) = (a.chars().collect(), b.chars().collect());
    let mut rows = vec![(0..=b.len()).collect::<Vec<_>>()];
    for i in 1..=a.len() {
        let mut row = vec![i];
        for j in 1..=b.len() {
            let mut c = (rows[i - 1][j] + 1)
                .min(row[j - 1] + 1)
                .min(rows[i - 1][j - 1] + usize::from(a[i - 1] != b[j - 1]));
            if i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1] {
                c = c.min(rows[i - 2][j - 2] + 1);
            }
            row.push(c);
        }
        rows.push(row);
    }
    rows[a.len()][b.len()]
}

/// Ten slash commands: prefix matches, then substring and typo matches, then the rest
/// (mirrors `ui_support.completion.rank_menu`), so the popup keeps a steady height.
fn rank_commands(commands: &[(String, Vec<String>)], needle: &str) -> Vec<String> {
    let needle = needle.trim_start_matches('/');
    let tolerance = if needle.chars().count() < 5 { 1 } else { 2 };
    let mut rows: Vec<(usize, &String)> = commands
        .iter()
        .map(|(name, aliases)| {
            let tier = std::iter::once(name)
                .chain(aliases)
                .map(|n| {
                    let n = n.trim_start_matches('/').to_lowercase();
                    if n.starts_with(needle) {
                        0
                    } else if n.contains(needle) {
                        1
                    } else if !needle.is_empty() && edit_distance(needle, &n) <= tolerance {
                        2
                    } else {
                        3
                    }
                })
                .min()
                .unwrap_or(3);
            (tier, name)
        })
        .collect();
    rows.sort();
    rows.into_iter().take(10).map(|(_, n)| n.clone()).collect()
}

/// Reuse only ancestor requests in the same token context while the host responds.
fn completion_candidates(cache: &[(String, Vec<String>)], prefix: &str) -> Vec<String> {
    let token = prefix.rsplit(char::is_whitespace).next().unwrap_or("");
    let context = &prefix[..prefix.len() - token.len()];
    cache
        .iter()
        .rev()
        .filter(|(key, _)| {
            let old_token = key.rsplit(char::is_whitespace).next().unwrap_or("");
            &key[..key.len() - old_token.len()] == context && token.starts_with(old_token)
        })
        .max_by_key(|(key, _)| key.len())
        .map(|(key, values)| {
            if key == prefix {
                return values.clone();
            }
            let needle = token.trim_start_matches('@').to_lowercase();
            let (mut hits, rest): (Vec<_>, Vec<_>) = values
                .iter()
                .partition(|value| value.to_lowercase().starts_with(&needle));
            // File menus keep their height while the host re-ranks.
            if token.starts_with('@') {
                hits.extend(rest);
                hits.truncate(10);
            }
            hits.into_iter().cloned().collect()
        })
        .unwrap_or_default()
}
struct Cleanup;
impl Drop for Cleanup {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(
            io::stderr(),
            PopKeyboardEnhancementFlags,
            event::DisableBracketedPaste,
            event::DisableMouseCapture,
            crossterm::cursor::Show,
            LeaveAlternateScreen
        );
        let _ = write!(io::stderr(), "\x1b[>4;0m");
    }
}
/// Minimum time between terminal frames (about 60 fps).
const FRAME_INTERVAL: Duration = Duration::from_millis(16);
/// `Ctrl+B`, `Ctrl+X B` and `/sessions`: show the sessions sidebar and move keyboard
/// focus into it. With `toggle_off`, a sidebar that already has focus is hidden again.
fn sessions_surface(
    s: &mut Snapshot,
    cache: &mut render::Cache,
    local_ui: &mut local_ui::LocalUi,
    toggle_off: bool,
    width: u16,
) -> io::Result<()> {
    // Narrow terminals use a local drawer (never the saved preference); wide ones the sidebar.
    let drawer = width < 90;
    let open = if drawer {
        s.sessions_drawer
    } else {
        s.sessions_sidebar
    };
    if open && cache.sessions_focus && toggle_off {
        cache.sessions_focus = false;
        if drawer {
            s.sessions_drawer = false;
            return Ok(());
        }
        return local_ui.dispatch(json!({"type":"toggle","key":"sessions_sidebar"}), s);
    }
    if !open {
        s.last_opened = "sessions".into();
        if drawer {
            s.sessions_drawer = true;
        } else {
            local_ui.dispatch(json!({"type":"toggle","key":"sessions_sidebar"}), s)?;
        }
    }
    cache.sessions_focus = true;
    select_visible_session(s, cache, true);
    Ok(())
}

/// `/sessions` or `/session` with no argument: the client opens the sidebar itself at
/// once (the host still refreshes the list), so it never waits on a round trip.
fn opens_sessions(text: &str) -> bool {
    matches!(text.trim(), "/sessions" | "/session")
}

/// Keep the keyboard selection on a session the sidebar shows (preferring the
/// current one when `prefer_active`), or the first match.
fn select_visible_session(s: &Snapshot, cache: &mut render::Cache, prefer_active: bool) {
    let visible = render::visible_sessions(s, &cache.filter, &cache.project);
    let kept = !prefer_active
        && cache
            .sessions_sel
            .as_deref()
            .is_some_and(|id| visible.iter().any(|i| s.sessions[*i].id == id));
    if !kept {
        cache.sessions_sel = visible
            .iter()
            .find(|i| prefer_active && s.sessions[**i].active)
            .or(visible.first())
            .map(|i| s.sessions[*i].id.clone());
    }
}

/// Escape or Ctrl+C in the focused sessions sidebar: close it (the drawer, or the
/// docked sidebar's saved preference, like `Ctrl+B`).
fn close_sessions(
    s: &mut Snapshot,
    cache: &mut render::Cache,
    local_ui: &mut local_ui::LocalUi,
    width: u16,
) -> io::Result<()> {
    cache.sessions_focus = false;
    cache.filtering = false;
    if width < 90 {
        s.sessions_drawer = false;
    } else if s.sessions_sidebar {
        local_ui.dispatch(json!({"type":"toggle","key":"sessions_sidebar"}), s)?;
    }
    Ok(())
}

/// The sidebar's rows exactly as drawn and hit-tested (without the selection).
fn sidebar_rows(
    s: &Snapshot,
    cache: &render::Cache,
    region: ratatui::layout::Rect,
) -> Vec<(ratatui::text::Line<'static>, Option<render::SidebarHit>)> {
    render::session_sidebar(
        s,
        &render::Palette::new(s.theme == "nexus-light"),
        usize::from(region.width.saturating_sub(3)),
        0,
        &cache.filter,
        &cache.project,
        cache.filtering,
        None,
    )
}

/// Keep the keyboard-selected session on screen: line index of its first row in the
/// drawn sidebar (row 0 is the title row, which is not scrolled).
fn sessions_follow(
    s: &Snapshot,
    cache: &render::Cache,
    region: ratatui::layout::Rect,
    scroll: usize,
) -> usize {
    let Some(id) = cache.sessions_sel.as_deref() else {
        return scroll;
    };
    let rows = sidebar_rows(s, cache, region);
    let Some(line) = rows.iter().position(
        |(_, hit)| matches!(hit, Some(render::SidebarHit::Session(i)) if s.sessions[*i].id == id),
    ) else {
        return scroll;
    };
    let height = render::sessions_list_height(s, region);
    // Scroll offsets count from row 1 (row 0 is the fixed title).
    let at = line - 1;
    // Show the group heading (and its blank row) above the first session of a group.
    let top = if matches!(rows[line - 1].1, Some(render::SidebarHit::Heading(_))) {
        at.saturating_sub(2)
    } else {
        at
    };
    if top < scroll {
        top
    } else if at + 1 > scroll + height {
        at + 1 - height
    } else {
        scroll
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::args().any(|arg| arg == "--version") {
        println!("Nexus Ratatui · bridge 3 (schema 1/2 compatible)");
        return Ok(());
    }
    let (tx, rx) = mpsc::sync_channel(2);
    thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            match line {
                Ok(line) if line.len() <= 16 * 1024 * 1024 => {
                    if tx.send((line, Instant::now())).is_err() {
                        break;
                    }
                }
                _ => break,
            }
        }
    });
    let _cleanup = Cleanup;
    enable_raw_mode()?;
    execute!(
        io::stderr(),
        EnterAlternateScreen,
        event::EnableBracketedPaste,
        event::EnableMouseCapture,
        PushKeyboardEnhancementFlags(
            KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES
                | KeyboardEnhancementFlags::REPORT_EVENT_TYPES
        )
    )?;
    write!(io::stderr(), "\x1b[>4;2m")?;
    io::stderr().flush()?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::BufWriter::with_capacity(
        64 * 1024,
        io::stderr(),
    )))?;
    let mut s = Snapshot::default();
    let mut draft = Editor::default();
    let mut session_drafts: HashMap<String, Editor> = HashMap::new();
    let mut form_editor = Editor::default();
    let mut answer = Editor::default();
    let mut prompt_id = String::new();
    let mut form_id = String::new();
    let mut form_revision = 0;
    let mut form_changed = None;
    let mut scroll = 0usize;
    let mut panel_scroll = 0usize;
    let mut sessions_scroll = 0usize;
    let mut details_scroll = 0usize;
    let mut page_positions: Vec<(String, usize, bool, usize)> = Vec::new();
    let mut panel_detail = false;
    let mut settings_nav_focus = false;
    let mut selection = 0usize;
    let mut filter = String::new();
    let mut completion_index = 0usize;
    let mut completion_cache: Vec<(String, Vec<String>)> = Vec::new();
    let mut completion_hidden = "\0".to_string();
    let mut dirty = true;
    // Frames are paced: a wheel fling used to emit hundreds of near-full-screen frames
    // faster than a terminal parses them, so a reversal waited behind the backlog.
    let mut last_draw = Instant::now() - FRAME_INTERVAL;
    let mut animating = false;
    let mut timings = trace::Trace::new();
    let mut input_received: Option<Instant> = None;
    let mut input_kind = "input→frame";
    let mut snapshot_received: Option<Instant> = None;
    let spin_clock = Instant::now();
    let mut nav: Option<usize> = None;
    let mut recording_since: Option<Instant> = None;
    let mut press: Option<(usize, usize)> = None;
    let mut typed = (String::new(), 0usize);
    let mut complete_due: Option<Instant> = None;
    let mut asked = String::new();
    let mut follow = true;
    let mut cache = render::Cache::default();
    let mut drawn_regions = render::Regions::default();
    let mut logs_open = false;
    let mut local_ui = local_ui::LocalUi::default();
    let mut details_focus = false;
    let mut details_visible = false;
    let mut details_index = 0usize;
    let mut logs_scroll = 0usize;
    let mut leader = None;
    let mut pending_sessions_focus = false;
    let mut escape = Instant::now() - Duration::from_secs(2);
    let mut last_ctrl_c = Instant::now() - Duration::from_secs(2);
    let mut history_loaded = false;
    // False until the host's first snapshot: the splash is drawn and input is held back.
    let mut loaded = false;
    'app: loop {
        // Coalesce a backlog (streamed tokens): only the newest snapshot is
        // parsed unless an older one carries a one-shot composer effect.
        let mut queued = Vec::new();
        loop {
            match rx.try_recv() {
                Ok(line) => queued.push(line),
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => {
                    if queued.is_empty() {
                        break 'app;
                    }
                    break;
                }
            }
        }
        let last = queued.len().saturating_sub(1);
        for (index, (line, received_at)) in queued.into_iter().enumerate() {
            if index < last
                && line.contains("\"schema\":1")
                && line.contains("\"restore\":\"\"")
                && line.contains("\"insert\":\"\"")
            {
                continue;
            }
            snapshot_received.get_or_insert(received_at);
            let parsed_at = Instant::now();
            let mut fields: serde_json::Map<String, serde_json::Value> =
                serde_json::from_str(&line)?;
            let transcript_changed = fields.contains_key("blocks");
            // Deserialize changed content once, then move omitted sections from the mirror.
            let block_value = fields.remove("blocks");
            let mut next: Snapshot =
                serde_json::from_value(serde_json::Value::Object(fields.clone()))?;
            if let Some(value) = block_value {
                next.blocks = serde_json::from_value(value)?;
            }
            if next.schema == 3 && !next.reset && next.generation != s.generation {
                return Err("section patch crosses session".into());
            }
            if next.revision < s.revision {
                continue;
            }
            next.merge_missing(&mut s, &fields);
            if cache.toasts.ingest(&next.toasts) {
                dirty = true;
            }
            if next.sessions_request > cache.sessions_request_seen {
                cache.sessions_request_seen = next.sessions_request;
                pending_sessions_focus = true;
            }
            if next.schema == 3 && !transcript_changed {
                next.blocks_from = s.blocks.len();
            }
            next.transcript_changed_from = if next.schema == 1 || next.reset {
                Some(0)
            } else if transcript_changed {
                Some(next.blocks_from)
            } else {
                None
            };
            trace::stall("snapshot parse", parsed_at.elapsed());
            timings.record("parse", parsed_at.elapsed());
            if !matches!(next.schema, 1 | 2 | 3) {
                return Err("unsupported bridge schema".into());
            }
            if next.revision < s.revision {
                continue;
            }
            if next.generation != s.generation {
                page_positions.clear();
                session_drafts.insert(s.composer_key.clone(), std::mem::take(&mut draft));
                draft = session_drafts
                    .remove(&next.composer_key)
                    .unwrap_or_default();
                follow = true;
                scroll = 0;
                details_scroll = 0;
                nav = None;
            }
            if next.agent_page != s.agent_page {
                if page_positions
                    .last()
                    .is_some_and(|saved| saved.0 == next.agent_page)
                {
                    let (_, saved_scroll, saved_follow, saved_details) =
                        page_positions.pop().unwrap();
                    scroll = saved_scroll;
                    follow = saved_follow;
                    details_scroll = saved_details;
                } else {
                    page_positions.push((s.agent_page.clone(), scroll, follow, details_scroll));
                    if page_positions.len() > 8 {
                        page_positions.remove(0);
                    }
                    follow = false;
                    scroll = 0;
                    details_scroll = 0;
                }
                nav = None;
            }
            cache.disclosure.scope = format!("{}|{}", next.composer_key, next.agent_page);
            if next.theme != s.theme {
                cache.reset();
            }
            if next.panel_title != s.panel_title {
                panel_scroll = 0;
                panel_detail = false;
                selection = 0;
                filter.clear();
            }
            if next.nav.is_none() {
                settings_nav_focus = false;
            }
            let new_prompt = next.prompt.as_ref().map(|p| p.id.as_str()).unwrap_or("");
            if new_prompt != prompt_id {
                answer = Editor::default();
                prompt_id = new_prompt.into();
                selection = 0;
                panel_scroll = 0;
            }
            let new_form = next.form.as_ref().map(|f| f.id.as_str()).unwrap_or("");
            if new_form != form_id {
                form_editor = Editor::default();
                if let Some(f) = &next.form {
                    form_editor.insert(&f.body);
                }
                form_id = new_form.into();
                form_changed = None;
                form_revision = 0;
            }
            if !next.restore.is_empty() {
                draft.cursor = 0;
                draft.insert(
                    &(next.restore.clone() + if draft.text.is_empty() { "" } else { "\n\n" }),
                );
            }
            if !next.insert.is_empty() {
                if next.insert_kind == "voice" {
                    let resume = draft.cursor;
                    if let Some(at) = cache.voice_cursor.take() {
                        if at <= draft.text.len() && draft.text.is_char_boundary(at) {
                            draft.cursor = at;
                        }
                    }
                    let prefix = draft.text[..draft.cursor]
                        .chars()
                        .last()
                        .is_some_and(|c| !c.is_whitespace());
                    let suffix = draft.text[draft.cursor..]
                        .chars()
                        .next()
                        .is_some_and(|c| !c.is_whitespace());
                    let insertion_at = draft.cursor;
                    let old_len = draft.text.len();
                    draft.insert(&format!(
                        "{}{}{}",
                        if prefix { " " } else { "" },
                        next.insert,
                        if suffix { " " } else { "" }
                    ));
                    if resume > insertion_at {
                        draft.cursor = (resume + draft.text.len() - old_len).min(draft.text.len());
                    }
                } else {
                    draft.insert(&next.insert);
                }
                if next.auto_send_insert && !draft.text.trim().is_empty() {
                    send(
                        json!({"type":"submit","text":draft.take(),"mode":"steer","generation":next.generation}),
                    )?;
                }
            }
            if history_loaded
                && (fields.contains_key("history") || fields.contains_key("history_append"))
                && !next.history.is_empty()
            {
                // The canonical prompt history is bounded; keep local editor history bounded too.
                draft.history = next.history.clone();
                draft.history_index = draft.history.len();
            }
            if !history_loaded {
                draft.history = next.history.clone();
                draft.history_index = draft.history.len();
                history_loaded = true;
            }
            if next.voice_phase == "recording" {
                if s.voice_phase != "recording" {
                    cache.voice_cursor = Some(draft.cursor);
                }
                let began = *recording_since.get_or_insert_with(Instant::now);
                cache.voice_elapsed = began.elapsed().as_secs();
                cache.voice_levels.push(next.voice_level as f32);
                let excess = cache.voice_levels.len().saturating_sub(28);
                cache.voice_levels.drain(..excess);
            } else if next.voice_phase != "transcribing" {
                recording_since = None;
                cache.voice_levels.clear();
                cache.voice_elapsed = 0;
                cache.voice_cursor = None;
            }
            if next.generation != s.generation {
                completion_cache.clear();
            }
            let prefix = if next.completion_prefix.is_empty() {
                &next.completion_query
            } else {
                &next.completion_prefix
            };
            if !prefix.is_empty() {
                completion_cache.retain(|(key, _)| key != prefix);
                completion_cache.push((prefix.clone(), next.completions.clone()));
                if completion_cache.len() > 32 {
                    completion_cache.remove(0);
                }
            }
            cache.note_patch(next.transcript_changed_from);
            cache.disclosure.verbose = next.transcript_verbose;
            next.restore_blocks(&mut s)?;
            local_ui.reconcile(&mut next, &s);
            logs_open = next.details_sidebar && next.details_panel.tab == "Logs";
            if next.details_panel.tab != s.details_panel.tab {
                details_scroll = 0;
                details_index = 0;
            }
            next.last_opened = s.last_opened.clone();
            // The completion cue is played by the Python shell
            // (`notify_completion`); ringing the terminal bell here as well
            // made one completion sound twice.
            s = next;
            loaded = true;
            animating = render::animating(&s);
            dirty = true;
        }
        if leader.is_some_and(|time: Instant| time.elapsed() > Duration::from_secs(3)) {
            leader = None;
            dirty = true;
        }
        if form_changed.is_some_and(|time: Instant| time.elapsed() > Duration::from_millis(700))
            && s.form.as_ref().is_some_and(|f| f.autosave)
        {
            save(&s, &form_editor, form_revision)?;
            form_changed = None;
        }
        // Cached ancestors keep rows visible on typing and backspace, without
        // borrowing candidates from a different command or attachment token.
        let prefix = &draft.text[..draft.cursor];
        let token = prefix.rsplit(char::is_whitespace).next().unwrap_or("");
        let mut completion = Snapshot::default();
        completion.theme = s.theme.clone();
        completion.command_help = s.command_help.clone();
        completion.completion_query = token.to_string();
        completion.completions = if draft.text.starts_with('!') {
            Vec::new() // shell mode: the draft is a bash command, never a slash or @ query
        } else if token.starts_with('/') && !prefix.contains(' ') && !s.commands.is_empty() {
                let needle = token.to_lowercase();
                rank_commands(&s.commands, &needle)
            } else {
                completion_candidates(&completion_cache, prefix)
            };
        let completion_visible = s.panel_title.is_empty()
            && s.prompt.is_none()
            && completion_hidden != completion.completion_query
            && !completion.completions.is_empty();
        if !completion_visible {
            dirty |= cache.component_hover.update_surfaces(
                &s,
                &drawn_regions,
                &filter,
                selection,
                panel_detail,
                cache.pointer,
                s.panel_title.is_empty()
                    && s.prompt.is_none()
                    && completion_hidden != completion.completion_query
                    && !completion.completions.is_empty(),
                Instant::now(),
            );
        }
        if completion_visible {
            dirty |= cache.component_hover.update_completion(
                &completion,
                &drawn_regions,
                completion_index,
                cache.pointer,
                Instant::now(),
            );
        }
        dirty |= cache.component_hover.needs_redraw(Instant::now());
        dirty |= cache.toasts.tick(Instant::now(), cache.pointer);
        if !(if terminal.size()?.width < 90 {
            s.sessions_drawer
        } else {
            s.sessions_sidebar
        }) {
            cache.sessions_focus = false;
        }
        if std::mem::take(&mut pending_sessions_focus) {
            details_focus = false;
            sessions_surface(
                &mut s,
                &mut cache,
                &mut local_ui,
                false,
                terminal.size()?.width,
            )?;
            dirty = true;
        }
        if !loaded {
            if dirty && last_draw.elapsed() >= FRAME_INTERVAL {
                last_draw = Instant::now();
                terminal.draw(render::draw_splash)?;
                dirty = false;
            }
        } else if dirty && last_draw.elapsed() >= FRAME_INTERVAL {
            last_draw = Instant::now();
            cache.focus = nav.and_then(|index| render::targets(&cache).get(index).copied());
            let drawing_at = Instant::now();
            let mut drawn_at = drawing_at;
            cache.settings_nav_focus = settings_nav_focus;
            terminal.draw(|frame| {
                let r = render::draw(
                    frame,
                    &s,
                    &draft,
                    &form_editor,
                    &answer,
                    &filter,
                    selection,
                    scroll,
                    panel_scroll,
                    &mut cache,
                    follow,
                    logs_open,
                    leader.is_some(),
                    panel_detail,
                    sessions_scroll,
                    logs_scroll,
                    details_scroll,
                );
                cache.component_hover.paint_tabs(frame, &s, &r);
                drawn_regions = r;
                let query = draft.text[..draft.cursor]
                    .rsplit(char::is_whitespace)
                    .next()
                    .unwrap_or("");
                if s.panel_title.is_empty()
                    && s.prompt.is_none()
                    && query == completion.completion_query
                    && completion_hidden != query
                    && !completion.completions.is_empty()
                {
                    render::completion(
                        frame,
                        &completion,
                        r.transcript,
                        completion_index,
                        &cache.component_hover,
                    );
                }
                drawn_at = Instant::now();
            })?;
            let visible = drawn_regions.details.width > 0 && s.details_panel.tab == "Logs";
            if visible != details_visible {
                details_visible = visible;
                send(json!({"type":"details_visible","open":visible}))?;
            }
            trace::stall("draw (layout+render)", drawn_at.duration_since(drawing_at));
            trace::stall("terminal flush", drawn_at.elapsed());
            timings.record("update_content", cache.content_elapsed);
            timings.record_count("layout_blocks", cache.content_blocks);
            timings.record_count("layout_reset", usize::from(cache.content_reset));
            timings.record("draw", drawn_at.duration_since(drawing_at));
            timings.record("flush", drawn_at.elapsed());
            if let Some(at) = input_received.take() {
                timings.record(input_kind, at.elapsed());
            }
            if s.event_sent_at > 0.0 {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)?
                    .as_secs_f64();
                if now >= s.event_sent_at {
                    let elapsed = Duration::from_secs_f64(now - s.event_sent_at);
                    timings.record("event→frame", elapsed);
                    if matches!(s.event_kind.as_str(), "text.delta" | "thinking.delta") {
                        timings.record("stream→frame", elapsed);
                    }
                }
                s.event_sent_at = 0.0;
            }
            if let Some(at) = snapshot_received.take() {
                timings.record("snapshot→frame", at.elapsed());
            }
            if let Some(summary) = timings.publish() {
                send(json!({"type":"ui_trace", "lines":summary}))?;
            }
            cache.component_hover.painted(Instant::now());
            dirty = false;
        }
        // As-you-type completion : a `/command`,
        // an `@file`, or a command argument asks the host after a 120 ms pause.
        if typed.0 != draft.text || typed.1 != draft.cursor {
            if typed.0 != draft.text
                && (s.attachments > 0
                    || typed.0.contains("[image ")
                    || draft.text.contains("[image ")
                    || typed.0.contains("[document ")
                    || draft.text.contains("[document "))
            {
                send(json!({"type":"draft_changed","text":draft.text,"generation":s.generation}))?;
            }
            if typed.0 != draft.text && !draft.text.is_empty() {
                follow = true; // typing returns to the live end of the conversation
            }
            typed = (draft.text.clone(), draft.cursor);
            let query = draft.text[..draft.cursor]
                .rsplit(char::is_whitespace)
                .next()
                .unwrap_or("");
            let argument = draft.text.starts_with('/') && draft.text[..draft.cursor].contains(' ');
            let local_command = query.starts_with('/') && !argument && !s.commands.is_empty();
            let triggers = !local_command
                && !draft.text.starts_with('!')
                && s.panel_title.is_empty()
                && s.prompt.is_none()
                && (query.starts_with('/') || query.starts_with('@') || argument);
            if !triggers {
                asked.clear();
            }
            complete_due = (triggers && asked != draft.text[..draft.cursor])
                .then(|| Instant::now() + Duration::from_millis(120));
            completion_index = 0;
        }
        if complete_due.is_some_and(|due| Instant::now() >= due) {
            complete_due = None;
            let prefix = draft.text[..draft.cursor].to_string();
            let query = prefix
                .rsplit(char::is_whitespace)
                .next()
                .unwrap_or("")
                .to_string();
            asked = prefix.clone();
            send(
                json!({"type":"complete","text":query,"prefix":prefix,"generation":s.generation}),
            )?;
        }
        // The animation clock advances on every pass, not only when input is idle:
        // a continuous wheel or pointer stream keeps `poll` returning true, which
        // used to freeze the spinner and activity bar while scrolling.
        if animating {
            let elapsed = spin_clock.elapsed();
            let frame = (elapsed.as_nanos() * 30 / 1_000_000_000) as usize;
            if (frame != cache.activity_frame
                && matches!(s.status.as_str(), "running" | "active" | "working"))
                || (elapsed.as_millis() / 125) as usize != cache.spin
            {
                cache.activity_frame = frame;
                cache.spin = (elapsed.as_millis() / 125) as usize;
                dirty = true;
            }
        }
        // A pending frame waits only for its slot; otherwise idle polling stays at 8 ms.
        let wait = if dirty {
            FRAME_INTERVAL
                .saturating_sub(last_draw.elapsed())
                .max(Duration::from_millis(1))
        } else {
            Duration::from_millis(8)
        };
        if !event::poll(wait.min(Duration::from_millis(8)))? {
            continue;
        }
        input_received.get_or_insert_with(Instant::now);
        let handling_at = Instant::now();
        let mut events = vec![event::read()?];
        while events.len() < 256 && event::poll(Duration::ZERO)? {
            events.push(event::read()?);
        }
        input_kind = if events.iter().any(|event| matches!(event,Event::Key(_))) { "key→frame" }
            else if events.iter().any(|event| matches!(event,Event::Mouse(mouse) if matches!(mouse.kind,MouseEventKind::ScrollUp|MouseEventKind::ScrollDown))) { "wheel→frame" }
            else if events.iter().any(|event| matches!(event,Event::Mouse(mouse) if matches!(mouse.kind,MouseEventKind::Up(_)))) { "click→frame" }
            else { "input→frame" };
        // Consume the queued burst in order; scrolling accumulates before one draw.
        for queued_event in events {
            if !loaded {
                // The session is not here yet: only quit and a resize mean anything.
                match &queued_event {
                    Event::Key(key)
                        if key.kind != event::KeyEventKind::Release
                            && key.code == KeyCode::Char('q')
                            && key.modifiers.contains(KeyModifiers::CONTROL) =>
                    {
                        break 'app;
                    }
                    Event::Resize(..) => dirty = true,
                    _ => {}
                }
                continue;
            }
            match queued_event {
                Event::Key(key) if key.kind != event::KeyEventKind::Release => {
                    if s.voice_phase == "transcribing" && key.code == KeyCode::Esc {
                        action("voice_discard", "")?;
                        continue;
                    }
                    if s.voice_phase == "recording" {
                        send(input::voice_stop(key.code))?;
                        if matches!(key.code, KeyCode::Esc | KeyCode::Enter) {
                            continue;
                        }
                    }
                    if key.code == KeyCode::Char('q')
                        && key.modifiers.contains(KeyModifiers::CONTROL)
                    {
                        action("quit", "")?;
                        break 'app;
                    }
                    if key.code == KeyCode::Esc
                        && s.panel_title.is_empty()
                        && s.prompt.is_none()
                        && drawn_regions.details.width > 0
                        && drawn_regions.details.x < drawn_regions.transcript.right()
                    {
                        local_ui
                            .dispatch(json!({"type":"toggle","key":"details_sidebar"}), &mut s)?;
                        details_focus = false;
                        dirty = true;
                        continue;
                    }
                    cache.selection = None;
                    if details_focus && s.panel_title.is_empty() && s.prompt.is_none() {
                        let tabs = ["Session", "Files", "MCP", "Logs"];
                        let index = tabs
                            .iter()
                            .position(|tab| *tab == s.details_panel.tab)
                            .unwrap_or(0);
                        match key.code {
                            KeyCode::Char('[') | KeyCode::Char(']') => {
                                let next = if key.code == KeyCode::Char(']') {
                                    (index + 1) % 4
                                } else {
                                    (index + 3) % 4
                                };
                                local_ui.dispatch(
                                    json!({"type":"details_tab","text":tabs[next]}),
                                    &mut s,
                                )?;
                                continue;
                            }
                            KeyCode::Esc => {
                                details_focus = false;
                                if drawn_regions.details.x < drawn_regions.transcript.right() {
                                    local_ui.dispatch(
                                        json!({"type":"toggle","key":"details_sidebar"}),
                                        &mut s,
                                    )?;
                                }
                                continue;
                            }
                            KeyCode::Up => {
                                details_index = details_index.saturating_sub(1);
                                details_scroll = details_scroll.saturating_sub(1);
                                dirty = true;
                                continue;
                            }
                            KeyCode::Down => {
                                details_index = (details_index + 1)
                                    .min(s.details_panel.files.len().saturating_sub(1));
                                details_scroll += 1;
                                dirty = true;
                                continue;
                            }
                            KeyCode::Enter if s.details_panel.tab == "Files" => {
                                if let Some(file) = s.details_panel.files.get(details_index) {
                                    local_ui.dispatch(
                                        json!({"type":"file_toggle","text":file.path}),
                                        &mut s,
                                    )?;
                                }
                                continue;
                            }
                            KeyCode::Char('c')
                                if s.details_panel.tab == "Session"
                                    || s.details_panel.tab == "Logs" =>
                            {
                                if let Some((_, session)) = s.details_panel.logs_header.first() {
                                    action("copy_text", session)?;
                                }
                                continue;
                            }
                            KeyCode::Char(_) => details_focus = false,
                            _ => {}
                        }
                    }
                    // Sessions filter (click the box): typing edits it, Enter keeps it, Escape clears it.
                    // In the focused sidebar, Enter ends typing and opens the selected match at once.
                    let mut open_selected = false;
                    if cache.sessions_focus
                        && key.code == KeyCode::Char('c')
                        && key.modifiers.contains(KeyModifiers::CONTROL)
                    {
                        close_sessions(&mut s, &mut cache, &mut local_ui, terminal.size()?.width)?;
                        dirty = true;
                        continue;
                    }
                    if cache.filtering {
                        match key.code {
                            KeyCode::Enter => {
                                cache.filtering = false;
                                open_selected = cache.sessions_focus;
                            }
                            // Escape clears typed text; with nothing typed it closes the sidebar.
                            KeyCode::Esc if cache.filter.is_empty() => {
                                close_sessions(
                                    &mut s,
                                    &mut cache,
                                    &mut local_ui,
                                    terminal.size()?.width,
                                )?;
                                dirty = true;
                                continue;
                            }
                            KeyCode::Esc => {
                                cache.filter.clear();
                                cache.filtering = false;
                            }
                            KeyCode::Backspace => {
                                cache.filter.pop();
                            }
                            KeyCode::Char(c)
                                if !key.modifiers.contains(KeyModifiers::CONTROL)
                                    && cache.filter.chars().count() < 80 =>
                            {
                                cache.filter.push(c)
                            }
                            _ => {}
                        }
                        if cache.sessions_focus {
                            // Keep the selection on a visible match while the filter changes.
                            select_visible_session(&s, &mut cache, false);
                        }
                        sessions_scroll = 0;
                        dirty = true;
                        if !open_selected {
                            continue;
                        }
                    }
                    // Keyboard focus in the sessions sidebar: arrows move, Enter opens, `/` or
                    // typing filters, Escape clears the filter and then closes the sidebar.
                    if cache.sessions_focus {
                        let visible = render::visible_sessions(&s, &cache.filter, &cache.project);
                        let at = cache
                            .sessions_sel
                            .as_deref()
                            .and_then(|id| visible.iter().position(|i| s.sessions[*i].id == id));
                        let move_to = |delta: isize, from: Option<usize>| {
                            let n = visible.len() as isize;
                            if n == 0 {
                                return None;
                            }
                            let base = from.map_or(if delta >= 0 { -1 } else { n }, |i| i as isize);
                            visible
                                .get((base + delta).clamp(0, n - 1) as usize)
                                .copied()
                        };
                        let narrow = terminal.size()?.width < 90;
                        let mut handled = true;
                        let mut target: Option<usize> = None;
                        match key.code {
                            KeyCode::Up => target = move_to(-1, at),
                            KeyCode::Down => target = move_to(1, at),
                            KeyCode::PageUp => target = move_to(-8, at),
                            KeyCode::PageDown => target = move_to(8, at),
                            KeyCode::Home => target = visible.first().copied(),
                            KeyCode::End => target = visible.last().copied(),
                            KeyCode::Enter => {
                                if let Some(row) = at.map(|i| &s.sessions[visible[i]]) {
                                    send(
                                        json!({"type":"session_open","workspace":row.workspace,"text":row.id,"generation":s.generation}),
                                    )?;
                                    cache.sessions_focus = false;
                                    if narrow {
                                        s.sessions_drawer = false;
                                    }
                                }
                            }
                            KeyCode::Esc => {
                                if !cache.filter.is_empty() {
                                    cache.filter.clear();
                                    sessions_scroll = 0;
                                } else {
                                    close_sessions(
                                        &mut s,
                                        &mut cache,
                                        &mut local_ui,
                                        terminal.size()?.width,
                                    )?;
                                }
                            }
                            KeyCode::Char('/')
                                if !key.modifiers.contains(KeyModifiers::CONTROL) =>
                            {
                                cache.filtering = true;
                            }
                            KeyCode::Char(c)
                                if !key
                                    .modifiers
                                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                                    && cache.filter.chars().count() < 80 =>
                            {
                                cache.filter.push(c);
                                cache.filtering = true;
                                sessions_scroll = 0;
                            }
                            _ => handled = false,
                        }
                        if handled {
                            if let Some(i) = target {
                                cache.sessions_sel = Some(s.sessions[i].id.clone());
                            } else if matches!(key.code, KeyCode::Char(_)) {
                                // The filter changed: select its first match.
                                cache.sessions_sel =
                                    render::visible_sessions(&s, &cache.filter, &cache.project)
                                        .first()
                                        .map(|i| s.sessions[*i].id.clone());
                            }
                            if cache.sessions_focus {
                                sessions_scroll = sessions_follow(
                                    &s,
                                    &cache,
                                    drawn_regions.sessions,
                                    sessions_scroll,
                                );
                            }
                            dirty = true;
                            continue;
                        }
                    }
                    // Keyboard focus over clickable transcript blocks: Tab (empty draft) enters,
                    // arrows/Tab move, Enter or Space opens what a click would, Escape or any
                    // other key leaves.
                    if let Some(index) = nav {
                        let blocks = render::targets(&cache);
                        let last = blocks.len().saturating_sub(1);
                        let next = match key.code {
                            KeyCode::Up | KeyCode::BackTab | KeyCode::Char('k') => {
                                Some(index.saturating_sub(1).min(last))
                            }
                            KeyCode::Down | KeyCode::Tab | KeyCode::Char('j') => {
                                Some((index + 1).min(last))
                            }
                            KeyCode::Home => Some(0),
                            KeyCode::End => Some(last),
                            _ => None,
                        };
                        if let Some(next) = next.filter(|_| !blocks.is_empty()) {
                            nav = Some(next);
                            let size = terminal.size()?;
                            let area = ratatui::layout::Rect::new(0, 0, size.width, size.height);
                            let height = render::regions(
                                area,
                                &s,
                                render::composer_height(area, &draft, &s),
                                logs_open,
                            )
                            .transcript
                            .height as usize;
                            let (first, end) = blocks[next];
                            if follow {
                                scroll = cache.max_scroll;
                            }
                            follow = false;
                            if first < scroll || end + 1 - first > height {
                                scroll = first;
                            } else if end >= scroll + height {
                                scroll = end + 1 - height;
                            }
                            dirty = true;
                            continue;
                        }
                        nav = None;
                        dirty = true;
                        match key.code {
                            KeyCode::Enter | KeyCode::Char(' ') => {
                                if let Some(operation) = blocks
                                    .get(index)
                                    .and_then(|(first, _)| cache.operations.get(*first))
                                    .and_then(|operation| resolve_operation(operation, None))
                                {
                                    if cache.disclosure.toggle(&operation, &s.blocks) {
                                        cache.invalidate_disclosure();
                                        dirty = true;
                                    } else {
                                        send(
                                            json!({"type":"operation","operation":&operation,"generation":s.generation}),
                                        )?;
                                    }
                                }
                                continue;
                            }
                            KeyCode::Esc => continue,
                            _ => {}
                        }
                    }
                    if key.code == KeyCode::Char('x')
                        && key.modifiers.contains(KeyModifiers::CONTROL)
                    {
                        leader = Some(Instant::now());
                        dirty = true;
                        continue;
                    }
                    if leader.take().is_some() {
                        if key.code == KeyCode::Char('c') {
                            action("context_popover", "")?;
                            continue;
                        }
                        if key.code == KeyCode::Char('z') {
                            action("update_help", "")?;
                            continue;
                        }
                        if key.code == KeyCode::Char('x') {
                            cache.toasts.dismiss_all();
                            dirty = true;
                            continue;
                        }
                        if key.code == KeyCode::Char('a') {
                            if let Some((id, operation)) = cache.toasts.newest_action() {
                                send(
                                    json!({"type":"operation","operation":operation,"generation":s.generation}),
                                )?;
                                cache.toasts.dismiss(id);
                                dirty = true;
                            }
                            continue;
                        }
                        let command = match key.code {
                            KeyCode::Char('m') => "/model",
                            KeyCode::Char('v') => "/voice",
                            KeyCode::Char('n') => "/new",
                            KeyCode::Char('o') => "/sessions",
                            KeyCode::Char('f') => "/fork",
                            KeyCode::Char('g') => "/agent",
                            KeyCode::Char('s') => "/settings",
                            KeyCode::Char('i') => "/context",
                            KeyCode::Char('u') => "/usage",
                            KeyCode::Char('r') => "/reconnect",
                            KeyCode::Char('?') => "/hotkeys",
                            _ => "",
                        };
                        if !command.is_empty() {
                            action("command", command)?;
                        } else {
                            match key.code {
                                KeyCode::Char('b') => {
                                    details_focus = false;
                                    sessions_surface(
                                        &mut s,
                                        &mut cache,
                                        &mut local_ui,
                                        true,
                                        terminal.size()?.width,
                                    )?;
                                }
                                KeyCode::Char('l') => {
                                    s.last_opened = "details".into();
                                    details_focus = true;
                                    local_ui.dispatch(
                                        json!({"type":"toggle","key":"details_sidebar"}),
                                        &mut s,
                                    )?;
                                }
                                KeyCode::Char('e') => {
                                    logs_open = !logs_open;
                                    details_focus = logs_open;
                                    s.last_opened = "details".into();
                                    local_ui.dispatch(
                                        json!({"type":"logs","open":logs_open}),
                                        &mut s,
                                    )?;
                                }
                                KeyCode::Char('t') => action("cycle_effort", "")?,
                                _ => {}
                            }
                        }
                        dirty = true;
                        continue;
                    }
                    if let Some(prompt) = &s.prompt {
                        match key.code {
                            KeyCode::Up => selection = selection.saturating_sub(1),
                            KeyCode::Down | KeyCode::Tab => {
                                selection =
                                    (selection + 1).min(prompt.choices.len().saturating_sub(1))
                            }
                            KeyCode::Enter if answer.text.is_empty() => {
                                if let Some(choice) = prompt
                                    .choices
                                    .get(selection)
                                    .filter(|choice| !choice.disabled)
                                {
                                    send(
                                        json!({"type":"answer","text":prompt.id,"value":choice.value,"generation":s.generation}),
                                    )?;
                                }
                            }
                            KeyCode::PageDown => panel_scroll += 10,
                            KeyCode::PageUp => panel_scroll = panel_scroll.saturating_sub(10),
                            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                                action("cancel", "")?
                            }
                            _ => {
                                if prompt.kind == "question" {
                                    if key.code == KeyCode::Enter && !answer.text.trim().is_empty()
                                    {
                                        send(
                                            json!({"type":"answer","text":prompt.id,"value":answer.take(),"generation":s.generation}),
                                        )?;
                                    } else {
                                        edit(&mut answer, key, false);
                                    }
                                } else if let KeyCode::Char(c) = key.code {
                                    if let Some(choice) = prompt
                                        .choices
                                        .iter()
                                        .find(|c2| c2.key == c.to_string() && !c2.disabled)
                                    {
                                        send(
                                            json!({"type":"answer","text":prompt.id,"value":choice.value,"generation":s.generation}),
                                        )?;
                                    }
                                }
                            }
                        }
                        dirty = true;
                        continue;
                    }
                    if s.panel_title.starts_with("Context · as of") && key.code == KeyCode::Enter {
                        action("command", "/context")?;
                        continue;
                    }
                    if logs_open && s.panel_title.is_empty() && s.prompt.is_none() {
                        if key.code == KeyCode::PageUp {
                            logs_scroll = (logs_scroll + 5).min(s.logs.len());
                            dirty = true;
                            continue;
                        }
                        if key.code == KeyCode::PageDown {
                            logs_scroll = logs_scroll.saturating_sub(5);
                            dirty = true;
                            continue;
                        }
                        if key.code == KeyCode::Esc
                            || key.code == KeyCode::Char('c')
                                && key.modifiers.contains(KeyModifiers::CONTROL)
                        {
                            logs_open = false;
                            local_ui.dispatch(json!({"type":"logs","open":false}), &mut s)?;
                            dirty = true;
                            continue;
                        }
                        if key.code == KeyCode::Char('a')
                            && key.modifiers.contains(KeyModifiers::CONTROL)
                        {
                            local_ui.dispatch(json!({"type":"logs_fold"}), &mut s)?;
                            continue;
                        }
                    }
                    if s.form.is_some() {
                        if key.code == KeyCode::Char('c')
                            && key.modifiers.contains(KeyModifiers::CONTROL)
                            && form_editor.selection().is_some()
                        {
                            action("copy_text", form_editor.selected())?;
                            continue;
                        }
                        if key.code == KeyCode::Esc
                            || key.code == KeyCode::Char('c')
                                && key.modifiers.contains(KeyModifiers::CONTROL)
                        {
                            if let Some(form) = &s.form {
                                if form.autosave {
                                    save(&s, &form_editor, form_revision)?;
                                } else if !form.secret {
                                    send(
                                        json!({"type":"form_draft","form":form.id,"body":form_editor.text,"revision":form_revision,"generation":s.generation}),
                                    )?;
                                }
                            }
                            form_changed = None;
                            action("dismiss", "")?;
                        } else if key.code == KeyCode::Char('s')
                            && key.modifiers.contains(KeyModifiers::CONTROL)
                        {
                            save(&s, &form_editor, form_revision)?;
                            form_changed = None;
                        } else if key.code == KeyCode::Char('d')
                            && key.modifiers.contains(KeyModifiers::CONTROL)
                        {
                            action("form_delete", "")?;
                        } else {
                            let before = form_editor.text.clone();
                            edit(&mut form_editor, key, true);
                            if before != form_editor.text {
                                form_revision += 1;
                                form_changed = Some(Instant::now());
                            }
                        }
                        dirty = true;
                        continue;
                    }
                    if !s.panel_title.is_empty() {
                        if key.code == KeyCode::Char('i')
                            && key.modifiers.contains(KeyModifiers::CONTROL)
                        {
                            if let Some(op) = s
                                .items
                                .iter()
                                .filter(|row| row.matches(&filter))
                                .nth(selection)
                                .and_then(|item| item.info_operation.as_ref())
                            {
                                send(
                                    json!({"type":"operation","operation":op,"generation":s.generation}),
                                )?;
                                dirty = true;
                                continue;
                            }
                        }
                        if s.panel_title == "Provider usage"
                            && (key.code == KeyCode::Char('r')
                                || key.code == KeyCode::Char('u')
                                    && key.modifiers.contains(KeyModifiers::CONTROL))
                        {
                            action("command", "/usage")?;
                            dirty = true;
                            continue;
                        }
                        if key.code == KeyCode::Tab && !s.items.is_empty() {
                            panel_detail = !panel_detail;
                            dirty = true;
                            continue;
                        }
                        if (key.code == KeyCode::PageDown || key.code == KeyCode::PageUp)
                            && !s.items.is_empty()
                        {
                            panel_detail = true;
                            panel_scroll = if key.code == KeyCode::PageDown {
                                panel_scroll + 10
                            } else {
                                panel_scroll.saturating_sub(10)
                            };
                            dirty = true;
                            continue;
                        }
                        // Alt+1…9 jumps to the n-th Settings area (Ctrl+digit is not delivered by terminals).
                        if let (Some(nav), KeyCode::Char(digit @ '1'..='9')) =
                            (s.nav.as_ref(), key.code)
                        {
                            if key.modifiers.contains(KeyModifiers::ALT) && !s.panel_loading {
                                let n = digit as usize - '1' as usize;
                                if let Some((index, _)) = nav
                                    .items
                                    .iter()
                                    .enumerate()
                                    .filter(|(_, item)| !item.2)
                                    .nth(n)
                                {
                                    send(
                                        json!({"type":"nav_select","text":index.to_string(),"generation":s.generation}),
                                    )?;
                                    dirty = true;
                                    continue;
                                }
                            }
                        }
                        // A typed one-page Settings area owns its keys; what it does not use passes on.
                        if !settings_nav_focus && !s.panel_loading {
                            if let Some(raw) = s.settings_page.as_ref() {
                                let page = cache.page_for(s.revision, raw);
                                let mut state = std::mem::take(&mut cache.page_state);
                                let act = settings_page::key(&page, &mut state, key);
                                cache.page_state = state;
                                match act {
                                    settings_page::Act::Pass => {}
                                    settings_page::Act::Done => {
                                        dirty = true;
                                        continue;
                                    }
                                    settings_page::Act::Send(operation) => {
                                        send(
                                            json!({"type":"operation","operation":operation,"generation":s.generation}),
                                        )?;
                                        dirty = true;
                                        continue;
                                    }
                                    settings_page::Act::Close => {
                                        action("dismiss", "")?;
                                        continue;
                                    }
                                    settings_page::Act::Nav => {
                                        settings_nav_focus = true;
                                        dirty = true;
                                        continue;
                                    }
                                }
                            }
                        }
                        if s.nav.is_some() {
                            if s.panel_loading
                                && matches!(
                                    key.code,
                                    KeyCode::Esc
                                        | KeyCode::Enter
                                        | KeyCode::Left
                                        | KeyCode::Right
                                        | KeyCode::Up
                                        | KeyCode::Down
                                )
                            {
                                continue;
                            }
                            if key.code == KeyCode::Left && s.form.is_none() {
                                settings_nav_focus = true;
                                dirty = true;
                                continue;
                            }
                            if key.code == KeyCode::Right && settings_nav_focus {
                                settings_nav_focus = false;
                                dirty = true;
                                continue;
                            }
                            if settings_nav_focus && matches!(key.code, KeyCode::Up | KeyCode::Down)
                            {
                                if let Some(index) = s.nav.as_ref().and_then(|nav| {
                                    render::nav_step(nav, key.code == KeyCode::Down)
                                }) {
                                    send(
                                        json!({"type":"nav_select","text":index.to_string(),"generation":s.generation}),
                                    )?;
                                }
                                dirty = true;
                                continue;
                            }
                            if settings_nav_focus && key.code == KeyCode::Enter {
                                continue;
                            }
                        }
                        if key.code == KeyCode::Esc
                            || key.code == KeyCode::Char('c')
                                && key.modifiers.contains(KeyModifiers::CONTROL)
                        {
                            action("dismiss", "")?;
                        } else if !s.items.is_empty() && !panel_detail {
                            let count = s.items.iter().filter(|row| row.matches(&filter)).count();
                            let selected = s
                                .items
                                .iter()
                                .filter(|row| row.matches(&filter))
                                .nth(selection);
                            let control_op = selected.and_then(|row| match key.code {
                                KeyCode::Up if key.modifiers.contains(KeyModifiers::ALT) => {
                                    row.move_up.as_ref()
                                }
                                KeyCode::Down if key.modifiers.contains(KeyModifiers::ALT) => {
                                    row.move_down.as_ref()
                                }
                                KeyCode::Delete => row.remove.as_ref(),
                                _ => None,
                            });
                            if let Some(op) = control_op {
                                send(
                                    json!({"type":"operation","operation":op,"generation":s.generation}),
                                )?;
                                dirty = true;
                                continue;
                            }
                            match key.code {
                                KeyCode::Up => selection = selection.saturating_sub(1),
                                KeyCode::Down => {
                                    selection = (selection + 1).min(count.saturating_sub(1))
                                }
                                KeyCode::Enter => pick(&s, selection, &filter)?,
                                KeyCode::Char(' ') => input::toggle(&s, selection, &filter)?,
                                KeyCode::Backspace => {
                                    filter.pop();
                                    selection = 0;
                                }
                                KeyCode::Char('r')
                                    if key.modifiers.contains(KeyModifiers::CONTROL) =>
                                {
                                    // A row button (MCP Restart) owns Ctrl+R; elsewhere it refreshes models.
                                    match selected.and_then(|row| row.action_operation.clone()) {
                                        Some(op) => send(
                                            json!({"type":"operation","operation":op,"generation":s.generation}),
                                        )?,
                                        None => action("refresh_models", "")?,
                                    }
                                }
                                KeyCode::Char('s')
                                    if key.modifiers.contains(KeyModifiers::CONTROL)
                                        && s.panel_title.starts_with("Models") =>
                                {
                                    action("model_sort", "")?
                                }
                                KeyCode::Char('f')
                                    if key.modifiers.contains(KeyModifiers::CONTROL) =>
                                {
                                    send(
                                        json!({"type":"favorite","selection":selection,"filter":filter}),
                                    )?
                                }
                                KeyCode::Char(c)
                                    if !key.modifiers.contains(KeyModifiers::CONTROL) =>
                                {
                                    filter.push(c);
                                    selection = 0;
                                }
                                _ => {}
                            }
                        } else {
                            match key.code {
                                KeyCode::Enter if panel_detail => {
                                    panel_detail = false;
                                }
                                KeyCode::Char(' ') if s.panel_toggle.is_some() => {
                                    send(
                                        json!({"type":"operation","operation":s.panel_toggle,"generation":s.generation}),
                                    )?;
                                }
                                KeyCode::PageDown => panel_scroll += 10,
                                KeyCode::Down => panel_scroll += 1,
                                KeyCode::Up => panel_scroll = panel_scroll.saturating_sub(1),
                                KeyCode::PageUp => panel_scroll = panel_scroll.saturating_sub(10),
                                _ => {}
                            }
                        }
                        dirty = true;
                        continue;
                    }
                    let query = draft.text[..draft.cursor]
                        .rsplit(char::is_whitespace)
                        .next()
                        .unwrap_or("")
                        .to_string();
                    if completion.completion_query == query
                        && completion_hidden != query
                        && !completion.completions.is_empty()
                    {
                        match key.code {
                            KeyCode::Up => {
                                completion_index = completion_index.saturating_sub(1);
                                dirty = true;
                                continue;
                            }
                            KeyCode::Down => {
                                completion_index =
                                    (completion_index + 1).min(completion.completions.len() - 1);
                                dirty = true;
                                continue;
                            }
                            KeyCode::Esc => {
                                completion_hidden = query;
                                dirty = true;
                                continue;
                            }
                            // The terminal: Enter on a standalone `/command` runs the highlighted
                            // command; on an argument that is already complete it submits.
                            KeyCode::Enter
                                if draft.text.starts_with('/')
                                    && !draft.text.contains(char::is_whitespace) =>
                            {
                                let value = completion.completions
                                    [completion_index.min(completion.completions.len() - 1)]
                                .clone();
                                draft.take();
                                if opens_sessions(&value) {
                                    details_focus = false;
                                    sessions_surface(&mut s, &mut cache, &mut local_ui, false, terminal.size()?.width)?;
                                }
                                send(
                                    json!({"type":"submit","text":value,"mode":"steer","generation":s.generation}),
                                )?;
                                dirty = true;
                                continue;
                            }
                            KeyCode::Enter
                                if draft.text.starts_with('/')
                                    && completion.completions.iter().any(|item| *item == query) => {
                            }
                            KeyCode::Enter | KeyCode::Tab => {
                                let value = &completion.completions
                                    [completion_index.min(completion.completions.len() - 1)];
                                for _ in query.graphemes(true) {
                                    draft.backspace();
                                }
                                if query.starts_with('@') {
                                    draft.insert("@");
                                }
                                draft.insert(value);
                                draft.insert(" ");
                                completion_hidden = query;
                                dirty = true;
                                continue;
                            }
                            _ => {}
                        }
                    }
                    if !s.agent_page.is_empty() && s.panel_title.is_empty() {
                        match key.code {
                            KeyCode::Esc | KeyCode::Up => {
                                action("dismiss", "")?;
                                continue;
                            }
                            KeyCode::PageUp => {
                                if follow {
                                    scroll = cache.max_scroll;
                                }
                                scroll = scroll.saturating_sub(10);
                                follow = scroll >= cache.max_scroll;
                            }
                            KeyCode::PageDown | KeyCode::Down => {
                                scroll = (scroll
                                    + if key.code == KeyCode::PageDown { 10 } else { 1 })
                                .min(cache.max_scroll);
                                follow = scroll >= cache.max_scroll;
                            }
                            KeyCode::Home => {
                                follow = false;
                                scroll = 0;
                            }
                            KeyCode::End => {
                                follow = true;
                            }
                            KeyCode::Tab => {
                                if !render::targets(&cache).is_empty() {
                                    nav = Some(0);
                                }
                            }
                            _ => continue,
                        }
                        dirty = true;
                        continue;
                    }
                    // F6 moves keyboard focus between the composer and the sessions sidebar.
                    if key.code == KeyCode::F(6)
                        || key.code == KeyCode::BackTab
                            && key.modifiers.contains(KeyModifiers::CONTROL)
                    {
                        if cache.sessions_focus {
                            cache.sessions_focus = false;
                        } else {
                            details_focus = false;
                            sessions_surface(
                                &mut s,
                                &mut cache,
                                &mut local_ui,
                                false,
                                terminal.size()?.width,
                            )?;
                        }
                        dirty = true;
                        continue;
                    }
                    if key.modifiers.contains(KeyModifiers::CONTROL) {
                        let command = match key.code {
                            KeyCode::Char('n') => "/new",
                            KeyCode::Char('o') => "/sessions",
                            KeyCode::Char('f') => "/fork",
                            KeyCode::Char('g') => "/agent",
                            KeyCode::Char('s') => "/settings",
                            KeyCode::Char('i') => "/context",
                            KeyCode::Char('p') => "/help",
                            KeyCode::Char('u') => "/usage",
                            KeyCode::Char('r') => "/reconnect",
                            KeyCode::Char(' ') | KeyCode::Null => "/voice",
                            _ => "",
                        };
                        if !command.is_empty() {
                            action("command", command)?;
                            continue;
                        }
                        match key.code {
                            KeyCode::Char('b') => {
                                details_focus = false;
                                sessions_surface(
                                    &mut s,
                                    &mut cache,
                                    &mut local_ui,
                                    true,
                                    terminal.size()?.width,
                                )?;
                                dirty = true;
                                continue;
                            }
                            KeyCode::Char('l') => {
                                s.last_opened = "details".into();
                                details_focus = true;
                                local_ui.dispatch(
                                    json!({"type":"toggle","key":"details_sidebar"}),
                                    &mut s,
                                )?;
                                continue;
                            }
                            KeyCode::Char('t') => {
                                action("cycle_effort", "")?;
                                continue;
                            }
                            KeyCode::Char('v') => {
                                action("clipboard", "")?;
                                continue;
                            }
                            KeyCode::Char('c') if draft.selection().is_some() => {
                                action("copy_text", draft.selected())?;
                                continue;
                            }
                            // Text in the composer: Ctrl+C clears it first; an empty one cancels the turn.
                            // Ctrl+C twice on an empty composer within 1.5 s quits; clearing a
                            // draft never counts as the first press, so clear-then-cancel is safe.
                            KeyCode::Char('c') if !draft.text.is_empty() => {
                                draft.clear();
                                dirty = true;
                                last_ctrl_c = Instant::now() - Duration::from_secs(2);
                                continue;
                            }
                            KeyCode::Char('c') => {
                                if last_ctrl_c.elapsed() < Duration::from_millis(1500) {
                                    action("quit", "")?;
                                    break 'app;
                                }
                                last_ctrl_c = Instant::now();
                                action("cancel", "")?;
                                continue;
                            }
                            _ => {}
                        }
                    }
                    match key.code {
                        // Escape closes a visible sessions sidebar first (focused or not);
                        // otherwise a double Escape cancels the turn.
                        KeyCode::Esc if drawn_regions.sessions.width > 0 => {
                            close_sessions(
                                &mut s,
                                &mut cache,
                                &mut local_ui,
                                terminal.size()?.width,
                            )?;
                        }
                        KeyCode::Esc => {
                            if escape.elapsed() < Duration::from_millis(1500) {
                                action("cancel", "")?;
                            }
                            escape = Instant::now();
                        }
                        KeyCode::Enter if key.modifiers.contains(KeyModifiers::SHIFT) => {
                            draft.insert("\n")
                        }
                        KeyCode::Enter => {
                            if !draft.text.trim().is_empty() || s.attachments > 0 {
                                let mode = if key.modifiers.contains(KeyModifiers::CONTROL) {
                                    "queue"
                                } else if key.modifiers.contains(KeyModifiers::ALT) {
                                    "interrupt"
                                } else {
                                    "steer"
                                };
                                let text = draft.take();
                                if opens_sessions(&text) {
                                    details_focus = false;
                                    sessions_surface(&mut s, &mut cache, &mut local_ui, false, terminal.size()?.width)?;
                                }
                                send(
                                    json!({"type":"submit","text":text,"mode":mode,"generation":s.generation}),
                                )?;
                                follow = true;
                            }
                        }
                        KeyCode::PageDown => {
                            scroll = (scroll + 10).min(cache.max_scroll);
                            // Reaching the bottom resumes auto-scroll.
                            follow = scroll >= cache.max_scroll;
                        }
                        KeyCode::PageUp => {
                            if follow {
                                scroll = cache.max_scroll;
                            }
                            scroll = scroll.saturating_sub(10);
                            follow = scroll >= cache.max_scroll;
                        }
                        KeyCode::End if key.modifiers.contains(KeyModifiers::CONTROL) => {
                            follow = true;
                            draft.cursor = draft.text.len();
                        }
                        KeyCode::Up | KeyCode::Down => {
                            let down = key.code == KeyCode::Down;
                            let shift = key.modifiers.contains(KeyModifiers::SHIFT);
                            let width =
                                usize::from(drawn_regions.composer.width.saturating_sub(9).max(1));
                            if !shift && draft.visual_layout(width).rows.len() == 1 {
                                draft.history(!down);
                            } else {
                                draft.select_move(shift);
                                draft.vertical_wrapped(down, width);
                            }
                        }
                        KeyCode::BackTab => action("cycle_agent", "")?,
                        KeyCode::Tab
                            if draft.text.is_empty()
                                && s.panel_title.is_empty()
                                && s.prompt.is_none()
                                && !render::targets(&cache).is_empty() =>
                        {
                            nav = Some(render::targets(&cache).len() - 1);
                        }
                        KeyCode::Tab => {
                            let query = draft.text[..draft.cursor]
                                .rsplit(char::is_whitespace)
                                .next()
                                .unwrap_or("");
                            completion_hidden = "\0".to_string();
                            completion_index = 0;
                            complete_due = None;
                            asked = draft.text[..draft.cursor].to_string();
                            send(
                                json!({"type":"complete","text":query,"prefix":draft.text[..draft.cursor],"generation":s.generation}),
                            )?;
                        }
                        _ => edit(&mut draft, key, true),
                    }
                    dirty = true;
                }
                Event::Paste(text) => {
                    if !s.agent_page.is_empty() && s.panel_title.is_empty() {
                        continue;
                    }
                    let text: String = text
                        .chars()
                        .filter(|c| !c.is_control() || *c == '\n' || *c == '\t')
                        .collect();
                    if text.len() <= MAX_DRAFT {
                        if s.form.is_some() {
                            form_editor.insert(&text);
                            form_revision += 1;
                            form_changed = Some(Instant::now());
                        } else if s.prompt.as_ref().is_some_and(|p| p.kind == "question") {
                            answer.insert(&text);
                        } else if s.prompt.is_none() {
                            draft.insert(&text);
                        }
                        dirty = true;
                    }
                }
                Event::Resize(_, _) => {
                    cache.reset();
                    dirty = true;
                }
                Event::Mouse(mouse) => {
                    let r = drawn_regions;
                    let pointer = Some((mouse.column, mouse.row));
                    if cache.pointer != pointer {
                        let left_transcript = cache.pointer.is_some_and(|(x, y)| {
                            r.transcript.contains(ratatui::layout::Position::new(x, y))
                        });
                        cache.pointer = pointer;
                        // Pointer motion is presentation-only; redraw only if a
                        // transcript hover target or a component transition changes.
                        if mouse.kind != MouseEventKind::Moved
                            || left_transcript
                            || r.transcript
                                .contains(ratatui::layout::Position::new(mouse.column, mouse.row))
                        {
                            dirty = true;
                        }
                    }
                    // A one-page Settings area takes the wheel and left clicks over its page
                    // (the area list on the left keeps its own handling).
                    if let Some(raw) = s
                        .settings_page
                        .as_ref()
                        .filter(|_| !s.panel_title.is_empty())
                    {
                        let area = render::panel_area(render::panel_host(&r, &s), &s);
                        let at = ratatui::layout::Position::new(mouse.column, mouse.row);
                        if area.contains(at) && !render::nav_rect(area).contains(at) {
                            let wheel = match mouse.kind {
                                MouseEventKind::ScrollUp => Some(-3),
                                MouseEventKind::ScrollDown => Some(3),
                                _ => None,
                            };
                            if let Some(delta) = wheel {
                                cache.page_state.scroll.offset =
                                    (i32::from(cache.page_state.scroll.offset) + delta)
                                        .clamp(0, 2000) as u16;
                                dirty = true;
                                continue;
                            }
                            if mouse.kind == MouseEventKind::Down(event::MouseButton::Left) {
                                let page = cache.page_for(s.revision, raw);
                                let mut state = std::mem::take(&mut cache.page_state);
                                let act = settings_page::click(
                                    &page,
                                    &mut state,
                                    mouse.column,
                                    mouse.row,
                                );
                                cache.page_state = state;
                                match act {
                                    settings_page::Act::Pass => {}
                                    settings_page::Act::Done | settings_page::Act::Nav => {
                                        dirty = true;
                                        continue;
                                    }
                                    settings_page::Act::Send(operation) => {
                                        send(
                                            json!({"type":"operation","operation":operation,"generation":s.generation}),
                                        )?;
                                        dirty = true;
                                        continue;
                                    }
                                    settings_page::Act::Close => {
                                        action("dismiss", "")?;
                                        continue;
                                    }
                                }
                            }
                        }
                    }
                    match mouse.kind {
                        MouseEventKind::ScrollUp => {
                            if !s.panel_title.is_empty() {
                                if s.form.is_none() && !s.items.is_empty() && !panel_detail {
                                    selection = selection.saturating_sub(3);
                                } else {
                                    panel_scroll = panel_scroll.saturating_sub(3);
                                }
                                dirty = true;
                                continue;
                            }
                            if logs_open
                                && render::logs_region(&r)
                                    .contains((mouse.column, mouse.row).into())
                            {
                                logs_scroll = (logs_scroll + 3).min(s.logs.len());
                                dirty = true;
                                continue;
                            }
                            if r.details.contains((mouse.column, mouse.row).into()) {
                                details_scroll = details_scroll.saturating_sub(3);
                                dirty = true;
                                continue;
                            }
                            if r.sessions.contains((mouse.column, mouse.row).into()) {
                                sessions_scroll = sessions_scroll.saturating_sub(3);
                                dirty = true;
                                continue;
                            }
                            if s.panel_title.is_empty() && s.prompt.is_none() {
                                if follow {
                                    scroll = cache.max_scroll;
                                }
                                scroll = scroll.saturating_sub(3);
                                follow = scroll >= cache.max_scroll;
                            } else {
                                panel_scroll = panel_scroll.saturating_sub(3);
                            }
                        }
                        MouseEventKind::ScrollDown => {
                            if !s.panel_title.is_empty() {
                                if s.form.is_none() && !s.items.is_empty() && !panel_detail {
                                    let count = s
                                        .items
                                        .iter()
                                        .filter(|item| {
                                            item.label
                                                .to_lowercase()
                                                .contains(&filter.to_lowercase())
                                        })
                                        .count();
                                    selection = (selection + 3).min(count.saturating_sub(1));
                                } else {
                                    panel_scroll += 3;
                                }
                                dirty = true;
                                continue;
                            }
                            if logs_open
                                && render::logs_region(&r)
                                    .contains((mouse.column, mouse.row).into())
                            {
                                logs_scroll = logs_scroll.saturating_sub(3);
                                dirty = true;
                                continue;
                            }
                            if r.details.contains((mouse.column, mouse.row).into()) {
                                details_focus = true;
                                if mouse.row == r.details.y {
                                    if let Some(tab) =
                                        render::details_tab_at(r.details, mouse.column)
                                    {
                                        local_ui.dispatch(
                                            json!({"type":"details_tab","text":tab}),
                                            &mut s,
                                        )?;
                                    }
                                    dirty = true;
                                    continue;
                                }
                                if mouse.row == r.details.y + 1 && s.details_panel.tab == "Logs" {
                                    if let Some((_, session)) = s.details_panel.logs_header.first()
                                    {
                                        action("copy_text", session)?;
                                    }
                                    continue;
                                }
                                let palette = render::Palette::new(s.theme == "nexus-light");
                                let rows = cache
                                    .detail_rows(&s, &palette, r.details.width.saturating_sub(5))
                                    .0
                                    .len();
                                details_scroll =
                                    (details_scroll + 3).min(rows.saturating_sub(
                                        r.details.height.saturating_sub(1) as usize,
                                    ));
                                dirty = true;
                                continue;
                            }
                            if r.sessions.contains((mouse.column, mouse.row).into()) {
                                sessions_scroll = (sessions_scroll + 3).min(
                                    sidebar_rows(&s, &cache, r.sessions).len().saturating_sub(
                                        1 + render::sessions_list_height(&s, r.sessions),
                                    ),
                                );
                                dirty = true;
                                continue;
                            }
                            if s.panel_title.is_empty() && s.prompt.is_none() {
                                scroll = (scroll + 3).min(cache.max_scroll);
                                // Back at the bottom: follow new output again.
                                follow = scroll >= cache.max_scroll;
                            } else {
                                panel_scroll += 3;
                            }
                        }
                        MouseEventKind::Down(event::MouseButton::Right) => {
                            if !s.panel_title.is_empty() || s.prompt.is_some() {
                                continue;
                            }
                            if r.sessions.contains((mouse.column, mouse.row).into()) {
                                if let Some(row) = render::sessions_row_at(
                                    &s,
                                    r.sessions,
                                    sessions_scroll,
                                    mouse.row,
                                )
                                .and_then(|at| {
                                    sidebar_rows(&s, &cache, r.sessions).into_iter().nth(at)
                                })
                                .and_then(|(_, hit)| match hit {
                                    Some(render::SidebarHit::Session(index)) => {
                                        s.sessions.get(index)
                                    }
                                    _ => None,
                                }) {
                                    send(
                                        json!({"type":"session_actions","workspace":row.workspace,"text":row.id,"generation":s.generation}),
                                    )?;
                                }
                            }
                        }
                        MouseEventKind::Drag(event::MouseButton::Left) => {
                            if let Some(start) = press {
                                let offset = if follow { cache.max_scroll } else { scroll };
                                let row = usize::from(
                                    mouse.row.clamp(
                                        r.transcript.y,
                                        r.transcript.y + r.transcript.height.saturating_sub(1),
                                    ) - r.transcript.y,
                                );
                                let column =
                                    usize::from(mouse.column.saturating_sub(r.transcript.x));
                                cache.selection = Some((start, (offset + row, column + 1)));
                            }
                        }
                        MouseEventKind::Up(event::MouseButton::Left) => {
                            if let Some((line, column)) = press.take() {
                                let text = if cache
                                    .selection
                                    .is_some_and(|(a, b)| a.0 != b.0 || a.1.abs_diff(b.1) > 1)
                                {
                                    render::selected_text(&cache)
                                } else {
                                    String::new()
                                };
                                if text.is_empty() {
                                    cache.selection = None;
                                    if let Some(copied) = copy_button::hit(
                                        &cache.operations,
                                        &cache.lines,
                                        line,
                                        column,
                                    )
                                    .and_then(|(block, fence)| {
                                        copy_button::text(&s.blocks, &block, fence)
                                    }) {
                                        let mut tty = io::stderr();
                                        write!(tty, "\x1b]52;c;{}\x07", base64(copied.as_bytes()))?;
                                        tty.flush()?;
                                        send(
                                            json!({"type":"copy_selection","text":copied,"generation":s.generation}),
                                        )?;
                                    } else if let Some(operation) =
                                        cache.operations.get(line).and_then(|operation| {
                                            resolve_operation(operation, Some(column))
                                        })
                                    {
                                        if cache.disclosure.toggle(&operation, &s.blocks) {
                                            cache.invalidate_disclosure();
                                        } else {
                                            send(
                                                json!({"type":"operation","operation":&operation,"generation":s.generation}),
                                            )?;
                                        }
                                    }
                                } else {
                                    // OSC 52 reaches the user's terminal even over SSH; Python also tries the desktop clipboard.
                                    let mut tty = io::stderr();
                                    write!(tty, "\x1b]52;c;{}\x07", base64(text.as_bytes()))?;
                                    tty.flush()?;
                                    send(
                                        json!({"type":"copy_selection","text":text,"generation":s.generation}),
                                    )?;
                                }
                            }
                        }
                        MouseEventKind::Down(event::MouseButton::Left) => {
                            if cache.sessions_focus
                                && !r.sessions.contains((mouse.column, mouse.row).into())
                            {
                                cache.sessions_focus = false;
                                dirty = true;
                            }
                            if let Some(hit) = cache.toasts.hit(mouse.column, mouse.row) {
                                use render::toasts::Hit;
                                match hit {
                                    Hit::Close(id) => cache.toasts.dismiss(id),
                                    Hit::Action(id) => {
                                        if let Some(operation) = cache.toasts.action_of(id) {
                                            send(
                                                json!({"type":"operation","operation":operation,"generation":s.generation}),
                                            )?;
                                        }
                                        cache.toasts.dismiss(id);
                                    }
                                    Hit::Body(_) => {}
                                }
                                dirty = true;
                                continue;
                            }
                            if let Some((id, op)) =
                                render::queue_control_at(r.composer, &s, mouse.column, mouse.row)
                            {
                                send(
                                    json!({"type":"queue_edit","text":id,"key":op,"generation":s.generation}),
                                )?;
                                continue;
                            }
                            if render::update_notice_at(&s, r.workspace, mouse.column, mouse.row) {
                                action("update_help", "")?;
                                continue;
                            }

                            let query = draft.text[..draft.cursor]
                                .rsplit(char::is_whitespace)
                                .next()
                                .unwrap_or("");
                            if s.panel_title.is_empty()
                                && s.prompt.is_none()
                                && query == completion.completion_query
                                && completion_hidden != query
                                && !completion.completions.is_empty()
                            {
                                let area = render::completion_area(
                                    r.transcript,
                                    completion.completions.len(),
                                );
                                if area.contains((mouse.column, mouse.row).into()) {
                                    let row = usize::from(mouse.row.saturating_sub(area.y));
                                    let index = completion_index.saturating_sub(9) + row;
                                    if mouse.row >= area.y && mouse.row < area.bottom() {
                                        if let Some(value) = completion.completions.get(index) {
                                            let start = draft.cursor - query.len();
                                            let replacement = format!(
                                                "{}{value} ",
                                                if query.starts_with('@') { "@" } else { "" }
                                            );
                                            draft
                                                .text
                                                .replace_range(start..draft.cursor, &replacement);
                                            draft.cursor = start + replacement.len();
                                            completion_hidden = value.clone();
                                        }
                                    }
                                    dirty = true;
                                    continue;
                                }
                            }
                            if !s.panel_title.is_empty() {
                                if s.panel_loading {
                                    continue;
                                }
                                if let Some(nav) = &s.nav {
                                    let list = render::nav_rect(render::panel_area(
                                        render::panel_host(&r, &s),
                                        &s,
                                    ));
                                    if list.contains((mouse.column, mouse.row).into()) {
                                        settings_nav_focus = true;
                                        let index = usize::from(mouse.row - list.y);
                                        if nav.items.get(index).is_some_and(|item| !item.2) {
                                            send(
                                                json!({"type":"nav_select","text":index.to_string(),"generation":s.generation}),
                                            )?;
                                        }
                                        dirty = true;
                                        continue;
                                    }
                                }
                            }
                            if !s.panel_title.is_empty() {
                                settings_nav_focus = false;
                                let area = render::panel_area(render::panel_host(&r, &s), &s);
                                if !area.contains((mouse.column, mouse.row).into()) {
                                    action("dismiss", "")?;
                                } else if s.form.is_none() && !panel_detail {
                                    if let Some(index) = render::panel_item_at(
                                        &s, area, &filter, selection, mouse.row,
                                    ) {
                                        selection = index;
                                        let button = render::panel_action_at(
                                            &s,
                                            area,
                                            &filter,
                                            selection,
                                            mouse.column,
                                            mouse.row,
                                        )
                                        .then(|| {
                                            s.items
                                                .iter()
                                                .filter(|row| row.matches(&filter))
                                                .nth(selection)
                                                .and_then(|row| row.action_operation.clone())
                                        })
                                        .flatten();
                                        if let Some(op) = button {
                                            send(
                                                json!({"type":"operation","operation":op,"generation":s.generation}),
                                            )?;
                                        } else if render::panel_toggle_at(
                                            &s,
                                            area,
                                            &filter,
                                            selection,
                                            mouse.column,
                                            mouse.row,
                                        ) {
                                            input::toggle(&s, selection, &filter)?;
                                        } else {
                                            pick(&s, selection, &filter)?;
                                        }
                                    }
                                }
                                dirty = true;
                                continue;
                            }
                            if let Some(prompt) = &s.prompt {
                                let (_, choices) = render::prompt_regions(
                                    render::prompt_area(r.transcript, prompt),
                                    prompt.choices.len(),
                                );
                                if choices.contains((mouse.column, mouse.row).into()) {
                                    if let Some(choice) = prompt
                                        .choices
                                        .get((mouse.row - choices.y) as usize)
                                        .filter(|choice| !choice.disabled)
                                    {
                                        send(
                                            json!({"type":"answer","text":prompt.id,"value":choice.value,"generation":s.generation}),
                                        )?;
                                    }
                                }
                                dirty = true;
                                continue;
                            }
                            if let Some(command) =
                                render::composer_control_at(r.composer, &s, mouse.column, mouse.row)
                                    .filter(|_| {
                                        !r.details.contains((mouse.column, mouse.row).into())
                                    })
                            {
                                action("command", command)?;
                                dirty = true;
                                continue;
                            }
                            if render::composer_context_at(r.composer, &s, mouse.column, mouse.row)
                            {
                                action("context_popover", "")?;
                                dirty = true;
                                continue;
                            }
                            if r.details.contains((mouse.column, mouse.row).into()) {
                                details_focus = true;
                                if mouse.row == r.details.y {
                                    if let Some(tab) =
                                        render::details_tab_at(r.details, mouse.column)
                                    {
                                        local_ui.dispatch(
                                            json!({"type":"details_tab","text":tab}),
                                            &mut s,
                                        )?;
                                    }
                                    dirty = true;
                                    continue;
                                }
                                if mouse.row == r.details.y + 1 && s.details_panel.tab == "Logs" {
                                    if let Some((_, session)) = s.details_panel.logs_header.first()
                                    {
                                        action("copy_text", session)?;
                                    }
                                    continue;
                                }
                                let palette = render::Palette::new(s.theme == "nexus-light");
                                let (_, files) = cache.detail_rows(
                                    &s,
                                    &palette,
                                    r.details.width.saturating_sub(5),
                                );
                                let row = details_scroll
                                    + (mouse.row - r.details.y).saturating_sub(1) as usize;
                                if let Some(file) = files
                                    .get(row)
                                    .copied()
                                    .flatten()
                                    .and_then(|i| s.details_panel.files.get(i))
                                {
                                    local_ui.dispatch(
                                        json!({"type":"file_toggle","text":file.path}),
                                        &mut s,
                                    )?;
                                }
                            } else if r.sessions.contains((mouse.column, mouse.row).into()) {
                                details_focus = false;
                                if mouse.row == r.sessions.bottom().saturating_sub(1) {
                                    cache.filtering = true;
                                    dirty = true;
                                    continue;
                                }
                                if mouse.row == r.sessions.y {
                                    if mouse.column < r.sessions.x + 4 {
                                        if terminal.size()?.width < 90 {
                                            s.sessions_drawer = false;
                                            cache.sessions_focus = false;
                                        } else {
                                            local_ui.dispatch(
                                                json!({"type":"toggle","key":"sessions_sidebar"}),
                                                &mut s,
                                            )?;
                                        }
                                    } else if mouse.column >= r.sessions.right().saturating_sub(4) {
                                        action("command", "/new")?;
                                    }
                                    continue;
                                }
                                // Project chips under the title filter the list to one project.
                                if let Some(project) =
                                    render::project_chip_at(&s, r.sessions, mouse.column, mouse.row)
                                {
                                    cache.project = project;
                                    sessions_scroll = 0;
                                    select_visible_session(&s, &mut cache, false);
                                    dirty = true;
                                    continue;
                                }
                                let rows = sidebar_rows(&s, &cache, r.sessions);
                                let at = render::sessions_row_at(
                                    &s,
                                    r.sessions,
                                    sessions_scroll,
                                    mouse.row,
                                );
                                let hit = at.and_then(|at| rows.get(at)).and_then(|(_, hit)| *hit);
                                match hit {
                                    Some(render::SidebarHit::New) => action("command", "/new")?,

                                    Some(render::SidebarHit::Archived) => {
                                        action("command", "/archived")?
                                    }
                                    // A heading picks its project; again, all projects.
                                    Some(render::SidebarHit::Heading(index)) => {
                                        let key =
                                            render::project_key(&s.sessions[index]).to_string();
                                        cache.project = if cache.project == key {
                                            String::new()
                                        } else {
                                            key
                                        };
                                        sessions_scroll = 0;
                                        select_visible_session(&s, &mut cache, false);
                                    }
                                    _ => {}
                                }
                                if let Some(row) =
                                    at.and_then(|at| rows.get(at))
                                        .and_then(|(_, hit)| match hit {
                                            Some(render::SidebarHit::Session(index)) => {
                                                s.sessions.get(*index)
                                            }
                                            _ => None,
                                        })
                                {
                                    send(
                                        json!({"type":"session_open","workspace":row.workspace,"text":row.id,"generation":s.generation}),
                                    )?;
                                    if terminal.size()?.width < 90 {
                                        s.sessions_drawer = false;
                                        cache.sessions_focus = false;
                                    }
                                }
                            } else if r.transcript.contains((mouse.column, mouse.row).into())
                                && s.panel_title.is_empty()
                                && s.prompt.is_none()
                            {
                                let offset = if follow { cache.max_scroll } else { scroll };
                                // Press only starts a selection; releasing without dragging is a click.
                                cache.selection = None;
                                press = Some((
                                    offset + (mouse.row - r.transcript.y) as usize,
                                    usize::from(mouse.column.saturating_sub(r.transcript.x)),
                                ));
                            } else if r.context.contains((mouse.column, mouse.row).into()) {
                                let index = usize::from(
                                    (mouse.column - r.context.x) * 5 / r.context.width.max(1),
                                )
                                .min(4);
                                send(
                                    json!({"type":"context_header","key":(["system","tools","agents","skills","mcp"][index]),"generation":s.generation}),
                                )?;
                            } else if r.tabs.contains((mouse.column, mouse.row).into())
                                && mouse.row == r.tabs.y
                            {
                                let column = usize::from(mouse.column.saturating_sub(r.tabs.x));
                                let hit = render::top_cells(&s, r.tabs).into_iter().find(|cell| {
                                    column >= cell.start && column < cell.end.max(cell.start + 1)
                                });
                                match hit.map(|cell| (cell.kind, cell.end)) {
                                    Some((render::TabHit::Tab(index), end))
                                        if mouse.row == r.tabs.y =>
                                    {
                                        let row = &s.tabs[index];
                                        let kind = if column + 3 >= end {
                                            "tab_close"
                                        } else {
                                            "session_open"
                                        };
                                        send(
                                            json!({"type":kind,"workspace":row.workspace,"text":row.id,"generation":s.generation}),
                                        )?;
                                    }
                                    Some((render::TabHit::New, _)) if mouse.row == r.tabs.y => {
                                        action("command", "/new")?
                                    }
                                    Some((render::TabHit::Sessions, _)) => {
                                        // Narrow: open (or close) locally at once; the list is
                                        // already kept fresh by the host's poll.
                                        if r.tabs.width < 110 {
                                            details_focus = false;
                                            sessions_surface(
                                                &mut s,
                                                &mut cache,
                                                &mut local_ui,
                                                true,
                                                terminal.size()?.width,
                                            )?;
                                        } else {
                                            s.last_opened = "sessions".into();
                                            local_ui.dispatch(
                                                json!({"type":"toggle","key":"sessions_sidebar"}),
                                                &mut s,
                                            )?;
                                        }
                                    }
                                    Some((render::TabHit::Details, _)) => {
                                        s.last_opened = "details".into();
                                        details_focus = true;
                                        local_ui.dispatch(
                                            json!({"type":"toggle","key":"details_sidebar"}),
                                            &mut s,
                                        )?;
                                    }
                                    _ => {}
                                }
                            }
                        }
                        _ => {}
                    }
                    dirty = true;
                }
                _ => {}
            }
        }
        trace::stall(input_kind, handling_at.elapsed());
        timings.record("event handling", handling_at.elapsed());
    }
    drop(_cleanup);
    timings.finish();
    Ok(())
}
use unicode_segmentation::UnicodeSegmentation;
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn completion_remains_visible_while_typing_and_backspacing() {
        let cache = vec![
            ("/".into(), vec!["/agent".into(), "/effort".into()]),
            ("/ag".into(), vec!["/agent".into()]),
            (
                "@".into(),
                vec!["src/main.rs".into(), "docs/index.md".into()],
            ),
            ("/effort ".into(), vec!["default".into(), "high".into()]),
        ];
        assert_eq!(completion_candidates(&cache, "/age"), vec!["/agent"]);
        assert_eq!(completion_candidates(&cache, "/a"), vec!["/agent"]);
        assert_eq!(
            completion_candidates(&cache, "@sr"),
            vec!["src/main.rs", "docs/index.md"]
        );
        assert_eq!(completion_candidates(&cache, "/effort h"), vec!["high"]);
        assert!(completion_candidates(&cache, "/agent h").is_empty());
        assert!(completion_candidates(&cache, "ordinary text").is_empty());
    }

    #[test]
    fn snapshot_contract() {
        let s: Snapshot = serde_json::from_str(
            r#"{"schema":1,"revision":2,"title":"Nexus","status":"idle","lines":["User: hello"]}"#,
        )
        .unwrap();
        assert_eq!(s.schema, 1);
        assert_eq!(s.lines[0], "User: hello");
    }
}

#[cfg(test)]
mod click_tests {
    use super::*;

    #[test]
    fn a_header_click_opens_the_chip_under_the_column_not_the_menu() {
        let row = Some(json!({"kind":"context_chips","chips":[
            {"start":4,"end":12,"operation":{"kind":"context_show","key":"tools"}},
            {"start":15,"end":30,"operation":{"kind":"context_show","key":"skills"}}]}));
        assert_eq!(resolve_operation(&row, Some(5)).unwrap()["key"], "tools");
        assert_eq!(resolve_operation(&row, Some(20)).unwrap()["key"], "skills");
        assert!(resolve_operation(&row, Some(13)).is_none());
        assert_eq!(
            resolve_operation(&row, None).unwrap()["kind"],
            "context_menu"
        );
    }
}
