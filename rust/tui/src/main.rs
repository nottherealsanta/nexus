//! Presentation-only terminal client. Python owns all host commands/reduction.
mod bridge;
mod editor;
mod markdown;
mod render;
mod transcript;
use bridge::Snapshot;
use crossterm::{
    event::{
        self, Event, KeyCode, KeyEvent, KeyModifiers, KeyboardEnhancementFlags, MouseEventKind,
        PopKeyboardEnhancementFlags, PushKeyboardEnhancementFlags,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use editor::Editor;
use ratatui::{backend::CrosstermBackend, Terminal};
use serde_json::{json, Value};
use std::{
    io::{self, BufRead, Write},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};
const MAX_DRAFT: usize = 1024 * 1024;
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
fn send(value: Value) -> io::Result<()> {
    let mut out = io::stdout().lock();
    writeln!(out, "{}", value)?;
    out.flush()
}
fn action(kind: &str, text: &str) -> io::Result<()> {
    send(json!({"type":kind,"text":text}))
}
fn edit(editor: &mut Editor, key: KeyEvent, multiline: bool) {
    let shift = key.modifiers.contains(KeyModifiers::SHIFT);
    let control = key.modifiers.contains(KeyModifiers::CONTROL);
    match key.code {
        KeyCode::Left => {
            editor.select_move(shift);
            if control {
                editor.word(false)
            } else {
                editor.left()
            }
        }
        KeyCode::Right => {
            editor.select_move(shift);
            if control {
                editor.word(true)
            } else {
                editor.right()
            }
        }
        KeyCode::Home => {
            editor.select_move(shift);
            if control {
                editor.cursor = 0
            } else {
                editor.home()
            }
        }
        KeyCode::End => {
            editor.select_move(shift);
            if control {
                editor.cursor = editor.text.len()
            } else {
                editor.end()
            }
        }
        KeyCode::Up if multiline => {
            editor.select_move(shift);
            editor.vertical(false)
        }
        KeyCode::Down if multiline => {
            editor.select_move(shift);
            editor.vertical(true)
        }
        KeyCode::Backspace => editor.backspace(),
        KeyCode::Delete => editor.delete(),
        KeyCode::Char('a') if control => {
            editor.anchor = Some(0);
            editor.cursor = editor.text.len()
        }
        KeyCode::Char('z') if control => {
            if shift {
                editor.redo()
            } else {
                editor.undo()
            }
        }
        KeyCode::Char('y') if control => editor.redo(),
        KeyCode::Enter if multiline => editor.insert("\n"),
        KeyCode::Char('j') if control && multiline => editor.insert("\n"),
        KeyCode::Tab if multiline => editor.insert("    "),
        KeyCode::Char(c) if !control && editor.text.len() < MAX_DRAFT => {
            editor.insert(&c.to_string())
        }
        _ => {}
    }
}
fn pick(s: &Snapshot, index: usize, filter: &str) -> io::Result<()> {
    if let Some(item) = s
        .items
        .iter()
        .filter(|row| row.label.to_lowercase().contains(&filter.to_lowercase()))
        .nth(index)
    {
        if let Some(operation) = &item.operation {
            send(json!({"type":"operation","operation":operation,"generation":s.generation}))
        } else {
            send(json!({"type":"pick","text":item.command,"generation":s.generation}))
        }
    } else {
        Ok(())
    }
}
fn save(s: &Snapshot, editor: &Editor, revision: u64) -> io::Result<()> {
    if let Some(form) = &s.form {
        send(
            json!({"type":"save","form":form.id,"body":editor.text,"revision":revision,"generation":s.generation}),
        )
    } else {
        Ok(())
    }
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::args().any(|arg| arg == "--version") {
        println!("Nexus Ratatui · bridge 1");
        return Ok(());
    }
    let (tx, rx) = mpsc::sync_channel(2);
    thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            match line {
                Ok(line) if line.len() <= 16 * 1024 * 1024 => {
                    if tx.send(line).is_err() {
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
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stderr()))?;
    let mut s = Snapshot::default();
    let mut draft = Editor::default();
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
    let mut panel_detail = false;
    let mut selection = 0usize;
    let mut filter = String::new();
    let mut completion_index = 0usize;
    let mut completion_hidden = "\0".to_string();
    let mut dirty = true;
    let spin_clock = Instant::now();
    let mut nav: Option<usize> = None;
    let mut press: Option<(usize, usize)> = None;
    let mut typed = (String::new(), 0usize);
    let mut complete_due: Option<Instant> = None;
    let mut asked = String::new();
    let mut follow = true;
    let mut cache = render::Cache::default();
    let mut logs_open = false;
    let mut logs_scroll = 0usize;
    let mut leader = None;
    let mut escape = Instant::now() - Duration::from_secs(2);
    let mut history_loaded = false;
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
        for (index, line) in queued.into_iter().enumerate() {
            if index < last
                && line.contains("\"restore\":\"\"")
                && line.contains("\"insert\":\"\"")
            {
                continue;
            }
            let next: Snapshot = serde_json::from_str(&line)?;
            if next.schema != 1 {
                return Err("unsupported bridge schema".into());
            }
            if next.revision < s.revision {
                continue;
            }
            if next.generation != s.generation {
                draft = Editor::default();
                follow = true;
                scroll = 0;
                details_scroll = 0;
                nav = None;
            }
            if next.theme != s.theme {
                cache.reset();
            }
            if next.panel_title != s.panel_title {
                panel_scroll = 0;
                panel_detail = false;
                selection = 0;
                filter.clear();
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
                    let prefix = draft.text[..draft.cursor]
                        .chars()
                        .last()
                        .is_some_and(|c| !c.is_whitespace());
                    let suffix = draft.text[draft.cursor..]
                        .chars()
                        .next()
                        .is_some_and(|c| !c.is_whitespace());
                    draft.insert(&format!(
                        "{}{}{}",
                        if prefix { " " } else { "" },
                        next.insert,
                        if suffix { " " } else { "" }
                    ));
                } else {
                    draft.insert(&next.insert);
                }
                if next.auto_send_insert && !draft.text.trim().is_empty() {
                    send(
                        json!({"type":"submit","text":draft.take(),"mode":"queue","generation":next.generation}),
                    )?;
                }
            }
            if !history_loaded {
                draft.history = next.history.clone();
                draft.history_index = draft.history.len();
                history_loaded = true;
            }
            s = next;
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
        if dirty {
            cache.focus = nav.and_then(|index| render::targets(&cache).get(index).copied());
            terminal.draw(|frame| {
                render::draw(
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
                let query = draft.text[..draft.cursor]
                    .rsplit(char::is_whitespace)
                    .next()
                    .unwrap_or("");
                if s.panel_title.is_empty()
                    && s.prompt.is_none()
                    && query == s.completion_query
                    && completion_hidden != query
                    && !s.completions.is_empty()
                {
                    render::completion(frame, &s.completions, completion_index);
                }
            })?;
            dirty = false;
        }
        // As-you-type completion (Textual `refresh_completion`): a `/command`,
        // an `@file`, or a command argument asks the host after a 120 ms pause.
        if typed != (draft.text.clone(), draft.cursor) {
            if typed.0 != draft.text && !draft.text.is_empty() {
                follow = true; // typing returns to the live end of the conversation
            }
            typed = (draft.text.clone(), draft.cursor);
            let query = draft.text[..draft.cursor].rsplit(char::is_whitespace).next().unwrap_or("");
            let argument = (draft.text.starts_with("/model ") || draft.text.starts_with("/agent ")) && draft.text[..draft.cursor].contains(' ');
            let triggers = s.panel_title.is_empty()
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
            let query = prefix.rsplit(char::is_whitespace).next().unwrap_or("").to_string();
            asked = prefix.clone();
            send(json!({"type":"complete","text":query,"prefix":prefix,"generation":s.generation}))?;
        }
        if !event::poll(Duration::from_millis(16))? {
            if render::animating(&s) {
                let frame = (spin_clock.elapsed().as_millis() / 125) as usize;
                if frame != cache.spin {
                    cache.spin = frame;
                    dirty = true;
                }
            }
            continue;
        }
        match event::read()? {
            Event::Key(key) if key.kind != event::KeyEventKind::Release => {
                if s.voice_phase == "transcribing" && key.code == KeyCode::Esc {
                    action("voice_discard", "")?;
                    continue;
                }
                if s.voice_phase == "recording" {
                    send(json!({"type":"voice_stop","discard":key.code==KeyCode::Esc}))?;
                    continue;
                }
                if key.code == KeyCode::Char('q') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    action("quit", "")?;
                    break;
                }
                cache.selection = None;
                // Sessions filter (click the box): typing edits it, Enter keeps it, Escape clears it.
                if cache.filtering {
                    match key.code {
                        KeyCode::Enter => cache.filtering = false,
                        KeyCode::Esc => {
                            cache.filter.clear();
                            cache.filtering = false;
                        }
                        KeyCode::Backspace => {
                            cache.filter.pop();
                        }
                        KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) && cache.filter.chars().count() < 80 => cache.filter.push(c),
                        _ => {}
                    }
                    sessions_scroll = 0;
                    dirty = true;
                    continue;
                }
                // Keyboard focus over clickable transcript blocks: Tab (empty draft) enters,
                // arrows/Tab move, Enter or Space opens what a click would, Escape or any
                // other key leaves.
                if let Some(index) = nav {
                    let blocks = render::targets(&cache);
                    let last = blocks.len().saturating_sub(1);
                    let next = match key.code {
                        KeyCode::Up | KeyCode::BackTab | KeyCode::Char('k') => Some(index.saturating_sub(1).min(last)),
                        KeyCode::Down | KeyCode::Tab | KeyCode::Char('j') => Some((index + 1).min(last)),
                        KeyCode::Home => Some(0),
                        KeyCode::End => Some(last),
                        _ => None,
                    };
                    if let Some(next) = next.filter(|_| !blocks.is_empty()) {
                        nav = Some(next);
                        let size = terminal.size()?;
                        let area = ratatui::layout::Rect::new(0, 0, size.width, size.height);
                        let height = render::regions(area, &s, render::composer_height(area, &draft, &s), logs_open).transcript.height as usize;
                        let (first, end) = blocks[next];
                        if follow {
                            scroll = cache.lines.len().saturating_sub(height);
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
                            if let Some(Some(operation)) = blocks.get(index).and_then(|(first, _)| cache.operations.get(*first)) {
                                send(json!({"type":"operation","operation":operation,"generation":s.generation}))?;
                            }
                            continue;
                        }
                        KeyCode::Esc => continue,
                        _ => {}
                    }
                }
                if key.code == KeyCode::Char('x') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    leader = Some(Instant::now());
                    dirty = true;
                    continue;
                }
                if leader.take().is_some() {
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
                                if terminal.size()?.width < 110 {
                                    action("command", "/sessions")?;
                                } else {
                                    send(json!({"type":"toggle","key":"sessions_sidebar"}))?;
                                }
                            }
                            KeyCode::Char('l') => {
                                if terminal.size()?.width
                                    < (if s.sessions_sidebar { 170 } else { 110 })
                                {
                                    action("command", "/details")?;
                                } else {
                                    send(json!({"type":"toggle","key":"details_sidebar"}))?;
                                }
                            }
                            KeyCode::Char('e') => {
                                logs_open = !logs_open;
                                send(json!({"type":"logs","open":logs_open}))?;
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
                            selection = (selection + 1).min(prompt.choices.len().saturating_sub(1))
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
                                if key.code == KeyCode::Enter && !answer.text.trim().is_empty() {
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
                if logs_open && s.panel_title.is_empty() && s.prompt.is_none() {
                    if key.code == KeyCode::PageUp {
                        logs_scroll = logs_scroll.saturating_sub(5);
                        dirty = true;
                        continue;
                    }
                    if key.code == KeyCode::PageDown {
                        logs_scroll = (logs_scroll + 5).min(s.logs.len().saturating_sub(1));
                        dirty = true;
                        continue;
                    }
                    if key.code == KeyCode::Esc
                        || key.code == KeyCode::Char('c')
                            && key.modifiers.contains(KeyModifiers::CONTROL)
                    {
                        logs_open = false;
                        send(json!({"type":"logs","open":false}))?;
                        dirty = true;
                        continue;
                    }
                    if key.code == KeyCode::Char('a')
                        && key.modifiers.contains(KeyModifiers::CONTROL)
                    {
                        action("logs_fold", "")?;
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
                    if key.code == KeyCode::Esc
                        || key.code == KeyCode::Char('c')
                            && key.modifiers.contains(KeyModifiers::CONTROL)
                    {
                        action("dismiss", "")?;
                    } else if !s.items.is_empty() && !panel_detail {
                        let count = s
                            .items
                            .iter()
                            .filter(|row| row.label.to_lowercase().contains(&filter.to_lowercase()))
                            .count();
                        match key.code {
                            KeyCode::Up => selection = selection.saturating_sub(1),
                            KeyCode::Down => {
                                selection = (selection + 1).min(count.saturating_sub(1))
                            }
                            KeyCode::Enter => pick(&s, selection, &filter)?,
                            KeyCode::Backspace => {
                                filter.pop();
                                selection = 0;
                            }
                            KeyCode::Char('r') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                                action("refresh_models", "")?
                            }
                            KeyCode::Char('s') if key.modifiers.contains(KeyModifiers::CONTROL) && s.panel_title.starts_with("Models") => {
                                action("model_sort", "")?
                            }
                            KeyCode::Char('f') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                                send(
                                    json!({"type":"favorite","selection":selection,"filter":filter}),
                                )?
                            }
                            KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
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
                            KeyCode::PageDown | KeyCode::Down => panel_scroll += 10,
                            KeyCode::PageUp | KeyCode::Up => {
                                panel_scroll = panel_scroll.saturating_sub(10)
                            }
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
                if s.completion_query == query
                    && completion_hidden != query
                    && !s.completions.is_empty()
                {
                    match key.code {
                        KeyCode::Up => {
                            completion_index = completion_index.saturating_sub(1);
                            dirty = true;
                            continue;
                        }
                        KeyCode::Down => {
                            completion_index = (completion_index + 1).min(s.completions.len() - 1);
                            dirty = true;
                            continue;
                        }
                        KeyCode::Esc => {
                            completion_hidden = query;
                            dirty = true;
                            continue;
                        }
                        // Textual: Enter on a standalone `/command` runs the highlighted
                        // command; on an argument that is already complete it submits.
                        KeyCode::Enter if draft.text.starts_with('/') && !draft.text.contains(char::is_whitespace) => {
                            let value = s.completions[completion_index.min(s.completions.len() - 1)].clone();
                            draft.take();
                            send(json!({"type":"submit","text":value,"mode":"queue","generation":s.generation}))?;
                            dirty = true;
                            continue;
                        }
                        KeyCode::Enter if draft.text.starts_with('/') && s.completions.iter().any(|item| *item == query) => {}
                        KeyCode::Enter | KeyCode::Tab => {
                            let value =
                                &s.completions[completion_index.min(s.completions.len() - 1)];
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
                            if terminal.size()?.width < 110 {
                                action("command", "/sessions")?;
                            } else {
                                send(json!({"type":"toggle","key":"sessions_sidebar"}))?;
                            }
                            continue;
                        }
                        KeyCode::Char('l') => {
                            if terminal.size()?.width < (if s.sessions_sidebar { 170 } else { 110 })
                            {
                                action("command", "/details")?;
                            } else {
                                send(json!({"type":"toggle","key":"details_sidebar"}))?;
                            }
                            continue;
                        }
                        KeyCode::Char('e') => {
                            logs_open = !logs_open;
                            send(json!({"type":"logs","open":logs_open}))?;
                            dirty = true;
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
                        KeyCode::Char('c') => {
                            action("cancel", "")?;
                            continue;
                        }
                        _ => {}
                    }
                }
                match key.code {
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
                                "steer"
                            } else if key.modifiers.contains(KeyModifiers::ALT) {
                                "interrupt"
                            } else {
                                "queue"
                            };
                            send(
                                json!({"type":"submit","text":draft.take(),"mode":mode,"generation":s.generation}),
                            )?;
                            follow = true;
                        }
                    }
                    KeyCode::PageDown => {
                        follow = false;
                        scroll = (scroll + 10).min(cache.lines.len().saturating_sub(1));
                    }
                    KeyCode::PageUp => {
                        if follow {
                            scroll = cache
                                .lines
                                .len()
                                .saturating_sub(terminal.size()?.height as usize);
                        }
                        follow = false;
                        scroll = scroll.saturating_sub(10);
                    }
                    KeyCode::End if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        follow = true;
                        draft.cursor = draft.text.len();
                    }
                    KeyCode::Up if !draft.text.contains('\n') => draft.history(true),
                    KeyCode::Down if !draft.text.contains('\n') => draft.history(false),
                    KeyCode::BackTab => action("cycle_agent", "")?,
                    KeyCode::Tab if draft.text.is_empty() && s.panel_title.is_empty() && s.prompt.is_none() && !render::targets(&cache).is_empty() => {
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
                let size = terminal.size()?;
                let r = render::regions(
                    ratatui::layout::Rect::new(0, 0, size.width, size.height),
                    &s,
                    render::composer_height(ratatui::layout::Rect::new(0, 0, size.width, size.height), &draft, &s),
                    logs_open,
                );
                match mouse.kind {
                    MouseEventKind::ScrollUp => {
                        if logs_open
                            && render::logs_region(&r)
                                .contains((mouse.column, mouse.row).into())
                        {
                            logs_scroll = logs_scroll.saturating_sub(3);
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
                                scroll = cache
                                    .lines
                                    .len()
                                    .saturating_sub(r.transcript.height as usize);
                            }
                            follow = false;
                            scroll = scroll.saturating_sub(3);
                        } else {
                            panel_scroll = panel_scroll.saturating_sub(3);
                        }
                    }
                    MouseEventKind::ScrollDown => {
                        if logs_open
                            && render::logs_region(&r)
                                .contains((mouse.column, mouse.row).into())
                        {
                            logs_scroll = (logs_scroll + 3).min(s.logs.len().saturating_sub(1));
                            dirty = true;
                            continue;
                        }
                        if r.details.contains((mouse.column, mouse.row).into()) {
                            let palette = render::Palette::new(s.theme == "nexus-light");
                            let rows = render::details_rows(&s, &palette, r.details.width.saturating_sub(5)).0.len();
                            details_scroll = (details_scroll + 3).min(rows.saturating_sub(r.details.height.saturating_sub(1) as usize));
                            dirty = true;
                            continue;
                        }
                        if r.sessions.contains((mouse.column, mouse.row).into()) {
                            sessions_scroll = (sessions_scroll + 3).min(
                                render::session_sidebar(&s, &render::Palette::new(s.theme == "nexus-light"), usize::from(r.sessions.width.saturating_sub(3)), 0, &cache.filter, cache.filtering)
                                    .len()
                                    .saturating_sub(r.sessions.height.saturating_sub(3) as usize),
                            );
                            dirty = true;
                            continue;
                        }
                        if s.panel_title.is_empty() && s.prompt.is_none() {
                            follow = false;
                            scroll = (scroll + 3).min(
                                cache
                                    .lines
                                    .len()
                                    .saturating_sub(r.transcript.height as usize),
                            );
                        } else {
                            panel_scroll += 3;
                        }
                    }
                    MouseEventKind::Down(event::MouseButton::Right) => {
                        if r.sessions.contains((mouse.column, mouse.row).into()) {
                            if let Some(row) = render::session_sidebar(&s, &render::Palette::new(s.theme == "nexus-light"), usize::from(r.sessions.width.saturating_sub(3)), 0, &cache.filter, cache.filtering)
                                .get(sessions_scroll + (mouse.row - r.sessions.y).saturating_sub(1) as usize)
                                .and_then(|(_, hit)| match hit {
                                    Some(render::SidebarHit::Session(index)) => s.sessions.get(*index),
                                    _ => None,
                                })
                            {
                                send(
                                    json!({"type":"session_actions","workspace":row.workspace,"text":row.id,"generation":s.generation}),
                                )?;
                            }
                        }
                    }
                    MouseEventKind::Drag(event::MouseButton::Left) => {
                        if let Some(start) = press {
                            let offset = if follow { cache.lines.len().saturating_sub(r.transcript.height as usize) } else { scroll };
                            let row = usize::from(mouse.row.clamp(r.transcript.y, r.transcript.y + r.transcript.height.saturating_sub(1)) - r.transcript.y);
                            let column = usize::from(mouse.column.saturating_sub(r.transcript.x));
                            cache.selection = Some((start, (offset + row, column + 1)));
                        }
                    }
                    MouseEventKind::Up(event::MouseButton::Left) => {
                        if let Some((line, _)) = press.take() {
                            let text = if cache.selection.is_some_and(|(a, b)| a.0 != b.0 || a.1.abs_diff(b.1) > 1) { render::selected_text(&cache) } else { String::new() };
                            if text.is_empty() {
                                cache.selection = None;
                                if let Some(Some(operation)) = cache.operations.get(line) {
                                    send(json!({"type":"operation","operation":operation,"generation":s.generation}))?;
                                }
                            } else {
                                // OSC 52 reaches the user's terminal even over SSH; Python also tries the desktop clipboard.
                                let mut tty = io::stderr();
                                write!(tty, "\x1b]52;c;{}\x07", base64(text.as_bytes()))?;
                                tty.flush()?;
                                send(json!({"type":"copy_selection","text":text,"generation":s.generation}))?;
                            }
                        }
                    }
                    MouseEventKind::Down(event::MouseButton::Left) => {
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
                        if r.details.contains((mouse.column, mouse.row).into()) {
                            let palette = render::Palette::new(s.theme == "nexus-light");
                            let (_, files) = render::details_rows(&s, &palette, r.details.width.saturating_sub(5));
                            let row = details_scroll + (mouse.row - r.details.y).saturating_sub(1) as usize;
                            if let Some(file) = files.get(row).copied().flatten().and_then(|i| s.details_panel.files.get(i)) {
                                send(json!({"type":"file_toggle","text":file.path,"generation":s.generation}))?;
                            }
                        } else if r.sessions.contains((mouse.column, mouse.row).into()) {
                            let hit = render::session_sidebar(&s, &render::Palette::new(s.theme == "nexus-light"), usize::from(r.sessions.width.saturating_sub(3)), 0, &cache.filter, cache.filtering)
                                .get(sessions_scroll + (mouse.row - r.sessions.y).saturating_sub(1) as usize)
                                .and_then(|(_, hit)| *hit);
                            match hit {
                                Some(render::SidebarHit::New) => action("command", "/new")?,
                                Some(render::SidebarHit::Filter) => cache.filtering = true,
                                Some(render::SidebarHit::Archived) => action("command", "/archived")?,
                                _ => {}
                            }
                            if let Some(row) = render::session_sidebar(&s, &render::Palette::new(s.theme == "nexus-light"), usize::from(r.sessions.width.saturating_sub(3)), 0, &cache.filter, cache.filtering)
                                .get(sessions_scroll + (mouse.row - r.sessions.y).saturating_sub(1) as usize)
                                .and_then(|(_, hit)| match hit {
                                    Some(render::SidebarHit::Session(index)) => s.sessions.get(*index),
                                    _ => None,
                                })
                            {
                                send(
                                    json!({"type":"session_open","workspace":row.workspace,"text":row.id,"generation":s.generation}),
                                )?;
                            }
                        } else if r.transcript.contains((mouse.column, mouse.row).into())
                            && s.panel_title.is_empty()
                            && s.prompt.is_none()
                        {
                            let offset = if follow {
                                cache
                                    .lines
                                    .len()
                                    .saturating_sub(r.transcript.height as usize)
                            } else {
                                scroll
                            };
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
                            let hit = render::tab_cells(&s, usize::from(r.tabs.width))
                                .into_iter()
                                .find(|cell| column >= cell.start && column < cell.end.max(cell.start + 1));
                            match hit.map(|cell| (cell.kind, cell.end)) {
                                Some((render::TabHit::Tab(index), end)) => {
                                    let row = &s.tabs[index];
                                    let kind = if column + 3 >= end { "tab_close" } else { "session_open" };
                                    send(json!({"type":kind,"workspace":row.workspace,"text":row.id,"generation":s.generation}))?;
                                }
                                Some((render::TabHit::New, _)) => action("command", "/new")?,
                                Some((render::TabHit::Sessions, _)) => {
                                    if r.tabs.width < 110 {
                                        action("command", "/sessions")?;
                                    } else {
                                        send(json!({"type":"toggle","key":"sessions_sidebar"}))?;
                                    }
                                }
                                Some((render::TabHit::Details, _)) => {
                                    if r.tabs.width < (if s.sessions_sidebar { 170 } else { 110 }) {
                                        action("command", "/details")?;
                                    } else {
                                        send(json!({"type":"toggle","key":"details_sidebar"}))?;
                                    }
                                }
                                None => {}
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
    Ok(())
}
use unicode_segmentation::UnicodeSegmentation;
#[cfg(test)]
mod tests {
    use super::*;
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

fn base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = (u32::from(chunk[0]) << 16) | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8) | u32::from(*chunk.get(2).unwrap_or(&0));
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { TABLE[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { TABLE[n as usize & 63] as char } else { '=' });
    }
    out
}
#[cfg(test)]
mod base64_tests {
    #[test]
    fn matches_the_standard_alphabet_with_padding() {
        assert_eq!(super::base64(b""), "");
        assert_eq!(super::base64(b"f"), "Zg==");
        assert_eq!(super::base64(b"fo"), "Zm8=");
        assert_eq!(super::base64(b"foo"), "Zm9v");
        assert_eq!(super::base64("héllo".as_bytes()), "aMOpbGxv");
    }
}
