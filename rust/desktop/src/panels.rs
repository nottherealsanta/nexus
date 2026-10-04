//! Desktop chrome, settings, pickers and approval sheets share host actions.
use super::*;
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
            .hover(move |s| s.bg(t.raised).text_color(t.text))
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
            .focus(move |s| s.bg(t.raised).text_color(t.accent))
            .when_some(symbol, |d, name| d.child(icons::icon(name, t.muted)))
            .when(!caption.is_empty(), |d| d.child(caption.to_owned()))
            .when(dispatch, |d| {
                d.on_click(cx.listener(move |this, _, w, cx| {
                    cx.stop_propagation();
                    this.dispatch(action.clone(), w, cx);
                }))
            })
    }
    pub(crate) fn topbar(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        div()
            .h(px(40.))
            .flex_shrink_0()
            .flex()
            .items_center()
            .bg(t.sidebar)
            .border_b_1()
            .border_color(t.border)
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
            .border_color(t.border);
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
    pub(crate) fn sidebar(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        let query = self.search.read(cx).content.to_lowercase();
        let mut cards = vec![];
        let mut group = String::new();
        for (i, s) in self.snapshot.sessions.iter().enumerate().filter(|(_, s)| {
            format!("{} {} {}", s.title, s.id, s.workspace)
                .to_lowercase()
                .contains(&query)
        }) {
            if s.group != group {
                group = s.group.clone();
                cards.push(
                    div()
                        .mt_5()
                        .mb_2()
                        .px_3()
                        .text_size(px(10.))
                        .font_weight(FontWeight::SEMIBOLD)
                        .text_color(t.muted)
                        .child(group.to_uppercase())
                        .into_any_element(),
                );
            }
            let id = s.id.clone();
            let workspace = s.workspace.clone();
            cards.push(div().id(("session", i)).focusable().tab_stop(self.snapshot.panel_title.is_empty() && self.snapshot.prompt.is_none()).focus(move |s| s.bg(t.raised)).flex().flex_col().gap_1().px_3().py_2().rounded(px(6.)).mb_1().cursor(CursorStyle::Arrow)
                .border_l(px(2.)).border_color(if s.active { t.accent } else { t.sidebar }).bg(if s.active { t.raised } else { t.sidebar }).hover(move |style| style.bg(t.raised))
                .child(div().flex().items_center().gap_2().child(icons::icon(if s.state == "running" { "circle-dot" } else if s.state == "done" { "check" } else { "message-square" }, if s.active { t.accent } else { t.muted }))
                    .child(div().flex_1().min_w_0().font_weight(if s.active { FontWeight::SEMIBOLD } else { FontWeight::NORMAL }).truncate().child(s.title.clone()))
                    .child(self.button(("session-more",i), "···", json!({"type":"session_actions","text":s.id,"workspace":s.workspace}), cx).px_1().py_0()))
                .child(div().ml_4().text_size(px(11.)).text_color(t.muted).truncate().child(if s.sub.is_empty() { s.status.clone() } else { s.sub.clone() }))
                .on_click(cx.listener(move |this, _, w, cx| this.dispatch(json!({"type":"session_open","text":id,"workspace":workspace}), w, cx))).into_any_element());
        }
        div()
            .w(px(252.))
            .flex_shrink_0()
            .h_full()
            .flex()
            .flex_col()
            .bg(t.sidebar)
            .border_r_1()
            .border_color(t.border)
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
                            .child(self.search.clone()),
                    ),
            )
            .child(
                div()
                    .id("session-list")
                    .flex_1()
                    .min_h_0()
                    .overflow_y_scroll()
                    .px_3()
                    .children(cards),
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
            .border_color(t.border)
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
        let used = self.snapshot.context_used.unwrap_or(0);
        let total = self.snapshot.context_window.unwrap_or(1).max(1);
        let percent = ((used as f64 / total as f64) * 100.).min(100.);
        div()
            .flex_shrink_0()
            .px_6()
            .pb_4()
            .pt_3()
            .flex()
            .flex_col()
            .gap_3()
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
                    .rounded(px(10.))
                    .child(icons::corner(t))
                    .border_1()
                    .border_color(t.border)
                    .bg(t.surface)
                    .flex()
                    .flex_col()
                    .when(!self.snapshot.attachment_lines.is_empty(), |d| {
                        d.child(
                            div().px_4().pt_3().flex().flex_wrap().gap_2().children(
                                self.snapshot.attachment_lines.iter().enumerate().map(
                                    |(i, line)| {
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
                                    },
                                ),
                            ),
                        )
                    })
                    .child(
                        div()
                            .id("composer-scroll")
                            .min_h(px(56.))
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
                            .child(
                                div()
                                    .flex()
                                    .items_center()
                                    .flex_wrap()
                                    .gap_2()
                                    .child(self.composer_choices(cx))
                                    .child(if self.snapshot.status == "running" {
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
                                        .bg(t.accent_bg)
                                        .rounded(px(8.))
                                        .text_color(t.accent)
                                        .on_click(cx.listener(|this, _, w, cx| this.submit(w, cx)))
                                    }),
                            ),
                    ),
            )
            .child(
                div()
                    .flex()
                    .flex_wrap()
                    .items_center()
                    .justify_between()
                    .gap_2()
                    .child(
                        div()
                            .text_size(px(10.))
                            .text_color(t.muted)
                            .child("Shift+Enter for a new line"),
                    )
                    .child(
                        self.button(
                            "context-usage",
                            if self.snapshot.context_used.is_some() {
                                format!("{:.1}k · {:.0}% context", used as f64 / 1000., percent)
                            } else {
                                "Context unavailable".into()
                            },
                            json!({"type":"context_popover"}),
                            cx,
                        )
                        .py_0()
                        .px_1(),
                    ),
            )
            .child(
                div()
                    .h(px(2.))
                    .w(px(64.))
                    .ml_auto()
                    .rounded_full()
                    .bg(t.border)
                    .child(
                        div()
                            .h_full()
                            .w(relative((percent / 100.) as f32))
                            .bg(t.accent),
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
                    format!("Agent: {} ▾", self.snapshot.agent),
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
                    format!("Model: {} ▾", self.snapshot.model),
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
                    format!("Effort: {} ▾", self.snapshot.effort),
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
        let query = self.filter.read(cx).content.to_lowercase();
        let mut items = vec![];
        let mut group = String::new();
        for (i, item) in self
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
                        }),
                )
                .on_click(cx.listener(move |this, _, w, cx| this.dispatch(action.clone(), w, cx)));
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
        if !self.snapshot.items.is_empty() {
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
        if let Some(image) = &self.image_preview {
            body = body.child(
                div().p_4().flex().justify_center().child(
                    img(image.clone())
                        .max_w(relative(1.))
                        .h(px(320.))
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
                            ("Cmd+N", "New session"),
                            ("Cmd+K", "Commands"),
                            ("Cmd+,", "Settings"),
                            ("Cmd+M", "Models"),
                            ("Cmd+B / Cmd+L", "Sessions / details"),
                            ("Cmd+I / Cmd+U", "Context / provider usage"),
                            ("Cmd+Shift+A", "Attach files"),
                            ("Ctrl+Space", "Dictation"),
                            ("Cmd+.", "Stop turn"),
                            ("Ctrl+Enter / Alt+Enter", "Queue / interrupt"),
                            ("Cmd+S", "Save file"),
                            ("Cmd+Shift+G / Cmd+Shift+E", "Cycle agent / effort"),
                            ("Cmd+Shift+L", "Logs"),
                            ("Option+Up / Down", "Prompt history"),
                            ("Cmd+Shift+T", "Dark / light theme"),
                            ("Tab / Shift+Tab", "Next / previous control"),
                            ("Up / Down, Enter / Tab", "Select / insert completion"),
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
                    .flex_1()
                    .min_h_0()
                    .overflow_y_scroll()
                    .p_5()
                    .font_family("Menlo")
                    .child(self.form.clone()),
            );
        } else {
            body = body.child(
                div()
                    .id("panel-scroll")
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
                                    .filter(|_| !markdown_details)
                                    .enumerate()
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
                                                "add" => t.green,
                                                "del" => t.red,
                                                "hunk" => t.accent,
                                                "header" | "title" => t.text,
                                                _ => t.muted,
                                            })
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
                    .h(px(660.))
                    .max_h(relative(0.88))
                    .rounded(px(10.))
                    .relative()
                    .child(icons::corner(t))
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
            .into_any_element()
    }
    pub(crate) fn prompt_view(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        let prompt = self.snapshot.prompt.as_ref().unwrap();
        let id = prompt.id.clone();
        div().absolute().inset_0().bg(gpui::rgba(0x00000080)).flex().items_center().justify_center()
            .child(div().w(px(680.)).max_w(relative(0.92)).max_h(relative(0.88)).rounded(px(10.)).relative().child(icons::corner(t)).border_1().border_color(t.border).bg(t.surface).shadow_xl().flex().flex_col()
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
                }))))
            .into_any_element()
    }
}
fn section(label: &str, t: Theme) -> AnyElement {
    div()
        .mt_4()
        .mb_2()
        .text_size(px(10.))
        .font_weight(FontWeight::SEMIBOLD)
        .text_color(t.muted)
        .child(label.to_string())
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
