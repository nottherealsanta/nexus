//! Desktop chrome, settings, pickers and approval sheets share host actions.
use super::*;
#[path = "primitives.rs"]
pub mod primitives;
use self::primitives::visible_range;
use gpui::ScrollHandle;
use std::cell::RefCell;

thread_local! {
    static PANEL_SCROLL: RefCell<ScrollHandle> = RefCell::new(ScrollHandle::new());
    static PICKER_FILTER_CACHE: RefCell<Option<(u64, Vec<usize>)>> = const { RefCell::new(None) };
}

fn panel_scroll_handle() -> ScrollHandle {
    PANEL_SCROLL.with(|scroll| scroll.borrow().clone())
}
impl Desktop {
    pub(crate) fn button(
        &self,
        id: impl Into<ElementId>,
        label: impl Into<SharedString>,
        action: Value,
        cx: &mut Context<Self>,
    ) -> Stateful<Div> {
        let t = self.theme();
        let dispatch = action["type"] != "ui_trace";
        let label: SharedString = label.into();
        let (symbol, caption) = icons::button_label(label.as_ref());
        let icon_only = matches!(
            label.as_ref(),
            "Attach"
                | "Dictate"
                | "Listen"
                | "Stop dictation"
                | "Commands"
                | "Sessions"
                | "Details"
                | "Settings"
        );
        let primary = label == "Send";
        let hint = label.clone();
        let caption = if icon_only { "" } else { caption };
        div()
            .id(id)
            .flex()
            .items_center()
            .justify_center()
            .gap_2()
            .px_3()
            .py_2()
            .rounded(px(6.))
            .text_size(px(12.))
            .text_color(t.muted)
            .cursor(CursorStyle::Arrow)
            .hover(move |s| {
                s.bg(if primary {
                    t.accent.opacity(0.9)
                } else {
                    t.raised
                })
                .text_color(if primary { t.background } else { t.text })
            })
            .focusable()
            .tab_stop(
                (self.snapshot.panel_title.is_empty() && self.snapshot.prompt.is_none())
                    || matches!(
                        action["type"].as_str(),
                        Some(
                            "nav_select"
                                | "answer"
                                | "save"
                                | "dismiss"
                                | "form_delete"
                                | "operation"
                        )
                    ),
            )
            .focus(move |s| {
                s.bg(if primary { t.accent } else { t.raised })
                    .text_color(if primary { t.background } else { t.accent })
            })
            .when(icon_only, |d| {
                d.w(px(32.))
                    .h(px(32.))
                    .px_0()
                    .py_0()
                    .tooltip(move |_, cx| cx.new(|_| ControlHint(hint.clone())).into())
            })
            .when_some(symbol, |d, name| d.child(icons::icon(name, t.muted)))
            .when(!caption.is_empty(), |d| d.child(caption.to_owned()))
            .when(dispatch, |d| {
                d.on_click(cx.listener(move |this, _, w, cx| {
                    cx.stop_propagation();
                    this.dispatch(action.clone(), w, cx);
                }))
            })
    }
    /// Top banner shown while the daemon is unreachable (§Phase 3). The composer
    /// stays visible and read-only with its draft kept; Reconnect replays state.
    pub(crate) fn disconnected_banner(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        div()
            .w_full()
            .flex_shrink_0()
            .flex()
            .items_center()
            .justify_center()
            .gap_3()
            .px_4()
            .py_2()
            .bg(t.amber.opacity(0.16))
            .border_b_1()
            .border_color(t.border)
            .text_size(px(12.))
            .text_color(t.text)
            .child("Disconnected — the daemon is unreachable. Your draft is kept.")
            .child(self.button(
                "reconnect-banner",
                "Reconnect  ⌘R",
                json!({"type":"command","text":"/reconnect"}),
                cx,
            ))
            .into_any_element()
    }

    /// Turn minimap rail (§2.4.3): one long tick per user turn, a short tick per
    /// assistant turn, and a brighter band for the visible range. Hidden by the
    /// caller below 900 px or when there are no turns.
    pub(crate) fn minimap(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        let total = self.snapshot.blocks.len().max(1) as f32;
        let (visible_start, visible_end) = self.transcript_visible.get();
        let band_top = (visible_start as f32 / total).clamp(0., 1.);
        let band_height =
            ((visible_end.saturating_sub(visible_start)) as f32 / total).clamp(0.02, 1.);
        let ticks = self.minimap_ticks.iter().cloned().map(|tick| {
            let top = (tick.block as f32 / total).clamp(0., 1.);
            let label = tick.label.clone();
            div()
                .id(SharedString::from(format!("minimap-{}", tick.block)))
                .absolute()
                .top(relative(top))
                .left(px(if tick.long { 0. } else { 3. }))
                .w(px(if tick.long { 12. } else { 6. }))
                .h(px(if tick.long { 3. } else { 2. }))
                .rounded(px(1.))
                .bg(if tick.long {
                    t.muted
                } else {
                    t.muted.opacity(0.45)
                })
                .cursor(CursorStyle::Arrow)
                .hover(move |s| s.bg(t.accent))
                .tooltip(move |_, cx| cx.new(|_| ControlHint(label.clone().into())).into())
                .on_click(cx.listener(move |this, _, _, cx| {
                    this.follow = false;
                    this.transcript.scroll_to(ListOffset {
                        item_ix: tick.block,
                        offset_in_item: px(0.),
                    });
                    cx.notify();
                }))
        });
        div()
            .w(px(12.))
            .flex_shrink_0()
            .h_full()
            .py(px(8.))
            .child(
                div()
                    .relative()
                    .w_full()
                    .h_full()
                    .child(
                        div()
                            .absolute()
                            .top(relative(band_top))
                            .h(relative(band_height))
                            .w_full()
                            .rounded(px(2.))
                            .bg(t.accent.opacity(0.12)),
                    )
                    .children(ticks),
            )
            .into_any_element()
    }

