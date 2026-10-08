//! Native rendering of the shared typed Settings contract (overhaul plan §5).
//! Values stay host-owned; only offered operations leave the presentation client.
use super::*;

fn label(value: &Value, key: &str) -> String {
    value[key].as_str().unwrap_or("").to_owned()
}
fn values<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    value[key].as_array().map(Vec::as_slice).unwrap_or(&[])
}
fn operation(base: &Value, fields: Value) -> Value {
    let mut result = base.clone();
    if let (Some(result), Some(fields)) = (result.as_object_mut(), fields.as_object()) {
        result.extend(fields.clone());
    }
    result
}

impl Desktop {
    fn settings_button(
        &self,
        id: String,
        caption: String,
        op: Value,
        active: bool,
        cx: &mut Context<Self>,
    ) -> Stateful<Div> {
        let t = self.theme();
        self.button(
            SharedString::from(id),
            caption,
            json!({"type":"ui_trace"}),
            cx,
        )
        .border_1()
        .border_color(if active { t.accent } else { t.border })
        .bg(if active { t.accent_bg } else { t.surface })
        .text_color(if active { t.text } else { t.muted })
        .tab_stop(true)
        .on_click(cx.listener(move |this, _, w, cx| {
            this.settings_open = None;
            this.dispatch(json!({"type":"operation", "operation":op}), w, cx);
        }))
    }

    pub(crate) fn settings_view(
        &self,
        page: &Value,
        w: &mut Window,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let t = self.theme();
        let mut body = div()
            .id("typed-settings-scroll")
            .flex_1()
            .min_h_0()
            .overflow_y_scroll()
            .p_5()
            .flex()
            .flex_col()
            .gap_3();
        if !label(page, "intro").is_empty() {
            body = body.child(
                div()
                    .text_color(t.muted)
                    .child(label(page, "intro").replace("terminal shell", "desktop client")),
            );
        }
        if page["scope"].is_object() {
            let scope = &page["scope"];
            let mut row = div().flex().flex_wrap().gap_2().child("Scope");
            for (i, option) in values(scope, "options").iter().enumerate() {
                row = row.child(self.settings_button(
                    format!("settings-scope-{i}"),
                    option.as_str().unwrap_or("").into(),
                    operation(&scope["operation"], json!({"value":i})),
                    scope["value"].as_u64() == Some(i as u64),
                    cx,
                ));
            }
            body = body.child(row);
        }
        for (i, block) in values(page, "blocks").iter().enumerate() {
            body = body.child(self.settings_block(
                block,
                &format!("{}-{i}", label(page, "area")),
                0,
                w,
                cx,
            ));
        }
        if !label(page, "footer").is_empty() {
            body = body.child(
                div()
                    .mt_3()
                    .text_size(px(crate::theme::size::CAPTION))
                    .text_color(t.muted)
                    .child(label(page, "footer")),
            );
        }
        body.into_any_element()
    }

