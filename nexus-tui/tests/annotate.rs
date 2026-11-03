//! Drawing on a composer image: the modal, the pen, and what ends up in the draft.
mod common;

use base64::{engine::general_purpose::STANDARD, Engine};
use common::*;
use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use nexus_tui::annotate::Annotation;
use nexus_tui::app::App;
use nexus_tui::images::Image;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::{Overlay, OverlayKind, Target};

const SIZE: (u16, u16) = (120, 40);

fn source() -> String {
    let image = image::RgbaImage::from_pixel(400, 200, image::Rgba([40, 40, 40, 255]));
    let mut png = std::io::Cursor::new(Vec::new());
    image.write_to(&mut png, image::ImageFormat::Png).unwrap();
    format!(
        "data:image/png;base64,{}",
        STANDARD.encode(png.into_inner())
    )
}

fn app_with_image() -> App {
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), SIZE);
    // As a paste does: the marker in the text keeps the image in the draft.
    app.composer().insert_str("[Image A]");
    app.composer().images.push(Image {
        name: "Image A".into(),
        data_url: source(),
    });
    draw(&mut app, SIZE.0, SIZE.1);
    app
}

fn mouse(app: &mut App, kind: MouseEventKind, column: u16, row: u16) {
    app.handle_mouse(MouseEvent {
        kind,
        column,
        row,
        modifiers: KeyModifiers::NONE,
    });
}

fn annotation(app: &App) -> Option<&Annotation> {
    match app.ui.overlay.as_ref() {
        Some(Overlay {
            kind: OverlayKind::Annotate(annotation),
            ..
        }) => Some(annotation),
        _ => None,
    }
}

fn open(app: &mut App) {
    let thumb = app
        .hits
        .rect_of(&Target::ComposerImage(0))
        .expect("thumbnail");
    click(app, thumb.x, thumb.y);
    assert!(annotation(app).is_some(), "the thumbnail opens the modal");
    draw(app, SIZE.0, SIZE.1);
}

/// The picture in cells, where the pointer can press.
fn picture(app: &App) -> (u16, u16, u16, u16) {
    let rect = annotation(app).unwrap().canvas.get();
    let (w, h) = app.annotate_live.borrow().font();
    let (w, h) = (u32::from(w), u32::from(h));
    (
        (rect.x / w) as u16,
        (rect.y / h) as u16,
        (rect.width / w) as u16,
        (rect.height / h) as u16,
    )
}

fn drag(app: &mut App) {
    let (x, y, w, h) = picture(app);
    mouse(app, MouseEventKind::Down(MouseButton::Left), x + 1, y + 1);
    mouse(
        app,
        MouseEventKind::Drag(MouseButton::Left),
        x + w / 2,
        y + h / 2,
    );
    mouse(
        app,
        MouseEventKind::Drag(MouseButton::Left),
        x + w - 1,
        y + h / 2,
    );
    mouse(
        app,
        MouseEventKind::Up(MouseButton::Left),
        x + w - 1,
        y + h / 2,
    );
    draw(app, SIZE.0, SIZE.1);
}

#[test]
fn the_modal_says_what_it_does() {
    let mut app = app_with_image();
    open(&mut app);
    let text = draw(&mut app, SIZE.0, SIZE.1).1.join("\n");
    for fact in [
        "Mark up Image A",
        "1-4 colour",
        "u undo",
        "x clear",
        "enter done",
        "esc cancel",
        "Undo",
        "Clear",
        "Done",
    ] {
        assert!(text.contains(fact), "{fact} in\n{text}");
    }
}

#[test]
fn done_puts_the_marks_in_the_draft() {
    let mut app = app_with_image();
    open(&mut app);
    drag(&mut app);
    assert_eq!(annotation(&app).unwrap().strokes.len(), 1);
    press(&mut app, &["enter"]);
    assert!(annotation(&app).is_none());
    let images = &app.composer_ref().images;
    assert_eq!(
        images.len(),
        1,
        "the original is replaced, not kept beside it"
    );
    assert_ne!(images[0].data_url, source());
    assert_eq!(images[0].name, "Image A");
}