    /// "New messages" affordance shown when content arrived while scrolled up
    /// (§3.6 follow logic). Clicking restores follow and jumps to the end.
    pub(crate) fn follow_pill(&self, cx: &mut Context<Self>) -> AnyElement {
        if !self.follow_pending || self.follow {
            return div().into_any_element();
        }
        let t = self.theme();
        div()
            .absolute()
            .bottom(px(118.))
            .left_0()
            .right_0()
            .flex()
            .justify_center()
            .child(
                div()
                    .id("follow-latest")
                    .px_3()
                    .py_2()
                    .rounded(px(999.))
                    .bg(t.raised)
                    .border_1()
                    .border_color(t.border)
                    .shadow_lg()
                    .text_size(px(12.))
                    .text_color(t.text)
                    .cursor(CursorStyle::Arrow)
                    .hover(move |s| s.bg(t.accent_bg).text_color(t.accent))
                    .on_click(cx.listener(|this, _, w, cx| this.latest(&JumpLatest, w, cx)))
                    .child("↓ New messages"),
            )
            .into_any_element()
    }
    /// Bounded toast stack, bottom-centre above the composer (§2.4.16).
    pub(crate) fn toasts(&self, cx: &mut Context<Self>) -> AnyElement {
        if self.notices.is_empty() {
            return div().into_any_element();
        }
        let t = self.theme();
        let notices: Vec<_> = self.notices.iter().cloned().collect();
        let items = notices.into_iter().map(|notice| {
            let accent = match notice.kind {
                NoticeKind::Info => t.accent,
                NoticeKind::Warning => t.amber,
                NoticeKind::Error => t.red,
            };
            let id = notice.id;
            let mut row = div()
                .id(SharedString::from(format!("toast-{id}")))
                .w_full()
                .max_w(px(460.))
                .flex()
                .items_start()
                .gap_3()
                .p_3()
                .rounded(px(10.))
                .bg(t.raised)
                .border_1()
                .border_l_4()
                .border_color(accent)
                .shadow_lg()
                .child(
                    div()
                        .flex_1()
                        .min_w_0()
                        .text_size(px(12.))
                        .text_color(t.text)
                        .child(notice.text.clone()),
                );
            match notice.action {
                NoticeAction::Reconnect => {
                    row = row.child(self.button(
                        SharedString::from(format!("toast-action-{id}")),
                        "Reconnect",
                        json!({"type":"command","text":"/reconnect"}),
                        cx,
                    ));
                }
                NoticeAction::UpdateHelp => {
                    row = row.child(self.button(
                        SharedString::from(format!("toast-action-{id}")),
                        "Update help",
                        json!({"type":"update_help"}),
                        cx,
                    ));
                }
                NoticeAction::None => {}
            }
            if notice.kind != NoticeKind::Info {
                row = row.child(
                    div()
                        .id(SharedString::from(format!("toast-dismiss-{id}")))
                        .px_2()
                        .py_1()
                        .rounded(px(6.))
                        .text_size(px(11.))
                        .text_color(t.muted)
                        .cursor(CursorStyle::Arrow)
                        .hover(move |s| s.bg(t.accent_bg).text_color(t.text))
                        .on_click(cx.listener(move |this, _, _, cx| this.dismiss_notice(id, cx)))
                        .child("Dismiss"),
                );
            }
            row.into_any_element()
        });
        div()
            .absolute()
            .bottom(px(150.))
            .left_0()
            .right_0()
            .flex()
            .flex_col()
            .items_center()
            .gap_2()
            .children(items)
            .into_any_element()
    }
    pub(crate) fn topbar(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        div()
            .h(px(TOP_BAR_HEIGHT))
            .flex_shrink_0()
            .flex()
            .items_center()
            .bg(t.sidebar)
            .border_b_1()
            .border_color(t.border.opacity(0.55))
            .child(
                div()
                    .w(px(104.))
                    .h_full()
                    .window_control_area(WindowControlArea::Drag),
            )
            .child(
                div()
                    .text_size(px(15.))
                    .font_weight(FontWeight::MEDIUM)
                    .text_color(t.muted)
                    .child("nexus"),
            )
            .child(
                div()
                    .ml_3()
                    .text_size(px(10.))
                    .text_color(t.muted)
                    .child(if self.preview { "PREVIEW" } else { "" }),
            )
            .child(
                div()
                    .flex_1()
                    .h_full()
                    .window_control_area(WindowControlArea::Drag),
            )
            .when(!self.snapshot.update_notice.is_empty(), |d| {
                d.child(
                    self.button(
                        "update-notice",
                        self.snapshot.update_notice.clone(),
                        json!({"type":"update_help"}),
                        cx,
                    )
                    .text_color(t.amber),
                )
            })
            .child(self.button(
                "palette",
                "Commands",
                json!({"type":"command","text":"/help"}),
                cx,
            ))
            .child(self.button(
                "sessions-toggle",
                "Sessions",
                json!({"type":"toggle","key":"sessions_sidebar"}),
                cx,
            ))
            .child(self.button(
                "details-toggle",
                "Details",
                json!({"type":"toggle","key":"details_sidebar"}),
                cx,
            ))
            .child(self.button(
                "settings",
                "Settings",
                json!({"type":"command","text":"/settings"}),
                cx,
            ))
            .child(div().w(px(16.)))
            .into_any_element()
    }
    pub(crate) fn breadcrumb(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        let title = self
            .snapshot
            .tabs
            .iter()
            .find(|s| s.active)
            .map(|s| s.title.clone())
            .unwrap_or_else(|| {
                if self.snapshot.agent_page.is_empty() {
                    "New conversation".into()
                } else {
                    self.snapshot.title.clone()
                }
            });
        let status = match self.snapshot.status.as_str() {
            "running" | "working" => "Working",
            "waiting" | "awaiting_permission" | "awaiting_input" => "Needs input",
            "failed" => "Failed",
            "done" => "Complete",
            _ => "Ready",
        };
        let mut row = div()
            .flex()
            .flex_col()
            .flex_shrink_0()
            .border_b_1()
            .border_color(t.border.opacity(0.55));
        if !self.snapshot.tabs.is_empty() {
            row = row.child(
                div()
                    .id("tabs")
                    .flex()
                    .overflow_x_scroll()
                    .px_4()
                    .pt_2()
                    .gap_1()
                    .children(self.snapshot.tabs.iter().enumerate().map(|(i, s)| {
                        let id = s.id.clone();
                        let workspace = s.workspace.clone();
                        div()
                            .id(("tab", i))
                            .focusable()
                            .tab_stop(
                                self.snapshot.panel_title.is_empty()
                                    && self.snapshot.prompt.is_none(),
                            )
                            .focus(move |s| s.bg(t.raised))
                            .flex()
                            .items_center()
                            .gap_2()
                            .px_3()
                            .py_2()
                            .rounded(px(6.))
                            .max_w(px(230.))
                            .bg(if s.active { t.raised } else { t.background })
                            .text_color(if s.active { t.text } else { t.muted })
                            .cursor(CursorStyle::Arrow)
                            .child(
                                div()
                                    .text_size(px(10.))
                                    .text_color(
                                        if s.state == "running"
                                            || matches!(s.status.as_str(), "running" | "done")
                                        {
                                            t.accent
                                        } else {
                                            t.muted
                                        },
                                    )
                                    .child(
                                        if s.state == "running"
                                            || matches!(s.status.as_str(), "running" | "done")
                                        {
                                            "●"
                                        } else {
                                            "◦"
                                        },
                                    ),
                            )
                            .child(div().truncate().child(s.title.clone()))
                            .on_click(cx.listener(move |this, _, w, cx| {
                                this.dispatch(
                                    json!({"type":"session_open","text":id,"workspace":workspace}),
                                    w,
                                    cx,
                                )
                            }))
                            .child(
                                self.button(
                                    ("close-tab", i),
                                    "×",
                                    json!({"type":"tab_close","text":s.id,"workspace":s.workspace}),
                                    cx,
                                )
                                .px_1()
                                .py_0(),
                            )
                    }))
                    .child(self.button(
                        "new-tab",
                        "+",
                        json!({"type":"command","text":"/new"}),
                        cx,
                    )),
            );
        }
        row.child(
            div()
                .flex()
                .items_center()
                .justify_between()
                .px_6()
                .py_3()
                .child(
                    div()
                        .flex()
                        .flex_col()
                        .gap_1()
                        .min_w_0()
                        .child(
                            div()
                                .font_weight(FontWeight::SEMIBOLD)
                                .text_size(px(15.))
                                .truncate()
                                .child(title),
                        )
                        .child(
                            div()
                                .text_size(px(11.))
                                .text_color(t.muted)
                                .truncate()
                                .child(self.snapshot.breadcrumb.clone()),
                        ),
                )
                .child(
                    div()
                        .flex()
                        .items_center()
                        .gap_2()
                        .flex_shrink_0()
                        .text_size(px(11.))
                        .text_color(if status == "Complete" {
                            t.green
                        } else if status == "Needs input" {
                            t.amber
                        } else if status == "Failed" {
                            t.red
                        } else {
                            t.muted
                        })
                        .child(icons::icon(
                            if status == "Complete" {
                                "check"
                            } else if status == "Needs input" {
                                "circle-help"
                            } else if status == "Failed" {
                                "triangle-alert"
                            } else {
                                "circle-dot"
                            },
                            if status == "Complete" {
                                t.green
                            } else if status == "Needs input" {
                                t.amber
                            } else if status == "Failed" {
                                t.red
                            } else {
                                t.muted
                            },
                        ))
                        .child(status),
                )
                .when(!self.snapshot.agent_page.is_empty(), |d| {
                    d.child(self.button(
                        "back-agent",
                        "← Conversation",
                        json!({"type":"dismiss"}),
                        cx,
                    ))
                }),
        )
        .into_any_element()
    }
    fn session_row(&self, row: usize, _window: &mut Window, cx: &mut Context<Self>) -> AnyElement {
        #[cfg(test)]
        self.session_render_count
            .set(self.session_render_count.get() + 1);
        let t = self.theme();
        let Some((i, heading)) = self.session_rows.borrow().get(row).copied() else {
            return div().into_any_element();
        };
        let Some(s) = self.snapshot.sessions.get(i) else {
            return div().into_any_element();
        };
        let id = s.id.clone();
        let workspace = s.workspace.clone();
        let selected = self.search_selected.as_deref() == Some(s.id.as_str());
        let card = div()
            .id(("session", i))
            .focusable()
            .tab_stop(self.snapshot.panel_title.is_empty() && self.snapshot.prompt.is_none())
            .focus(move |s| s.bg(t.raised))
            .flex()
            .flex_col()
            .gap_1()
            .px_2()
            .py_1()
            .rounded(px(8.))
            .mb_0()
            .cursor(CursorStyle::Arrow)
            .when(selected, |d| d.border_1().border_color(t.accent))
            .bg(if selected {
                t.accent_bg
            } else if s.active {
                t.raised
            } else {
                t.sidebar
            })
            .hover(move |style| style.bg(t.raised))
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap_2()
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .font_weight(if s.active {
                                FontWeight::SEMIBOLD
                            } else {
                                FontWeight::NORMAL
                            })
                            .truncate()
                            .child(s.title.clone()),
                    )
                    .child(
                        self.button(
                            ("session-more", i),
                            "···",
                            json!({"type":"session_actions","text":s.id,"workspace":s.workspace}),
                            cx,
                        )
                        .px_1()
                        .py_0(),
                    ),
            )
            .child(
                div()
                    .text_size(px(11.))
                    .text_color(t.muted)
                    .truncate()
                    .child(if s.sub.is_empty() {
                        s.status.clone()
                    } else if let Some((count, age)) = s.sub.split_once(" · ") {
                        format!(
                            "{}{} message{} · active {}",
                            if s.status == "working" {
                                "Working · "
                            } else if s.status == "input" {
                                "Needs input · "
                            } else {
                                ""
                            },
                            count,
                            if count == "1" { "" } else { "s" },
                            age
                        )
                    } else {
                        format!("{} messages", s.sub)
                    }),
            )
            .on_click(cx.listener(move |this, _, w, cx| {
                this.dispatch(
                    json!({"type":"session_open","text":id,"workspace":workspace}),
                    w,
                    cx,
                )
            }))
            .into_any_element();
        div()
            .when(heading, |d| {
                d.child(
                    div()
                        .mt_5()
                        .mb_2()
                        .px_3()
                        .text_size(px(10.))
                        .font_weight(FontWeight::SEMIBOLD)
                        .text_color(t.muted)
                        .child(s.group.to_uppercase()),
                )
            })
            .child(card)
            .into_any_element()
    }
    pub(crate) fn sidebar(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        let query = self.search.read(cx).content.to_lowercase();
        let mut rows = vec![];
        let mut group = String::new();
        for i in search_matches(&self.snapshot.sessions, &query) {
            let session = &self.snapshot.sessions[i];
            let heading = session.group != group;
            group = session.group.clone();
            rows.push((i, heading));
        }
        if *self.session_rows.borrow() != rows {
            self.session_list.reset(rows.len());
            *self.session_rows.borrow_mut() = rows;
        }

        let view = cx.entity().downgrade();
        let sessions = list(self.session_list.clone(), move |row, window, cx| {
            view.update(cx, |this, cx| this.session_row(row, window, cx))
                .unwrap_or_else(|_| div().into_any_element())
        })
        .size_full();
        div()
            .w(px(252.))
            .flex_shrink_0()
            .h_full()
            .flex()
            .flex_col()
            .bg(t.sidebar)
            .border_r_1()
            .border_color(t.border.opacity(0.55))
            .child(
                div()
                    .p_4()
                    .flex()
                    .flex_col()
                    .gap_3()
                    .child(
                        self.button(
                            "new-session",
                            "New conversation   ⌘N",
                            json!({"type":"command","text":"/new"}),
                            cx,
                        )
                        .w_full()
                        .bg(t.surface)
                        .border_1()
                        .border_color(t.border)
                        .text_color(t.text),
                    )
                    .child(
                        div()
                            .px_3()
                            .py_2()
                            .rounded(px(6.))
                            .bg(t.surface)
                            .border_1()
                            .border_color(t.border)
                            .flex()
                            .items_center()
                            .gap_2()
                            .child(div().flex_1().min_w_0().child(self.search.clone()))
                            .when(!self.search.read(cx).content.is_empty(), |d| {
                                d.child(
                                    div()
                                        .id("search-clear")
                                        .px_2()
                                        .rounded(px(4.))
                                        .text_color(t.muted)
                                        .cursor(CursorStyle::Arrow)
                                        .hover(move |s| s.bg(t.accent_bg).text_color(t.text))
                                        .on_click(cx.listener(|this, _, _, cx| {
                                            this.search.update(cx, |input, cx| {
                                                input.set(String::new(), cx)
                                            });
                                            this.search_selected = None;
                                            cx.notify();
                                        }))
                                        .child("×"),
                                )
                            }),
                    ),
            )
            .child(
                div()
                    .id("session-list")
                    .flex_1()
                    .min_h_0()
                    .px_3()
                    .child(sessions),
            )
            .when(!self.snapshot.archived_label.is_empty(), |d| {
                d.child(
                    self.button(
                        "archived-sessions",
                        self.snapshot.archived_label.clone(),
                        json!({"type":"command","text":"/archived"}),
                        cx,
                    )
                    .mx_3()
                    .justify_start(),
                )
            })
            .when(self.snapshot.sessions_truncated, |d| {
                d.child(
                    div()
                        .p_3()
                        .text_color(t.amber)
                        .child("Showing first 1,000 sessions"),
                )
            })
            .child(
                div()
                    .p_4()
                    .border_t_1()
                    .border_color(t.border)
                    .flex()
                    .items_center()
                    .justify_between()
                    .text_size(px(11.))
                    .text_color(t.muted)
                    .child(if self.preview {
                        "Fixture workspace"
                    } else {
                        "Workspace connected"
                    })
                    .child(
                        self.button(
                            "sidebar-settings",
                            "⚙",
                            json!({"type":"command","text":"/settings"}),
                            cx,
                        )
                        .px_2(),
                    ),
            )
            .into_any_element()
    }
    pub(crate) fn details(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        let panel = &self.snapshot.details_panel;
        let mut rows = vec![];
        if panel.tab == "Logs" {
            rows.push(
                self.button(
                    "logs-fold",
                    "Show/hide routine entries",
                    json!({"type":"logs_fold"}),
                    cx,
                )
                .justify_start()
                .into_any_element(),
            );
            for (key, value) in &panel.logs_header {
                rows.push(kv(key, value, t));
            }
            for line in &self.snapshot.logs {
                rows.push(
                    div()
                        .text_size(px(11.))
                        .font_family("Menlo")
                        .line_height(px(19.))
                        .child(line.replace(" · Ctrl+A toggle", ""))
                        .into_any_element(),
                );
            }
        } else {
            if !matches!(panel.tab.as_str(), "Files" | "MCP") {
                rows.push(section("SESSION", t));
                if let Some((_, id)) = panel.logs_header.first() {
                    rows.push(
                        self.button(
                            "copy-session-id",
                            "Copy session ID",
                            json!({"type":"copy_text","text":id}),
                            cx,
                        )
                        .justify_start()
                        .into_any_element(),
                    );
                }
                for (key, value) in &panel.session {
                    rows.push(kv(key, value, t));
                }
            }
            if panel.tab == "Files" {
                rows.push(section("MODIFIED FILES", t));
                if panel.files.is_empty() {
                    rows.push(
                        div()
                            .text_color(t.muted)
                            .text_size(px(12.))
                            .child("No files changed yet")
                            .into_any_element(),
                    );
                }
                for (i, file) in panel.files.iter().enumerate() {
                    let path = file.path.clone();
                    rows.push(
                        div()
                            .id(("file", i))
                            .focusable()
                            .tab_stop(
                                self.snapshot.panel_title.is_empty()
                                    && self.snapshot.prompt.is_none(),
                            )
                            .focus(move |s| s.bg(t.raised).text_color(t.accent))
                            .py_2()
                            .cursor(CursorStyle::Arrow)
                            .flex()
                            .flex_col()
                            .gap_2()
                            .child(
                                div()
                                    .flex()
                                    .items_center()
                                    .gap_2()
                                    .child(
                                        div()
                                            .flex_1()
                                            .min_w_0()
                                            .truncate()
                                            .text_size(px(12.))
                                            .child(file.path.clone()),
                                    )
                                    .child(
                                        div()
                                            .text_color(t.green)
                                            .text_size(px(11.))
                                            .child(format!("+{}", file.added)),
                                    )
                                    .child(
                                        div()
                                            .text_color(t.red)
                                            .text_size(px(11.))
                                            .child(format!("−{}", file.removed)),
                                    ),
                            )
                            .children(file.diff.iter().map(|line| {
                                div()
                                    .font_family("Menlo")
                                    .text_size(px(10.))
                                    .text_color(if line.starts_with('+') {
                                        t.green
                                    } else if line.starts_with('-') {
                                        t.red
                                    } else {
                                        t.muted
                                    })
                                    .child(line.clone())
                            }))
                            .on_click(cx.listener(move |this, _, w, cx| {
                                this.dispatch(json!({"type":"file_toggle","text":path}), w, cx)
                            }))
                            .into_any_element(),
                    );
                }
                if !panel.files_summary.is_empty() {
                    rows.push(
                        div()
                            .text_size(px(11.))
                            .text_color(t.muted)
                            .child(panel.files_summary.clone())
                            .into_any_element(),
                    );
                }
            }
            if panel.tab == "MCP" {
                rows.push(section("MCP SERVERS", t));
                if panel.mcp.is_empty() {
                    rows.push(
                        div()
                            .text_color(t.muted)
                            .text_size(px(12.))
                            .child("No servers configured")
                            .into_any_element(),
                    );
                }
                for (tone, text, note) in &panel.mcp {
                    rows.push(
                        div()
                            .flex()
                            .flex_col()
                            .gap_1()
                            .py_1()
                            .child(
                                div()
                                    .text_size(px(12.))
                                    .text_color(if tone == "error" { t.red } else { t.text })
                                    .child(text.clone()),
                            )
                            .child(
                                div()
                                    .text_size(px(11.))
                                    .text_color(t.muted)
                                    .child(note.clone()),
                            )
                            .into_any_element(),
                    );
                }
            }
        }
        div()
            .w(px(260.))
            .flex_shrink_0()
            .h_full()
            .flex()
            .flex_col()
            .bg(t.sidebar)
            .border_l_1()
            .border_color(t.border.opacity(0.55))
            .child(
                div()
                    .flex()
                    .px_3()
                    .py_3()
                    .border_b_1()
                    .border_color(t.border)
                    .children(["Session", "Files", "MCP", "Logs"].into_iter().map(|tab| {
                        self.button(
                            SharedString::from(format!("details-{tab}")),
                            tab,
                            json!({"type":"details_tab","text":tab}),
                            cx,
                        )
                        .px_2()
                        .bg(if panel.tab == tab {
                            t.raised
                        } else {
                            t.sidebar
                        })
                        .text_color(if panel.tab == tab {
                            t.accent
                        } else {
                            t.muted
                        })
                    })),
            )
            .child(
                div()
                    .id("details-scroll")
                    .flex_1()
                    .min_h_0()
                    .overflow_y_scroll()
                    .p_5()
                    .flex()
                    .flex_col()
                    .gap_3()
                    .children(rows),
            )
            .into_any_element()
    }
    pub(crate) fn composer_view(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        if !self.snapshot.agent_page.is_empty() {
            return div()
                .px_6()
                .py_3()
                .border_t_1()
                .border_color(t.border)
                .text_color(t.muted)
                .child("Subagent transcript · updates live · Esc returns to conversation")
                .into_any_element();
        }
        div()
            .flex_shrink_0()
            .px_6()
            .pb_4()
            .pt_3()
            .flex()
            .flex_col()
            .gap_2()
            .when(!self.follow, |d| {
                d.child(
                    self.button(
                        "latest",
                        "Jump to latest   ⌘J",
                        json!({"type":"ui_trace","lines":[]}),
                        cx,
                    )
                    .on_click(cx.listener(|this, _, w, cx| this.latest(&JumpLatest, w, cx))),
                )
            })
            .when(!self.snapshot.queue_lines.is_empty(), |d| {
                d.child(
                    div()
                        .p_3()
                        .rounded(px(6.))
                        .bg(t.raised)
                        .text_size(px(12.))
                        .text_color(t.muted)
                        .children(self.snapshot.queue_lines.iter().cloned()),
                )
            })
            .when(
                self.snapshot.voice_phase != "idle" && !self.snapshot.voice_phase.is_empty(),
                |d| {
                    d.child(
                        div()
                            .p_3()
                            .rounded(px(6.))
                            .bg(t.accent_bg)
                            .flex()
                            .flex_col()
                            .gap_2()
                            .child(format!("●  {}", self.snapshot.voice_phase))
                            .child(self.snapshot.voice_preview.clone())
                            .child(
                                div()
                                    .flex()
                                    .flex_wrap()
                                    .gap_2()
                                    .when(self.snapshot.voice_phase == "recording", |d| {
                                        d.child(self.button(
                                            "voice-stop",
                                            "Insert dictation",
                                            json!({"type":"voice_stop"}),
                                            cx,
                                        ))
                                        .child(
                                            self.button(
                                                "voice-send",
                                                "Send dictation",
                                                json!({"type":"voice_stop","send":true}),
                                                cx,
                                            ),
                                        )
                                    })
                                    .child(self.button(
                                        "voice-discard",
                                        "Discard",
                                        json!({"type":"voice_discard"}),
                                        cx,
                                    )),
                            ),
                    )
                },
            )
            .when(self.completion_active(cx), |d| {
                d.child(
                    div()
                        .id("completions-scroll")
                        .track_scroll(&self.completion_scroll)
                        .max_h(px(160.))
                        .overflow_y_scroll()
                        .flex()
                        .flex_col()
                        .gap_1()
                        .children(
                            self.snapshot
                                .completions
                                .iter()
                                .enumerate()
                                .map(|(i, text)| {
                                    let value = text.clone();
                                    self.button(
                                        ("complete", i),
                                        text.clone(),
                                        json!({"type":"ui_trace","lines":[]}),
                                        cx,
                                    )
                                    .justify_start()
                                    .bg(if i == self.completion_index {
                                        t.raised
                                    } else {
                                        t.background
                                    })
                                    .text_color(if i == self.completion_index {
                                        t.accent
                                    } else {
                                        t.muted
                                    })
                                    .on_click(cx.listener(move |this, _, _, cx| {
                                        if let Some(content) = completion_text(
                                            &this.composer.read(cx).content,
                                            &this.snapshot.completion_prefix,
                                            &this.snapshot.completion_query,
                                            &value,
                                        ) {
                                            if !this.preview {
                                                send(
                                                    json!({"type":"draft_changed","text":content}),
                                                    this.snapshot.generation,
                                                );
                                            }
                                            this.completion_hidden =
                                                Some(this.snapshot.completion_prefix.clone());
                                            this.composer
                                                .update(cx, |input, cx| input.set(content, cx));
                                        }
                                    }))
                                }),
                        ),
                )
            })
            .child(
                div()
                    .relative()
                    .rounded(px(14.))
                    .border_1()
                    .border_color(t.border)
                    .bg(t.surface)
                    .flex()
                    .flex_col()
                    .when(
                        self.snapshot
                            .inline_images
                            .iter()
                            .any(|image| image.message.is_empty()),
                        |d| {
                            d.child(
                                div().px_4().pt_3().flex().flex_wrap().gap_2().children(
                                    self.snapshot
                                        .inline_images
                                        .iter()
                                        .filter(|image| image.message.is_empty())
                                        .map(|image| self.image_thumbnail(image, cx)),
                                ),
                            )
                        },
                    )
                    .when(
                        self.snapshot
                            .attachment_lines
                            .iter()
                            .enumerate()
                            .any(|(i, _)| {
                                !self
                                    .snapshot
                                    .inline_images
                                    .iter()
                                    .any(|image| image.draft_index == Some(i))
                            }),
                        |d| {
                            d.child(
                                div().px_4().pt_3().flex().flex_wrap().gap_2().children(
                                    self.snapshot
                                        .attachment_lines
                                        .iter()
                                        .enumerate()
                                        .filter(|(i, _)| {
                                            !self
                                                .snapshot
                                                .inline_images
                                                .iter()
                                                .any(|image| image.draft_index == Some(*i))
                                        })
                                        .map(|(i, line)| {
                                            self.button(
                                                ("draft-attachment", i),
                                                line.clone(),
                                                json!({"type":"command","text":"/attach"}),
                                                cx,
                                            )
                                            .px_2()
                                            .py_1()
                                            .bg(t.accent_bg)
                                            .text_size(px(11.))
                                            .text_color(t.accent)
                                        }),
                                ),
                            )
                        },
                    )
                    .child(
                        div()
                            .id("composer-scroll")
                            .min_h(px(44.))
                            .max_h(px(220.))
                            .overflow_y_scroll()
                            .p_4()
                            .child(self.composer.clone()),
                    )
                    .child(
                        div()
                            .flex()
                            .items_center()
                            .justify_between()
                            .flex_wrap()
                            .gap_2()
                            .px_3()
                            .pb_2()
                            .child(
                                div()
                                    .flex()
                                    .items_center()
                                    .gap_1()
                                    .child(
                                        self.button(
                                            "attach",
                                            "Attach",
                                            json!({"type":"ui_trace","lines":[]}),
                                            cx,
                                        )
                                        .on_click(
                                            cx.listener(|this, _, w, cx| {
                                                this.attach(&Attach, w, cx)
                                            }),
                                        ),
                                    )
                                    .child(self.button(
                                        "voice",
                                        if self.snapshot.voice_phase == "recording" {
                                            "Stop dictation"
                                        } else {
                                            "Dictate"
                                        },
                                        if self.snapshot.voice_phase == "recording" {
                                            json!({"type":"voice_stop"})
                                        } else {
                                            json!({"type":"command","text":"/voice"})
                                        },
                                        cx,
                                    ))
                                    .child(self.button(
                                        "speak",
                                        "Listen",
                                        json!({"type":"command","text":"/speak"}),
                                        cx,
                                    )),
                            )
                            .child(div().flex().items_center().flex_wrap().gap_2().child(
                                if self.snapshot.status == "running" {
                                    self.button("stop", "Stop", json!({"type":"cancel"}), cx)
                                        .bg(t.raised)
                                        .text_color(t.amber)
                                } else {
                                    self.button(
                                        "send",
                                        "Send",
                                        json!({"type":"ui_trace","lines":[]}),
                                        cx,
                                    )
                                    .bg(t.accent)
                                    .rounded(px(10.))
                                    .text_color(t.background)
                                    .on_click(cx.listener(|this, _, w, cx| this.submit(w, cx)))
                                },
                            )),
                    ),
            )
            .child(
                div()
                    .flex()
                    .flex_wrap()
                    .items_center()
                    .justify_between()
                    .gap_2()
                    .child(self.composer_choices(cx))
                    .child(
                        self.button(
                            "context-usage",
                            if !self.snapshot.context_label.is_empty() {
                                self.snapshot.context_label.clone()
                            } else {
                                "Context unavailable".into()
                            },
                            json!({"type":"context_popover"}),
                            cx,
                        )
                        .px_1()
                        .py_0(),
                    ),
            )
            .into_any_element()
    }
    fn composer_choices(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        div()
            .flex()
            .flex_wrap()
            .items_center()
            .gap_1()
            .min_w_0()
            .child(
                self.button(
                    "agent",
                    format!("{} ▾", self.snapshot.agent),
                    json!({"type":"command","text":"/agent"}),
                    cx,
                )
                .py_1()
                .px_2()
                .text_color(t.muted),
            )
            .child(div().mx_1().w(px(1.)).h(px(12.)).bg(t.border))
            .child(
                self.button(
                    "model",
                    format!("{} ▾", self.snapshot.model),
                    json!({"type":"command","text":"/model"}),
                    cx,
                )
                .py_1()
                .px_2()
                .text_color(t.text)
                .font_weight(FontWeight::MEDIUM)
                .max_w(px(280.))
                .overflow_hidden(),
            )
            .child(
                self.button(
                    "effort",
                    format!("{} ▾", self.snapshot.effort),
                    json!({"type":"command","text":"/effort"}),
                    cx,
                )
                .py_0()
                .px_1(),
            )
            .into_any_element()
    }
    pub(crate) fn panel(&self, _window: &mut Window, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        let query = normalize_query(&self.filter.read(cx).content);
        let mut items = vec![];
        let mut group = String::new();
        let filtered = ranked_filter(&self.snapshot.items, &query);
        let visible = visible_range(filtered.len(), 0, 100, 0);
        for (i, item) in filtered[visible].iter().copied().enumerate() {
            if item.group != group {
                group = item.group.clone();
                items.push(section(&group.to_uppercase(), t));
            }
            let action = item_action(item);
            let mut row = div()
                .id(("choice", i))
                .focusable()
                .tab_stop(true)
                .focus(move |s| s.bg(t.raised).text_color(t.accent))
                .flex()
                .items_center()
                .gap_3()
                .px_4()
                .py_3()
                .rounded(px(6.))
                .cursor(CursorStyle::Arrow)
                .bg(if i == self.selection {
                    t.raised
                } else {
                    t.surface
                })
                .hover(move |s| s.bg(t.raised))
                .child(
                    div()
                        .w(px(16.))
                        .text_color(t.accent)
                        .child(if item.current { "●" } else { "" }),
                )
                .child(
                    div()
                        .flex_1()
                        .min_w_0()
                        .flex()
                        .flex_col()
                        .gap_1()
                        .child(item.label.clone())
                        .when(!item.detail.is_empty(), |d| {
                            d.child(
                                div()
                                    .text_size(px(11.))
                                    .text_color(t.muted)
                                    .child(item.detail.clone()),
                            )
                        })
                        .children(item.lines.iter().map(|line| {
                            div()
                                .text_size(px(11.))
                                .text_color(t.muted)
                                .child(line.clone())
                        })),
                )
                .on_click(cx.listener(move |this, _, w, cx| this.dispatch(action.clone(), w, cx)));
            if !item.trailing.is_empty() {
                row = row.child(
                    div()
                        .text_size(px(11.))
                        .text_color(t.muted)
                        .child(item.trailing.clone()),
                );
            }
            if let Some(enabled) = item.toggle_enabled {
                if item.toggle_locked {
                    row = row.child(div().text_size(px(11.)).text_color(t.muted).child("Locked"));
                } else if let Some(operation) = &item.toggle_operation {
                    row = row.child(
                        self.button(
                            ("toggle", i),
                            if enabled { "On" } else { "Off" },
                            json!({"type":"operation","operation":operation}),
                            cx,
                        )
                        .bg(if enabled { t.accent_bg } else { t.raised })
                        .text_color(if enabled {
                            t.accent
                        } else {
                            t.muted
                        }),
                    );
                }
            } else {
                row = row.child(icons::icon("chevron-right", t.muted));
            }
            items.push(row.into_any_element());
        }
        let nav = self.snapshot.nav.as_ref().map(|nav| {
            div()
                .id("settings-nav-scroll")
                .overflow_y_scroll()
                .w(px(195.))
                .flex_shrink_0()
                .p_3()
                .bg(t.sidebar)
                .border_r_1()
                .border_color(t.border)
                .flex()
                .flex_col()
                .gap_1()
                .children(
                    nav.items
                        .iter()
                        .enumerate()
                        .map(|(i, (label, _, heading))| {
                            if *heading {
                                section(label, t)
                            } else {
                                self.button(
                                    ("settings-nav", i),
                                    label.clone(),
                                    json!({"type":"nav_select","text":i.to_string()}),
                                    cx,
                                )
                                .justify_start()
                                .bg(if nav.selected == i as i64 {
                                    t.accent_bg
                                } else {
                                    t.sidebar
                                })
                                .text_color(if nav.selected == i as i64 {
                                    t.accent
                                } else {
                                    t.muted
                                })
                                .into_any_element()
                            }
                        }),
                )
                .into_any_element()
        });
        let markdown_details = self.snapshot.panel_format == "markdown";
        let mut body = div().flex().flex_col().flex_1().min_w_0().min_h_0();
        if !self.snapshot.items.is_empty()
            && !(self.snapshot.panel_format == "image" && self.snapshot.items.len() == 1)
        {
            body = body.child(
                div()
                    .mx_4()
                    .mt_4()
                    .px_3()
                    .py_2()
                    .border_1()
                    .border_color(t.border)
                    .rounded(px(6.))
                    .child(self.filter.clone()),
            );
        }
        if let Some(image) = self
            .image_preview
            .as_ref()
            .filter(|_| self.snapshot.panel_format == "image")
        {
            body = body.child(
                div().p_4().flex().justify_center().child(
                    img(image.clone())
                        .max_w(relative(1.))
                        .h((_window.viewport_size().height * 0.35).min(px(280.)))
                        .object_fit(ObjectFit::Contain),
                ),
            );
        }
        if self.snapshot.panel_title == "Keyboard shortcuts" {
            body = body.child(
                div()
                    .id("desktop-shortcuts")
                    .max_h(px(240.))
                    .overflow_y_scroll()
                    .flex_shrink_0()
                    .p_4()
                    .flex()
                    .flex_col()
                    .gap_2()
                    .child(section("DESKTOP SHORTCUTS", t))
                    .children(
                        [
                            ("Cmd+N / Ctrl+N", "New session"),
                            ("Cmd+K / Ctrl+P", "Commands"),
                            ("Cmd+, / Ctrl+S", "Settings"),
                            ("Cmd+M", "Models"),
                            ("Cmd+O / Ctrl+O", "Sessions list"),
                            ("Cmd+B / Cmd+L", "Sessions / details"),
                            ("Cmd+I / Cmd+U", "Context / provider usage"),
                            ("Cmd+Shift+A", "Attach files"),
                            ("Ctrl+Space", "Dictation"),
                            ("Cmd+. / Ctrl+C", "Stop turn (Esc twice)"),
                            ("Ctrl+Enter / Alt+Enter", "Queue / interrupt"),
                            ("Cmd+S / Ctrl+S", "Save file"),
                            ("Ctrl+T / Ctrl+E", "Cycle effort / Logs"),
                            ("Cmd+Shift+G / Cmd+Shift+E", "Cycle agent / effort"),
                            ("Cmd+Shift+L", "Logs"),
                            ("Option+Up / Down", "Prompt history"),
                            ("PageUp / PageDown", "Scroll transcript"),
                            ("[ / ]", "Previous / next inspector tab"),
                            ("a", "Agent picker (outside the editor)"),
                            ("Cmd+Shift+T", "Dark / light theme"),
                            ("Ctrl+Tab / Ctrl+Shift+Tab", "Next / previous control"),
                            (
                                "Tab",
                                "Completion, or transcript navigation on an empty draft",
                            ),
                            ("Up / Down, Enter / Tab", "Select / insert completion"),
                            (
                                "j / k, Enter / Esc",
                                "Move / open / leave transcript navigation",
                            ),
                            (
                                "Ctrl+X c / z / ?",
                                "Context popover / update help / shortcuts",
                            ),
                        ]
                        .into_iter()
                        .map(|(key, label)| kv(key, label, t)),
                    )
                    .child(section("TERMINAL SHORTCUT REFERENCE", t)),
            );
        }
        if self.snapshot.form.is_some() {
            body = body.child(
                div()
                    .id("form-scroll")
                    .key_context("Form")
                    .flex_1()
                    .min_h_0()
                    .overflow_y_scroll()
                    .p_5()
                    .font_family("Menlo")
                    .child(self.form.clone()),
            );
        } else {
            let panel_scroll = panel_scroll_handle();
            let first_diff = (panel_scroll.offset().y.abs() / px(22.)) as usize;
            let visible_diff = visible_range(self.snapshot.panel_lines.len(), first_diff, 160, 8);
            body = body.child(
                div()
                    .id("panel-scroll")
                    .track_scroll(&panel_scroll)
                    .flex_1()
                    .min_h_0()
                    .overflow_y_scroll()
                    .p_4()
                    .flex()
                    .flex_col()
                    .gap_1()
                    .when(self.snapshot.panel_loading, |d| {
                        d.child(div().p_3().text_color(t.accent).child("Loading…"))
                    })
                    .child(
                        div()
                            .id("selection-details")
                            .flex_shrink_0()
                            .when(!self.snapshot.items.is_empty(), |d| {
                                d.max_h(px(160.))
                                    .overflow_y_scroll()
                                    .pb_3()
                                    .border_b_1()
                                    .border_color(t.border)
                            })
                            .when(markdown_details, |d| {
                                d.child(markdown::render(
                                    &self.snapshot.panel_lines.join("\n"),
                                    t,
                                    &format!("panel-{}", self.panel_identity),
                                    |block, key, t| self.selectable(block, key, t, cx),
                                ))
                            })
                            .children(
                                self.snapshot
                                    .panel_lines
                                    .iter()
                                    .enumerate()
                                    .filter(|(i, _)| visible_diff.contains(i))
                                    .filter(|_| !markdown_details)
                                    .map(|(i, line)| {
                                        let tone = self
                                            .snapshot
                                            .panel_tones
                                            .get(i)
                                            .map(String::as_str)
                                            .unwrap_or("");
                                        if matches!(tone, "" | "kv" | "label") {
                                            if let Some((key, value)) = line.split_once(": ") {
                                                let key = key.trim();
                                                if key.len() <= 64
                                                    && key.chars().all(|c| {
                                                        c.is_alphanumeric() || "_-.[] /".contains(c)
                                                    })
                                                {
                                                    return div()
                                                        .flex()
                                                        .gap_4()
                                                        .py_1()
                                                        .border_b_1()
                                                        .border_color(t.border.opacity(0.4))
                                                        .child(
                                                            div()
                                                                .w(relative(0.34))
                                                                .flex_shrink_0()
                                                                .text_size(px(11.))
                                                                .text_color(t.muted)
                                                                .child(key.to_owned()),
                                                        )
                                                        .child(
                                                            div()
                                                                .flex_1()
                                                                .min_w_0()
                                                                .text_size(px(12.))
                                                                .text_color(t.text)
                                                                .child(value.to_owned()),
                                                        );
                                                }
                                            }
                                        }
                                        let mut row = div()
                                            .text_size(px(12.))
                                            .line_height(px(21.))
                                            .text_color(match tone {
                                                "add" => t.diff_add,
                                                "del" => t.diff_remove,
                                                "hunk" => t.accent,
                                                "header" | "title" => t.text,
                                                _ => t.muted,
                                            })
                                            .when(tone == "add", |d| d.bg(t.diff_add_bg))
                                            .when(tone == "del", |d| d.bg(t.diff_remove_bg))
                                            .child(
                                                line.replace("terminal shell", "desktop client")
                                                    .replace("narrow terminals", "narrow windows"),
                                            );
                                        if matches!(tone, "header" | "title") {
                                            row = row.mt_3().font_weight(FontWeight::SEMIBOLD);
                                        }
                                        row
                                    }),
                            ),
                    )
                    .child(
                        div()
                            .id("picker-items")
                            .flex_1()
                            .min_h_0()
                            .overflow_y_scroll()
                            .track_scroll(&self.picker_scroll)
                            .flex()
                            .flex_col()
                            .gap_1()
                            .children(items),
                    ),
            );
        }
        if let Some(form) = &self.snapshot.form {
            body = body.child(div().p_4().border_t_1().border_color(t.border).flex().items_center().justify_between().child(div().text_size(px(11.)).text_color(t.muted).child(form.status.clone())).child(div().flex().gap_2()
                .when(!form.secret, |d| d.child(self.button("delete-form", "Delete / reset", json!({"type":"form_delete"}), cx)))
                .child(self.button("save-form","Save changes  ⌘S",json!({"type":"save","form":form.id,"body":self.form.read(cx).content,"revision":self.form_revision}),cx).bg(t.accent_bg).text_color(t.accent))));
        }
        if !self.snapshot.panel_hint.is_empty() {
            body = body.child(
                div()
                    .px_4()
                    .py_3()
                    .text_size(px(10.))
                    .text_color(t.muted)
                    .child(self.snapshot.panel_hint.clone()),
            );
        }
        div()
            .absolute()
            .inset_0()
            .bg(gpui::rgba(0x00000070))
            .flex()
            .items_center()
            .justify_center()
            .on_mouse_down(
                MouseButton::Left,
                cx.listener(|this, _, w, cx| this.dismiss(&Dismiss, w, cx)),
            )
            .child(
                div()
                    .id("panel")
                    .w(if nav.is_some() { px(960.) } else { px(760.) })
                    .max_w(relative(0.92))
                    .h(px(if self.snapshot.panel_format == "image" { if self.snapshot.items.is_empty() { 500. } else { 620. } } else { 660. }))
                    .max_h(relative(0.88))
                    .rounded(px(14.))
                    .relative()
                    .border_1()
                    .border_color(t.border)
                    .bg(t.surface)
                    .shadow_xl()
                    .overflow_hidden()
                    .flex()
                    .flex_col()
                    .on_mouse_down(MouseButton::Left, |_, _, cx| cx.stop_propagation())
                    .child(
                        div()
                            .px_5()
                            .py_4()
                            .border_b_1()
                            .border_color(t.border)
                            .flex()
                            .items_center()
                            .justify_between()
                            .child(
                                div()
                                    .flex()
                                    .items_center()
                                    .gap_3()
                                    .child(self.button(
                                        "panel-back",
                                        "←",
                                        json!({"type":"dismiss"}),
                                        cx,
                                    ))
                                    .child(
                                        div()
                                            .text_size(px(15.))
                                            .font_weight(FontWeight::SEMIBOLD)
                                            .child(self.snapshot.panel_title.clone()),
                                    ),
                            )
                            .child(div().flex().items_center().gap_2()
                                .when(!self.snapshot.panel_lines.is_empty(), |d| d.child(self.button("copy-panel", "Copy details", json!({"type":"copy_text","text":self.snapshot.panel_lines.join("\n")}), cx).tab_stop(true)))
                                .child(div().text_size(px(11.)).text_color(t.muted).child("esc"))),
                    )
                    .child(div().flex().flex_1().min_h_0().children(nav).child(body)),
            )
            .with_animation(SharedString::from(format!("panel-enter-{}", self.snapshot.panel_title)),
            Animation::new(std::time::Duration::from_millis(140)).with_easing(ease_in_out),
            |d, progress| d.opacity(0.7 + 0.3 * progress))
        .into_any_element()
    }
    pub(crate) fn prompt_view(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        let prompt = self.snapshot.prompt.as_ref().unwrap();
        let id = prompt.id.clone();
        let backdrop = div()
            .absolute()
            .inset_0()
            .bg(gpui::rgba(0x00000080))
            .flex()
            .items_center()
            .justify_center();
        let sheet = div().w(px(680.)).max_w(relative(0.92)).max_h(relative(0.88)).rounded(px(14.)).relative().border_1().border_color(t.border).bg(t.surface).shadow_xl().flex().flex_col()
                .child(div().px_6().pt_6().text_size(px(11.)).text_color(t.amber).font_weight(FontWeight::SEMIBOLD).child(if prompt.kind=="permission" { "YOUR APPROVAL IS NEEDED" } else { "A QUESTION FOR YOU" }))
                .child(div().px_6().pt_2().pb_4().text_size(px(22.)).font_weight(FontWeight::SEMIBOLD).child(if prompt.kind=="permission" { "Review this action" } else { "Choose how to proceed" }))
                .child(div().id("prompt-scroll").px_6().max_h(px(310.)).overflow_y_scroll().children(prompt.lines.iter().map(|line|div().py_1().text_size(px(13.)).line_height(px(21.)).child(line.clone()))))
                .when(prompt.kind=="question", |d| d.child(div().px_6().pt_4().flex().flex_col().gap_2()
                    .child(div().p_3().rounded(px(6.)).border_1().border_color(t.border).child(self.filter.clone()))
                    .child(self.button("custom-answer","Send answer",json!({"type":"answer","text":prompt.id,"value":self.filter.read(cx).content}),cx).bg(t.accent_bg).text_color(t.accent))))
                .child(div().p_6().flex().flex_col().gap_2().children(prompt.choices.iter().enumerate().map(|(i,choice)| {
                    let action=json!({"type":"answer","text":id,"value":choice.value});
                    if choice.disabled { div().px_4().py_3().rounded(px(6.)).text_color(t.muted).child(format!("{} · unavailable",choice.label)).into_any_element() }
                    else { self.button(("answer",i),choice.label.clone(),action,cx).justify_start().bg(if i==0 { t.accent_bg } else { t.raised }).text_color(if i==0 { t.accent } else { t.text }).into_any_element() }
                })));
        if self.snapshot.panel_title.is_empty() {
            backdrop.child(sheet).into_any_element()
        } else {
            // A panel-contained confirmation sheet tracks that panel instead of
            // dimming and blocking the whole desktop window.
            div()
                .relative()
                .flex_1()
                .min_h_0()
                .child(sheet)
                .into_any_element()
        }
    }
}

