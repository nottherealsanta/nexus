//! Nexus client as an NSH app: one root, layout by window size, same code on
//! web and native. Modules (docs/nsh-client.md):
//!
//! * [`platform`] - the only target-specific code (launch config, cookies)
//! * [`api`] - HTTP to `/v1/web/*`; [`ops`] - patch-frame applier
//! * [`store`] - signals + live loop (snapshot, long-poll, resync)
//! * [`present`] - pure view -> rows; [`components`] - UI regions

pub mod api;
pub mod components;
pub mod ops;
pub mod platform;
pub mod present;
pub mod store;

use nsh_base::{safe_area_view, theme};
use nsh_core::ui::prelude::*;

use api::Api;
use store::Store;

/// The app's UI set (`nsh.json` `"rust"`).
#[native_module("app/native")]
fn native(registry: &mut Registry) {
    registry.register::<App>();
}

/// The application's root component.
#[component]
pub fn App() -> impl IntoElement {
    let launch = platform::launch();
    let store = Store::new(Api::new(launch.clone().unwrap_or_else(|_| platform::Launch {
        base: String::new(),
        origin: None,
        ticket: None,
    })));
    match launch {
        Ok(_) => store.start(),
        Err(message) => store.offline(message),
    }

    view()
        .flex_1()
        .bg(move || theme().background)
        .text_color(move || theme().foreground)
        .child(
            safe_area_view().flex_1().child(
                view().flex_1().child(components::topbar::topbar(store)).child(
                    view()
                        .flex_1()
                        .flex_row()
                        .child(components::sidebar::sidebar(store))
                        .child(
                            view()
                                .flex_1()
                                .child(components::timeline::timeline(store))
                                .child(components::composer::composer(store)),
                        ),
                ),
            ),
        )
}
