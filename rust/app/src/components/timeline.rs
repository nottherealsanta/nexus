//! Conversation timeline: labelled rows from `present::timeline`.

use nsh_base::theme;
use nsh_core::prelude::*;

use crate::present::timeline::{Row, RowKind, rows_from_view};
use crate::store::Store;

pub fn timeline(store: Store) -> impl IntoElement {
    let rows = computed(move || store.view.with(rows_from_view));
    scroll().flex_1().child(
        view()
            .w_full()
            .max_w(px(820.0))
            .self_center()
            .p_4()
            .gap_3()
            .child(each(move || rows.get(), |r: &Row| r.key.clone(), |row, _| row_view(row))),
    )
}

/// One labelled row; reads the row inside bindings (cells recycle).
fn row_view(row: ReadSignal<Row>) -> impl IntoElement {
    view()
        .p_3()
        .gap_1()
        .rounded(move || theme().radius)
        .border_1()
        .border_color(move || theme().border)
        .bg(move || match row.with(|r| r.kind) {
            RowKind::User => theme().secondary,
            RowKind::Tool => theme().muted,
            _ => theme().card,
        })
        .child(
            text(move || row.with(|r| r.heading.clone()))
                .text_sm()
                .font_weight(FontWeight::SEMIBOLD)
                .text_color(move || {
                    if row.with(|r| r.kind) == RowKind::Error { theme().destructive } else { theme().foreground }
                }),
        )
        .child(dynamic(move || {
            row.with(|r| {
                r.lines
                    .iter()
                    .map(|l| {
                        let body = if l.label.is_empty() { l.text.clone() } else { format!("{}: {}", l.label, l.text) };
                        text(body).text_sm().selectable(true)
                    })
                    .collect::<Vec<_>>()
            })
        }))
}