pub(crate) fn normalize_query(query: &str) -> String {
    query
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

pub(crate) fn ranked_filter<'a>(
    items: &'a [crate::bridge::Item],
    query: &str,
) -> Vec<&'a crate::bridge::Item> {
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    query.hash(&mut hasher);
    for item in items {
        item.label.hash(&mut hasher);
        item.detail.hash(&mut hasher);
        item.command.hash(&mut hasher);
    }
    let cache_key = hasher.finish();
    let cached = PICKER_FILTER_CACHE.with(|cache| {
        cache
            .borrow()
            .as_ref()
            .filter(|(key, _)| *key == cache_key)
            .map(|(_, indices)| indices.clone())
    });
    let indices = cached.unwrap_or_else(|| {
        if query.is_empty() {
            return (0..items.len()).collect();
        }
        let mut matches: Vec<(usize, usize)> = items
            .iter()
            .enumerate()
            .filter_map(|(index, item)| {
                let label = item.label.to_lowercase();
                let detail = item.detail.to_lowercase();
                let command = item.command.to_lowercase();
                let score = if label == query {
                    0
                } else if label.starts_with(query) {
                    1
                } else if label.split_whitespace().any(|word| word.starts_with(query)) {
                    2
                } else if label.contains(query) {
                    3
                } else if detail.contains(query) {
                    4
                } else if command.contains(query) {
                    5
                } else {
                    return None;
                };
                Some((score, index))
            })
            .collect();
        // Stable order preserves the host's group/order for equal relevance.
        matches.sort_by_key(|(score, _)| *score);
        matches.into_iter().map(|(_, index)| index).collect()
    });
    PICKER_FILTER_CACHE.with(|cache| *cache.borrow_mut() = Some((cache_key, indices.clone())));
    indices
        .into_iter()
        .filter_map(|index| items.get(index))
        .collect()
}

