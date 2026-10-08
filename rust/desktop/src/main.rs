//! Nexus GPUI desktop: presentation-only client of the shared native bridge.
#[cfg(test)]
use core::prelude::v1::test;
#[path = "../../tui/src/bridge.rs"]
mod bridge;
mod icons;
mod input;
mod keymap;
mod markdown;
mod minimap;
mod notice;
mod panels;
mod settings;
mod text;
mod theme;
mod trace;
mod transcript;
mod wire;
use base64::Engine;
use bridge::{Content, Item, Snapshot};
use gpui::{prelude::*, *};
use input::{Input, InputEvent};
use notice::{NoticeAction, NoticeKind, NoticeStack};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{self, BufRead, Read, Write},
};
use theme::Theme;
use wire::Decoder;
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
        StopTurn,
        Palette,
        InspectContext,
        Usage,
        Attach,
        Voice,
        Speak,
        ThemeToggle,
        Reconnect,
        ForkSession,
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
        ToggleLogs,
        Shortcuts,
        ContextPopover,
        UpdateHelp,
        TranscriptPageUp,
        TranscriptPageDown,
        DetailsPrevious,
        DetailsNext,
        FocusNext
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
    parsed_docs: markdown::ParsedDocCache,
    image_preview: Option<std::sync::Arc<Image>>,
    inline_images: HashMap<String, std::sync::Arc<Image>>,
    image_decode_revision: u64,
    image_decode_task: Option<Task<()>>,
    focus: FocusHandle,
    transcript: ListState,
    session_list: ListState,
    #[cfg(test)]
    session_render_count: std::cell::Cell<usize>,
    #[cfg(test)]
    resize_reset_count: std::cell::Cell<usize>,
    session_rows: std::cell::RefCell<Vec<(usize, bool)>>,
    transcript_visible: std::cell::Cell<(usize, usize)>,
    minimap_ticks: Vec<minimap::Tick>,
    picker_scroll: ScrollHandle,
    logs_visible: bool,
    trace: trace::SharedTrace,
    compact_pane: Option<String>,
    history_index: Option<usize>,
    history_draft: String,
    drafts: HashMap<String, String>,
    follow: bool,
    follow_pending: bool,
    /// Transcript keyboard-navigation target (`Tab` from an empty composer).
    nav: Option<usize>,
    last_escape: Option<std::time::Instant>,
    preview: bool,
    notices: NoticeStack,
    seen_update_notice: String,
    seen_host_toast: u64,
    panel_identity: String,
    settings_inputs: std::cell::RefCell<HashMap<String, (Entity<Input>, Subscription)>>,
    settings_open: Option<String>,
    form_identity: String,
    form_revision: u64,
    autosave_task: Option<Task<()>>,
    draft_task: Option<Task<()>>,
    completion_task: Option<Task<()>>,
    pending_draft: bool,
    search_selected: Option<String>,
    draft_revision: u64,
    completion_revision: u64,
    selection: usize,
    completion_index: usize,
    completion_hidden: Option<String>,
    completion_scroll: ScrollHandle,
    light_override: Option<bool>,
    last_width: Pixels,
    resize_task: Option<Task<()>>,
    resize_revision: u64,
    _subscriptions: Vec<Subscription>,
}
impl Desktop {
    /// Small conversations can infer their height and sit at the top. Full
    /// measurement is limited to 64 blocks; larger sessions stay virtualized.
    fn make_transcript(cx: &mut Context<Self>, measure_all: bool) -> ListState {
        let transcript = ListState::new(0, ListAlignment::Bottom, px(800.));
        let transcript = if measure_all {
            transcript.measure_all()
        } else {
            transcript
        };
        let weak = cx.entity().downgrade();
        transcript.set_scroll_handler(move |event, _, cx| {
            weak.update(cx, |this, _| {
                this.transcript_visible
                    .set((event.visible_range.start, event.visible_range.end));
                // Read the block count from the snapshot, never the list: this
                // handler runs inside the list's own layout borrow, so calling
                // `transcript.item_count()` here would re-enter its RefCell.
                let count = this.snapshot.blocks.len();
                if count == 0 {
                    // Nothing to follow yet; keep the initial follow state.
                    return;
                }
                // Bottom alignment reports a real pixel departure from the end,
                // even while a tall final block remains visible.
                this.follow = !event.is_scrolled;
                if this.follow {
                    this.follow_pending = false;
                }
            })
            .ok();
        });
        transcript
    }
    fn new(window: &mut Window, cx: &mut Context<Self>, preview: bool) -> Self {
        let composer = cx.new(|cx| {
            let mut input = Input::new("Ask anything, or / for commands…", cx);
            input.atomic_markers = true;
            input
        });
        let search = cx.new(|cx| {
            // Up/Down navigate the filtered session rows instead of the caret
            // (the field is single-line), matching the picker behaviour.
            let mut input = Input::new("Search sessions", cx);
            input.menu = true;
            input
        });
        let filter = cx.new(|cx| {
            let mut i = Input::new("Filter choices…", cx);
            i.menu = true;
            i
        });
        let form = cx.new(|cx| Input::new("", cx));
        let transcript = Self::make_transcript(cx, true);
        let mut subscriptions = vec![];
        subscriptions.push(cx.observe_window_bounds(window, |this, window, cx| {
            this.sync_logs_visibility(window);
            cx.notify();
        }));

        let composer_focus = composer.read(cx).focus.clone();
        subscriptions.push(cx.on_blur(&composer_focus, window, |this, _, cx| this.flush_draft(cx)));
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
                        this.trace.borrow_mut().input();
                        this.completion_index = 0;
                        this.completion_hidden = None;
                        this.schedule_draft_and_completion(cx);
                        this.sync_input_style(cx);
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
                    .filter(|item| {
                        format!("{} {}", item.label, item.detail)
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
                    .filter(|item| {
                        format!("{} {}", item.label, item.detail)
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
            match event {
                InputEvent::Changed => this.search_selected = None,
                InputEvent::Navigate(direction) => {
                    let query = this.search.read(cx).content.to_lowercase();
                    let matches = search_matches(&this.snapshot.sessions, &query);
                    if !matches.is_empty() {
                        let selected = this.search_selected.clone();
                        let current = selected.as_deref().and_then(|id| {
                            matches
                                .iter()
                                .position(|&index| this.snapshot.sessions[index].id == id)
                        });
                        let next = next_selection(current, matches.len(), *direction);
                        this.search_selected =
                            Some(this.snapshot.sessions[matches[next]].id.clone());
                        // Reveal the selected session in the variable-height list.
                        this.reveal_selected_session();
                    }
                }
                InputEvent::Submitted => {
                    let query = this.search.read(cx).content.to_lowercase();
                    let matches = search_matches(&this.snapshot.sessions, &query);
                    let selected = this
                        .search_selected
                        .as_deref()
                        .and_then(|id| {
                            matches
                                .iter()
                                .copied()
                                .find(|&index| this.snapshot.sessions[index].id == id)
                        })
                        .or_else(|| matches.first().copied());
                    if let Some(index) = selected {
                        let row = &this.snapshot.sessions[index];
                        this.dispatch(
                            json!({"type":"session_open","text":row.id,"workspace":row.workspace}),
                            w,
                            cx,
                        );
                    }
                }
                _ => {}
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
            parsed_docs: markdown::ParsedDocCache::new(256),
            image_preview: None,
            inline_images: HashMap::new(),
            image_decode_revision: 0,
            image_decode_task: None,
            focus: cx.focus_handle(),
            transcript,
            #[cfg(test)]
            session_render_count: std::cell::Cell::new(0),
            #[cfg(test)]
            resize_reset_count: std::cell::Cell::new(0),
            session_list: ListState::new(0, ListAlignment::Top, px(100.)),
            session_rows: std::cell::RefCell::new(Vec::new()),
            transcript_visible: std::cell::Cell::new((0, 0)),
            minimap_ticks: Vec::new(),
            picker_scroll: ScrollHandle::new(),
            logs_visible: false,
            trace: trace::Trace::new(),
            compact_pane: None,
            history_index: None,
            history_draft: String::new(),
            drafts: HashMap::new(),
            follow: true,
            follow_pending: false,
            nav: None,
            last_escape: None,
            preview,
            notices: NoticeStack::default(),
            seen_update_notice: String::new(),
            seen_host_toast: 0,
            panel_identity: String::new(),
            settings_inputs: std::cell::RefCell::new(HashMap::new()),
            settings_open: None,
            form_identity: String::new(),
            form_revision: 0,
            autosave_task: None,
            draft_task: None,
            completion_task: None,
            pending_draft: false,
            search_selected: None,
            draft_revision: 0,
            completion_revision: 0,
            selection: 0,
            completion_index: 0,
            completion_hidden: None,
            completion_scroll: ScrollHandle::new(),
            light_override: None,
            last_width: px(0.),
            resize_task: None,
            resize_revision: 0,
            _subscriptions: subscriptions,
        }
    }
    fn sync_input_style(&self, cx: &mut Context<Self>) {
        let t = self.theme();
        let overlay = !self.snapshot.panel_title.is_empty() || self.snapshot.prompt.is_some();
        let completions = self.completion_active(cx) && !overlay;
        self.composer
            .update(cx, |input, _| input.menu = completions);
        for (input, background) in [
            (&self.composer, true),
            (&self.search, true),
            (&self.filter, false),
            (&self.form, false),
        ] {
            input.update(cx, |i, cx| {
                let tab_enabled = !background || !overlay;
                if i.foreground != t.text
                    || i.muted != t.muted
                    || i.accent != t.accent
                    || i.tab_enabled != tab_enabled
                {
                    i.foreground = t.text;
                    i.muted = t.muted;
                    i.accent = t.accent;
                    i.tab_enabled = tab_enabled;
                    cx.notify();
                }
            });
        }
        // While disconnected the composer is read-only (the draft is kept) and
        // the placeholder points at the Reconnect route.
        let disconnected = self.snapshot.disconnected;
        let placeholder = if disconnected {
            "Disconnected — reconnect (⌘R)".to_string()
        } else {
            "Ask anything, or / for commands…".to_string()
        };
        self.composer.update(cx, |input, cx| {
            if input.readonly != disconnected || input.placeholder != placeholder {
                input.readonly = disconnected;
                input.placeholder = placeholder;
                cx.notify();
            }
        });
    }
    fn sync_logs_visibility(&mut self, window: &Window) {
        let visible = (self.snapshot.details_sidebar && window.viewport_size().width > px(1150.)
            || self.compact_pane.as_deref() == Some("details_sidebar"))
            && self.snapshot.details_panel.tab == "Logs";
        if visible != self.logs_visible {
            self.logs_visible = visible;
            if !self.preview {
                send(
                    json!({"type":"details_visible","open":visible}),
                    self.snapshot.generation,
                );
            }
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
        if action["type"] == "session_open" || action["type"] == "command" {
            self.flush_draft(cx);
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
                self.sync_logs_visibility(w);
                cx.notify();
                return;
            }
        }
        if self.preview {
            self.push_notice(
                NoticeKind::Error,
                "Preview fixture · connect with nexus desktop to use host actions",
                NoticeAction::None,
                cx,
            );
        } else {
            send(action, self.snapshot.generation);
        }
    }
    fn push_notice(
        &mut self,
        kind: NoticeKind,
        text: impl Into<String>,
        action: NoticeAction,
        cx: &mut Context<Self>,
    ) {
        let id = self.notices.push(kind, text, action);
        if kind == NoticeKind::Info {
            let timer = cx
                .background_executor()
                .timer(std::time::Duration::from_secs(6));
            let entity = cx.entity().downgrade();
            cx.spawn(async move |_, cx| {
                timer.await;
                entity
                    .update(cx, |this, cx| {
                        if this.notices.dismiss(id) {
                            cx.notify();
                        }
                    })
                    .ok();
            })
            .detach();
        }
        cx.notify();
    }
    fn dismiss_notice(&mut self, id: u64, cx: &mut Context<Self>) {
        if self.notices.dismiss(id) {
            cx.notify();
        }
    }
    fn schedule_transcript_reset(&mut self, cx: &mut Context<Self>) {
        self.resize_revision = self.resize_revision.wrapping_add(1);
        let revision = self.resize_revision;
        let timer = cx
            .background_executor()
            .timer(std::time::Duration::from_millis(120));
        self.resize_task = Some(cx.spawn(async move |weak, cx| {
            timer.await;
            weak.update(cx, |this, cx| {
                if this.resize_revision == revision {
                    let position = this.transcript.logical_scroll_top();
                    this.transcript.reset(this.snapshot.blocks.len());
                    if !this.follow {
                        this.transcript.scroll_to(position);
                    }
                    cx.notify();
                    #[cfg(test)]
                    this.resize_reset_count
                        .set(this.resize_reset_count.get() + 1);
                    this.resize_task = None;
                }
            })
            .ok();
        }));
    }
    fn reveal_selected_session(&self) {
        let Some(selected) = self.search_selected.as_deref() else {
            return;
        };
        let row = self.session_rows.borrow().iter().position(|(index, _)| {
            self.snapshot
                .sessions
                .get(*index)
                .is_some_and(|session| session.id == selected)
        });
        if let Some(row) = row {
            self.session_list.scroll_to_reveal_item(row);
        }
    }
    fn submit(&mut self, w: &mut Window, cx: &mut Context<Self>) {
        self.submit_mode("steer", w, cx);
    }
    fn submit_mode(&mut self, mode: &str, w: &mut Window, cx: &mut Context<Self>) {
        if self.snapshot.disconnected
            || !self.snapshot.agent_page.is_empty()
            || self.snapshot.prompt.is_some()
            || !self.snapshot.panel_title.is_empty()
        {
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
        self.flush_draft(cx);
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
    fn schedule_draft_and_completion(&mut self, cx: &mut Context<Self>) {
        let text = self.composer.read(cx).content.clone();
        let cursor = self.composer.read(cx).cursor();
        let generation = self.snapshot.generation;
        self.draft_revision = self.draft_revision.wrapping_add(1);
        let draft_revision = self.draft_revision;
        self.pending_draft = true;
        let draft_timer = cx
            .background_executor()
            .timer(std::time::Duration::from_millis(250));
        let draft_text = text.clone();
        self.draft_task = Some(cx.spawn(async move |weak, cx| {
            draft_timer.await;
            weak.update(cx, |this, _| {
                if this.draft_revision == draft_revision {
                    if !this.preview && this.snapshot.generation == generation {
                        send(
                            json!({"type":"draft_changed", "text":draft_text}),
                            generation,
                        );
                    }
                    this.pending_draft = false;
                }
            })
            .ok();
        }));

        self.completion_task = None;
        let Some((query, prefix)) = completion_request(&text, cursor) else {
            self.completion_revision = self.completion_revision.wrapping_add(1);
            return;
        };
        self.completion_revision = self.completion_revision.wrapping_add(1);
        let completion_revision = self.completion_revision;
        let timer = cx
            .background_executor()
            .timer(std::time::Duration::from_millis(120));
        self.completion_task = Some(cx.spawn(async move |weak, cx| {
            timer.await;
            weak.update(cx, |this, _| {
                if this.completion_revision == completion_revision
                    && !this.preview
                    && this.snapshot.generation == generation
                {
                    send(
                        json!({"type":"complete", "text":query, "prefix":prefix}),
                        generation,
                    );
                }
            })
            .ok();
        }));
    }
    fn flush_draft(&mut self, cx: &mut Context<Self>) {
        self.draft_revision = self.draft_revision.wrapping_add(1);
        self.completion_revision = self.completion_revision.wrapping_add(1);
        self.draft_task = None;
        self.completion_task = None;
        let pending = std::mem::replace(&mut self.pending_draft, false);
        if pending && !self.preview {
            send(
                json!({"type":"draft_changed", "text":self.composer.read(cx).content}),
                self.snapshot.generation,
            );
        }
    }
    fn schedule_image_decode(&mut self, next: &Snapshot, cx: &mut Context<Self>) {
        let generation_changed = next.generation != self.snapshot.generation;
        let preview_changed = next.preview_image != self.snapshot.preview_image
            || next.preview_image_media != self.snapshot.preview_image_media;
        let inline_changed = next.inline_images.len() != self.snapshot.inline_images.len()
            || next
                .inline_images
                .iter()
                .zip(&self.snapshot.inline_images)
                .any(|(a, b)| a.id != b.id || a.media != b.media || a.data != b.data);
        if !generation_changed && !preview_changed && !inline_changed {
            return;
        }

        self.image_decode_revision = self.image_decode_revision.wrapping_add(1);
        self.image_decode_task = None;
        let revision = self.image_decode_revision;
        let generation = next.generation;
        let preview = (!next.preview_image.is_empty()).then(|| {
            (
                "__preview__".to_string(),
                next.preview_image_media.clone(),
                next.preview_image.clone(),
            )
        });
        let inline = next
            .inline_images
            .iter()
            .take(8)
            .map(|image| (image.id.clone(), image.media.clone(), image.data.clone()))
            .collect::<Vec<_>>();
        if preview_changed || generation_changed {
            self.image_preview = None;
        }
        if generation_changed {
            self.inline_images.clear();
        } else {
            self.inline_images.retain(|id, _| {
                next.inline_images.iter().take(8).any(|image| {
                    &image.id == id
                        && self.snapshot.inline_images.iter().any(|old| {
                            old.id == image.id && old.media == image.media && old.data == image.data
                        })
                })
            });
        }

        let requests = preview.into_iter().chain(inline).collect::<Vec<_>>();
        if requests.is_empty() {
            return;
        }
        self.image_decode_task = Some(cx.spawn(async move |this, cx| {
            let decoded = cx
                .background_spawn(async move {
                    const MAX_IMAGE_BYTES: usize = 4 * 1024 * 1024;
                    const MAX_BASE64_BYTES: usize = (MAX_IMAGE_BYTES * 4 / 3) + 4;
                    requests
                        .into_iter()
                        .filter_map(|(id, media, encoded)| {
                            if encoded.len() > MAX_BASE64_BYTES {
                                return None;
                            }
                            let bytes = base64::engine::general_purpose::STANDARD
                                .decode(encoded)
                                .ok()?;
                            (bytes.len() <= MAX_IMAGE_BYTES).then_some((id, media, bytes))
                        })
                        .collect::<Vec<_>>()
                })
                .await;
            let _ = this.update(cx, |state, cx| {
                if state.snapshot.generation != generation
                    || state.image_decode_revision != revision
                {
                    return;
                }
                for (id, media, bytes) in decoded {
                    let format = match media.as_str() {
                        "image/jpeg" => ImageFormat::Jpeg,
                        "image/gif" => ImageFormat::Gif,
                        "image/webp" => ImageFormat::Webp,
                        _ => ImageFormat::Png,
                    };
                    let image = std::sync::Arc::new(Image::from_bytes(format, bytes));
                    if id == "__preview__" {
                        state.image_preview = Some(image);
                    } else {
                        state.inline_images.insert(id, image);
                    }
                }
                cx.notify();
            });
        }));
    }
    fn apply(&mut self, mut next: Snapshot, window: &mut Window, cx: &mut Context<Self>) {
        let patch_start = next.blocks_from;
        let changed_session = next.composer_key != self.snapshot.composer_key;
        let changed_generation = next.generation != self.snapshot.generation;
        let changed_page = next.agent_page != self.snapshot.agent_page;
        if changed_session || changed_generation {
            self.flush_draft(cx);
        }
        let old_count = self.snapshot.blocks.len();
        let old_tail = self
            .snapshot
            .blocks
            .last()
            .map(|block| (block.id.clone(), block.text.len()));
        if let Err(error) = next.restore_blocks(&mut self.snapshot) {
            self.push_notice(NoticeKind::Error, error, NoticeAction::None, cx);
            return;
        }
        let new_tail = next
            .blocks
            .last()
            .map(|block| (block.id.clone(), block.text.len()));
        let transcript_changed = next.blocks.len() != old_count || new_tail != old_tail;
        self.schedule_image_decode(&next, cx);
        let measurement_changed = (old_count <= 64) != (next.blocks.len() <= 64);
        let position = self.transcript.logical_scroll_top();
        if measurement_changed {
            self.transcript = Self::make_transcript(cx, next.blocks.len() <= 64);
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
        if changed_session || changed_generation || changed_page {
            self.transcript.reset(next.blocks.len());
            self.history_index = None;
            self.history_draft.clear();
            self.follow = true;
        } else if measurement_changed {
            self.transcript.reset(next.blocks.len());
            if !self.follow {
                self.transcript.scroll_to(position);
            }
        } else {
            let start = if next.schema == 2 { patch_start } else { 0 };
            self.transcript
                .splice(start..old_count, next.blocks.len().saturating_sub(start));
        }
        if self.follow {
            self.follow_pending = false;
            self.transcript.scroll_to(ListOffset {
                item_ix: next.blocks.len(),
                offset_in_item: px(0.),
            });
        } else if transcript_changed {
            // Content arrived while the reader was scrolled up: offer a way back.
            self.follow_pending = true;
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
        if panel != self.panel_identity || next.generation != self.snapshot.generation {
            self.settings_inputs.borrow_mut().clear();
            self.settings_open = None;
            self.panel_identity = panel;
            self.filter
                .update(cx, |input, cx| input.set(String::new(), cx));
            self.selection = 0;
            if !next.panel_title.is_empty() {
                if next.items.is_empty() || (next.panel_format == "image" && next.items.len() == 1)
                {
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
        let host_toasts: Vec<_> = self
            .snapshot
            .toasts
            .iter()
            .filter(|toast| toast.id > self.seen_host_toast)
            .cloned()
            .collect();
        for toast in host_toasts {
            self.seen_host_toast = self.seen_host_toast.max(toast.id);
            let text = if toast.body.is_empty() {
                toast.title
            } else {
                format!("{}\n{}", toast.title, toast.body)
            };
            self.push_notice(
                match toast.level.as_str() {
                    "error" => NoticeKind::Error,
                    "warning" | "warn" => NoticeKind::Warning,
                    _ => NoticeKind::Info,
                },
                text,
                if toast.action.as_ref().is_some_and(|a| a.operation.is_some()) {
                    NoticeAction::Host(toast.id)
                } else {
                    NoticeAction::None
                },
                cx,
            );
        }
        self.minimap_ticks = minimap::ticks(&self.snapshot.blocks);
        if self.snapshot.update_notice != self.seen_update_notice {
            self.seen_update_notice = self.snapshot.update_notice.clone();
            if !self.snapshot.update_notice.is_empty() {
                self.push_notice(
                    NoticeKind::Info,
                    self.snapshot.update_notice.clone(),
                    NoticeAction::UpdateHelp,
                    cx,
                );
            }
        }
        self.prepare_markdown_cache();
        self.sync_input_style(cx);
        self.sync_logs_visibility(window);
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
        if self.nav.is_some() {
            // Transcript navigation (entered with Tab from an empty draft):
            // move, open, and any other key returns to the composer.
            let handled = match event.keystroke.key.as_str() {
                "up" | "k" => {
                    self.move_nav(-1);
                    true
                }
                "down" | "j" => {
                    self.move_nav(1);
                    true
                }
                "tab" if event.keystroke.modifiers.shift => {
                    self.move_nav(-1);
                    true
                }
                "enter" | "space" => {
                    self.open_nav(w, cx);
                    true
                }
                _ => false,
            };
            if !handled {
                self.nav = None;
                window_focus(&self.composer, w, cx);
            }
            cx.notify();
            cx.stop_propagation();
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
        if self.nav.take().is_some() {
            window_focus(&self.composer, w, cx);
            cx.notify();
            return;
        }
        if self.search.read(cx).focus.is_focused(w) && !self.search.read(cx).content.is_empty() {
            self.search
                .update(cx, |input, cx| input.set(String::new(), cx));
            self.search_selected = None;
            cx.notify();
            return;
        }
        if self.composer.read(cx).focus.is_focused(w) && self.completion_active(cx) {
            self.completion_hidden = Some(self.snapshot.completion_prefix.clone());
            self.composer.update(cx, |input, _| input.menu = false);
            cx.notify();
        } else if self.compact_pane.take().is_some() {
            self.sync_logs_visibility(w);
            window_focus(&self.composer, w, cx);
            cx.notify();
        } else if self.settings_open.take().is_some() {
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
            // Two escapes within 1.5 s stop the active turn, matching the
            // terminal's composer-level double-escape (Ratatui parity).
            let now = std::time::Instant::now();
            if self
                .last_escape
                .is_some_and(|at| now.duration_since(at) < std::time::Duration::from_millis(1500))
            {
                self.last_escape = None;
                self.dispatch(json!({"type":"cancel"}), w, cx);
            } else {
                self.last_escape = Some(now);
                window_focus(&self.composer, w, cx);
            }
        }
    }
    fn shortcuts(&mut self, _: &Shortcuts, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/hotkeys", w, cx);
    }
    fn context_popover(&mut self, _: &ContextPopover, w: &mut Window, cx: &mut Context<Self>) {
        self.dispatch(json!({"type":"context_popover"}), w, cx);
    }
    fn update_help(&mut self, _: &UpdateHelp, w: &mut Window, cx: &mut Context<Self>) {
        self.dispatch(json!({"type":"update_help"}), w, cx);
    }
    fn transcript_page_up(&mut self, _: &TranscriptPageUp, _: &mut Window, cx: &mut Context<Self>) {
        self.follow = false;
        self.transcript.scroll_by(px(-320.));
        cx.notify();
    }
    fn transcript_page_down(
        &mut self,
        _: &TranscriptPageDown,
        _: &mut Window,
        cx: &mut Context<Self>,
    ) {
        self.transcript.scroll_by(px(320.));
        cx.notify();
    }
    fn details_previous(&mut self, _: &DetailsPrevious, w: &mut Window, cx: &mut Context<Self>) {
        self.cycle_details_tab(-1, w, cx);
    }
    fn details_next(&mut self, _: &DetailsNext, w: &mut Window, cx: &mut Context<Self>) {
        self.cycle_details_tab(1, w, cx);
    }
    fn cycle_details_tab(&mut self, delta: i32, w: &mut Window, cx: &mut Context<Self>) {
        let visible = if w.viewport_size().width <= px(1150.) {
            self.compact_pane.as_deref() == Some("details_sidebar")
        } else {
            self.snapshot.details_sidebar
        };
        if !visible {
            return;
        }
        let next = next_details_tab(&self.snapshot.details_panel.tab, delta);
        self.dispatch(json!({"type":"details_tab","text":next}), w, cx);
    }
    fn focus_next(&mut self, _: &FocusNext, w: &mut Window, _: &mut Context<Self>) {
        w.focus_next();
    }
    fn nav_targets(&self) -> Vec<usize> {
        self.snapshot
            .blocks
            .iter()
            .enumerate()
            .filter(|(_, block)| block.operation.is_some())
            .map(|(index, _)| index)
            .collect()
    }
    fn move_nav(&mut self, delta: i32) {
        let targets = self.nav_targets();
        if targets.is_empty() {
            self.nav = None;
            return;
        }
        let current = self
            .nav
            .and_then(|index| targets.iter().position(|&t| t == index));
        // Transcript navigation clamps at the ends (unlike the wrapping pickers).
        let next = match current {
            Some(index) => (index as i32 + delta).clamp(0, targets.len() as i32 - 1) as usize,
            None if delta < 0 => targets.len() - 1,
            None => 0,
        };
        self.nav = Some(targets[next]);
        self.follow = false;
        self.transcript.scroll_to_reveal_item(targets[next]);
    }
    fn open_nav(&mut self, w: &mut Window, cx: &mut Context<Self>) {
        if let Some(operation) = self
            .nav
            .and_then(|index| self.snapshot.blocks.get(index))
            .and_then(|block| block.operation.clone())
        {
            self.dispatch(json!({"type":"operation","operation":operation}), w, cx);
        }
    }
    fn cancel(&mut self, _: &Cancel, w: &mut Window, cx: &mut Context<Self>) {
        if self.snapshot.prompt.is_some()
            || !self.snapshot.panel_title.is_empty()
            || !self.snapshot.agent_page.is_empty()
            || self.compact_pane.is_some()
            || self.nav.is_some()
        {
            self.dismiss(&Dismiss, w, cx);
            return;
        }
        if !self.composer.read(cx).content.is_empty() && !self.composer.read(cx).readonly {
            self.composer.update(cx, |input, cx| input.clear(w, cx));
            self.flush_draft(cx);
            self.completion_hidden = Some(self.snapshot.completion_prefix.clone());
            self.composer.update(cx, |input, _| input.menu = false);
            self.last_escape = None;
            window_focus(&self.composer, w, cx);
            cx.notify();
            return;
        }
        self.stop_turn(&StopTurn, w, cx);
    }
    fn stop_turn(&mut self, _: &StopTurn, w: &mut Window, cx: &mut Context<Self>) {
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
            self.push_notice(
                NoticeKind::Warning,
                "Attachments require a live host connection",
                NoticeAction::None,
                cx,
            );
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
            self.sync_input_style(cx);
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
    fn fork_session(&mut self, _: &ForkSession, w: &mut Window, cx: &mut Context<Self>) {
        self.command("/fork", w, cx);
    }
    fn latest(&mut self, _: &JumpLatest, _: &mut Window, cx: &mut Context<Self>) {
        self.follow = true;
        self.follow_pending = false;
        self.transcript.scroll_to(ListOffset {
            item_ix: self.snapshot.blocks.len(),
            offset_in_item: px(0.),
        });
        cx.notify();
    }
    fn focus_composer(&mut self, _: &FocusComposer, w: &mut Window, cx: &mut Context<Self>) {
        if !self.snapshot.panel_title.is_empty() || self.snapshot.prompt.is_some() {
            return;
        }
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
        self.flush_draft(cx);
        self.composer
            .update(cx, |input, cx| input.set(text.clone(), cx));
        self.sync_input_style(cx);
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
        self.flush_draft(cx);
        self.composer
            .update(cx, |input, cx| input.set(next.clone(), cx));
        self.completion_hidden = Some(self.snapshot.completion_prefix.clone());
        self.sync_input_style(cx);
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
        if !self.composer.read(cx).focus.is_focused(w) {
            return;
        }
        // A visible completion inserts; otherwise a non-empty draft forces a
        // completion request, and an empty draft enters transcript navigation.
        if self.insert_completion(cx) {
            return;
        }
        let input = self.composer.read(cx);
        let text = input.content.clone();
        let cursor = input.cursor();
        if !text.is_empty() {
            if let Some((query, prefix)) = completion_request(&text, cursor) {
                self.completion_revision = self.completion_revision.wrapping_add(1);
                self.completion_task = None;
                if !self.preview {
                    send(
                        json!({"type":"complete", "text":query, "prefix":prefix}),
                        self.snapshot.generation,
                    );
                }
            }
            return;
        }
        if self.snapshot.panel_title.is_empty() && self.snapshot.prompt.is_none() {
            if let Some(index) = self.nav_targets().last().copied() {
                self.nav = Some(index);
                self.follow = false;
                self.transcript.scroll_to_reveal_item(index);
                w.focus(&self.focus);
                cx.notify();
            }
        }
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

fn completion_request(text: &str, cursor: usize) -> Option<(String, String)> {
    if cursor > text.len() || !text.is_char_boundary(cursor) {
        return None;
    }
    let prefix = text[..cursor].to_owned();
    let query = prefix.rsplit(char::is_whitespace).next()?;
    Some((query.to_owned(), prefix))
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
/// Unified title-bar height. Overlay drawers (sessions/details) start below it
/// rather than at a guessed offset, so a collapsed pane never covers the bar.
pub const TOP_BAR_HEIGHT: f32 = 40.;

/// Wrapping movement for a filtered list, used by the sidebar search and
/// pickers. `len` must be greater than zero.
fn next_selection(current: Option<usize>, len: usize, direction: i32) -> usize {
    debug_assert!(len > 0);
    match current {
        Some(index) => (index as i32 + direction).rem_euclid(len as i32) as usize,
        None if direction < 0 => len - 1,
        None => 0,
    }
}

/// Indices of the sessions matching the sidebar search, in list order. The
/// order matches the visible list, so the arrow-key selection stays stable while
/// the query is unchanged.
fn search_matches(sessions: &[bridge::Session], query: &str) -> Vec<usize> {
    let query = query.to_lowercase();
    sessions
        .iter()
        .enumerate()
        .filter(|(_, session)| {
            format!("{} {} {}", session.title, session.id, session.workspace)
                .to_lowercase()
                .contains(&query)
        })
        .map(|(index, _)| index)
        .collect()
}

/// Drawer state after a resize. A pane that is now docked (wide enough) must
/// lose its drawer flag; otherwise a later narrow resize would silently reopen
/// an old drawer. Returns the drawer to keep, if any.
fn compact_pane_after_resize<'a>(
    pane: Option<&'a str>,
    sidebar: bool,
    details: bool,
) -> Option<&'a str> {
    match pane {
        Some("sessions_sidebar") if sidebar => None,
        Some("details_sidebar") if details => None,
        other => other,
    }
}

/// Inspector tab order shared by the `[` / `]` navigation keys.
const DETAILS_TABS: [&str; 4] = ["Session", "Files", "MCP", "Logs"];

fn next_details_tab(current: &str, delta: i32) -> &'static str {
    let index = DETAILS_TABS
        .iter()
        .position(|tab| *tab == current)
        .unwrap_or(0) as i32;
    let next = (index + delta).rem_euclid(DETAILS_TABS.len() as i32) as usize;
    DETAILS_TABS[next]
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
        let width = window.viewport_size().width;
        if width != self.last_width {
            self.last_width = width;
            // Re-measure once the drag settles; during the drag the list reuses
            // the previous heights instead of resetting every frame (§3.6).
            self.schedule_transcript_reset(cx);
        }
        let sidebar = self.snapshot.sessions_sidebar && width > px(820.);
        let details = self.snapshot.details_sidebar && width > px(1150.);
        // Widening past the collapse threshold re-docks the pane; drop the stale
        // drawer flag so it does not reappear on the next narrow resize (§1.2.10).
        let compact = compact_pane_after_resize(self.compact_pane.as_deref(), sidebar, details);
        if compact != self.compact_pane.as_deref() {
            self.compact_pane = compact.map(str::to_owned);
            self.sync_logs_visibility(window);
        }
        // GPUI's inferred list sizing measures at min-content width, which
        // overestimates wrapped prose and leaves a gap above short transcripts.
        // Measure only the bounded small list at its actual conversation width.
        let short_height = if self.snapshot.blocks.len() <= 64 {
            let rail = if width > px(900.) && !self.minimap_ticks.is_empty() {
                12.
            } else {
                0.
            };
            let available_width = width
                - px(if sidebar { 252. } else { 0. })
                - px(if details { 260. } else { 0. })
                - px(rail);
            self.snapshot
                .blocks
                .iter()
                .map(|block| {
                    let mut element = div()
                        .font_family(".AppleSystemUIFont")
                        .text_size(px(theme::size::UI))
                        .child(self.render_block(block, window, cx))
                        .into_any_element();
                    element
                        .layout_as_root(
                            size(
                                AvailableSpace::Definite(available_width),
                                AvailableSpace::MaxContent,
                            ),
                            window,
                            cx,
                        )
                        .height
                })
                .fold(px(0.), |height, next| height + next)
        } else {
            px(0.)
        };
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
                    let selected = this.nav == Some(ix);
                    let theme = this.theme();
                    this.snapshot
                        .blocks
                        .get(ix)
                        .map(|block| {
                            let element = this.render_block(block, window, cx);
                            div()
                                .w_full()
                                .when(selected, |d| {
                                    d.rounded(px(crate::theme::radius::CONTROL))
                                        .border_1()
                                        .border_color(theme.accent)
                                        .bg(theme.accent_bg.opacity(0.6))
                                })
                                .child(element)
                                .into_any_element()
                        })
                        .unwrap_or_else(|| div().into_any_element())
                })
                .unwrap_or_else(|_| div().into_any_element())
            })
            .when(self.snapshot.blocks.len() <= 64, |d| {
                d.w_full().h(short_height).max_h(relative(1.))
            })
            .when(self.snapshot.blocks.len() > 64, |d| d.size_full())
            .into_any_element()
        };
        let mut root = div()
            .id("desktop")
            .key_context("Nexus")
            .when(self.snapshot.panel_title == "Select model", |this| {
                this.key_context("ModelPanel")
            })
            .track_focus(&self.focus)
            .size_full()
            .flex()
            .flex_col()
            .relative()
            .overflow_hidden()
            .font_family(".AppleSystemUIFont")
            .text_size(px(crate::theme::size::UI))
            .text_color(t.text)
            .bg(t.background)
            .on_action(cx.listener(Self::history_previous))
            .on_action(cx.listener(Self::history_next))
            .on_action(cx.listener(Self::complete_first))
            .on_action(cx.listener(Self::focus_next))
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
            .on_action(cx.listener(Self::stop_turn))
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
            .on_action(cx.listener(Self::fork_session))
            .on_action(cx.listener(Self::latest))
            .on_action(cx.listener(Self::save_form))
            .on_action(cx.listener(Self::focus_composer))
            .on_action(cx.listener(Self::shortcuts))
            .on_action(cx.listener(Self::context_popover))
            .on_action(cx.listener(Self::update_help))
            .on_action(cx.listener(Self::transcript_page_up))
            .on_action(cx.listener(Self::transcript_page_down))
            .on_action(cx.listener(Self::details_previous))
            .on_action(cx.listener(Self::details_next))
            .on_key_down(cx.listener(Self::prompt_key))
            .on_drop(cx.listener(|this, paths: &ExternalPaths, w, cx| {
                for path in paths.paths().iter().take(8) {
                    this.command(&attachment_command(path), w, cx);
                }
            }))
            .child(self.topbar(cx))
            .when(self.snapshot.disconnected, |d| {
                d.child(self.disconnected_banner(cx))
            })
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
                            .child(
                                div()
                                    .flex()
                                    .flex_1()
                                    .min_h_0()
                                    .relative()
                                    .overflow_hidden()
                                    .when(width > px(900.) && !self.minimap_ticks.is_empty(), |d| {
                                        d.child(self.minimap(cx))
                                    })
                                    .child(
                                        div()
                                            .flex()
                                            .flex_col()
                                            .flex_1()
                                            .min_w_0()
                                            .min_h_0()
                                            .child(content),
                                    )
                                    .child(self.toasts(cx)),
                            )
                            .child(
                                div()
                                    .w_full()
                                    .max_w(px(theme::size::READING + theme::space::XL * 2.))
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
                        .top(px(TOP_BAR_HEIGHT))
                        .bottom_0()
                        .left_0()
                        .right_0()
                        .bg(gpui::rgba(0x000000a0))
                        .on_mouse_down(
                            MouseButton::Left,
                            cx.listener(|this, _, w, cx| {
                                this.compact_pane = None;
                                this.sync_logs_visibility(w);
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
        root = root.child(self.follow_pill(cx));
        if self.trace.borrow().enabled() {
            trace::TracedElement::new(root, self.trace.clone()).into_any_element()
        } else {
            root.into_any_element()
        }
    }
}
fn bind_desktop_keys(cx: &mut App) {
    cx.bind_keys(crate::keymap::key_bindings());
    input::bindings(cx);
}

fn main() {
    let args: Vec<_> = std::env::args().collect();
    let preview_path = args
        .iter()
        .position(|arg| arg == "--preview")
        .and_then(|i| args.get(i + 1))
        .cloned();
    let (tx, rx) = async_channel::bounded::<Result<(Snapshot, usize), String>>(32);
    if let Some(path) = &preview_path {
        let mut decoder = Decoder::default();
        let result = std::fs::metadata(path)
            .map_err(|e| e.to_string())
            .and_then(|m| {
                if m.len() > 64 * 1024 * 1024 {
                    Err("Preview exceeds the 16 MiB limit".into())
                } else {
                    std::fs::read_to_string(path).map_err(|e| e.to_string())
                }
            })
            .and_then(|s| {
                decoder
                    .decode(s.as_bytes())
                    .map(|snapshot| (snapshot, s.len()))
                    .map_err(|e| format!("Invalid snapshot: {e}"))
            });
        tx.send_blocking(result).ok();
    } else {
        std::thread::spawn(move || {
            let mut input = io::stdin().lock();
            let mut decoder = Decoder::default();
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
                    Ok(_) if line.len() <= 64 * 1024 * 1024 => {
                        if tx
                            .send_blocking(
                                decoder
                                    .decode(&line)
                                    .map(|snapshot| (snapshot, line.len()))
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
            theme::init_code_font(&cx.text_system().all_font_names());
            bind_desktop_keys(cx);
            if std::env::var("NEXUS_DESKTOP_REVIEW").as_deref() == Ok("1") {
                cx.bind_keys([
                    KeyBinding::new("cmd-shift-y", ReviewCompact, Some("Nexus")),
                    KeyBinding::new("cmd-shift-o", ReviewWide, Some("Nexus")),
                ]);
            }
            cx.on_action(|_: &Quit, cx| {
                for handle in cx.windows() {
                    let _ = cx.update_window(handle, |root, _, cx| {
                        if let Ok(desktop) = root.downcast::<Desktop>() {
                            let _ = desktop.update(cx, |this, cx| this.flush_draft(cx));
                        }
                    });
                }
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
                    let close_weak = view.downgrade();
                    window.on_window_should_close(cx, move |_, cx| {
                        close_weak.update(cx, |this, cx| this.flush_draft(cx)).ok();
                        true
                    });
                    let weak = view.downgrade();
                    let mut async_window = window.to_async(cx);
                    cx.spawn(async move |_| {
                        while let Ok(result) = rx.recv().await {
                            if weak
                                .update_in(&mut async_window, |this, window, cx| match result {
                                    Ok((snapshot, bytes)) => {
                                        let at = std::time::Instant::now();
                                        this.trace
                                            .borrow_mut()
                                            .sample("snapshot_bytes", bytes as f64);
                                        this.apply(snapshot, window, cx);
                                        this.trace
                                            .borrow_mut()
                                            .record("snapshot_apply", at.elapsed());
                                    }
                                    Err(error) => {
                                        this.push_notice(
                                            NoticeKind::Error,
                                            error,
                                            NoticeAction::Reconnect,
                                            cx,
                                        );
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
    #[gpui::test]
    fn stale_image_decode_cannot_restore_images_from_an_older_generation(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                let old = Snapshot {
                    generation: 1,
                    preview_image: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jQZkAAAAASUVORK5CYII=".into(),
                    preview_image_media: "image/png".into(),
                    inline_images: vec![bridge::InlineImage {
                        id: "old-message".into(),
                        media: "image/png".into(),
                        data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jQZkAAAAASUVORK5CYII=".into(),
                        data_ref: String::new(),
                        ..Default::default()
                    }],
                    ..Default::default()
                };
                this.apply(old, w, cx);
                this.apply(Snapshot { generation: 2, ..Default::default() }, w, cx);
            })
            .unwrap();
        cx.executor().run_until_parked();
        window
            .update(cx, |this, _, _| {
                assert!(this.image_preview.is_none());
                assert!(this.inline_images.is_empty());
            })
            .unwrap();
    }
    #[gpui::test]
    fn background_image_decode_replaces_same_id_and_rejects_invalid_payload(
        cx: &mut TestAppContext,
    ) {
        let window = cx.add_window(|window, cx| Desktop::new(window, cx, true));
        let image = bridge::InlineImage {
            id: "message-image".into(),
            media: "image/png".into(),
            data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jQZkAAAAASUVORK5CYII=".into(),
            ..Default::default()
        };
        window
            .update(cx, |this, w, cx| {
                this.apply(
                    Snapshot {
                        inline_images: vec![image.clone()],
                        ..Default::default()
                    },
                    w,
                    cx,
                )
            })
            .unwrap();
        cx.executor().run_until_parked();
        window
            .update(cx, |this, w, cx| {
                assert!(this.inline_images.contains_key(&image.id));
                this.apply(
                    Snapshot {
                        inline_images: vec![bridge::InlineImage {
                            data: "invalid base64".into(),
                            ..image.clone()
                        }],
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                assert!(!this.inline_images.contains_key(&image.id));
            })
            .unwrap();
        cx.executor().run_until_parked();
        window
            .update(cx, |this, _, _| assert!(this.inline_images.is_empty()))
            .unwrap();
    }
    #[test]
    fn completion_request_uses_the_text_before_the_cursor() {
        assert_eq!(
            completion_request("read @src after", 9),
            Some(("@src".into(), "read @src".into()))
        );
        assert_eq!(
            completion_request("hello world", 5),
            Some(("hello".into(), "hello".into()))
        );
        assert_eq!(completion_request("é", 1), None);
    }
    #[gpui::test]
    fn flushing_composer_cancels_pending_debounce_tasks(cx: &mut TestAppContext) {
        let handle = cx.add_window(|window, cx| Desktop::new(window, cx, true));
        handle
            .update(cx, |this, _, cx| {
                this.composer
                    .update(cx, |input, cx| input.set("draft".into(), cx));
                this.schedule_draft_and_completion(cx);
                assert!(this.pending_draft);
                this.flush_draft(cx);
                assert!(!this.pending_draft);
                assert!(this.draft_task.is_none());
                assert!(this.completion_task.is_none());
            })
            .unwrap();
    }
    #[gpui::test]
    fn composer_debounce_waits_for_quiet_and_session_switch_cancels_it(cx: &mut TestAppContext) {
        let window = cx.add_window(|window, cx| Desktop::new(window, cx, true));
        window
            .update(cx, |this, w, cx| {
                this.composer
                    .update(cx, |input, cx| input.insert("first", w, cx));
            })
            .unwrap();
        cx.executor().run_until_parked();
        cx.executor()
            .advance_clock(std::time::Duration::from_millis(240));
        window
            .update(cx, |this, w, cx| {
                assert!(this.pending_draft);
                this.composer
                    .update(cx, |input, cx| input.insert(" second", w, cx));
            })
            .unwrap();
        cx.executor().run_until_parked();
        cx.executor()
            .advance_clock(std::time::Duration::from_millis(20));
        window
            .update(cx, |this, _, _| assert!(this.pending_draft))
            .unwrap();
        cx.executor()
            .advance_clock(std::time::Duration::from_millis(240));
        window
            .update(cx, |this, w, cx| {
                assert!(!this.pending_draft);
                this.composer
                    .update(cx, |input, cx| input.insert(" third", w, cx));
            })
            .unwrap();
        cx.executor().run_until_parked();
        window
            .update(cx, |this, w, cx| {
                assert!(this.pending_draft);
                this.apply(
                    Snapshot {
                        composer_key: "next-session".into(),
                        generation: 2,
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                assert!(!this.pending_draft);
                assert!(this.draft_task.is_none());
                assert!(this.completion_task.is_none());
            })
            .unwrap();
    }
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
    fn thousand_sessions_only_render_visible_rows_and_search_reaches_the_last(
        cx: &mut TestAppContext,
    ) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, _, cx| {
                this.snapshot.sessions_sidebar = true;
                this.snapshot.sessions = (0..1000)
                    .map(|i| bridge::Session {
                        id: format!("session-{i}"),
                        title: format!("Conversation {i}"),
                        group: format!("Workspace {}", i / 100),
                        workspace: "/tmp/review".into(),
                        state: "idle".into(),
                        status: "Ready".into(),
                        sub: "1 · now".into(),
                        active: false,
                    })
                    .collect();
                cx.notify();
            })
            .unwrap();
        cx.update(|cx| {
            cx.update_window(window.into(), |_, w, cx| {
                let _ = w.draw(cx);
            })
            .unwrap()
        });
        window
            .update(cx, |this, _, cx| {
                assert_eq!(this.session_rows.borrow().len(), 1000);
                assert!(
                    this.session_render_count.get() < 100,
                    "offscreen rows must not create controls"
                );
                assert!(this.session_render_count.get() > 0);
                eprintln!(
                    "1,000-session initial frame created {} rows",
                    this.session_render_count.get()
                );
                this.search
                    .update(cx, |input, cx| input.set("Conversation 999".into(), cx));
                cx.notify();
            })
            .unwrap();
        cx.update(|cx| {
            cx.update_window(window.into(), |_, w, cx| {
                let _ = w.draw(cx);
            })
            .unwrap()
        });
        window
            .update(cx, |this, _, _| {
                assert_eq!(*this.session_rows.borrow(), vec![(999, true)]);
            })
            .unwrap();
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
                this.schedule_draft_and_completion(cx);
                assert!(this.pending_draft);
                assert!(this.insert_completion(cx));
                assert!(!this.pending_draft);
                assert!(this.draft_task.is_none());
                assert!(this.completion_task.is_none());
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
                assert!(this.notices.is_empty());
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
            .update(cx, |this, _, _| assert!(this.notices.is_empty()))
            .unwrap();
        cx.executor()
            .advance_clock(std::time::Duration::from_millis(101));
        cx.executor().run_until_parked();
        window
            .update(cx, |this, w, cx| {
                assert!(this.notices.contains_text("Preview fixture"));
                this.notices.clear();
                this.schedule_autosave(w, cx);
                this.apply(Snapshot::default(), w, cx);
            })
            .unwrap();
        cx.executor().run_until_parked();
        cx.executor()
            .advance_clock(std::time::Duration::from_secs(1));
        cx.executor().run_until_parked();
        window
            .update(cx, |this, _, _| assert!(this.notices.is_empty()))
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
                assert!(this.notices.contains_text("Preview fixture"));
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
                assert!(this.notices.is_empty()); // Presentation-only toggle doesn't invoke fixture host actions.
                this.dismiss(&Dismiss, w, cx);
                assert!(this.compact_pane.is_none());
                assert!(this.composer.read(cx).focus.is_focused(w));
            })
            .unwrap();
    }
    #[gpui::test]
    fn tab_is_not_routed_to_attach_and_enter_uses_input_submission(cx: &mut TestAppContext) {
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
                assert!(this.composer.read(cx).focus.is_focused(w));
                assert!(this.notices.is_empty());
            })
            .unwrap();
        cx.simulate_keystrokes(handle.into(), "enter");
        cx.run_until_parked();
        handle
            .update(cx, |this, _, _| {
                assert!(
                    this.notices.is_empty(),
                    "Tab or Enter invoked attach: {}",
                    this.notices.len()
                )
            })
            .unwrap();
    }
    #[gpui::test]
    fn control_c_copies_clears_with_undo_and_only_then_cancels(cx: &mut TestAppContext) {
        cx.update(bind_desktop_keys);
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, cx| {
                this.composer
                    .update(cx, |input, cx| input.set("Hello 世界 🦀".into(), cx));
                window_focus(&this.composer, w, cx);
            })
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        cx.simulate_keystrokes(handle.into(), "cmd-a ctrl-c");
        cx.run_until_parked();
        handle
            .update(cx, |this, _, cx| {
                assert_eq!(
                    cx.read_from_clipboard().unwrap().text().as_deref(),
                    Some("Hello 世界 🦀")
                );
                assert_eq!(this.composer.read(cx).content, "Hello 世界 🦀");
                assert!(this.notices.is_empty());
            })
            .unwrap();
        cx.simulate_keystrokes(handle.into(), "right ctrl-c");
        cx.run_until_parked();
        handle
            .update(cx, |this, _, cx| {
                assert!(this.composer.read(cx).content.is_empty());
                assert!(this.notices.is_empty(), "clearing must not cancel the turn");
            })
            .unwrap();
        cx.simulate_keystrokes(handle.into(), "ctrl-z");
        cx.run_until_parked();
        handle
            .update(cx, |this, _, cx| {
                assert_eq!(this.composer.read(cx).content, "Hello 世界 🦀");
            })
            .unwrap();
        cx.simulate_keystrokes(handle.into(), "ctrl-c ctrl-c");
        cx.run_until_parked();
        handle
            .update(cx, |this, _, cx| {
                assert!(this.composer.read(cx).content.is_empty());
                assert!(this.notices.contains_text("Preview fixture"));
            })
            .unwrap();
    }
    #[gpui::test]
    fn command_enter_restores_composer_focus_and_command_theme_changes_palette(
        cx: &mut TestAppContext,
    ) {
        cx.update(bind_desktop_keys);
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, _| w.focus(&this.focus))
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        cx.simulate_keystrokes(handle.into(), "cmd-enter cmd-shift-t");
        cx.run_until_parked();
        handle
            .update(cx, |this, w, cx| {
                assert!(this.composer.read(cx).focus.is_focused(w));
                assert_eq!(this.light_override, Some(true));
                assert!(this.notices.is_empty());
            })
            .unwrap();
    }
    #[gpui::test]
    fn command_period_stops_without_clearing_the_draft(cx: &mut TestAppContext) {
        cx.update(bind_desktop_keys);
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, _, cx| {
                this.composer
                    .update(cx, |input, cx| input.set("Unsent draft".into(), cx));
            })
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        cx.simulate_keystrokes(handle.into(), "cmd-.");
        cx.run_until_parked();
        handle
            .update(cx, |this, _, cx| {
                assert_eq!(this.composer.read(cx).content, "Unsent draft");
                assert!(this.notices.contains_text("Preview fixture"));
            })
            .unwrap();
    }
    #[gpui::test]
    fn control_c_dismisses_drawer_and_retains_draft(cx: &mut TestAppContext) {
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, cx| {
                this.composer
                    .update(cx, |input, cx| input.set("Keep draft".into(), cx));
                this.compact_pane = Some("details_sidebar".into());
                this.cancel(&Cancel, w, cx);
                assert!(this.compact_pane.is_none());
                assert_eq!(this.composer.read(cx).content, "Keep draft");
                assert!(this.notices.is_empty());
                assert!(this.composer.read(cx).focus.is_focused(w));
            })
            .unwrap();
    }
    #[gpui::test]
    fn inspector_tab_shortcuts_work_in_narrow_drawer(cx: &mut TestAppContext) {
        let handle = cx.update(|cx| {
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
        handle
            .update(cx, |this, w, cx| {
                assert!(w.viewport_size().width <= px(1150.));
                this.snapshot.details_sidebar = false;
                this.cycle_details_tab(1, w, cx);
                assert!(
                    this.notices.is_empty(),
                    "closed inspector must ignore tab keys"
                );
                this.compact_pane = Some("details_sidebar".into());
                this.cycle_details_tab(1, w, cx);
                assert!(this.notices.contains_text("Preview fixture"));
            })
            .unwrap();
    }
    #[gpui::test]
    fn double_escape_stops_the_turn_but_a_single_escape_does_not(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                this.dismiss(&Dismiss, w, cx);
                assert!(this.last_escape.is_some());
                assert!(this.notices.is_empty(), "one escape must not cancel");
                this.dismiss(&Dismiss, w, cx);
                assert!(this.last_escape.is_none());
                // Preview dispatch surfaces host actions locally instead of
                // sending them, so the cancel is observable here.
                assert!(this.notices.contains_text("Preview fixture"));
            })
            .unwrap();
    }
    #[gpui::test]
    fn host_errors_render_once_with_complete_body_and_offered_action(cx: &mut TestAppContext) {
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, cx| {
                let snapshot = || Snapshot {
                    toasts: vec![bridge::ToastWire {
                        id: 42,
                        level: "error".into(),
                        title: "Request failed".into(),
                        body: "The daemon rejected this request".into(),
                        action: Some(bridge::ToastAction {
                            label: "Retry".into(),
                            operation: Some(json!({"kind":"retry"})),
                        }),
                        ..Default::default()
                    }],
                    ..Default::default()
                };
                this.apply(snapshot(), w, cx);
                assert_eq!(this.notices.len(), 1);
                let notice = this.notices.iter().next().unwrap();
                assert_eq!(notice.kind, NoticeKind::Error);
                assert_eq!(
                    notice.text,
                    "Request failed\nThe daemon rejected this request"
                );
                assert_eq!(notice.action, NoticeAction::Host(42));
                this.apply(snapshot(), w, cx);
                assert_eq!(this.notices.len(), 1);
                this.notices.clear();
                this.apply(snapshot(), w, cx);
                assert!(
                    this.notices.is_empty(),
                    "polls must not resurrect a dismissed host notice"
                );
            })
            .unwrap();
    }
    #[gpui::test]
    fn preview_refusals_collect_as_bounded_deduplicated_notices(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                for _ in 0..5 {
                    this.dispatch(json!({"type":"command","text":"/model"}), w, cx);
                }
                assert_eq!(this.notices.len(), 1);
                assert!(this.notices.contains_text("Preview fixture"));
                this.notices.clear();
                assert!(this.notices.is_empty());
                // Building the toast element must not panic with no notices.
                let _ = this.toasts(cx);
            })
            .unwrap();
    }
    #[test]
    fn sidebar_search_matches_title_id_and_workspace_case_insensitively() {
        let sessions = vec![
            bridge::Session {
                id: "s-1".into(),
                title: "Fix parser".into(),
                workspace: "/work/nexus".into(),
                ..Default::default()
            },
            bridge::Session {
                id: "abc".into(),
                title: "Docs".into(),
                workspace: "/work/site".into(),
                ..Default::default()
            },
        ];
        assert_eq!(search_matches(&sessions, "parser"), vec![0]);
        assert_eq!(search_matches(&sessions, "abc"), vec![1]);
        assert_eq!(search_matches(&sessions, "NEXUS"), vec![0]);
        assert_eq!(search_matches(&sessions, "site"), vec![1]);
        assert_eq!(search_matches(&sessions, ""), vec![0, 1]);
        assert!(search_matches(&sessions, "missing").is_empty());
    }
    #[gpui::test]
    fn scrolling_within_a_long_final_reply_survives_updates_and_resize(cx: &mut TestAppContext) {
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, cx| {
                w.resize(size(px(1000.), px(700.)));
                this.apply(
                    Snapshot {
                        schema: 2,
                        generation: 1,
                        composer_key: "history".into(),
                        blocks: vec![
                            Content {
                                id: "user".into(),
                                kind: "user".into(),
                                text: "First request".into(),
                                ..Default::default()
                            },
                            Content {
                                id: "early".into(),
                                kind: "markdown".into(),
                                text: "Earlier reply".into(),
                                ..Default::default()
                            },
                            Content {
                                id: "last".into(),
                                kind: "markdown".into(),
                                text: "A long final reply line\n".repeat(80),
                                ..Default::default()
                            },
                        ],
                        ..Default::default()
                    },
                    w,
                    cx,
                );
            })
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        let mut visual = VisualTestContext::from_window(handle.into(), cx);
        visual.simulate_event(ScrollWheelEvent {
            position: point(px(500.), px(300.)),
            delta: ScrollDelta::Pixels(point(px(0.), px(120.))),
            ..Default::default()
        });
        handle
            .update(cx, |this, w, cx| {
                assert!(
                    !this.follow,
                    "scrolling upward within the last reply must stop following"
                );
                let position = this.transcript.logical_scroll_top();
                this.apply(
                    Snapshot {
                        schema: 2,
                        generation: 1,
                        composer_key: "history".into(),
                        blocks_from: 3,
                        status: "Ready".into(),
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                assert_eq!(this.snapshot.blocks.len(), 3);
                let after = this.transcript.logical_scroll_top();
                assert_eq!(after.item_ix, position.item_ix);
                assert_eq!(after.offset_in_item, position.offset_in_item);
                assert!(
                    !this.follow_pending,
                    "metadata updates are not new transcript content"
                );
                this.schedule_transcript_reset(cx);
            })
            .unwrap();
        let position = handle
            .update(cx, |this, _, _| this.transcript.logical_scroll_top())
            .unwrap();
        cx.executor()
            .advance_clock(std::time::Duration::from_millis(130));
        cx.run_until_parked();
        handle
            .update(cx, |this, _, _| {
                let after = this.transcript.logical_scroll_top();
                assert_eq!(after.item_ix, position.item_ix);
                assert_eq!(after.offset_in_item, position.offset_in_item);
            })
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        visual.simulate_event(ScrollWheelEvent {
            position: point(px(500.), px(300.)),
            delta: ScrollDelta::Pixels(point(px(0.), px(10000.))),
            ..Default::default()
        });
        // A virtual list measures earlier rows on the next frame before a
        // subsequent wheel event can reach their newly known pixel offsets.
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        visual.simulate_event(ScrollWheelEvent {
            position: point(px(500.), px(300.)),
            delta: ScrollDelta::Pixels(point(px(0.), px(10000.))),
            ..Default::default()
        });
        handle
            .update(cx, |this, _, _| {
                assert_eq!(
                    this.transcript.logical_scroll_top().item_ix,
                    0,
                    "earliest message remains reachable"
                );
                assert_eq!(this.transcript.item_count(), 3);
            })
            .unwrap();
    }

    #[gpui::test]
    fn mock_metadata_chip_reveals_every_field_without_changing_the_title(cx: &mut TestAppContext) {
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        let title = "Inspect the workspace ⟦mock scenario=hello actor=main speed=12 seed=0⟧";
        handle
            .update(cx, |this, w, cx| {
                w.resize(size(px(640.), px(700.)));
                this.apply(
                    Snapshot {
                        schema: 2,
                        generation: 1,
                        tabs: vec![bridge::Session {
                            id: "fixture".into(),
                            title: title.into(),
                            active: true,
                            ..Default::default()
                        }],
                        breadcrumb: "/private/tmp/review/sandbox/workspace › main".into(),
                        ..Default::default()
                    },
                    w,
                    cx,
                );
            })
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        let mut visual = VisualTestContext::from_window(handle.into(), cx);
        let chip = visual.debug_bounds("mock-directive").unwrap();
        assert!(chip.right() <= px(640.));
        visual.simulate_click(chip.center(), Modifiers::default());
        handle
            .update(cx, |this, _, _| {
                assert_eq!(this.snapshot.tabs[0].title, title);
                let notice = this.notices.iter().next().unwrap();
                for field in ["scenario: hello", "actor: main", "speed: 12", "seed: 0"] {
                    assert!(notice.text.contains(field));
                }
            })
            .unwrap();
    }

    #[gpui::test]
    fn short_conversation_starts_below_the_header_instead_of_above_composer(
        cx: &mut TestAppContext,
    ) {
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, cx| {
                w.resize(size(px(1200.), px(1000.)));
                this.apply(
                    Snapshot {
                        schema: 2,
                        generation: 1,
                        blocks: vec![
                            Content {
                                id: "user".into(),
                                kind: "user".into(),
                                text: "A short request".into(),
                                number: 1,
                                ..Default::default()
                            },
                            Content {
                                id: "reply".into(),
                                kind: "markdown".into(),
                                text: "The session, context, conversation and details hierarchy works here. Tools stay compact beneath the reply.\n\n```python\ndef inspect_workspace():\n    return \"complete context\"\n```".into(),
                                ..Default::default()
                            },
                        ],
                        ..Default::default()
                    },
                    w,
                    cx,
                );
            })
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        let mut visual = VisualTestContext::from_window(handle.into(), cx);
        assert!(visual.debug_bounds("user-card").unwrap().top() < px(250.));
        handle
            .update(cx, |this, w, cx| {
                this.apply(
                    Snapshot {
                        schema: 2,
                        generation: 1,
                        blocks: (0..70)
                            .map(|i| Content {
                                id: format!("reply-{i}"),
                                kind: "markdown".into(),
                                text: format!("Reply {i}"),
                                ..Default::default()
                            })
                            .collect(),
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                assert_eq!(this.transcript.item_count(), 70);
                this.apply(
                    Snapshot {
                        schema: 2,
                        generation: 1,
                        blocks: vec![Content {
                            id: "user".into(),
                            kind: "user".into(),
                            text: "Short again".into(),
                            ..Default::default()
                        }],
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                assert_eq!(this.transcript.item_count(), 1);
            })
            .unwrap();
    }

    #[gpui::test]
    fn user_and_reply_cards_share_the_left_edge_and_width(cx: &mut TestAppContext) {
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, cx| {
                w.resize(size(px(1200.), px(800.)));
                this.apply(
                    Snapshot {
                        schema: 2,
                        generation: 1,
                        blocks: vec![
                            Content {
                                id: "user".into(),
                                kind: "user".into(),
                                text: "Left aligned request".into(),
                                ..Default::default()
                            },
                            Content {
                                id: "reply".into(),
                                kind: "markdown".into(),
                                text: "Left aligned reply".into(),
                                ..Default::default()
                            },
                        ],
                        ..Default::default()
                    },
                    w,
                    cx,
                );
            })
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        let mut visual = VisualTestContext::from_window(handle.into(), cx);
        let user = visual.debug_bounds("user-card").unwrap();
        let reply = visual.debug_bounds("reply-card").unwrap();
        assert_eq!(user.left(), reply.left());
        assert_eq!(user.size.width, reply.size.width);
        assert_eq!(user.size.width, px(theme::size::READING));
    }

    #[gpui::test]
    fn follow_pending_tracks_content_while_scrolled_up(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                let block = |id: &str, text: &str| Content {
                    id: id.into(),
                    kind: "markdown".into(),
                    text: text.into(),
                    ..Default::default()
                };
                let snapshot = |blocks: Vec<Content>| Snapshot {
                    schema: 2,
                    composer_key: "s".into(),
                    generation: 1,
                    blocks_from: 0,
                    blocks,
                    ..Default::default()
                };
                this.apply(snapshot(vec![block("a", "one")]), w, cx);
                assert!(!this.follow_pending);
                this.follow = false;
                this.apply(snapshot(vec![block("a", "one"), block("b", "two")]), w, cx);
                assert!(this.follow_pending);
                this.latest(&JumpLatest, w, cx);
                assert!(this.follow);
                assert!(!this.follow_pending);
            })
            .unwrap();
    }
    #[gpui::test]
    fn resize_remeasure_is_debounced_to_one_reset(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, _, cx| {
                // The first draw of the new window already queued a reset from
                // width 0; the two moves below must supersede it.
                let base = this.resize_revision;
                let resets = this.resize_reset_count.get();
                this.schedule_transcript_reset(cx);
                this.schedule_transcript_reset(cx);
                assert_eq!(this.resize_revision, base + 2);
                assert_eq!(
                    this.resize_reset_count.get(),
                    resets,
                    "no reset during the drag"
                );
            })
            .unwrap();
        cx.executor()
            .advance_clock(std::time::Duration::from_millis(130));
        cx.executor().run_until_parked();
        window
            .update(cx, |this, _, _| {
                assert_eq!(
                    this.resize_reset_count.get(),
                    1,
                    "only the last resize resets"
                );
                assert!(this.resize_task.is_none());
            })
            .unwrap();
    }
    #[test]
    fn filtered_selection_wraps_in_both_directions() {
        assert_eq!(next_selection(None, 3, 1), 0);
        assert_eq!(next_selection(None, 3, -1), 2);
        assert_eq!(next_selection(Some(0), 3, 1), 1);
        assert_eq!(next_selection(Some(2), 3, 1), 0);
        assert_eq!(next_selection(Some(0), 3, -1), 2);
    }
    #[gpui::test]
    fn drawing_a_populated_transcript_does_not_reenter_the_list(cx: &mut TestAppContext) {
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, cx| {
                let blocks = (0..12)
                    .map(|i| Content {
                        id: format!("b{i}"),
                        kind: "markdown".into(),
                        text: format!("line {i}"),
                        ..Default::default()
                    })
                    .collect();
                this.apply(
                    Snapshot {
                        schema: 2,
                        composer_key: "s".into(),
                        generation: 1,
                        blocks,
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                w.resize(size(px(1000.), px(800.)));
            })
            .unwrap();
        // Laying out the list runs the scroll handler; it must not borrow the
        // list's RefCell again (that panicked at runtime).
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
    }
    #[gpui::test]
    fn notice_dismiss_does_not_activate_the_tool_behind_it(cx: &mut TestAppContext) {
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, cx| {
                this.apply(
                    Snapshot {
                        schema: 2,
                        generation: 1,
                        blocks: (0..20)
                            .map(|i| Content {
                                id: format!("tool-{i}"),
                                kind: "tool".into(),
                                title: "read file".into(),
                                operation: Some(json!({"kind":"toggle"})),
                                ..Default::default()
                            })
                            .collect(),
                        toasts: vec![bridge::ToastWire {
                            id: 1,
                            level: "error".into(),
                            title: "Request failed".into(),
                            body: "This notice sits over tool rows".into(),
                            ..Default::default()
                        }],
                        ..Default::default()
                    },
                    w,
                    cx,
                );
            })
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        let mut visual = VisualTestContext::from_window(handle.into(), cx);
        let dismiss = visual.debug_bounds("notice-dismiss").unwrap();
        visual.simulate_click(dismiss.center(), Modifiers::default());
        handle
            .update(cx, |this, _, _| {
                assert!(
                    this.notices.is_empty(),
                    "dismiss must not dispatch the underlying host operation"
                )
            })
            .unwrap();
    }

    #[gpui::test]
    fn reply_copy_remains_keyboard_reachable(cx: &mut TestAppContext) {
        cx.update(bind_desktop_keys);
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, cx| {
                this.apply(
                    Snapshot {
                        schema: 2,
                        generation: 1,
                        blocks: vec![Content {
                            id: "reply".into(),
                            kind: "markdown".into(),
                            text: "Complete reply 🦀".into(),
                            ..Default::default()
                        }],
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                window_focus(&this.composer, w, cx);
            })
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        // Cycle through visible controls; a focused copy action must work even
        // when the pointer never enters its reply group.
        for _ in 0..30 {
            cx.simulate_keystrokes(handle.into(), "ctrl-tab");
            cx.update_window(handle.into(), |_, w, cx| {
                let _ = w.draw(cx);
            })
            .unwrap();
            let mut visual = VisualTestContext::from_window(handle.into(), cx);
            visual.simulate_event(KeyUpEvent {
                keystroke: Keystroke::parse("enter").unwrap(),
            });
            if cx
                .update(|cx| cx.read_from_clipboard().and_then(|item| item.text()))
                .as_deref()
                == Some("Complete reply 🦀")
            {
                return;
            }
        }
        panic!("keyboard focus did not reach the copy action");
    }

    #[gpui::test]
    fn long_notice_wraps_above_multiline_composer_at_both_widths(cx: &mut TestAppContext) {
        for width in [640., 1440.] {
            let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
            handle
                .update(cx, |this, w, cx| {
                    w.resize(size(px(width), px(940.)));
                    this.apply(
                        Snapshot {
                            schema: 2,
                            generation: 1,
                            composer_key: "notice-layout".into(),
                            restore: "first draft line\nsecond draft line\nthird draft line".into(),
                            blocks: vec![Content {
                                id: "reply".into(),
                                kind: "markdown".into(),
                                text: "An inspectable reply".into(),
                                ..Default::default()
                            }],
                            toasts: vec![bridge::ToastWire {
                                id: 1,
                                level: "error".into(),
                                title: "Request failed".into(),
                                body:
                                    "A long error message that must wrap within its notice card. "
                                        .repeat(8),
                                ..Default::default()
                            }],
                            ..Default::default()
                        },
                        w,
                        cx,
                    );
                })
                .unwrap();
            cx.update_window(handle.into(), |_, w, cx| {
                let _ = w.draw(cx);
            })
            .unwrap();
            handle
                .update(cx, |this, _, cx| {
                    let notice_id = this.notices.iter().next().unwrap().id;
                    let text = this.text_cache.borrow();
                    let notice = text
                        .get(&format!("notice-text-{notice_id}"))
                        .unwrap()
                        .read(cx)
                        .painted_bounds()
                        .unwrap();
                    let composer = this.composer.read(cx).painted_bounds().unwrap();
                    assert!(
                        notice.size.width < px(460.),
                        "notice text fits inside the padded card"
                    );
                    assert!(
                        notice.bottom() < composer.top(),
                        "notice cannot spill onto a multiline draft"
                    );
                    assert!(
                        notice.size.height > px(theme::size::BODY_LINE * 2.),
                        "long error wraps"
                    );
                    assert_eq!(this.composer.read(cx).content.lines().count(), 3);
                })
                .unwrap();
        }
    }

    #[gpui::test]
    fn disconnected_state_disables_the_composer_and_keeps_the_draft(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                let snapshot = |generation: u64, disconnected: bool| Snapshot {
                    schema: 2,
                    composer_key: "s".into(),
                    generation,
                    disconnected,
                    ..Default::default()
                };
                // Establish the session key first so the draft is not swapped.
                this.apply(snapshot(0, false), w, cx);
                this.composer
                    .update(cx, |input, cx| input.set("unfinished".into(), cx));
                this.apply(snapshot(1, true), w, cx);
                assert!(this.snapshot.disconnected);
                assert!(this.composer.read(cx).readonly);
                assert_eq!(this.composer.read(cx).content, "unfinished");
                this.submit_mode("steer", w, cx);
                assert!(this.notices.is_empty(), "a refused submit adds no notice");
                this.apply(snapshot(2, false), w, cx);
                assert!(!this.composer.read(cx).readonly);
                assert_eq!(this.composer.read(cx).content, "unfinished");
            })
            .unwrap();
    }
    #[gpui::test]
    fn tab_from_an_empty_draft_enters_transcript_navigation(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                let block = |id: &str, kind: &str| Content {
                    id: id.into(),
                    kind: kind.into(),
                    text: id.into(),
                    operation: Some(json!({"kind": "turn_toggle", "id": id})),
                    ..Default::default()
                };
                this.apply(
                    Snapshot {
                        schema: 2,
                        composer_key: "s".into(),
                        generation: 1,
                        blocks: vec![block("u", "user"), block("t", "tool")],
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                this.complete_first(&CompleteFirst, w, cx);
                assert_eq!(
                    this.nav,
                    Some(1),
                    "empty draft Tab selects the newest target"
                );
                this.move_nav(-1);
                assert_eq!(this.nav, Some(0));
                this.move_nav(-1);
                assert_eq!(this.nav, Some(0), "selection stops at the first target");
                this.open_nav(w, cx);
                assert!(this.notices.contains_text("Preview fixture"));
                this.dismiss(&Dismiss, w, cx);
                assert!(this.nav.is_none(), "Escape leaves navigation");
            })
            .unwrap();
    }
    #[gpui::test]
    fn applying_blocks_populates_the_minimap_ticks(cx: &mut TestAppContext) {
        let window = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        window
            .update(cx, |this, w, cx| {
                let blocks = vec![
                    Content {
                        id: "u".into(),
                        kind: "user".into(),
                        text: "Fix it".into(),
                        ..Default::default()
                    },
                    Content {
                        id: "a".into(),
                        kind: "markdown".into(),
                        text: "Done".into(),
                        ..Default::default()
                    },
                    Content {
                        id: "t".into(),
                        kind: "tool".into(),
                        text: "ignored".into(),
                        ..Default::default()
                    },
                ];
                this.apply(
                    Snapshot {
                        schema: 2,
                        generation: 1,
                        blocks,
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                assert_eq!(this.minimap_ticks.len(), 2);
                assert!(this.minimap_ticks[0].long);
                assert!(!this.minimap_ticks[1].long);
                // Rendering the rail must not panic.
                let _ = this.minimap(cx);
            })
            .unwrap();
    }
    #[test]
    fn widening_drops_a_stale_drawer_flag() {
        assert_eq!(
            compact_pane_after_resize(Some("sessions_sidebar"), true, false),
            None
        );
        assert_eq!(
            compact_pane_after_resize(Some("details_sidebar"), false, true),
            None
        );
        assert_eq!(
            compact_pane_after_resize(Some("sessions_sidebar"), false, false),
            Some("sessions_sidebar")
        );
        assert_eq!(
            compact_pane_after_resize(Some("details_sidebar"), false, false),
            Some("details_sidebar")
        );
        assert_eq!(compact_pane_after_resize(None, true, true), None);
    }
    #[test]
    fn details_bracket_keys_cycle_the_inspector_tabs() {
        assert_eq!(next_details_tab("Session", 1), "Files");
        assert_eq!(next_details_tab("Logs", 1), "Session");
        assert_eq!(next_details_tab("Session", -1), "Logs");
        assert_eq!(next_details_tab("unknown", 1), "Files");
    }
    #[test]
    fn attachment_paths_are_quoted_without_losing_spaces_or_apostrophes() {
        assert_eq!(
            attachment_command(std::path::Path::new("a  b's.md")),
            "/attach \"a  b's.md\""
        );
    }
}