    fn settings_block(
        &self,
        block: &Value,
        id: &str,
        depth: usize,
        w: &mut Window,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let t = self.theme();
        let mut body = div().flex().flex_col().gap_2();
        if depth > 8 {
            return body
                .child("Settings nesting limit reached")
                .into_any_element();
        }
        match block["t"].as_str().unwrap_or("") {
            "gap" => body = body.h(px(8.)),
            "heading" => {
                body = body
                    .mt_3()
                    .font_weight(FontWeight::SEMIBOLD)
                    .child(label(block, "text"))
            }
            "note" | "callout" => {
                body = body
                    .text_color(match block["tone"].as_str().or(block["level"].as_str()) {
                        Some("warning") => t.amber,
                        Some("error") => t.red,
                        _ => t.muted,
                    })
                    .child(label(block, "text"));
                if block["action"].is_object() {
                    body = body.child(self.settings_button(
                        format!("{id}-action"),
                        label(&block["action"], "label"),
                        block["action"]["operation"].clone(),
                        false,
                        cx,
                    ));
                }
            }
            "row" => {
                let mut title = div().flex().flex_col().gap_1().flex_1().min_w_0().child(
                    div()
                        .font_weight(FontWeight::MEDIUM)
                        .child(label(block, "label")),
                );
                for key in ["description", "scope", "error"] {
                    let text = label(block, key);
                    if !text.is_empty() {
                        title = title.child(
                            div()
                                .text_size(px(crate::theme::size::CAPTION))
                                .text_color(if key == "error" { t.red } else { t.muted })
                                .child(text.replace("this terminal", "this desktop")),
                        );
                    }
                }
                body = body
                    .py_2()
                    .border_b_1()
                    .border_color(t.border.opacity(0.5))
                    .child(title)
                    .child(self.settings_control(&block["control"], id, w, cx));
            }
            "buttons" => {
                if !label(block, "label").is_empty() {
                    body = body.child(label(block, "label"));
                }
                body = body.child(div().flex().flex_wrap().gap_2().children(
                    values(block, "items").iter().enumerate().map(|(i, item)| {
                        self.settings_button(
                            format!("{id}-{i}"),
                            label(item, "label"),
                            item["operation"].clone(),
                            false,
                            cx,
                        )
                        .when(item["variant"] == "danger", |d| d.text_color(t.red))
                    }),
                ));
            }
            "tabs" => {
                body = body.child(div().flex().flex_wrap().gap_2().children(
                    values(block, "items").iter().enumerate().map(|(i, item)| {
                        let caption = format!(
                            "{} {}",
                            item[0].as_str().unwrap_or(""),
                            item[1].as_str().unwrap_or("")
                        );
                        self.settings_button(
                            format!("{id}-{i}"),
                            caption,
                            operation(&block["operation"], json!({"value":i})),
                            block["active"].as_u64() == Some(i as u64),
                            cx,
                        )
                    }),
                ));
            }
            "section" => {
                body = body
                    .p_3()
                    .border_1()
                    .border_color(t.border)
                    .rounded(px(crate::theme::radius::CONTROL))
                    .child(
                        div()
                            .font_weight(FontWeight::SEMIBOLD)
                            .child(label(block, "title")),
                    )
                    .child(div().text_color(t.muted).child(label(block, "summary")));
                // Showing all sections keeps every provider and diagnostic accessible.
                for (i, child) in values(block, "blocks").iter().enumerate() {
                    body = body.child(self.settings_block(
                        child,
                        &format!("{id}-{i}"),
                        depth + 1,
                        w,
                        cx,
                    ));
                }
            }
            "ordered" => {
                let items = values(block, "items");
                let editable = block["editable"].as_bool().unwrap_or(true);
                for (i, item) in items.iter().enumerate() {
                    let tag = item[1]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .unwrap_or(if i == 0 { "in use" } else { "fallback" });
                    let mut row = div().flex().flex_wrap().items_center().gap_2().child(
                        div().flex_1().min_w_0().child(format!(
                            "{}. {} · {}",
                            i + 1,
                            item[0].as_str().unwrap_or(""),
                            tag
                        )),
                    );
                    if editable {
                        for (action, caption, available) in [
                            ("up", "↑", i > 0),
                            ("down", "↓", i + 1 < items.len()),
                            ("remove", "Remove", true),
                        ] {
                            if available {
                                row = row.child(self.settings_button(
                                    format!("{id}-{i}-{action}"),
                                    caption.into(),
                                    operation(
                                        &block["operation"],
                                        json!({"action":action, "index":i}),
                                    ),
                                    false,
                                    cx,
                                ));
                            }
                        }
                    }
                    body = body.child(row);
                    if let Some(note) = item[2].as_str().filter(|s| !s.is_empty()) {
                        body = body.child(
                            div()
                                .text_size(px(crate::theme::size::CAPTION))
                                .text_color(t.muted)
                                .child(note.to_owned()),
                        );
                    }
                }
                if editable {
                    body = body.child(self.settings_button(
                        format!("{id}-add"),
                        label(block, "add_label"),
                        operation(
                            &block["operation"],
                            json!({"action":"add", "index":items.len()}),
                        ),
                        false,
                        cx,
                    ));
                }
            }
            "table" => {
                for row in values(block, "rows") {
                    let mut entry = div()
                        .flex()
                        .flex_col()
                        .gap_1()
                        .py_2()
                        .border_b_1()
                        .border_color(t.border);
                    for (i, col) in values(block, "cols").iter().enumerate() {
                        entry = entry.child(div().text_size(px(crate::theme::size::SMALL)).child(
                            format!(
                                "{}: {}",
                                col[0].as_str().unwrap_or(""),
                                row[i].as_str().unwrap_or("")
                            ),
                        ));
                    }
                    body = body.child(entry);
                }
            }
            "progress" => {
                body = body.text_color(t.muted).child(format!(
                    "{} · {:.0}%",
                    label(block, "label"),
                    block["fraction"].as_f64().unwrap_or(0.) * 100.
                ))
            }
            _ => {
                body = body
                    .text_color(t.amber)
                    .child(format!("Unsupported settings block: {}", label(block, "t")))
            }
        }
        body.into_any_element()
    }

