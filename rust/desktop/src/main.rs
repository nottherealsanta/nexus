//! Nexus GPUI desktop: presentation-only client of the shared native bridge.
#[cfg(test)]
use core::prelude::v1::test;
#[path = "../../tui/src/bridge.rs"]
mod bridge;
mod icons;
mod input;
mod markdown;
mod panels;
mod theme;
mod transcript;
use base64::Engine;
use bridge::{Content, Item, Snapshot};
use gpui::{prelude::*, *};
use input::{Input, InputEvent};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{self, BufRead, Read, Write},
};
use theme::Theme;
actions!(
    nexus,
    [
        Quit,
        NewSession,
        Settings,
        Models,
        Agents,
        Effort,
        Sessions,
        ToggleSessions,
        ToggleDetails,
        Dismiss,
        Cancel,
        Palette,
        InspectContext,
        Usage,
        Attach,
        Voice,
        Speak,
        ThemeToggle,
        Reconnect,
        JumpLatest,
        SaveForm,
        FocusComposer,
        QueueSubmit,
        InterruptSubmit,
        FavoriteModel,
        SortModels,
        RefreshModels,
        HistoryPrevious,
        HistoryNext,
        CompleteFirst,
        PreviousFocus,
        ReviewCompact,
        ReviewWide,
        CycleAgent,
        CycleEffort,
        ToggleLogs
    ]
);
fn send(mut action: Value, generation: u64) {
    action["generation"] = generation.into();
    let mut out = io::stdout().lock();
    if let Err(error) = writeln!(out, "{action}").and_then(|_| out.flush()) {
        eprintln!("Desktop action transport: {error}");
    }
}
pub struct Desktop {
    snapshot: Snapshot,
    composer: Entity<Input>,
    search: Entity<Input>,
    filter: Entity<Input>,
    form: Entity<Input>,
    text_cache: std::cell::RefCell<HashMap<String, Entity<Input>>>,
    image_preview: Option<std::sync::Arc<Image>>,
    focus: FocusHandle,
    transcript: ListState,
    picker_scroll: ScrollHandle,
    logs_visible: bool,
    compact_pane: Option<String>,
    history_index: Option<usize>,
    history_draft: String,
    drafts: HashMap<String, String>,
    follow: bool,
    preview: bool,
    error: String,
    panel_identity: String,
    form_identity: String,
    form_revision: u64,
    autosave_task: Option<Task<()>>,
    selection: usize,
    completion_index: usize,
    completion_hidden: Option<String>,
    completion_scroll: ScrollHandle,
    light_override: Option<bool>,
    last_width: Pixels,
    _subscriptions: Vec<Subscription>,
}
impl Desktop {
    fn new(window: &mut Window, cx: &mut Context<Self>, preview: bool) -> Self {
        let composer = cx.new(|cx| {
            let mut input = Input::new("Ask anything, or / for commands…", cx);
            input.atomic_markers = true;
            input
        });
        let search = cx.new(|cx| Input::new("Search sessions", cx));
        let filter = cx.new(|cx| {
            let mut i = Input::new("Filter choices…", cx);
            i.menu = true;
            i
        });
        let form = cx.new(|cx| Input::new("", cx));
        let transcript = ListState::new(0, ListAlignment::Top, px(800.));
        let weak = cx.entity().downgrade();
        transcript.set_scroll_handler(move |event, _, cx| {
            weak.update(cx, |this, _| this.follow = !event.is_scrolled)
                .ok();
        });
        let mut subscriptions = vec![];
        subscriptions.push(
            cx.subscribe_in(
                &composer,
                window,
                |this, _, event, window, cx| match event {
                    InputEvent::Submitted => {
                        if !this.insert_completion(cx) {
                            this.submit(window, cx);
                        }
                    }
                    InputEvent::Navigate(direction) => {
                        if this.completion_active(cx) {
                            this.completion_index = (this.completion_index as i32 + direction)
                                .clamp(0, this.snapshot.completions.len().saturating_sub(1) as i32)
                                as usize;
                            this.completion_scroll.scroll_to_item(this.completion_index);
                            cx.notify();
                        }
                    }
                    InputEvent::ClipboardImage => {
                        this.dispatch(json!({"type":"clipboard"}), window, cx)
                    }
                    InputEvent::Changed => {
                        this.completion_index = 0;
                        this.completion_hidden = None;
                        let text = this.composer.read(cx).content.clone();
                        let prefix = text[..this.composer.read(cx).cursor()].to_owned();
                        send(
                            json!({"type":"draft_changed", "text":text}),
                            this.snapshot.generation,
                        );
                        if let Some(query) = prefix.rsplit(char::is_whitespace).next() {
                            send(
                                json!({"type":"complete", "text":query, "prefix":prefix}),
                                this.snapshot.generation,
                            );
                        }
                        cx.notify();
                    }
                },
            ),
        );
        subscriptions.push(cx.subscribe_in(&filter, window, |this, _, event, w, cx| {
            if let InputEvent::Navigate(direction) = event {
                let query = this.filter.read(cx).content.to_lowercase();
                let count = this
                    .snapshot
                    .items
                    .iter()
                    .filter(|i| {
                        format!("{} {}", i.label, i.detail)
                            .to_lowercase()
                            .contains(&query)
                    })
                    .count();
                this.selection = (this.selection as i32 + direction)
                    .max(0)
                    .min(count.saturating_sub(1) as i32) as usize;
                let mut group = String::new();
                let mut row = 0;
                for (index, item) in this
                    .snapshot
                    .items
                    .iter()
                    .filter(|i| {
                        format!("{} {}", i.label, i.detail)
                            .to_lowercase()
                            .contains(&query)
                    })
                    .enumerate()
                {
                    if item.group != group {
                        group = item.group.clone();
                        row += 1;
                    }
                    if index == this.selection {
                        break;
                    }
                    row += 1;
                }
                this.picker_scroll.scroll_to_item(row);
                cx.notify();
            } else if matches!(event, InputEvent::Submitted) {
                if let Some(prompt) = &this.snapshot.prompt {
                    if prompt.kind == "question" {
                        let answer = this.filter.read(cx).content.clone();
                        let value = if answer.trim().is_empty() {
                            prompt
                                .choices
                                .iter()
                                .find(|choice| !choice.disabled)
                                .map(|choice| choice.value.clone())
                        } else {
                            Some(answer)
                        };
                        if let Some(value) = value {
                            this.dispatch(
                                json!({"type":"answer","text":prompt.id,"value":value}),
                                w,
                                cx,
                            );
                        }
                    }
                    return;
                }
                let query = this.filter.read(cx).content.to_lowercase();
                let items: Vec<_> = this
                    .snapshot
                    .items
                    .iter()
                    .filter(|i| {
                        format!("{} {}", i.label, i.detail)
                            .to_lowercase()
                            .contains(&query)
                    })
                    .collect();
                if let Some(item) = items.get(this.selection.min(items.len().saturating_sub(1))) {
                    let action = item_action(item);
                    this.dispatch(action, w, cx);
                }
            } else {
                this.selection = 0;
                cx.notify();
            }
        }));
        subscriptions.push(cx.subscribe_in(&search, window, |this, _, event, w, cx| {
            if matches!(event, InputEvent::Submitted) {
                let query = this.search.read(cx).content.to_lowercase();
                if let Some(row) = this.snapshot.sessions.iter().find(|s| {
                    format!("{} {} {}", s.title, s.id, s.workspace)
                        .to_lowercase()
                        .contains(&query)
                }) {
                    this.dispatch(
                        json!({"type":"session_open","text":row.id,"workspace":row.workspace}),
                        w,
                        cx,
                    );
                }
            }
            cx.notify();
        }));
        subscriptions.push(cx.subscribe_in(&form, window, |this, _, event, w, cx| {
            if matches!(event, InputEvent::Submitted) {
                if this.snapshot.form.as_ref().is_some_and(|form| form.secret) {
                    this.save_form(&SaveForm, w, cx);
                } else {
                    this.form.update(cx, |input,cx| input.insert("\n",w,cx));
                }
            }
            if matches!(event, InputEvent::Changed) {
                this.form_revision += 1;
                if !this.form.read(cx).secret {
                    send(json!({"type":"form_draft", "form":this.form_identity, "body":this.form.read(cx).content, "revision":this.form_revision}), this.snapshot.generation);
                }
                this.schedule_autosave(w, cx);
            }
            cx.notify();
        }));
        window.focus(&composer.read(cx).focus);
        Self {
            snapshot: Snapshot::default(),
            composer,
            search,
            filter,
            form,
            text_cache: std::cell::RefCell::new(HashMap::new()),
            image_preview: None,
            focus: cx.focus_handle(),
            transcript,
            picker_scroll: ScrollHandle::new(),
            logs_visible: false,
            compact_pane: None,
            history_index: None,
            history_draft: String::new(),
            drafts: HashMap::new(),
            follow: true,
            preview,
            error: String::new(),
            panel_identity: String::new(),
            form_identity: String::new(),
            form_revision: 0,
            autosave_task: None,
            selection: 0,
            completion_index: 0,
            completion_hidden: None,
            completion_scroll: ScrollHandle::new(),
            light_override: None,
            last_width: px(0.),
            _subscriptions: subscriptions,
        }
    }
    fn theme(&self) -> Theme {
        Theme::new(
            self.light_override
                .unwrap_or(self.snapshot.theme == "nexus-light"),
        )
    }
    fn command(&mut self, command: &str, w: &mut Window, cx: &mut Context<Self>) {
        self.dispatch(json!({"type":"command", "text":command}), w, cx);
    }
    fn dispatch(&mut self, action: Value, w: &mut Window, cx: &mut Context<Self>) {
        if action["type"] == "ui_trace" {
            return;
        }
        if action["type"] == "toggle" {
            let key = action["key"].as_str().unwrap_or("");
            let narrow = (key == "sessions_sidebar" && w.viewport_size().width <= px(820.))
                || (key == "details_sidebar" && w.viewport_size().width <= px(1150.));
            if narrow {
                self.compact_pane = if self.compact_pane.as_deref() == Some(key) {
                    None
                } else {
                    Some(key.into())
                };
                w.focus(&self.focus);
                cx.notify();
                return;
            }
        }
        if self.preview {
            self.error = "Preview fixture · connect with nexus desktop to use host actions".into();
            cx.notify();
        } else {
            send(action, self.snapshot.generation);
        }
    }
    fn submit(&mut self, w: &mut Window, cx: &mut Context<Self>) {
        self.submit_mode("steer", w, cx);
    }
    fn submit_mode(&mut self, mode: &str, w: &mut Window, cx: &mut Context<Self>) {
        if !self.snapshot.agent_page.is_empty() || self.snapshot.prompt.is_some() {
            return;
        }
        let text = self.composer.read(cx).content.clone();
        if text.trim().is_empty() && self.snapshot.attachment_lines.is_empty() {
            return;
        }
        if self.preview {
            self.dispatch(json!({"type":"submit", "text":text}), w, cx);
            return;
        }
        send(
            json!({"type":"submit", "text":text, "mode":mode}),
            self.snapshot.generation,
        );
        self.composer
            .update(cx, |input, cx| input.set(String::new(), cx));
        self.follow = true;
        self.transcript.scroll_to(ListOffset {
            item_ix: self.snapshot.blocks.len(),
            offset_in_item: px(0.),
        });
    }
    fn apply(&mut self, mut next: Snapshot, window: &mut Window, cx: &mut Context<Self>) {
        if next.preview_image != self.snapshot.preview_image {
            self.image_preview = if next.preview_image.is_empty() {
                None
            } else {
                base64::engine::general_purpose::STANDARD
                    .decode(&next.preview_image)
                    .ok()
                    .filter(|b| b.len() <= 4 * 1024 * 1024)
                    .map(|bytes| {
                        std::sync::Arc::new(Image::from_bytes(
                            match next.preview_image_media.as_str() {
                                "image/jpeg" => ImageFormat::Jpeg,
                                "image/gif" => ImageFormat::Gif,
                                "image/webp" => ImageFormat::Webp,
                                _ => ImageFormat::Png,
                            },
                            bytes,
                        ))
                    })
            };
        }
        let patch_start = next.blocks_from;
        let changed_session = next.composer_key != self.snapshot.composer_key;
        let changed_page = next.agent_page != self.snapshot.agent_page;
        let old_count = self.snapshot.blocks.len();
        if let Err(error) = next.restore_blocks(&mut self.snapshot) {
            self.error = error.into();
            cx.notify();
            return;
        }
        if changed_session {
            self.text_cache.borrow_mut().clear();
            if self.drafts.len() >= 128 {
                if let Some(key) = self
                    .drafts
                    .keys()
                    .find(|key| **key != self.snapshot.composer_key)
                    .cloned()
                {
                    self.drafts.remove(&key);
                }
            }
            self.drafts.insert(
                self.snapshot.composer_key.clone(),
                self.composer.read(cx).content.clone(),
            );
            let draft = self
                .drafts
                .get(&next.composer_key)
                .cloned()
                .unwrap_or_default();
            self.composer.update(cx, |input, cx| input.set(draft, cx));
        }
        if changed_session || changed_page {
            self.transcript.reset(next.blocks.len());
            self.history_index = None;
            self.history_draft.clear();
            self.follow = true;
        } else {
            let start = if next.schema == 2 { patch_start } else { 0 };
            self.transcript
                .splice(start..old_count, next.blocks.len().saturating_sub(start));
        }
        if self.follow {
            self.transcript.scroll_to(ListOffset {
                item_ix: next.blocks.len(),
                offset_in_item: px(0.),
            });
        }
        if !next.restore.is_empty() {
            self.composer.update(cx, |input, cx| {
                let draft = std::mem::take(&mut input.content);
                input.set(
                    if draft.is_empty() {
                        next.restore.clone()
                    } else {
                        format!("{}\n\n{}", next.restore, draft)
                    },
                    cx,
                );
            });
        }
        if !next.insert.is_empty() {
            self.composer
                .update(cx, |input, cx| input.insert(&next.insert, window, cx));
        }
        let panel = format!(
            "{}:{}",
            next.panel_title,
            next.nav.as_ref().map(|n| n.selected).unwrap_or(-1)
        );
        if panel != self.panel_identity {
            self.panel_identity = panel;
            self.filter
                .update(cx, |input, cx| input.set(String::new(), cx));
            self.selection = 0;
            if !next.panel_title.is_empty() {
                if next.items.is_empty() {
                    window.focus(&self.focus);
                } else {
                    window.focus(&self.filter.read(cx).focus);
                }
            } else {
                window.focus(&self.composer.read(cx).focus);
            }
        }
        if let Some(form) = &next.form {
            if form.id != self.form_identity {
                self.autosave_task = None;
                self.form_identity = form.id.clone();
                self.form_revision = form.revision;
                self.form.update(cx, |input, cx| {
                    input.secret = form.secret;
                    input.set(form.body.clone(), cx);
                });
                window.focus(&self.form.read(cx).focus);
            } else if form.revision > self.form_revision {
                self.form_revision = form.revision;
                self.form
                    .update(cx, |input, cx| input.set(form.body.clone(), cx));
            }
        } else {
            self.autosave_task = None;
            self.form_identity.clear();
        }
        window.set_window_title(&format!(
            "{} — Nexus",
            next.tabs
                .iter()
                .find(|t| t.active)
                .map(|t| t.title.as_str())
                .unwrap_or(&next.title)
        ));
        if next.prompt.as_ref().map(|p| &p.id) != self.snapshot.prompt.as_ref().map(|p| &p.id) {
            self.filter.update(cx, |input, cx| {
                input.menu = !next.prompt.as_ref().is_some_and(|p| p.kind == "question");
                input.placeholder = if next.prompt.as_ref().is_some_and(|p| p.kind == "question") {
                    "Type your answer…".into()
                } else {
                    "Filter choices…".into()
                };
                input.set(String::new(), cx);
            });
            if next.prompt.as_ref().is_some_and(|p| p.kind == "question") {
                window.focus(&self.filter.read(cx).focus);
            } else if next.prompt.is_some() {
                window.focus(&self.focus);
            } else {
                window.focus(&self.composer.read(cx).focus);
            }
        }
        let auto_send = next.auto_send_insert;
        self.snapshot = next;
        if auto_send {
            self.submit(window, cx);
        }
        cx.notify();
    }
    fn prompt_key(&mut self, event: &KeyDownEvent, w: &mut Window, cx: &mut Context<Self>) {
        if event.keystroke.modifiers.control
            || event.keystroke.modifiers.platform
            || event.keystroke.modifiers.alt
        {
            return;
        }
        if let Some(prompt) = &self.snapshot.prompt {
            // A digit typed into a free-form answer must not pick a numbered choice.
            if prompt.kind == "question" && self.filter.read(cx).focus.is_focused(w) {
                return;
            }
            if let Some(choice) = prompt
                .choices
                .iter()
                .find(|c| !c.disabled && c.key.to_lowercase() == event.keystroke.key.to_lowercase())
            {
                let action = json!({"type":"answer","text":prompt.id,"value":choice.value});
                self.dispatch(action, w, cx);
                cx.stop_propagation();
            }
        }
    }
    fn new_session(&mut self, _: &NewSession, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/new", w, cx);
    }
    fn settings(&mut self, _: &Settings, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/settings", w, cx);
    }
    fn models(&mut self, _: &Models, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/model", w, cx);
    }
    fn agents(&mut self, _: &Agents, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/agent", w, cx);
    }
    fn effort(&mut self, _: &Effort, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/effort", w, cx);
    }
    fn sessions(&mut self, _: &Sessions, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/sessions", w, cx);
    }
    fn toggle_sessions(&mut self, _: &ToggleSessions, w: &mut Window, cx: &mut Context<Self>) {
        self.dispatch(json!({"type":"toggle","key":"sessions_sidebar"}), w, cx);
    }
    fn toggle_details(&mut self, _: &ToggleDetails, w: &mut Window, cx: &mut Context<Self>) {
        self.dispatch(json!({"type":"toggle","key":"details_sidebar"}), w, cx);
    }
    fn dismiss(&mut self, _: &Dismiss, w: &mut Window, cx: &mut Context<Self>) {
        if let Some(prompt) = &self.snapshot.prompt {
            if prompt.kind == "question" {
                window_focus(&self.filter, w, cx);
            } else {
                w.focus(&self.focus);
            }
            return;
        }
        if self.composer.read(cx).focus.is_focused(w) && self.completion_active(cx) {
            self.completion_hidden = Some(self.snapshot.completion_prefix.clone());
            self.composer.update(cx, |input, _| input.menu = false);
            cx.notify();
        } else if self.compact_pane.take().is_some() {
            window_focus(&self.composer, w, cx);
            cx.notify();
        } else if !self.snapshot.panel_title.is_empty() || !self.snapshot.agent_page.is_empty() {
            if self
                .snapshot
                .form
                .as_ref()
                .is_some_and(|form| form.autosave && !form.secret)
            {
                self.autosave_task = None;
                self.save_form(&SaveForm, w, cx);
            }
            self.dispatch(json!({"type":"dismiss"}), w, cx);
        } else if matches!(
            self.snapshot.voice_phase.as_str(),
            "recording" | "transcribing"
        ) {
            self.dispatch(json!({"type":"voice_discard"}), w, cx);
        } else {
            window_focus(&self.composer, w, cx);
        }
    }
    fn cancel(&mut self, _: &Cancel, w: &mut Window, cx: &mut Context<Self>) {
        self.dispatch(json!({"type":"cancel"}), w, cx);
    }
    fn palette(&mut self, _: &Palette, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/help", w, cx);
    }
    fn context(&mut self, _: &InspectContext, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/context", w, cx);
    }
    fn usage(&mut self, _: &Usage, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/usage", w, cx);
    }
    fn attach(&mut self, _: &Attach, w: &mut Window, cx: &mut Context<Self>) {
        if self.preview {
            self.error = "Attachments require a live host connection".into();
            cx.notify();
            return;
        }
        let generation = self.snapshot.generation;
        let paths = cx.prompt_for_paths(PathPromptOptions {
            files: true,
            directories: false,
            multiple: true,
            prompt: Some("Attach to Nexus".into()),
        });
        let entity = cx.entity().downgrade();
        cx.spawn_in(w, async move |_, cx| {
            if let Ok(Ok(Some(paths))) = paths.await {
                for path in paths.into_iter().take(8) {
                    entity
                        .update(cx, |this, _| {
                            if this.snapshot.generation == generation {
                                send(
                                    json!({"type":"command", "text":attachment_command(&path)}),
                                    generation,
                                )
                            }
                        })
                        .ok();
                }
            }
        })
        .detach();
    }
    fn voice(&mut self, _: &Voice, w: &mut Window, cx: &mut Context<Self>) {
        if self.snapshot.voice_phase == "recording" {
            self.dispatch(json!({"type":"voice_stop"}), w, cx);
        } else {
            self.command("/voice", w, cx);
        }
    }
    fn speak(&mut self, _: &Speak, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/speak", w, cx);
    }
    fn theme_toggle(&mut self, _: &ThemeToggle, w: &mut Window, cx: &mut Context<Self>) {
        if self.preview {
            self.light_override = Some(
                !self
                    .light_override
                    .unwrap_or(self.snapshot.theme == "nexus-light"),
            );
            cx.notify();
        } else {
            self.command("/theme", w, cx);
        }
    }
    fn cycle_agent(&mut self, _: &CycleAgent, w: &mut Window, cx: &mut Context<Self>) {
        self.dispatch(json!({"type":"cycle_agent"}), w, cx);
    }
    fn cycle_effort(&mut self, _: &CycleEffort, w: &mut Window, cx: &mut Context<Self>) {
        self.dispatch(json!({"type":"cycle_effort"}), w, cx);
    }
    fn toggle_logs(&mut self, _: &ToggleLogs, w: &mut Window, cx: &mut Context<Self>) {
        self.dispatch(json!({"type":"logs"}), w, cx);
    }
    fn reconnect(&mut self, _: &Reconnect, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/reconnect", w, cx);
    }
    fn latest(&mut self, _: &JumpLatest, _: &mut Window, cx: &mut Context<Self>) {
        self.follow = true;
        self.transcript.scroll_to(ListOffset {
            item_ix: self.snapshot.blocks.len(),
            offset_in_item: px(0.),
        });
        cx.notify();
    }
    fn focus_composer(&mut self, _: &FocusComposer, w: &mut Window, cx: &mut Context<Self>) {
        window_focus(&self.composer, w, cx);
    }
    fn history_previous(&mut self, _: &HistoryPrevious, w: &mut Window, cx: &mut Context<Self>) {
        self.history_step(false, w, cx);
    }
    fn history_next(&mut self, _: &HistoryNext, w: &mut Window, cx: &mut Context<Self>) {
        self.history_step(true, w, cx);
    }
    fn history_step(&mut self, next: bool, w: &mut Window, cx: &mut Context<Self>) {
        if !self.composer.read(cx).focus.is_focused(w) || self.snapshot.history.is_empty() {
            return;
        }
        let text = if next {
            match self.history_index {
                Some(index) if index + 1 < self.snapshot.history.len() => {
                    self.history_index = Some(index + 1);
                    self.snapshot.history[index + 1].clone()
                }
                Some(_) => {
                    self.history_index = None;
                    std::mem::take(&mut self.history_draft)
                }
                None => return,
            }
        } else {
            let index = match self.history_index {
                Some(index) => index.saturating_sub(1).min(self.snapshot.history.len() - 1),
                None => {
                    self.history_draft = self.composer.read(cx).content.clone();
                    self.snapshot.history.len() - 1
                }
            };
            self.history_index = Some(index);
            self.snapshot.history[index].clone()
        };
        self.composer
            .update(cx, |input, cx| input.set(text.clone(), cx));
        if !self.preview {
            send(
                json!({"type":"draft_changed","text":text}),
                self.snapshot.generation,
            );
        }
    }
    fn completion_active(&self, cx: &App) -> bool {
        let input = self.composer.read(cx);
        !self.snapshot.completions.is_empty()
            && self.completion_hidden.as_deref() != Some(self.snapshot.completion_prefix.as_str())
            && input.cursor() == self.snapshot.completion_prefix.len()
            && input.content.starts_with(&self.snapshot.completion_prefix)
    }
    fn insert_completion(&mut self, cx: &mut Context<Self>) -> bool {
        if !self.completion_active(cx) {
            return false;
        }
        let Some(value) = self.snapshot.completions.get(
            self.completion_index
                .min(self.snapshot.completions.len() - 1),
        ) else {
            return false;
        };
        let Some(next) = completion_text(
            &self.composer.read(cx).content,
            &self.snapshot.completion_prefix,
            &self.snapshot.completion_query,
            value,
        ) else {
            return false;
        };
        self.composer
            .update(cx, |input, cx| input.set(next.clone(), cx));
        self.completion_hidden = Some(self.snapshot.completion_prefix.clone());
        if !self.preview {
            send(
                json!({"type":"draft_changed","text":next}),
                self.snapshot.generation,
            );
        }
        cx.notify();
        true
    }
    fn complete_first(&mut self, _: &CompleteFirst, w: &mut Window, cx: &mut Context<Self>) {
        if self.composer.read(cx).focus.is_focused(w) && self.insert_completion(cx) {
            return;
        }
        w.focus_next();
    }
    fn queue_submit(&mut self, _: &QueueSubmit, w: &mut Window, cx: &mut Context<Self>) {
        self.submit_mode("queue", w, cx);
    }
    fn interrupt_submit(&mut self, _: &InterruptSubmit, w: &mut Window, cx: &mut Context<Self>) {
        self.submit_mode("interrupt", w, cx);
    }
    fn sort_models(&mut self, _: &SortModels, w: &mut Window, cx: &mut Context<Self>) {
        if self.snapshot.panel_title == "Select model" {
            self.dispatch(json!({"type":"model_sort"}), w, cx);
        }
    }
    fn refresh_models(&mut self, _: &RefreshModels, w: &mut Window, cx: &mut Context<Self>) {
        if self.snapshot.panel_title == "Select model" {
            self.dispatch(json!({"type":"refresh_models"}), w, cx);
        }
    }
    fn favorite_model(&mut self, _: &FavoriteModel, w: &mut Window, cx: &mut Context<Self>) {
        if self.snapshot.panel_title != "Select model" {
            return;
        }
        let query = self.filter.read(cx).content.to_lowercase();
        if let Some((index, _)) = self
            .snapshot
            .items
            .iter()
            .enumerate()
            .filter(|(_, item)| {
                format!("{} {}", item.label, item.detail)
                    .to_lowercase()
                    .contains(&query)
            })
            .nth(self.selection)
        {
            self.dispatch(
                json!({"type":"favorite","filter":"","selection":index}),
                w,
                cx,
            );
        }
    }
    fn schedule_autosave(&mut self, w: &mut Window, cx: &mut Context<Self>) {
        self.autosave_task = None;
        if !self
            .snapshot
            .form
            .as_ref()
            .is_some_and(|form| form.autosave && !form.secret)
        {
            return;
        }
        let identity = self.form_identity.clone();
        let revision = self.form_revision;
        let generation = self.snapshot.generation;
        let timer = cx
            .background_executor()
            .timer(std::time::Duration::from_millis(700));
        self.autosave_task = Some(cx.spawn_in(w, async move |weak, cx| {
            timer.await;
            weak.update_in(cx, |this, w, cx| {
                if this.form_identity == identity
                    && this.form_revision == revision
                    && this.snapshot.generation == generation
                {
                    this.save_form(&SaveForm, w, cx);
                }
            })
            .ok();
        }));
    }
    fn save_form(&mut self, _: &SaveForm, w: &mut Window, cx: &mut Context<Self>) {
        if !self.form_identity.is_empty() {
            self.dispatch(json!({"type":"save", "form":self.form_identity, "body":self.form.read(cx).content, "revision":self.form_revision}), w, cx);
        }
    }
}
fn window_focus(input: &Entity<Input>, w: &mut Window, cx: &mut App) {
    w.focus(&input.read(cx).focus);
}
fn attachment_command(path: &std::path::Path) -> String {
    format!("/attach \"{}\"", path.to_string_lossy())
}
fn completion_text(text: &str, prefix: &str, query: &str, value: &str) -> Option<String> {
    if !text.starts_with(prefix) || !prefix.ends_with(query) {
        return None;
    }
    Some(format!(
        "{}{}{} {}",
        &prefix[..prefix.len() - query.len()],
        if query.starts_with('@') { "@" } else { "" },
        value,
        &text[prefix.len()..]
    ))
}
fn item_action(item: &Item) -> Value {
    if let Some(operation) = &item.operation {
        json!({"type":"operation", "operation":operation})
    } else {
        json!({"type":"pick", "text":item.command})
    }
}
impl Render for Desktop {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let t = self.theme();
        let overlay = !self.snapshot.panel_title.is_empty() || self.snapshot.prompt.is_some();
        for (input, background) in [
            (&self.composer, true),
            (&self.search, true),
            (&self.filter, false),
            (&self.form, false),
        ] {
            input.update(cx, |i, _| {
                i.foreground = t.text;
                i.muted = t.muted;
                i.accent = t.accent;
                i.tab_enabled = !background || !overlay;
            });
        }
        let completions = self.completion_active(cx) && !overlay;
        self.composer
            .update(cx, |input, _| input.menu = completions);
        let width = window.viewport_size().width;
        if width != self.last_width {
            self.last_width = width;
            self.transcript.reset(self.snapshot.blocks.len());
        }
        let sidebar = self.snapshot.sessions_sidebar && width > px(820.);
        let details = self.snapshot.details_sidebar && width > px(1150.);
        let logs_visible = (details || self.compact_pane.as_deref() == Some("details_sidebar"))
            && self.snapshot.details_panel.tab == "Logs";
        if logs_visible != self.logs_visible {
            self.logs_visible = logs_visible;
            if !self.preview {
                send(
                    json!({"type":"details_visible","open":logs_visible}),
                    self.snapshot.generation,
                );
            }
        }
        let view = cx.entity().downgrade();
        let content = if !self.snapshot.blocks.iter().any(|b| {
            matches!(
                b.kind.as_str(),
                "user" | "markdown" | "tool" | "tool_group" | "thought" | "error" | "literal"
            )
        }) {
            self.empty_state(cx)
        } else {
            list(self.transcript.clone(), move |ix, window, cx| {
                view.update(cx, |this, cx| {
                    let block = this.snapshot.blocks.get(ix).cloned().unwrap_or_default();
                    this.render_block(&block, window, cx)
                })
                .unwrap_or_else(|_| div().into_any_element())
            })
            .size_full()
            .into_any_element()
        };
        let mut root = div()
            .id("desktop")
            .key_context("Nexus")
            .track_focus(&self.focus)
            .size_full()
            .flex()
            .flex_col()
            .relative()
            .overflow_hidden()
            .font_family(".AppleSystemUIFont")
            .text_size(px(13.))
            .text_color(t.text)
            .bg(t.background)
            .on_action(cx.listener(Self::history_previous))
            .on_action(cx.listener(Self::history_next))
            .on_action(cx.listener(Self::complete_first))
            .on_action(cx.listener(|_, _: &PreviousFocus, w, _| w.focus_prev()))
            .on_action(cx.listener(Self::queue_submit))
            .on_action(cx.listener(Self::interrupt_submit))
            .on_action(cx.listener(Self::sort_models))
            .on_action(cx.listener(Self::refresh_models))
            .on_action(cx.listener(Self::favorite_model))
            .on_action(cx.listener(Self::new_session))
            .on_action(cx.listener(Self::settings))
            .on_action(cx.listener(Self::models))
            .on_action(cx.listener(Self::agents))
            .on_action(cx.listener(Self::effort))
            .on_action(cx.listener(Self::sessions))
            .on_action(cx.listener(|_, _: &ReviewCompact, w, _| {
                w.resize(size(px(780.), px(720.)));
                eprintln!("Native review: requested compact window");
            }))
            .on_action(cx.listener(|_, _: &ReviewWide, w, _| {
                w.resize(size(px(1440.), px(940.)));
                eprintln!("Native review: requested wide window");
            }))
            .on_action(cx.listener(Self::toggle_sessions))
            .on_action(cx.listener(Self::toggle_details))
            .on_action(cx.listener(Self::dismiss))
            .on_action(cx.listener(Self::cancel))
            .on_action(cx.listener(Self::palette))
            .on_action(cx.listener(Self::context))
            .on_action(cx.listener(Self::usage))
            .on_action(cx.listener(Self::attach))
            .on_action(cx.listener(Self::voice))
            .on_action(cx.listener(Self::speak))
            .on_action(cx.listener(Self::theme_toggle))
            .on_action(cx.listener(Self::cycle_agent))
            .on_action(cx.listener(Self::cycle_effort))
            .on_action(cx.listener(Self::toggle_logs))
            .on_action(cx.listener(Self::reconnect))
            .on_action(cx.listener(Self::latest))
            .on_action(cx.listener(Self::save_form))
            .on_action(cx.listener(Self::focus_composer))
            .on_key_down(cx.listener(Self::prompt_key))
            .on_drop(cx.listener(|this, paths: &ExternalPaths, w, cx| {
                for path in paths.paths().iter().take(8) {
                    this.command(&attachment_command(path), w, cx);
                }
            }))
            .child(self.topbar(cx))
            .child(
                div()
                    .flex()
                    .flex_1()
                    .min_h_0()
                    .when(sidebar, |d| d.child(self.sidebar(cx)))
                    .child(
                        div()
                            .flex()
                            .flex_col()
                            .flex_1()
                            .min_w_0()
                            .child(self.breadcrumb(cx))
                            .child(div().flex_1().min_h_0().child(content))
                            .child(
                                div()
                                    .w_full()
                                    .max_w(px(980.))
                                    .mx_auto()
                                    .child(self.composer_view(cx)),
                            ),
                    )
                    .when(details, |d| d.child(self.details(cx))),
            );
        if let Some(pane) = self.compact_pane.as_deref() {
            if (pane == "sessions_sidebar" && !sidebar) || (pane == "details_sidebar" && !details) {
                let content = if pane == "sessions_sidebar" {
                    self.sidebar(cx)
                } else {
                    self.details(cx)
                };
                root = root.child(
                    div()
                        .absolute()
                        .top(px(46.))
                        .bottom_0()
                        .left_0()
                        .right_0()
                        .bg(gpui::rgba(0x000000a0))
                        .on_mouse_down(
                            MouseButton::Left,
                            cx.listener(|this, _, w, cx| {
                                this.compact_pane = None;
                                window_focus(&this.composer, w, cx);
                                cx.notify();
                            }),
                        )
                        .child(
                            div()
                                .absolute()
                                .top_0()
                                .bottom_0()
                                .when(pane == "sessions_sidebar", |d| d.left_0())
                                .when(pane == "details_sidebar", |d| d.right_0())
                                .on_mouse_down(MouseButton::Left, |_, _, cx| cx.stop_propagation())
                                .child(content),
                        ),
                );
            }
        }
        if !self.snapshot.panel_title.is_empty() {
            root = root.child(self.panel(window, cx));
        }
        if self.snapshot.prompt.is_some() {
            root = root.child(self.prompt_view(cx));
        }
        if !self.error.is_empty() {
            root = root.child(
                div()
                    .absolute()
                    .bottom(px(180.))
                    .left(px(300.))
                    .right(px(30.))
                    .p_3()
                    .rounded(px(3.))
                    .bg(t.raised)
                    .text_color(t.amber)
                    .child(self.error.clone())
                    .on_mouse_down(
                        MouseButton::Left,
                        cx.listener(|this, _, _, cx| {
                            this.error.clear();
                            cx.notify();
                        }),
                    ),
            );
        }
        root
    }
}
fn bind_desktop_keys(cx: &mut App) {
    input::bindings(cx);
    cx.bind_keys([
        KeyBinding::new("alt-up", HistoryPrevious, Some("Nexus")),
        KeyBinding::new("alt-down", HistoryNext, Some("Nexus")),
        KeyBinding::new("tab", CompleteFirst, Some("Nexus")),
        KeyBinding::new("shift-tab", PreviousFocus, Some("Nexus")),
        KeyBinding::new("ctrl-enter", QueueSubmit, Some("Nexus")),
        KeyBinding::new("alt-enter", InterruptSubmit, Some("Nexus")),
        KeyBinding::new("ctrl-f", FavoriteModel, Some("Nexus")),
        KeyBinding::new("ctrl-s", SortModels, Some("Nexus")),
        KeyBinding::new("ctrl-r", RefreshModels, Some("Nexus")),
        KeyBinding::new("ctrl-n", NewSession, Some("Nexus")),
        KeyBinding::new("ctrl-p", Sessions, Some("Nexus")),
        KeyBinding::new("ctrl-x m", Models, Some("Nexus")),
        KeyBinding::new("ctrl-x g", Agents, Some("Nexus")),
        KeyBinding::new("ctrl-x e", Effort, Some("Nexus")),
        KeyBinding::new("cmd-q", Quit, None),
        KeyBinding::new("cmd-n", NewSession, Some("Nexus")),
        KeyBinding::new("cmd-,", Settings, Some("Nexus")),
        KeyBinding::new("cmd-k", Palette, Some("Nexus")),
        KeyBinding::new("cmd-m", Models, Some("Nexus")),
        KeyBinding::new("cmd-b", ToggleSessions, Some("Nexus")),
        KeyBinding::new("ctrl-b", ToggleSessions, Some("Nexus")),
        KeyBinding::new("cmd-l", ToggleDetails, Some("Nexus")),
        KeyBinding::new("ctrl-l", ToggleDetails, Some("Nexus")),
        KeyBinding::new("escape", Dismiss, Some("Nexus")),
        KeyBinding::new("cmd-period", Cancel, Some("Nexus")),
        KeyBinding::new("ctrl-c", Cancel, Some("Nexus")),
        KeyBinding::new("cmd-i", InspectContext, Some("Nexus")),
        KeyBinding::new("ctrl-i", InspectContext, Some("Nexus")),
        KeyBinding::new("cmd-u", Usage, Some("Nexus")),
        KeyBinding::new("ctrl-u", Usage, Some("Nexus")),
        KeyBinding::new("cmd-shift-a", Attach, Some("Nexus")),
        KeyBinding::new("ctrl-space", Voice, Some("Nexus")),
        KeyBinding::new("cmd-shift-t", ThemeToggle, Some("Nexus")),
        KeyBinding::new("cmd-shift-g", CycleAgent, Some("Nexus")),
        KeyBinding::new("cmd-shift-e", CycleEffort, Some("Nexus")),
        KeyBinding::new("cmd-shift-l", ToggleLogs, Some("Nexus")),
        KeyBinding::new("cmd-r", Reconnect, Some("Nexus")),
        KeyBinding::new("cmd-s", SaveForm, Some("Nexus")),
        KeyBinding::new("cmd-j", JumpLatest, Some("Nexus")),
        KeyBinding::new("cmd-enter", FocusComposer, Some("Nexus")),
    ]);
}
fn main() {
    let args: Vec<_> = std::env::args().collect();
    let preview_path = args
        .iter()
        .position(|arg| arg == "--preview")
        .and_then(|i| args.get(i + 1))
        .cloned();
    let (tx, rx) = async_channel::bounded::<Result<Snapshot, String>>(32);
    if let Some(path) = &preview_path {
        let result = std::fs::metadata(path)
            .map_err(|e| e.to_string())
            .and_then(|m| {
                if m.len() > 16 * 1024 * 1024 {
                    Err("Preview exceeds the 16 MiB limit".into())
                } else {
                    std::fs::read_to_string(path).map_err(|e| e.to_string())
                }
            })
            .and_then(|s| serde_json::from_str(&s).map_err(|e| e.to_string()));
        tx.send_blocking(result).ok();
    } else {
        std::thread::spawn(move || {
            let mut input = io::stdin().lock();
            loop {
                let mut line = Vec::new();
                let result = Read::by_ref(&mut input)
                    .take(16 * 1024 * 1024 + 1)
                    .read_until(b'\n', &mut line);
                match result {
                    Ok(0) => {
                        tx.send_blocking(Err(
                            "Presentation bridge disconnected. Relaunch nexus desktop to reattach."
                                .into(),
                        ))
                        .ok();
                        break;
                    }
                    Ok(_) if line.len() <= 16 * 1024 * 1024 => {
                        if tx
                            .send_blocking(
                                serde_json::from_slice(&line)
                                    .map_err(|e| format!("Invalid snapshot: {e}")),
                            )
                            .is_err()
                        {
                            break;
                        }
                    }
                    _ => {
                        tx.send_blocking(Err(
                            "Presentation snapshot exceeds the 16 MiB limit".into()
                        ))
                        .ok();
                        break;
                    }
                }
            }
        });
    }
    Application::new()
        .with_assets(icons::Assets)
        .run(move |cx: &mut App| {
            bind_desktop_keys(cx);
            if std::env::var("NEXUS_DESKTOP_REVIEW").as_deref() == Ok("1") {
                cx.bind_keys([
                    KeyBinding::new("cmd-shift-y", ReviewCompact, Some("Nexus")),
                    KeyBinding::new("cmd-shift-o", ReviewWide, Some("Nexus")),
                ]);
            }
            cx.on_action(|_: &Quit, cx| {
                send(json!({"type":"quit"}), 0);
                cx.quit();
            });
            cx.on_window_closed(|cx| {
                if cx.windows().is_empty() {
                    send(json!({"type":"quit"}), 0);
                    cx.quit();
                }
            })
            .detach();
            let initial_size = std::env::var("NEXUS_DESKTOP_WINDOW_SIZE")
                .ok()
                .and_then(|s| {
                    let (width, height) = s.split_once('x')?;
                    Some(size(
                        px(width.parse::<f32>().ok()?.clamp(640., 3840.)),
                        px(height.parse::<f32>().ok()?.clamp(480., 2160.)),
                    ))
                })
                .unwrap_or(size(px(1440.), px(940.)));
            let bounds = Bounds::centered(None, initial_size, cx);
            cx.open_window(
                WindowOptions {
                    window_bounds: Some(WindowBounds::Windowed(bounds)),
                    titlebar: Some(TitlebarOptions {
                        title: Some("Nexus".into()),
                        appears_transparent: true,
                        traffic_light_position: Some(point(px(18.), px(20.))),
                    }),
                    window_min_size: Some(size(px(640.), px(480.))),
                    ..Default::default()
                },
                |window, cx| {
                    let view = cx.new(|cx| Desktop::new(window, cx, preview_path.is_some()));
                    let weak = view.downgrade();
                    let mut async_window = window.to_async(cx);
                    cx.spawn(async move |_| {
                        while let Ok(result) = rx.recv().await {
                            if weak
                                .update_in(&mut async_window, |this, window, cx| match result {
                                    Ok(snapshot) => this.apply(snapshot, window, cx),
                                    Err(error) => {
                                        this.error = error;
                                        cx.notify();
                                    }
                                })
                                .is_err()
                            {
                                break;
                            }
                        }
                    })
                    .detach();
                    window.resize(initial_size);
                    cx.activate(true);
                    view
                },
            )
            .expect("Unable to open Nexus desktop window");
        });
}