#[cfg(test)]
mod picker_tests {
    use super::{normalize_query, ranked_filter};
    use crate::bridge::Item;

    fn item(label: &str, detail: &str, command: &str) -> Item {
        Item {
            label: label.into(),
            detail: detail.into(),
            command: command.into(),
            ..Item::default()
        }
    }

    #[test]
    fn query_normalization_collapses_whitespace_and_case() {
        assert_eq!(normalize_query("  Open   File "), "open file");
    }

    #[test]
    fn picker_ranks_exact_prefix_word_and_substring_then_preserves_ties() {
        let items = vec![
            item("Files", "", ""),
            item("Find files", "", ""),
            item("Open", "files", ""),
            item("Recently opened", "", ""),
            item("Bookmarks", "", ""),
        ];
        let got = ranked_filter(&items, "files");
        assert_eq!(
            got.iter()
                .map(|item| item.label.as_str())
                .collect::<Vec<_>>(),
            ["Files", "Find files", "Open"]
        );
    }
}
fn section(label: &str, t: Theme) -> AnyElement {
    div()
        .mt_3()
        .child(primitives::section_label(label.to_uppercase(), t))
        .into_any_element()
}
fn kv(key: &str, value: &str, t: Theme) -> AnyElement {
    div()
        .flex()
        .justify_between()
        .gap_3()
        .text_size(px(12.))
        .child(div().text_color(t.muted).child(key.to_string()))
        .child(div().min_w_0().text_color(t.text).child(value.to_string()))
        .into_any_element()
}

struct ControlHint(SharedString);
impl Render for ControlHint {
    fn render(&mut self, _: &mut Window, _: &mut Context<Self>) -> impl IntoElement {
        div()
            .px_3()
            .py_2()
            .rounded(px(6.))
            .bg(rgb(0x2c2e33))
            .text_color(rgb(0xf0f0f2))
            .text_size(px(12.))
            .shadow_md()
            .child(self.0.clone())
    }
}