    fn settings_control(
        &self,
        control: &Value,
        id: &str,
        w: &mut Window,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let t = self.theme();
        let op = &control["operation"];
        let mut body = div().flex().flex_wrap().items_center().gap_2();
        match control["c"].as_str().unwrap_or("") {
            "value" => body = body.child(label(control, "value")),
            "button" => {
                body = body.child(self.settings_button(
                    id.into(),
                    label(control, "label"),
                    op.clone(),
                    false,
                    cx,
                ))
            }
            "toggle" => {
                let on = control["on"].as_bool().unwrap_or(false);
                let locked = label(control, "locked");
                if locked.is_empty() {
                    body = body.child(self.settings_button(
                        id.into(),
                        if on { "On" } else { "Off" }.into(),
                        operation(op, json!({"value":!on})),
                        on,
                        cx,
                    ));
                } else {
                    body = body.text_color(t.muted).child(format!(
                        "{} · {}",
                        if on { "On" } else { "Off" },
                        locked
                    ));
                }
            }
            "segmented" => {
                for (i, option) in values(control, "options").iter().enumerate() {
                    body = body.child(self.settings_button(
                        format!("{id}-{i}"),
                        option.as_str().unwrap_or("").into(),
                        operation(op, json!({"value":control["values"][i]})),
                        control["active"].as_u64() == Some(i as u64),
                        cx,
                    ));
                }
            }
            "select" => {
                let key = id.to_owned();
                body = body.child(
                    self.button(
                        SharedString::from(format!("{id}-open")),
                        format!("{} ▾", label(control, "value")),
                        json!({"type":"ui_trace"}),
                        cx,
                    )
                    .tab_stop(true)
                    .border_1()
                    .border_color(t.border)
                    .on_click(cx.listener(move |this, _, _, cx| {
                        this.settings_open = if this.settings_open.as_ref() == Some(&key) {
                            None
                        } else {
                            Some(key.clone())
                        };
                        cx.notify();
                    })),
                );
                if self.settings_open.as_deref() == Some(id) {
                    let options = div()
                        .id(SharedString::from(format!("{id}-options")))
                        .w_full()
                        .max_h(px(180.))
                        .overflow_y_scroll()
                        .flex()
                        .flex_col()
                        .gap_1()
                        .children(values(control, "options").iter().enumerate().map(
                            |(i, option)| {
                                self.settings_button(
                                    format!("{id}-{i}"),
                                    option[0].as_str().unwrap_or("").into(),
                                    operation(op, json!({"value":option[1]})),
                                    option[0] == control["value"],
                                    cx,
                                )
                                .justify_start()
                            },
                        ));
                    body = body.child(options);
                }
            }
            "stepper" => {
                body = body.child(label(control, "display"));
                let value = control["value"].as_f64().unwrap_or(0.);
                let step = control["step"].as_f64().unwrap_or(1.);
                let low = control["min"].as_f64().unwrap_or(0.);
                let high = control["max"].as_f64().unwrap_or(100.);
                for (caption, value) in [
                    ("−", (value - step).max(low)),
                    ("+", (value + step).min(high)),
                ] {
                    body = body.child(self.settings_button(
                        format!("{id}-{caption}"),
                        caption.into(),
                        operation(op, json!({"value":value})),
                        false,
                        cx,
                    ));
                }
            }
            "text" => {
                let mut inputs = self.settings_inputs.borrow_mut();
                if inputs.len() >= 200 && !inputs.contains_key(id) {
                    return body
                        .text_color(t.amber)
                        .child("Settings input limit reached (200)")
                        .into_any_element();
                }
                let (input, _) = inputs.entry(id.to_owned()).or_insert_with(|| {
                    let input = cx.new(|cx| { let mut input = Input::new(&label(control, "placeholder"), cx); input.secret = control["secret"].as_bool().unwrap_or(false); input.set(label(control, "value"), cx); input });
                    let op = op.clone();
                    let generation = self.snapshot.generation;
                    let subscription = cx.subscribe_in(&input, w, move |this, input, event, w, cx| {
                        if matches!(event, InputEvent::Submitted) && this.snapshot.generation == generation {
                            let text = input.read(cx).content.clone();
                            if text.chars().count() <= 400 {
                                this.dispatch(json!({"type":"operation", "operation":operation(&op, json!({"value":text}))}), w, cx);
                                input.update(cx, |input, cx| input.set(String::new(), cx));
                            } else { this.push_notice(NoticeKind::Error, "Settings value exceeds 400 characters", NoticeAction::None, cx); }
                        }
                    });
                    (input, subscription)
                });
                input.update(cx, |input, _| {
                    input.foreground = t.text;
                    input.muted = t.muted;
                    input.accent = t.accent;
                });
                body = body
                    .child(
                        div()
                            .w_full()
                            .p_2()
                            .rounded(px(crate::theme::radius::CONTROL))
                            .border_1()
                            .border_color(t.border)
                            .child(input.clone()),
                    )
                    .child(
                        div()
                            .text_size(px(crate::theme::size::CAPTION))
                            .text_color(t.muted)
                            .child("Enter to submit"),
                    );
            }
            _ => {
                body = body.text_color(t.amber).child(format!(
                    "Unsupported settings control: {}",
                    label(control, "c")
                ))
            }
        }
        body.into_any_element()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn page() -> Value {
        json!({"area":"providers", "intro":"Test settings", "footer":"Fixture only",
        "scope":{"options":["Global","Project"], "value":0, "operation":{"kind":"sp_providers","key":"scope"}},
        "blocks":[
            {"t":"heading","text":"Controls"},
            {"t":"row","label":"Theme","control":{"c":"segmented","options":["Dark","Light"],"values":["nexus-dark","nexus-light"],"active":0,"operation":{"kind":"sp_appearance","key":"theme"}}},
            {"t":"row","label":"Enabled","control":{"c":"toggle","on":true,"locked":"Locked after first turn","operation":{"kind":"sp_layout","key":"toggle"}}},
            {"t":"row","label":"Model","control":{"c":"select","value":"A","options":[["A","a"],["B","b"]],"operation":{"kind":"sp_models","key":"model"}}},
            {"t":"row","label":"Speed","control":{"c":"stepper","display":"1×","value":1,"min":0.5,"max":2,"step":0.5,"operation":{"kind":"sp_voice","key":"speed"}}},
            {"t":"row","label":"Status","control":{"c":"value","value":"Connected"}},
            {"t":"row","label":"API key","control":{"c":"text","secret":true,"value":"","placeholder":"API key","operation":{"kind":"sp_providers","key":"api_key","provider":"test"}}},
            {"t":"row","label":"Refresh","control":{"c":"button","label":"Refresh","operation":{"kind":"refresh"}}},
            {"t":"tabs","items":[["Low","1"],["High","2"]],"active":0,"operation":{"kind":"tab"}},
            {"t":"ordered","items":[["A","in use","First"],["B","fallback","Second"]],"editable":true,"add_label":"Add model","operation":{"kind":"chain"}},
            {"t":"buttons","items":[{"label":"Reset","operation":{"kind":"reset"},"variant":"danger"}]},
            {"t":"section","title":"Diagnostics","summary":"Connected","blocks":[{"t":"note","text":"No errors"}]},
            {"t":"table","cols":[["Action",0],["Keys",24]],"rows":[["Send","Enter"]]},
            {"t":"progress","label":"Download","fraction":0.5},
            {"t":"callout","level":"warning","text":"Requires setup","action":{"label":"Connect","operation":{"kind":"connect"}}},
            {"t":"gap"}
        ]})
    }

    #[gpui::test]
    fn typed_settings_draw_all_controls_and_keep_secret_input_local(cx: &mut TestAppContext) {
        cx.update(bind_desktop_keys);
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, cx| {
                this.apply(
                    Snapshot {
                        panel_title: "Settings · Providers".into(),
                        settings_page: Some(page()),
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
            .update(cx, |this, w, cx| {
                let input = this
                    .settings_inputs
                    .borrow()
                    .get("providers-6")
                    .unwrap()
                    .0
                    .clone();
                assert!(input.read(cx).secret);
                input.update(cx, |input, cx| input.insert("test-secret", w, cx));
                window_focus(&input, w, cx);
                assert!(
                    this.notices.is_empty(),
                    "typing must not dispatch secret drafts"
                );
                this.settings_open = Some("providers-3".into());
            })
            .unwrap();
        cx.update_window(handle.into(), |_, w, cx| {
            let _ = w.draw(cx);
        })
        .unwrap();
        cx.simulate_keystrokes(handle.into(), "enter");
        cx.run_until_parked();
        handle
            .update(cx, |this, w, cx| {
                assert!(
                    this.notices.contains_text("Preview fixture"),
                    "Enter submits the offered operation"
                );
                assert!(this
                    .settings_inputs
                    .borrow()
                    .get("providers-6")
                    .unwrap()
                    .0
                    .read(cx)
                    .content
                    .is_empty());
                this.dismiss(&Dismiss, w, cx);
                assert!(
                    this.settings_open.is_none(),
                    "Escape closes the choice list first"
                );
                this.apply(
                    Snapshot {
                        generation: 2,
                        panel_title: "Settings · Providers".into(),
                        settings_page: Some(page()),
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                assert!(
                    this.settings_inputs.borrow().is_empty(),
                    "reconnect must release secret drafts"
                );
            })
            .unwrap();
    }

    #[gpui::test]
    fn settings_sheet_cannot_focus_or_submit_background_composer(cx: &mut TestAppContext) {
        let handle = cx.add_window(|w, cx| Desktop::new(w, cx, true));
        handle
            .update(cx, |this, w, cx| {
                this.composer
                    .update(cx, |input, cx| input.set("Keep draft".into(), cx));
                this.apply(
                    Snapshot {
                        panel_title: "Settings · Appearance".into(),
                        settings_page: Some(page()),
                        ..Default::default()
                    },
                    w,
                    cx,
                );
                this.focus_composer(&FocusComposer, w, cx);
                assert!(!this.composer.read(cx).focus.is_focused(w));
                this.submit(w, cx);
                assert!(this.notices.is_empty(), "background Enter must not submit");
                assert_eq!(this.composer.read(cx).content, "Keep draft");
            })
            .unwrap();
    }
}