#[cfg(test)]
mod native_tests {
    use super::*;
    #[test]
    fn completion_preserves_file_mentions_and_rejects_stale_prefixes() {
        assert_eq!(
            completion_text("read @src", "read @src", "@src", "src/main.rs"),
            Some("read @src/main.rs ".into())
        );
        assert_eq!(
            completion_text("/theme ", "/theme ", "", "light"),
            Some("/theme light ".into())
        );
        assert_eq!(
            completion_text("/agent bu", "/agent bu", "bu", "build"),
            Some("/agent build ".into())
        );
        assert_eq!(
            completion_text("read @src then explain", "read @src", "@src", "src/main.rs"),
            Some("read @src/main.rs  then explain".into())
        );
        assert_eq!(
            completion_text("other bu", "/agent bu", "bu", "build"),
            None
        );
    }
    #[gpui::test]
    fn escape_keeps_pending_decisions_focused_instead_of_the_composer(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                for kind in ["question", "permission"] {
                    this.apply(
                        Snapshot {
                            prompt: Some(bridge::Prompt {
                                kind: kind.into(),
                                id: kind.into(),
                                lines: vec![],
                                choices: vec![],
                            }),
                            ..Default::default()
                        },
                        w,
                        cx,
                    );
                    this.dismiss(&Dismiss, w, cx);
                    assert!(!this.composer.read(cx).focus.is_focused(w));
                    if kind == "question" {
                        assert!(this.filter.read(cx).focus.is_focused(w));
                    } else {
                        assert!(this.focus.is_focused(w));
                    }
                }
            })
            .unwrap();
    }
    #[gpui::test]
    fn completion_navigation_inserts_the_selected_candidate_and_escape_dismisses(
        cx: &mut TestAppContext,
    ) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                this.apply(
                    Snapshot {
                        completion_prefix: "@s".into(),
                        completion_query: "@s".into(),
                        completions: vec!["src/one.rs".into(), "src/two.rs".into()],
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                this.composer.update(cx, |input, cx| {
                    input.set("@s".into(), cx);
                    cx.emit(InputEvent::Navigate(1));
                });
            })
            .unwrap();
        cx.executor().run_until_parked();
        window
            .update(cx, |this, w, cx| {
                assert_eq!(this.completion_index, 1);
                assert!(this.insert_completion(cx));
                assert_eq!(this.composer.read(cx).content, "@src/two.rs ");
                this.composer
                    .update(cx, |input, cx| input.set("@s".into(), cx));
                this.completion_hidden = None;
                this.dismiss(&Dismiss, w, cx);
                assert!(!this.completion_active(cx));
                assert_eq!(this.composer.read(cx).content, "@s");
            })
            .unwrap();
    }
    #[gpui::test]
    fn autosave_waits_for_quiet_and_does_not_save_a_replaced_form(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                this.apply(
                    Snapshot {
                        form: Some(bridge::Form {
                            id: "settings".into(),
                            autosave: true,
                            ..Default::default()
                        }),
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                this.form
                    .update(cx, |input, cx| input.set("first".into(), cx));
                this.schedule_autosave(w, cx);
            })
            .unwrap();
        cx.executor().run_until_parked();
        cx.executor()
            .advance_clock(std::time::Duration::from_millis(600));
        window
            .update(cx, |this, w, cx| {
                assert!(this.error.is_empty());
                this.form_revision += 1;
                this.form
                    .update(cx, |input, cx| input.set("second".into(), cx));
                this.schedule_autosave(w, cx);
            })
            .unwrap();
        cx.executor().run_until_parked();
        cx.executor()
            .advance_clock(std::time::Duration::from_millis(600));
        window
            .update(cx, |this, _, _| assert!(this.error.is_empty()))
            .unwrap();
        cx.executor()
            .advance_clock(std::time::Duration::from_millis(101));
        cx.executor().run_until_parked();
        window
            .update(cx, |this, w, cx| {
                assert!(this.error.contains("Preview fixture"));
                this.error.clear();
                this.schedule_autosave(w, cx);
                this.apply(Snapshot::default(), w, cx);
            })
            .unwrap();
        cx.executor().run_until_parked();
        cx.executor()
            .advance_clock(std::time::Duration::from_secs(1));
        cx.executor().run_until_parked();
        window
            .update(cx, |this, _, _| assert!(this.error.is_empty()))
            .unwrap();
    }
    #[gpui::test]
    fn secret_enter_saves_without_inserting_a_newline(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                this.apply(
                    Snapshot {
                        form: Some(bridge::Form {
                            id: "secret".into(),
                            secret: true,
                            ..Default::default()
                        }),
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                this.form.update(cx, |input, cx| {
                    input.set("test-only-secret".into(), cx);
                    cx.emit(InputEvent::Submitted);
                });
            })
            .unwrap();
        cx.executor().run_until_parked();
        window
            .update(cx, |this, _, cx| {
                assert_eq!(this.form.read(cx).content, "test-only-secret");
                assert!(this.error.contains("Preview fixture"));
                assert!(this.autosave_task.is_none());
            })
            .unwrap();
    }
    #[gpui::test]
    fn session_switch_keeps_local_draft_and_applies_ordered_patches(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                let snapshot = |key: &str, generation: u64| Snapshot {
                    composer_key: key.into(),
                    generation,
                    theme: "nexus-dark".into(),
                    ..Default::default()
                };
                this.apply(snapshot("one", 1), w, cx);
                this.composer
                    .update(cx, |input, cx| input.set("unfinished 🦀".into(), cx));
                this.apply(snapshot("two", 2), w, cx);
                assert!(this.composer.read(cx).content.is_empty());
                this.apply(snapshot("one", 3), w, cx);
                assert_eq!(this.composer.read(cx).content, "unfinished 🦀");
                let mut first = snapshot("one", 3);
                first.schema = 2;
                first.blocks = vec![Content {
                    id: "a".into(),
                    kind: "markdown".into(),
                    text: "hello".into(),
                    ..Default::default()
                }];
                this.apply(first, w, cx);
                let mut patch = snapshot("one", 3);
                patch.schema = 2;
                patch.blocks_from = 1;
                patch.blocks = vec![Content {
                    id: "b".into(),
                    kind: "markdown".into(),
                    text: "world".into(),
                    ..Default::default()
                }];
                this.apply(patch, w, cx);
                assert_eq!(this.snapshot.blocks.len(), 2);
                assert_eq!(this.transcript.item_count(), 2);
            })
            .unwrap();
    }
    #[gpui::test]
    fn read_only_panel_keeps_keyboard_focus_on_visible_root(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                this.apply(
                    Snapshot {
                        panel_title: "Context usage".into(),
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                assert!(this.focus.is_focused(w));
                assert!(!this.filter.read(cx).focus.is_focused(w));
                this.apply(Snapshot::default(), w, cx);
                assert!(this.composer.read(cx).focus.is_focused(w));
            })
            .unwrap();
    }
    #[gpui::test]
    fn history_restores_unsubmitted_draft(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                this.snapshot.history = vec!["first".into(), "last".into()];
                this.composer
                    .update(cx, |input, cx| input.set("unfinished".into(), cx));
                this.history_step(false, w, cx);
                assert_eq!(this.composer.read(cx).content, "last");
                this.history_step(false, w, cx);
                assert_eq!(this.composer.read(cx).content, "first");
                this.history_step(true, w, cx);
                this.history_step(true, w, cx);
                assert_eq!(this.composer.read(cx).content, "unfinished");
            })
            .unwrap();
    }
    #[gpui::test]
    fn collapsed_details_opens_as_a_drawer_and_escape_restores_composer(cx: &mut TestAppContext) {
        let window = cx.update(|cx| {
            cx.open_window(
                WindowOptions {
                    window_bounds: Some(WindowBounds::Windowed(Bounds::new(
                        Point::default(),
                        size(px(780.), px(720.)),
                    ))),
                    ..Default::default()
                },
                |w, cx| cx.new(|cx| Desktop::new(w, cx, true)),
            )
            .unwrap()
        });
        window
            .update(cx, |this, w, cx| {
                assert!(w.viewport_size().width <= px(1150.));
                this.toggle_details(&ToggleDetails, w, cx);
                assert_eq!(this.compact_pane.as_deref(), Some("details_sidebar"));
                assert!(this.error.is_empty()); // Presentation-only toggle doesn't invoke fixture host actions.
                this.dismiss(&Dismiss, w, cx);
                assert!(this.compact_pane.is_none());
                assert!(this.composer.read(cx).focus.is_focused(w));
            })
            .unwrap();
    }
    #[gpui::test]
    fn tab_reaches_attach_and_enter_activates_the_native_button(cx: &mut TestAppContext) {
        cx.update(bind_desktop_keys);
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        cx.simulate_keystrokes(handle.into(), "tab");
        cx.run_until_parked();
        handle
            .update(cx, |this, w, cx| {
                assert!(!this.composer.read(cx).focus.is_focused(w))
            })
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        cx.simulate_keystrokes(handle.into(), "enter");
        let mut visual = VisualTestContext::from_window(handle.into(), cx);
        visual.simulate_event(KeyUpEvent {
            keystroke: Keystroke::parse("enter").unwrap(),
        });
        cx.run_until_parked();
        handle
            .update(cx, |this, _, _| {
                assert!(this.error.contains("Attachments require"), "{}", this.error)
            })
            .unwrap();
    }
    #[test]
    fn attachment_paths_are_quoted_without_losing_spaces_or_apostrophes() {
        assert_eq!(
            attachment_command(std::path::Path::new("a  b's.md")),
            "/attach \"a  b's.md\""
        );
    }
}