#[test]
fn done_button_matches_the_key() {
    let mut app = app_with_image();
    open(&mut app);
    drag(&mut app);
    let done = app.hits.rect_of(&Target::AnnotateDone).unwrap();
    click(&mut app, done.x, done.y);
    assert!(annotation(&app).is_none());
    assert_ne!(app.composer_ref().images[0].data_url, source());
}

#[test]
fn esc_cancels_and_leaves_the_image_alone() {
    let mut app = app_with_image();
    open(&mut app);
    drag(&mut app);
    press(&mut app, &["esc"]);
    assert!(annotation(&app).is_none());
    assert_eq!(app.composer_ref().images[0].data_url, source());
}

#[test]
fn done_without_marks_keeps_the_original_bytes() {
    let mut app = app_with_image();
    open(&mut app);
    press(&mut app, &["enter"]);
    assert!(annotation(&app).is_none());
    assert_eq!(app.composer_ref().images[0].data_url, source());
}

#[test]
fn undo_clear_and_colour_keys() {
    let mut app = app_with_image();
    open(&mut app);
    press(&mut app, &["3"]);
    drag(&mut app);
    let inks = nexus_tui::theme::annotation_inks();
    assert_eq!(annotation(&app).unwrap().strokes[0].color, inks[2]);
    drag(&mut app);
    press(&mut app, &["u"]);
    assert_eq!(annotation(&app).unwrap().strokes.len(), 1);
    press(&mut app, &["x"]);
    assert!(annotation(&app).unwrap().strokes.is_empty());
}

#[test]
fn a_stray_click_beside_the_modal_keeps_the_drawing() {
    let mut app = app_with_image();
    open(&mut app);
    drag(&mut app);
    click(&mut app, 0, 0);
    assert_eq!(annotation(&app).unwrap().strokes.len(), 1);
}

#[test]
fn a_stroke_keeps_the_pointer_past_the_picture_edge() {
    let mut app = app_with_image();
    open(&mut app);
    let (x, y, ..) = picture(&app);
    mouse(
        &mut app,
        MouseEventKind::Down(MouseButton::Left),
        x + 1,
        y + 1,
    );
    mouse(&mut app, MouseEventKind::Drag(MouseButton::Left), 0, 0);
    mouse(&mut app, MouseEventKind::Up(MouseButton::Left), 0, 0);
    let stroke = &annotation(&app).unwrap().strokes[0];
    assert_eq!(*stroke.points.last().unwrap(), [0.0, 0.0]);
    assert!(!annotation(&app).unwrap().drawing);
}

#[test]
fn pixel_mouse_reaches_the_buttons_and_the_pen() {
    let mut app = app_with_image();
    app.annotate_live.borrow_mut().configure(true, (10, 20));
    open(&mut app);
    assert!(app.annotate_live.borrow().pixel_mouse());
    let rect = annotation(&app).unwrap().canvas.get();
    let (px, py) = (
        (rect.x + rect.width / 2) as u16,
        (rect.y + rect.height / 2) as u16,
    );
    mouse(&mut app, MouseEventKind::Down(MouseButton::Left), px, py);
    mouse(&mut app, MouseEventKind::Up(MouseButton::Left), px, py);
    assert_eq!(
        annotation(&app).unwrap().strokes[0].points,
        vec![[0.5, 0.5]]
    );
    // A cell-based button still answers a pixel-based click.
    draw(&mut app, SIZE.0, SIZE.1);
    let done = app.hits.rect_of(&Target::AnnotateDone).unwrap();
    let (x, y) = (done.x * 10 + 5, done.y * 20 + 10);
    mouse(&mut app, MouseEventKind::Down(MouseButton::Left), x, y);
    assert!(annotation(&app).is_none());
    // The next frame takes the pictures down and gives the cells back.
    draw(&mut app, SIZE.0, SIZE.1);
    assert!(!app.annotate_live.borrow().pixel_mouse());
    assert!(app
        .annotate_live
        .borrow_mut()
        .take()
        .ends_with("\x1b[?1016l"));
}
