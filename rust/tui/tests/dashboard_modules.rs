// Compile and test the dashboard modules on their own, away from the large application.
#[path = "../src/picker.rs"]
mod picker;
#[path = "../src/render/sessions.rs"]
mod sessions;

#[test]
fn dashboard_draws_without_a_preview() {
    let backend = ratatui::backend::TestBackend::new(90, 25);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    let mut state = picker::Dashboard::default();
    terminal
        .draw(|frame| {
            sessions::draw(
                frame,
                frame.area(),
                &serde_json::json!({"rows":[]}),
                &mut state,
                &sessions::Colors::default(),
                0,
            )
        })
        .unwrap();
}
