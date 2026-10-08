//! Virtualized transcript blocks retain every host-projected detail action.
use super::*;
use crate::panels::ControlHint;
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
                if block.heading <= 2 {
                    theme::size::TITLE
                } else {
                    theme::size::BODY
                }
            } else if block.code.is_some() {
                theme::size::SMALL
            } else {
                theme::size::BODY
            };
            input.foreground = t.text;
            input.accent = t.accent;
            input.highlights = markdown::highlights(block, t);
            input.links = block.links.clone();
        });
        input.into_any_element()
    }

    pub(crate) fn prepare_markdown_cache(&mut self) {
        let sources: Vec<String> = self
            .snapshot
            .blocks
            .iter()
            .filter(|block| block.kind == "markdown")
            .map(|block| block.text.clone())
            .collect();
        self.parsed_docs
            .retain_sources(sources.iter().map(String::as_str));
        for source in sources {
            self.parsed_docs.prepare(&source);
        }
    }
    pub(crate) fn image_thumbnail(
        &self,
        image: &bridge::InlineImage,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let t = self.theme();
        let operation = image.operation.clone();
        div()
            .id(SharedString::from(format!("image-{}", image.id)))
            .focusable()
            .tab_stop(self.snapshot.panel_title.is_empty() && self.snapshot.prompt.is_none())
            .rounded(px(crate::theme::radius::CONTROL))
            .overflow_hidden()
            .border_1()
            .border_color(t.border)
            .hover(move |s| s.border_color(t.accent))
            .focus(move |s| s.border_color(t.accent))
            .cursor(CursorStyle::Arrow)
            .flex()
            .flex_col()
            .w(px(112.))
            .when_some(self.inline_images.get(&image.id), |d, data| {
                d.child(
                    img(data.clone())
                        .w_full()
                        .h(px(72.))
                        .object_fit(ObjectFit::Contain)
                        .bg(t.raised),
                )
            })
            .child(
                div()
                    .px_2()
                    .py_1()
                    .text_size(px(crate::theme::size::CAPTION))
                    .text_color(t.muted)
                    .truncate()
                    .child(image.label.clone()),
            )
            .on_click(cx.listener(move |this, _, w, cx| {
                cx.stop_propagation();
                if let Some(operation) = &operation {
                    this.dispatch(json!({"type":"operation", "operation":operation}), w, cx);
                }
            }))
            .with_animation(
                SharedString::from(format!("thumbnail-enter-{}", image.id)),
                Animation::new(std::time::Duration::from_millis(160)).with_easing(ease_in_out),
                |d, progress| d.opacity(0.6 + 0.4 * progress),
            )
            .into_any_element()
    }
    pub(crate) fn empty_state(&self, cx: &mut Context<Self>) -> AnyElement {
        let t = self.theme();
        div().size_full().flex().flex_col().items_center().justify_center().gap_4().p_6()
            .child(div().w_full().max_w(px(740.)).flex().flex_col().gap_4()
                .child(div().flex().items_center().gap_3().child(icons::icon("terminal", t.muted)).child(div().text_size(px(crate::theme::size::TITLE)).font_weight(FontWeight::MEDIUM).child("New conversation")))
                .child(div().text_size(px(crate::theme::size::BODY)).text_color(t.muted).child("Describe a task, attach context, or choose a starting point."))
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
        // The TUI's context rows: an agent-coloured rail, bold titles in their own
        // colour, `[counts]` and status muted. Empty sources arrive neutral.
        let t = self.theme();
        let rail = t.resolve(&block.color, t.blue);
        div()
            .flex()
            .flex_wrap()
            .items_center()
            .gap_x(px(theme::space::LG))
            .gap_y(px(theme::space::XS))
            .mt_3()
            .py_1()
            .pl_3()
            .border_l_2()
            .border_color(rail)
            .children(block.members.iter().enumerate().map(|(i, chip)| {
                let tone = if chip.color.is_empty() { rail } else { t.resolve(&chip.color, rail) };
                let counts = (!chip.counts.is_empty()).then(|| {
                    format!(
                        "[{}]",
                        chip.counts.iter().map(|n| n.to_string()).collect::<Vec<_>>().join(" ")
                    )
                });
                let status = chip.status.replace(" tokens", "");
                let key = chip
                    .operation
                    .as_ref()
                    .and_then(|op| op["key"].as_str())
                    .unwrap_or("")
                    .to_string();
                div()
                    .id(("context-chip", i))
                    .debug_selector(|| "context-chip".into())
                    .focusable()
                    .tab_stop(self.snapshot.panel_title.is_empty() && self.snapshot.prompt.is_none())
                    .flex()
                    .items_baseline()
                    .gap_1()
                    .px_1()
                    .rounded(px(theme::radius::CHIP))
                    .cursor(CursorStyle::Arrow)
                    .hover(move |s| s.bg(t.hover))
                    .focus(move |s| s.bg(t.raised))
                    .text_size(px(theme::size::SMALL))
                    .child(
                        div()
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(tone)
                            .child(chip.title.clone()),
                    )
                    .when_some(counts, |d, counts| d.child(div().text_color(t.muted).child(counts)))
                    .when(!status.is_empty(), |d| {
                        d.child(div().text_color(t.text_tertiary).child(status))
                    })
                    .on_click(cx.listener(move |this, _, w, cx| {
                        this.dispatch(json!({"type":"context_header","key":key}), w, cx)
                    }))
            }))
            .into_any_element()
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
            .px(px(if depth == 0 { theme::space::XL } else { 0. }))
            .pt(px(if depth == 0 && block.gap > 0 {
                if block.kind == "user" {
                    theme::space::XL
                } else {
                    theme::space::XS
                }
            } else {
                0.
            }))
            .pb(px(if depth == 0 { theme::space::XS } else { 0. }));
        let copy = block.text.clone();
        let content = match block.kind.as_str() {
            "context_header" => self.context_header(block, cx),
            "user" => {
                let op = block.operation.clone();
                let user_text = if block.title.is_empty() {
                    block.text.clone()
                } else if block.text.is_empty() {
                    block.title.clone()
                } else {
                    format!("{}\n{}", block.title, block.text)
                };
                let user_copy = user_text.clone();
                let user_group = SharedString::from(format!("user-{}", block.id));
                div()
                    .id(SharedString::from(block.id.clone()))
                    .group(user_group.clone())
                    .focusable()
                    .tab_stop(
                        self.snapshot.panel_title.is_empty() && self.snapshot.prompt.is_none(),
                    )
                    .focus(move |s| s.bg(t.hover))
                    .bg(t.surface)
                    .debug_selector(|| "user-card".into())
                    .rounded(px(crate::theme::radius::CHIP))
                    .w_full()
                    .border_l_2()
                    .border_color(t.resolve(&block.color, t.blue))
                    .px_4()
                    .py_2()
                    .flex()
                    .flex_col()
                    .gap_2()
                    .when(!user_text.is_empty(), |d| {
                        d.child(
                            div()
                                .flex()
                                .items_start()
                                .gap_2()
                                .when(block.operation.is_some(), |d| {
                                    d.child(icons::icon(
                                        if block.collapsed {
                                            "chevron-right"
                                        } else {
                                            "chevron-down"
                                        },
                                        t.text_tertiary,
                                    ))
                                })
                                .child(div().flex_1().min_w_0().child(self.selectable(
                                    &markdown::Block {
                                        text: user_text,
                                        ..Default::default()
                                    },
                                    SharedString::from(format!("user-text-{}", block.id)),
                                    t,
                                    cx,
                                )))
                                .child(
                                    div()
                                        .id(SharedString::from(format!("user-copy-{}", block.id)))
                                        .debug_selector(|| "user-copy".into())
                                        .focusable()
                                        .tab_stop(
                                            self.snapshot.panel_title.is_empty()
                                                && self.snapshot.prompt.is_none(),
                                        )
                                        .flex_shrink_0()
                                        .opacity(0.)
                                        .group_hover(user_group, |s| s.opacity(1.))
                                        .focus(move |s| {
                                            s.opacity(1.).bg(t.hover).text_color(t.accent)
                                        })
                                        .rounded(px(crate::theme::radius::CHIP))
                                        .px_2()
                                        .text_size(px(crate::theme::size::CAPTION))
                                        .text_color(t.muted)
                                        .hover(move |s| s.bg(t.raised).text_color(t.text))
                                        .cursor(CursorStyle::Arrow)
                                        .tooltip(|_, cx| {
                                            cx.new(|_| ControlHint("Copy message".into())).into()
                                        })
                                        .child("Copy")
                                        .on_click(move |_, _, cx| {
                                            // The card's own click toggles collapse.
                                            cx.stop_propagation();
                                            cx.write_to_clipboard(ClipboardItem::new_string(
                                                user_copy.clone(),
                                            ))
                                        }),
                                )
                                .when(block.number > 0, |d| {
                                    d.child(
                                        div()
                                            .flex_shrink_0()
                                            .text_size(px(theme::size::CAPTION))
                                            .text_color(t.text_tertiary)
                                            .child(format!("#{}", block.number)),
                                    )
                                }),
                        )
                    })
                    .child(
                        div().flex().flex_wrap().gap_2().children(
                            self.snapshot
                                .inline_images
                                .iter()
                                .filter(|image| image.message == block.id.trim_end_matches(":user"))
                                .map(|image| self.image_thumbnail(image, cx)),
                        ),
                    )
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
                                .text_size(px(crate::theme::size::CAPTION))
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
                .id(SharedString::from(format!("reply-card-{}", block.id)))
                .debug_selector(|| "reply-card".into())
                .relative()
                .group(SharedString::from(format!("reply-{}", block.id)))
                .pr(px(theme::space::XL * 2.))
                .flex()
                .flex_col()
                .gap_1()
                .py_1()
                .child({
                    let parsed = self.parsed_docs.get(&block.text);
                    if let Some(parsed) = parsed {
                        markdown::render_parsed(&parsed.blocks, t, &block.id, |block, key, t| {
                            self.selectable(block, key, t, cx)
                        })
                    } else {
                        markdown::render(&block.text, t, &block.id, |block, key, t| {
                            self.selectable(block, key, t, cx)
                        })
                    }
                })
                .child(
                    div()
                        .id(SharedString::from(format!("copy-{}", block.id)))
                        .debug_selector(|| "reply-copy".into())
                        .focusable()
                        .tab_stop(
                            self.snapshot.panel_title.is_empty() && self.snapshot.prompt.is_none(),
                        )
                        .absolute()
                        .top_0()
                        .right_0()
                        .opacity(0.)
                        .group_hover(SharedString::from(format!("reply-{}", block.id)), |s| {
                            s.opacity(1.)
                        })
                        .focus(move |s| s.opacity(1.).bg(t.hover).text_color(t.accent))
                        .map(|mut d| {
                            d.style().align_self = Some(AlignSelf::FlexStart);
                            d
                        })
                        .rounded(px(crate::theme::radius::CHIP))
                        .px_2()
                        .py_1()
                        .text_size(px(crate::theme::size::CAPTION))
                        .text_color(t.muted)
                        .hover(move |s| s.bg(t.raised).text_color(t.text))
                        .cursor(CursorStyle::Arrow)
                        .tooltip(|_, cx| cx.new(|_| ControlHint("Copy reply".into())).into())
                        .child("Copy")
                        .on_click(move |_, _, cx| {
                            cx.write_to_clipboard(ClipboardItem::new_string(copy.clone()))
                        }),
                )
                .into_any_element(),
            "summary" => div()
                .flex()
                .justify_end()
                .pt_1()
                .text_size(px(crate::theme::size::CAPTION))
                .text_color(t.text_tertiary)
                .child(block.text.clone())
                .into_any_element(),
            "agent" => div()
                .flex()
                .items_center()
                .gap_2()
                .py_1()
                .text_color(t.resolve(&block.color, t.blue))
                .child("◆")
                .child(div().font_weight(FontWeight::SEMIBOLD).child(block.title.clone()))
                .into_any_element(),
            "hints" => div()
                .flex()
                .flex_col()
                .items_center()
                .gap_1()
                .py_4()
                .text_size(px(crate::theme::size::SMALL))
                .children(block.text.lines().map(|row| {
                    let (keys, text) = row.split_once('\t').unwrap_or(("", row));
                    div()
                        .flex()
                        .gap_3()
                        .child(
                            div()
                                .w(px(140.))
                                .text_right()
                                .font_weight(FontWeight::SEMIBOLD)
                                .text_color(t.muted)
                                .child(keys.trim().to_string()),
                        )
                        .child(div().w(px(220.)).text_color(t.text_tertiary).child(text.trim().to_string()))
                }))
                .into_any_element(),
            "context" => div()
                .flex()
                .flex_col()
                .gap_1()
                .py_1()
                .child(
                    div()
                        .flex()
                        .items_center()
                        .gap_2()
                        .child(
                            div()
                                .px_2()
                                .rounded(px(theme::radius::CHIP))
                                .bg(t.resolve(&block.color, t.text_tertiary))
                                .text_color(t.background)
                                .text_size(px(crate::theme::size::SMALL))
                                .font_weight(FontWeight::SEMIBOLD)
                                .child(block.title.clone()),
                        )
                        .when(!block.status.is_empty(), |d| {
                            d.child(
                                div()
                                    .text_size(px(crate::theme::size::SMALL))
                                    .text_color(t.text_tertiary)
                                    .child(block.status.clone()),
                            )
                        }),
                )
                .when(!block.text.is_empty(), |d| {
                    d.child(
                        div()
                            .pl_4()
                            .text_size(px(crate::theme::size::SMALL))
                            .line_height(px(theme::size::CODE_LINE))
                            .text_color(t.muted)
                            .child(block.text.clone()),
                    )
                })
                .into_any_element(),
            "collapsed" => div()
                .flex()
                .flex_wrap()
                .gap_2()
                .py_1()
                .text_size(px(crate::theme::size::SMALL))
                .child(div().text_color(t.muted).child(block.title.clone()))
                .child(div().text_color(t.text_tertiary).child(block.text.clone()))
                .into_any_element(),
            "error" => div()
                .flex()
                .items_start()
                .gap_2()
                .py_1()
                .text_size(px(crate::theme::size::UI))
                .line_height(px(theme::size::CODE_LINE))
                .text_color(t.red)
                .child(icons::icon("triangle-alert", t.red))
                .child(div().flex_1().min_w_0().child(block.text.clone()))
                .into_any_element(),
            "diff" => {
                let gutter = |text: String| {
                    div()
                        .w(px(34.))
                        .flex_shrink_0()
                        .pr_2()
                        .text_right()
                        .text_color(t.muted.opacity(0.65))
                        .child(text)
                };
                let mut rows = vec![];
                for (old_no, old, new_no, new, kind) in &block.diff_rows {
                    let added = kind == "add";
                    let removed = kind == "del";
                    rows.push(
                        div()
                            .flex()
                            .items_center()
                            .text_size(px(crate::theme::size::CAPTION))
                            .font_family(crate::theme::code_font())
                            .line_height(px(theme::size::CODE_LINE))
                            .bg(if added {
                                t.diff_add_bg
                            } else if removed {
                                t.diff_remove_bg
                            } else {
                                t.surface.opacity(0.)
                            })
                            .child(gutter(if *old_no == 0 {
                                String::new()
                            } else {
                                old_no.to_string()
                            }))
                            .child(gutter(if *new_no == 0 {
                                String::new()
                            } else {
                                new_no.to_string()
                            }))
                            .child(
                                div()
                                    .w(px(12.))
                                    .flex_shrink_0()
                                    .text_color(if added {
                                        t.diff_add
                                    } else if removed {
                                        t.diff_remove
                                    } else {
                                        t.muted
                                    })
                                    .child(if added {
                                        "+"
                                    } else if removed {
                                        "-"
                                    } else {
                                        " "
                                    }),
                            )
                            .child(
                                div()
                                    .flex_1()
                                    .min_w_0()
                                    .text_color(if added {
                                        t.diff_add
                                    } else if removed {
                                        t.diff_remove
                                    } else {
                                        t.muted
                                    })
                                    .child(if removed { old.clone() } else { new.clone() }),
                            ),
                    );
                }
                div()
                    .rounded(px(crate::theme::radius::CHIP))
                    .border_1()
                    .border_color(t.border)
                    .overflow_hidden()
                    .child(
                        div()
                            .px_3()
                            .py_2()
                            .bg(t.surface)
                            .flex()
                            .items_center()
                            .gap_2()
                            .text_size(px(crate::theme::size::SMALL))
                            .child(div().text_color(t.muted).child(if block.path.is_empty() {
                                block.title.clone()
                            } else {
                                block.path.clone()
                            }))
                            .child(diff_counts(block, t)),
                    )
                    .child(
                        div()
                            .id(SharedString::from(format!("diff-{}", block.id)))
                            .overflow_x_scroll()
                            .px_3()
                            .py_2()
                            .children(rows),
                    )
                    .into_any_element()
            }
            "thought" | "tool" | "task" | "explored" | "tool_group" => {
                // TUI hierarchy: thoughts are amber with reasoning behind a rule; tasks
                // take their agent colour and show metrics; tool rows stay quiet and
                // expand into labelled parameter/result rows.
                let op = block.operation.clone();
                let running = block.status == "running";
                let agent_tone = t.resolve(&block.color, t.blue);
                let (title_tone, weight) = match block.kind.as_str() {
                    "thought" => (t.amber, FontWeight::NORMAL),
                    "task" => (agent_tone, FontWeight::MEDIUM),
                    _ => (t.muted, FontWeight::NORMAL),
                };
                let title = if block.title.is_empty() {
                    if block.heading.is_empty() {
                        // The host's spinner slot; the status icon already shows activity.
                        block.text.lines().next().unwrap_or("").replace("\u{e000} ", "").replace('\u{e000}', "")
                    } else {
                        block.heading.clone()
                    }
                } else {
                    block.title.clone()
                };
                let inline_text = block.kind == "thought" && !block.title.is_empty();
                let mut card = div()
                    .id(SharedString::from(block.id.clone()))
                    .flex()
                    .flex_col()
                    .when(depth > 0, |d| d.ml_3().pl_1().border_l_1().border_color(t.border))
                    .child(
                        div()
                            .id(SharedString::from(format!("heading-{}", block.id)))
                            .focusable()
                            .tab_stop(
                                self.snapshot.panel_title.is_empty()
                                    && self.snapshot.prompt.is_none(),
                            )
                            .focus(move |s| s.bg(t.raised))
                            .px_2()
                            .py(px(3.))
                            .rounded(px(crate::theme::radius::CHIP))
                            .flex()
                            .items_center()
                            .gap_2()
                            .text_size(px(crate::theme::size::SMALL))
                            .text_color(t.muted)
                            .when(op.is_some(), |d| {
                                d.child(icons::icon(
                                    if block.collapsed {
                                        "chevron-right"
                                    } else {
                                        "chevron-down"
                                    },
                                    t.text_tertiary,
                                ))
                            })
                            .cursor(CursorStyle::Arrow)
                            .hover(move |s| s.bg(t.hover))
                            .child(
                                div()
                                    .min_w_0()
                                    .truncate()
                                    .font_weight(weight)
                                    .text_color(if running && block.kind != "thought" {
                                        agent_tone
                                    } else {
                                        title_tone
                                    })
                                    .child(title),
                            )
                            .when(inline_text && !block.text.is_empty(), |d| {
                                d.child(
                                    div()
                                        .min_w_0()
                                        .truncate()
                                        .text_color(t.text_tertiary)
                                        .child(block.text.clone()),
                                )
                            })
                            .when(!block.path.is_empty() && block.kind == "tool_group", |d| {
                                d.child(diff_counts(block, t))
                            })
                            .when(!block.status.is_empty(), |d| {
                                let (symbol, label) = status_label(&block.status);
                                let color = match symbol {
                                    "check" => t.text_tertiary,
                                    "x" => t.red,
                                    "refresh-cw" => t.amber,
                                    _ => t.muted,
                                };
                                d.child(
                                    div()
                                        .id(SharedString::from(format!("status-{}", block.id)))
                                        .flex()
                                        .items_center()
                                        .flex_shrink_0()
                                        .gap_1()
                                        .text_size(px(theme::size::CAPTION))
                                        .text_color(color)
                                        .child(icons::icon(symbol, color))
                                        .when(!label.is_empty(), |d| d.child(label.to_owned()))
                                        .tooltip({
                                            let status = block.status.clone();
                                            move |_, cx| {
                                                cx.new(|_| ControlHint(status.clone().into()))
                                                    .into()
                                            }
                                        }),
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
                // Tasks: latest activity while running, metrics (tools · duration) always.
                if block.kind == "task" {
                    let activity = if running { &block.text } else { &String::new() };
                    for line in activity.lines().chain(block.metrics.lines()) {
                        card = card.child(
                            div()
                                .pl(px(theme::space::XL + 2.))
                                .text_size(px(crate::theme::size::SMALL))
                                .line_height(px(theme::size::CODE_LINE))
                                .text_color(t.text_tertiary)
                                .child(line.to_string()),
                        );
                    }
                } else if !block.text.is_empty() && !block.title.is_empty() && !inline_text {
                    card = card.child(
                        div()
                            .px_4()
                            .pb_1()
                            .text_size(px(crate::theme::size::SMALL))
                            .line_height(px(theme::size::CODE_LINE))
                            .text_color(t.muted)
                            .child(block.text.clone()),
                    );
                }
                if !block.detail.is_empty() {
                    card = card.child(if block.kind == "thought" {
                        thought_detail(&block.detail, t)
                    } else if block.kind == "task" {
                        div()
                            .pl(px(theme::space::XL + 2.))
                            .text_size(px(crate::theme::size::SMALL))
                            .line_height(px(theme::size::CODE_LINE))
                            .text_color(t.muted)
                            .child(block.detail.clone())
                            .into_any_element()
                    } else {
                        labelled_detail(&block.detail, t)
                    });
                }
                for member in &block.members {
                    card = card.child(self.render_block_depth(member, _w, cx, depth + 1));
                }
                if let Some(op) = &block.output_operation {
                    card = card.child(
                        div().pl(px(theme::space::XL)).child(
                            self.button(
                                SharedString::from(format!("output-{}", block.id)),
                                "Show full output",
                                json!({"type":"operation","operation":op}),
                                cx,
                            )
                            .py_0()
                            .text_size(px(crate::theme::size::CAPTION))
                            .text_color(t.brand),
                        ),
                    );
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
                            .text_color(t.brand)
                            .child(block.title.clone()),
                    )
                })
                .child(
                    div()
                        .line_height(px(theme::size::BODY_LINE))
                        .text_size(px(crate::theme::size::UI))
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
                .max_w(px(theme::size::READING))
                .child(content),
        );
        outer.into_any_element()
    }
}

/// `N files +added −removed` in diff colours, as the TUI ends a file-change row.
/// The `N files` prefix appears only when a standalone file-change row touched more
/// than one file (`block.files > 1`).
fn diff_counts(block: &Content, t: Theme) -> AnyElement {
    div()
        .flex()
        .flex_shrink_0()
        .gap_1()
        .text_size(px(theme::size::CAPTION))
        .font_family(theme::code_font())
        .when(block.files > 1, |d| {
            d.child(div().text_color(t.muted).child(format!("{} files", block.files)))
        })
        .child(div().text_color(t.green).child(format!("+{}", block.added)))
        .child(div().text_color(t.red).child(format!("−{}", block.removed)))
        .into_any_element()
}

/// One parsed detail line: a `  label: value` parameter row, or plain text that is
/// emphasised when indented (result content) and muted otherwise (section labels).
#[derive(Debug, PartialEq)]
enum DetailRow<'a> {
    Pair(&'a str, &'a str),
    Text(&'a str, bool),
}
fn detail_rows(detail: &str) -> Vec<DetailRow<'_>> {
    detail
        .lines()
        .map(|row| {
            let pair = (row.starts_with("  ") && !row.starts_with("    "))
                .then(|| row.trim_start().split_once(": "))
                .flatten();
            match pair {
                Some((label, value)) => DetailRow::Pair(label, value),
                None => DetailRow::Text(row.strip_prefix("  ").unwrap_or(row), row.starts_with(' ')),
            }
        })
        .collect()
}
/// Tool parameters and results behind a thin rule: labels in a fixed muted column,
/// values in full text colour, nothing clipped (the TUI's `│ label  value`).
fn labelled_detail(detail: &str, t: Theme) -> AnyElement {
    div()
        .ml(px(theme::space::LG))
        .mb_1()
        .pl_3()
        .border_l_1()
        .border_color(t.border)
        .flex()
        .flex_col()
        .text_size(px(theme::size::SMALL))
        .line_height(px(theme::size::CODE_LINE))
        .font_family(theme::code_font())
        .children(detail_rows(detail).into_iter().map(|row| match row {
            DetailRow::Pair(label, value) => div()
                .flex()
                .gap_3()
                .child(
                    div()
                        .w(px(104.))
                        .flex_shrink_0()
                        .text_color(t.muted)
                        .child(label.to_string()),
                )
                .child(div().flex_1().min_w_0().text_color(t.text).child(value.to_string())),
            // Unindented rows are section titles (PARAMETERS, RESULT): bold, muted.
            DetailRow::Text(text, false) if !text.is_empty() => div()
                .pt_1()
                .font_weight(FontWeight::SEMIBOLD)
                .text_size(px(theme::size::CAPTION))
                .text_color(t.muted)
                .child(text.to_string()),
            // `label:` introduces a multi-line value; the label stays muted.
            DetailRow::Text(text, _) if !text.starts_with(' ') && text.ends_with(':') => {
                div().text_color(t.muted).child(text.to_string())
            }
            DetailRow::Text(text, _) => div()
                .text_color(t.text)
                .child(if text.is_empty() { " ".to_string() } else { text.to_string() }),
        }))
        .into_any_element()
}
/// Reasoning dimmed behind a rule; whole-line `**headings**` become muted bold.
fn thought_detail(detail: &str, t: Theme) -> AnyElement {
    div()
        .ml(px(theme::space::LG))
        .mb_1()
        .pl_3()
        .border_l_1()
        .border_color(t.border)
        .flex()
        .flex_col()
        .text_size(px(theme::size::SMALL))
        .line_height(px(theme::size::CODE_LINE))
        .children(detail.lines().map(|line| {
            let trimmed = line.trim();
            let heading = trimmed.len() > 4
                && trimmed.starts_with("**")
                && trimmed.ends_with("**")
                && !trimmed[2..trimmed.len() - 2].contains("**");
            if heading {
                div()
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(t.muted)
                    .child(trimmed[2..trimmed.len() - 2].to_string())
            } else {
                div()
                    .text_color(t.text_tertiary)
                    .child(if line.is_empty() { " ".to_string() } else { line.replace("**", "") })
            }
        }))
        .into_any_element()
}

/// Only successful terminal status becomes an icon; all other host wording
/// remains visible, including cancellations and unknown provider states.
fn status_label(status: &str) -> (&'static str, &str) {
    match status {
        "completed" | "complete" | "done" | "success" => ("check", ""),
        "failed" | "error" => ("x", status),
        "running" | "pending" => ("refresh-cw", status),
        _ => ("circle-dot", status),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn status_keeps_failure_running_and_unknown_words() {
        assert_eq!(status_label("completed"), ("check", ""));
        assert_eq!(status_label("failed"), ("x", "failed"));
        assert_eq!(status_label("running"), ("refresh-cw", "running"));
        assert_eq!(status_label("cancelled"), ("circle-dot", "cancelled"));
    }
    #[test]
    fn detail_rows_split_parameters_like_the_tui() {
        let rows = detail_rows("Parameters\n  path: src/a.py\n  url: http://x: y\n    nested: no\n plain");
        assert_eq!(rows[0], DetailRow::Text("Parameters", false));
        assert_eq!(rows[1], DetailRow::Pair("path", "src/a.py"));
        assert_eq!(rows[2], DetailRow::Pair("url", "http://x: y"));
        assert_eq!(rows[3], DetailRow::Text("  nested: no", true));
        assert_eq!(rows[4], DetailRow::Text(" plain", true));
    }
}
