//! Left sidebar: new-session button and the session list.

use nsh_base::theme;
use nsh_core::prelude::*;

use crate::present::sessions::SessionItem;
use crate::store::Store;

pub fn sidebar(store: Store) -> impl IntoElement {
    view()
        .w(px(260.0))
        .p_2()
        .gap_2()
        .border_r(px(1.0))
        .border_r_color(move || theme().border)
        .bg(move || theme().sidebar)
        .child(
            view()
                .px_3()
                .py_2()
                .rounded(move || theme().radius)
                .bg(move || theme().primary)
                .active(|s| s.opacity(0.6))
                .on_press(move || store.new_session())
                .child(text("New session").text_sm().text_color(move || theme().primary_foreground)),
        )
        .child(scroll().flex_1().child(each(
            move || store.sessions.get(),
            |s: &SessionItem| s.id.clone(),
            move |item, _| {
                view()
                    .px_3()
                    .py_2()
                    .rounded(move || theme().radius)
                    .bg(move || {
                        if store.selected.get() == Some(item.with(|i| i.id.clone())) {
                            theme().accent
                        } else {
                            theme().sidebar
                        }
                    })
                    .active(|s| s.opacity(0.6))
                    .on_press(move || store.select(item.with(|i| i.id.clone())))
                    .child(text(move || item.with(|i| i.title.clone())).text_sm())
                    .child(
                        text(move || item.with(|i| i.state.clone()))
                            .text_xs()
                            .text_color(move || theme().muted_foreground),
                    )
            },
        )))
}
