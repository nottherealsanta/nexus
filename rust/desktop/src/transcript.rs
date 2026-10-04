//! Virtualized transcript blocks retain every host-projected detail action.
use super::*;
impl Desktop {
    pub(crate) fn selectable(
        &self,
        block: &markdown::Block,
        key: SharedString,
        t: Theme,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let mut cache = self.text_cache.borrow_mut();
        if cache.len() >= 512 && !cache.contains_key(key.as_ref()) {
            if let Some(key) = cache.keys().next().cloned() {
                cache.remove(&key);
            }
        }
        let input = cache
            .entry(key.to_string())
            .or_insert_with(|| {
                cx.new(|cx| {
                    let mut input = Input::new("", cx);
                    input.readonly = true;
                    input
                })
            })
            .clone();
        input.update(cx, |input, cx| {
            if input.content != block.text {
                input.set(block.text.clone(), cx);
            }
            input.font_size = if block.heading > 0 {
                24. - block.heading as f32 * 2.
            } else if block.code.is_some() {
                12.
            } else {
                15.
            };
            input.foreground = t.text;
            input.accent = t.accent;
            input.highlights = markdown::highlights(block, t);
            input.links = block.links.clone();
        });
        input.into_any_element()
    }
    pub(crate) fn empty_state(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        div().size_full().flex().flex_col().items_center().justify_center().gap_4().p_6()
            .child(div().w_full().max_w(px(740.)).flex().flex_col().gap_4()
                .child(div().flex().items_center().gap_3().child(icons::icon("terminal", t.muted)).child(div().text_size(px(22.)).font_weight(FontWeight::MEDIUM).child("New conversation")))
                .child(div().text_size(px(14.)).text_color(t.muted).child("Describe a task, attach context, or choose a starting point."))
                .child(div().mt_2().flex().flex_col().gap_2().children([
                    ("Inspect workspace", "Explain the architecture of this project and the important entry points."),
                    ("Review changes", "Review the current workspace changes for correctness."),
                    ("Plan implementation", "Help me plan a new feature for this project.")
                ].into_iter().enumerate().map(|(i,(label,prompt))| {
                    self.button(("starter",i),label,json!({"type":"ui_trace","lines":[]}),cx).justify_start().py_3().border_1().border_color(t.border).bg(t.surface)
                        .on_click(cx.listener(move |this,_,w,cx| { this.composer.update(cx,|input,cx|input.set(prompt.into(),cx)); w.focus(&this.composer.read(cx).focus); }))
                })))
                .children(self.snapshot.blocks.iter().filter(|b| b.kind=="context_header").map(|b|self.context_header(b,cx))))
            .into_any_element()
    }
    pub(crate) fn context_header(&self, block: &Content, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        div().flex().flex_wrap().items_center().gap_2().py_3()
            .child(div().text_size(px(10.)).text_color(t.muted).mr_2().child("CONTEXT"))
            .children(block.members.iter().enumerate().map(|(i,chip)| {
                let count=chip.counts.iter().map(|n|n.to_string()).collect::<Vec<_>>().join(" / ");
                self.button(("context-chip",i),format!("{}{}",chip.title,if count.is_empty(){String::new()}else{format!("  {count}")}),json!({"type":"context_header","key":chip.operation.as_ref().and_then(|op|op["key"].as_str()).unwrap_or("")}),cx)
                    .py_1().px_2().text_size(px(11.)).bg(t.surface).border_1().border_color(t.border)
            })).into_any_element()
    }
    pub(crate) fn render_block(
        &self,
        block: &Content,
        _w: &mut Window,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        self.render_block_depth(block, _w, cx, 0)
    }
    fn render_block_depth(
        &self,
        block: &Content,
        _w: &mut Window,
        cx: &mut Context<Self>,
        depth: usize,
    ) -> AnyElement {
        let t = self.theme();
        let mut outer = div()
            .w_full()
            .flex()
            .justify_center()
            .px(px(if depth == 0 { 24. } else { 0. }))
            .pt(px(if depth == 0 && block.gap > 0 { 16. } else { 2. }))
            .pb_2();
        let copy = block.text.clone();
        let content = match block.kind.as_str() {
            "context_header" => self.context_header(block, cx),
            "user" => {
                let op = block.operation.clone();
                div()
                    .id(SharedString::from(block.id.clone()))
                    .focusable()
                    .tab_stop(
                        self.snapshot.panel_title.is_empty() && self.snapshot.prompt.is_none(),
                    )
                    .focus(move |s| s.bg(t.raised))
                    .bg(t.raised)
                    .rounded(px(4.))
                    .max_w(relative(0.85))
                    .ml_auto()
                    .px_4()
                    .py_3()
                    .flex()
                    .flex_col()
                    .gap_2()
                    .child(
                        div()
                            .flex()
                            .items_center()
                            .justify_between()
                            .child(
                                div()
                                    .text_size(px(10.))
                                    .font_weight(FontWeight::SEMIBOLD)
                                    .text_color(t.accent)
                                    .child(if self.snapshot.agent_page.is_empty() {
                                        "YOU"
                                    } else {
                                        "TASK"
                                    }),
                            )
                            .child(div().text_size(px(10.)).text_color(t.muted).child(
                                if block.number > 0 {
                                    format!("#{:02}", block.number)
                                } else {
                                    "TASK".into()
                                },
                            )),
                    )
                    .child(
                        div()
                            .text_size(px(15.))
                            .line_height(px(25.))
                            .child(block.title.clone()),
                    )
                    .when(!block.text.is_empty(), |d| {
                        d.child(
                            div()
                                .text_size(px(14.))
                                .line_height(px(24.))
                                .child(block.text.clone()),
                        )
                    })
                    .when(!block.chips.is_empty(), |d| {
                        d.child(div().flex().flex_wrap().gap_2().children(
                            block.chips.iter().enumerate().map(|(i, chip)| {
                                self.button(
                                    (SharedString::from(format!("attachment-{}", block.id)), i),
                                    chip.clone(),
                                    json!({"type":"operation","operation":block.chip_operation}),
                                    cx,
                                )
                                .px_2()
                                .py_1()
                                .text_size(px(11.))
                                .bg(t.accent_bg)
                                .text_color(t.accent)
                            }),
                        ))
                    })
                    .on_click(cx.listener(move |this, _, w, cx| {
                        if let Some(op) = &op {
                            this.dispatch(json!({"type":"operation","operation":op}), w, cx);
                        }
                    }))
                    .into_any_element()
            }
            "markdown" => div()
                .flex()
                .flex_col()
                .gap_2()
                .py_2()
                .child(markdown::render(
                    &block.text,
                    t,
                    &block.id,
                    |block, key, t| self.selectable(block, key, t, cx),
                ))
                .child(
                    div()
                        .id(SharedString::from(format!("copy-{}", block.id)))
                        .focusable()
                        .tab_stop(
                            self.snapshot.panel_title.is_empty() && self.snapshot.prompt.is_none(),
                        )
                        .focus(move |s| s.text_color(t.accent))
                        .text_size(px(10.))
                        .text_color(t.muted)
                        .cursor(CursorStyle::Arrow)
                        .child("Copy reply")
                        .on_click(move |_, _, cx| {
                            cx.write_to_clipboard(ClipboardItem::new_string(copy.clone()))
                        }),
                )
                .into_any_element(),
            "summary" => div()
                .py_3()
                .border_t_1()
                .border_color(t.border)
                .text_size(px(11.))
                .text_color(t.muted)
                .child(block.text.clone())
                .into_any_element(),
            "diff" => {
                let mut rows = vec![];
                for (old_no, old, new_no, new, kind) in &block.diff_rows {
                    rows.push(
                        div()
                            .flex()
                            .gap_3()
                            .text_size(px(11.))
                            .font_family("Menlo")
                            .line_height(px(21.))
                            .bg(if kind == "add" {
                                t.accent_bg
                            } else {
                                t.sidebar
                            })
                            .child(div().w(px(32.)).text_color(t.muted).child(if *old_no == 0 {
                                String::new()
                            } else {
                                old_no.to_string()
                            }))
                            .child(
                                div()
                                    .w_1_2()
                                    .text_color(if kind == "del" { t.red } else { t.text })
                                    .child(old.clone()),
                            )
                            .child(div().w(px(32.)).text_color(t.muted).child(if *new_no == 0 {
                                String::new()
                            } else {
                                new_no.to_string()
                            }))
                            .child(
                                div()
                                    .w_1_2()
                                    .text_color(if kind == "add" { t.green } else { t.text })
                                    .child(new.clone()),
                            ),
                    );
                }
                div()
                    .rounded(px(3.))
                    .border_1()
                    .border_color(t.border)
                    .overflow_hidden()
                    .child(div().px_4().py_3().bg(t.surface).child(block.path.clone()))
                    .child(
                        div()
                            .id(SharedString::from(format!("diff-{}", block.id)))
                            .overflow_x_scroll()
                            .p_3()
                            .children(rows),
                    )
                    .into_any_element()
            }
            "thought" | "tool" | "task" | "explored" | "tool_group" => {
                let op = block.operation.clone();
                let mut card = div()
                    .id(SharedString::from(block.id.clone()))
                    .flex()
                    .flex_col()
                    .when(depth > 0, |d| d.ml_3().border_l_1().border_color(t.border))
                    .child(
                        div()
                            .id(SharedString::from(format!("heading-{}", block.id)))
                            .focusable()
                            .tab_stop(
                                self.snapshot.panel_title.is_empty()
                                    && self.snapshot.prompt.is_none(),
                            )
                            .focus(move |s| s.bg(t.raised).text_color(t.accent))
                            .px_2()
                            .py_2()
                            .flex()
                            .items_center()
                            .gap_2()
                            .text_color(t.muted)
                            .child(icons::icon(
                                if block.collapsed {
                                    "chevron-right"
                                } else {
                                    "chevron-down"
                                },
                                t.muted,
                            ))
                            .cursor(CursorStyle::Arrow)
                            .hover(move |s| s.bg(t.raised))
                            .child(icons::icon(
                                if block.kind == "thought" {
                                    "brain"
                                } else if block.kind == "task" {
                                    "network"
                                } else if block.kind == "error" {
                                    "triangle-alert"
                                } else {
                                    "terminal"
                                },
                                if block.kind == "error" {
                                    t.red
                                } else {
                                    t.muted
                                },
                            ))
                            .child(
                                div()
                                    .flex_1()
                                    .min_w_0()
                                    .font_weight(FontWeight::MEDIUM)
                                    .text_size(px(12.))
                                    .child(if block.title.is_empty() {
                                        if block.heading.is_empty() {
                                            block
                                                .text
                                                .lines()
                                                .next()
                                                .unwrap_or("")
                                                .replace("\u{e000}", "●")
                                        } else {
                                            block.heading.clone()
                                        }
                                    } else {
                                        block.title.clone()
                                    }),
                            )
                            .when(!block.status.is_empty(), |d| {
                                d.child(
                                    div()
                                        .text_size(px(10.))
                                        .text_color(t.muted)
                                        .child(block.status.clone()),
                                )
                            })
                            .on_click(cx.listener(move |this, _, w, cx| {
                                if let Some(op) = &op {
                                    this.dispatch(
                                        json!({"type":"operation","operation":op}),
                                        w,
                                        cx,
                                    );
                                }
                            })),
                    );
                if !block.text.is_empty() && !block.title.is_empty() {
                    card = card.child(
                        div()
                            .px_4()
                            .pb_3()
                            .text_size(px(12.))
                            .line_height(px(21.))
                            .text_color(if block.kind == "error" {
                                t.red
                            } else {
                                t.muted
                            })
                            .child(block.text.clone()),
                    );
                }
                if !block.detail.is_empty() {
                    card = card.child(
                        div()
                            .px_4()
                            .pb_4()
                            .text_size(px(12.))
                            .line_height(px(21.))
                            .font_family("Menlo")
                            .child(block.detail.clone()),
                    );
                }
                for member in &block.members {
                    card = card.child(self.render_block_depth(member, _w, cx, depth + 1));
                }
                if let Some(op) = &block.output_operation {
                    card = card.child(self.button(
                        SharedString::from(format!("output-{}", block.id)),
                        "Show full output",
                        json!({"type":"operation","operation":op}),
                        cx,
                    ));
                }
                card.into_any_element()
            }
            _ => div()
                .flex()
                .flex_col()
                .gap_2()
                .py_2()
                .when(!block.title.is_empty(), |d| {
                    d.child(
                        div()
                            .font_weight(FontWeight::SEMIBOLD)
                            .child(block.title.clone()),
                    )
                })
                .child(
                    div()
                        .line_height(px(23.))
                        .text_size(px(13.))
                        .text_color(t.muted)
                        .child(block.text.clone()),
                )
                .when(!block.detail.is_empty(), |d| d.child(block.detail.clone()))
                .children(
                    block
                        .members
                        .iter()
                        .map(|m| self.render_block_depth(m, _w, cx, depth + 1)),
                )
                .when(block.operation.is_some(), |d| {
                    d.child(self.button(
                        SharedString::from(format!("open-{}", block.id)),
                        "Inspect",
                        json!({"type":"operation","operation":block.operation}),
                        cx,
                    ))
                })
                .into_any_element(),
        };
        outer = outer.child(
            div()
                .w_full()
                .max_w(px(920.))
                .when(block.kind == "user", |d| d.flex().justify_end())
                .child(content),
        );
        outer.into_any_element()
    }
}
