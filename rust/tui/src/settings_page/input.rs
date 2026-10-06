//! Focus, keys and mouse for a Settings page. The client owns focus, scroll, popups
//! and text editing; the host owns the values. A change is one operation sent back with
//! `value` filled in; the host rebuilds the page, so what is shown is what is saved.
use super::model::*;
use nexus_widgets::{hit::Part, HitMap, Scroll, TextState};
use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::{json, Value};
use std::collections::HashMap;

#[derive(Default)]
pub struct Popup {
    pub id: String,
    pub highlighted: usize,
}

pub struct Edit {
    pub id: String,
    pub text: TextState,
}

#[derive(Default)]
pub struct PageState {
    /// Focus stop id (`row:…`, `ord:…:2`, `tabs:…`, `sec:…`, `btn:…`, `scope`).
    pub focus: String,
    pub scroll: Scroll,
    pub popup: Option<Popup>,
    pub edit: Option<Edit>,
    /// Section open/closed overrides, kept across host rebuilds.
    pub sections: HashMap<String, bool>,
    /// Focus order and hit rectangles of the last drawn frame.
    pub order: Vec<String>,
    pub hits: HitMap,
    /// The area the state belongs to; a different area starts with fresh focus and scroll.
    pub area: String,
}

impl PageState {
    pub fn reset_for(&mut self, area: &str) {
        if self.area != area {
            *self = PageState { area: area.to_string(), ..Default::default() };
        }
    }
    pub fn is_open(&self, sec: &Section) -> bool {
        self.sections.get(&sec.id).copied().unwrap_or(sec.open)
    }
}

/// What the caller should do after a key or click.
#[derive(Debug, PartialEq)]
pub enum Act {
    /// Not ours: let the normal handlers see the key.
    Pass,
    /// Handled, nothing to send (redraw).
    Done,
    /// Send this operation to the host.
    Send(Value),
    /// Close Settings (Escape with nothing local to close).
    Close,
    /// Move focus to the area list on the left.
    Nav,
}

fn with(op: &Value, fields: &[(&str, Value)]) -> Value {
    let mut out = op.clone();
    if let Value::Object(map) = &mut out {
        for (k, v) in fields {
            map.insert((*k).to_string(), v.clone());
        }
    }
    out
}

fn move_focus(st: &mut PageState, delta: i64) {
    if st.order.is_empty() {
        return;
    }
    let at = st.order.iter().position(|i| *i == st.focus).map_or(if delta >= 0 { -1 } else { st.order.len() as i64 }, |i| i as i64);
    let to = (at + delta).clamp(0, st.order.len() as i64 - 1) as usize;
    st.focus = st.order[to].clone();
}

fn stepped(value: f64, delta: f64, min: f64, max: f64) -> f64 {
    (value + delta).clamp(min, max)
}

fn num(v: f64) -> Value {
    if v.fract() == 0.0 {
        json!(v as i64)
    } else {
        json!(v)
    }
}

/// Apply a Left/Right (`dir` = ∓1) to the focused composite control.
fn adjust(page: &Page, st: &mut PageState, dir: i64) -> Act {
    match page.find(&st.focus) {
        Some(Target::Row(r)) => match &r.control {
            Control::Segmented { values, options, active, op } => {
                let to = (*active as i64 + dir).clamp(0, options.len() as i64 - 1) as usize;
                if to == *active {
                    return Act::Done;
                }
                Act::Send(with(op, &[("value", values.get(to).cloned().unwrap_or_else(|| json!(options[to])))]))
            }
            Control::Stepper { value, min, max, step, op, .. } => {
                let next = stepped(*value, dir as f64 * step, *min, *max);
                if (next - value).abs() < f64::EPSILON {
                    return Act::Done;
                }
                Act::Send(with(op, &[("value", num(next))]))
            }
            _ if dir < 0 => Act::Nav,
            _ => Act::Done,
        },
        Some(Target::Tabs(t)) => {
            let to = (t.active as i64 + dir).clamp(0, t.items.len() as i64 - 1) as usize;
            if to == t.active {
                Act::Done
            } else {
                Act::Send(with(&t.op, &[("value", json!(to))]))
            }
        }
        Some(Target::Scope(sc)) => {
            let to = (sc.value as i64 + dir).clamp(0, sc.options.len() as i64 - 1) as usize;
            if to == sc.value {
                Act::Done
            } else {
                Act::Send(with(&sc.op, &[("value", json!(to))]))
            }
        }
        Some(Target::Section(sec)) => {
            let open = st.is_open(sec);
            if dir > 0 && !open {
                st.sections.insert(sec.id.clone(), true);
                Act::Done
            } else if dir < 0 && open && sec.collapsible {
                st.sections.insert(sec.id.clone(), false);
                Act::Done
            } else if dir < 0 {
                Act::Nav
            } else {
                Act::Done
            }
        }
        _ if dir < 0 => Act::Nav,
        _ => Act::Done,
    }
}

/// Enter or Space on the focused stop.
fn activate(page: &Page, st: &mut PageState) -> Act {
    match page.find(&st.focus) {
        Some(Target::Row(r)) => match &r.control {
            Control::Toggle { on, locked, op } => {
                if locked.is_empty() {
                    Act::Send(with(op, &[("value", json!(!on))]))
                } else {
                    Act::Done
                }
            }
            Control::Select { value, options, .. } => {
                let highlighted = options.iter().position(|(label, _)| label == value).unwrap_or(0);
                st.popup = Some(Popup { id: st.focus.clone(), highlighted });
                Act::Done
            }
            Control::Text { value, .. } => {
                st.edit = Some(Edit { id: st.focus.clone(), text: TextState::new(value) });
                Act::Done
            }
            Control::Button(b) => Act::Send(b.op.clone()),
            _ => Act::Done,
        },
        Some(Target::Button(b)) => Act::Send(b.op.clone()),
        Some(Target::Section(sec)) => {
            if sec.collapsible {
                let open = st.is_open(sec);
                st.sections.insert(sec.id.clone(), !open);
            }
            Act::Done
        }
        Some(Target::OrderedAdd(o)) if o.editable => Act::Send(with(&o.op, &[("action", json!("add")), ("index", json!(o.items.len()))])),
        _ => Act::Done,
    }
}

/// Keys while a select popup is open.
fn popup_key(page: &Page, st: &mut PageState, key: KeyEvent) -> Act {
    let Some(popup) = st.popup.as_mut() else { return Act::Pass };
    let Some(Target::Row(Row { control: Control::Select { options, op, .. }, .. })) = page.find(&popup.id) else {
        st.popup = None;
        return Act::Done;
    };
    match key.code {
        KeyCode::Up => popup.highlighted = popup.highlighted.saturating_sub(1),
        KeyCode::Down => popup.highlighted = (popup.highlighted + 1).min(options.len().saturating_sub(1)),
        KeyCode::Home => popup.highlighted = 0,
        KeyCode::End => popup.highlighted = options.len().saturating_sub(1),
        KeyCode::Esc => st.popup = None,
        KeyCode::Enter => {
            let chosen = options.get(popup.highlighted).map(|(_, v)| v.clone());
            st.popup = None;
            if let Some(v) = chosen {
                return Act::Send(with(op, &[("value", v)]));
            }
        }
        KeyCode::Char(c) => {
            let lc = c.to_lowercase().next().unwrap_or(c);
            if let Some(i) = options.iter().position(|(l, _)| l.to_lowercase().starts_with(lc)) {
                popup.highlighted = i;
            }
        }
        _ => {}
    }
    Act::Done
}

/// Keys while a text field is being edited.
fn edit_key(page: &Page, st: &mut PageState, key: KeyEvent) -> Act {
    let Some(edit) = st.edit.as_mut() else { return Act::Pass };
    match key.code {
        KeyCode::Esc => st.edit = None,
        KeyCode::Enter => {
            let value = edit.text.value.clone();
            let id = edit.id.clone();
            st.edit = None;
            if let Some(Target::Row(Row { control: Control::Text { op, .. }, .. })) = page.find(&id) {
                return Act::Send(with(op, &[("value", json!(value))]));
            }
        }
        KeyCode::Backspace => edit.text.backspace(),
        KeyCode::Left => edit.text.left(),
        KeyCode::Right => edit.text.right(),
        KeyCode::Char(c) if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) && edit.text.value.chars().count() < 400 => edit.text.insert(c),
        _ => {}
    }
    Act::Done
}

pub fn key(page: &Page, st: &mut PageState, key: KeyEvent) -> Act {
    if st.popup.is_some() {
        return popup_key(page, st, key);
    }
    if st.edit.is_some() {
        return edit_key(page, st, key);
    }
    if st.focus.is_empty() || !st.order.contains(&st.focus) {
        if let Some(first) = st.order.first() {
            st.focus = first.clone();
        }
    }
    let alt = key.modifiers.contains(KeyModifiers::ALT);
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    match key.code {
        KeyCode::Esc => Act::Close,
        KeyCode::Up | KeyCode::Down if alt => reorder(page, st, if key.code == KeyCode::Up { -1 } else { 1 }),
        KeyCode::Up => {
            move_focus(st, -1);
            Act::Done
        }
        KeyCode::Down => {
            move_focus(st, 1);
            Act::Done
        }
        KeyCode::Home => {
            if let Some(first) = st.order.first() {
                st.focus = first.clone();
            }
            Act::Done
        }
        KeyCode::End => {
            if let Some(last) = st.order.last() {
                st.focus = last.clone();
            }
            Act::Done
        }
        KeyCode::PageUp if ctrl => tab_step(page, -1),
        KeyCode::PageDown if ctrl => tab_step(page, 1),
        KeyCode::PageUp => {
            move_focus(st, -8);
            Act::Done
        }
        KeyCode::PageDown => {
            move_focus(st, 8);
            Act::Done
        }
        KeyCode::Tab => {
            let n = st.order.len() as i64;
            if n > 0 {
                let at = st.order.iter().position(|i| *i == st.focus).map_or(-1, |i| i as i64);
                st.focus = st.order[((at + 1).rem_euclid(n)) as usize].clone();
            }
            Act::Done
        }
        KeyCode::BackTab => {
            let n = st.order.len() as i64;
            if n > 0 {
                let at = st.order.iter().position(|i| *i == st.focus).map_or(0, |i| i as i64);
                st.focus = st.order[((at - 1).rem_euclid(n)) as usize].clone();
            }
            Act::Done
        }
        KeyCode::Left => adjust(page, st, -1),
        KeyCode::Right => adjust(page, st, 1),
        KeyCode::Enter => activate(page, st),
        KeyCode::Char(' ') => activate(page, st),
        KeyCode::Delete | KeyCode::Backspace => remove(page, st),
        _ => Act::Pass,
    }
}

fn tab_step(page: &Page, dir: i64) -> Act {
    let Some(t) = page.first_tabs() else { return Act::Done };
    let to = (t.active as i64 + dir).clamp(0, t.items.len() as i64 - 1) as usize;
    if to == t.active {
        Act::Done
    } else {
        Act::Send(with(&t.op, &[("value", json!(to))]))
    }
}

fn reorder(page: &Page, st: &mut PageState, delta: i64) -> Act {
    let Some(Target::OrderedItem(o, i)) = page.find(&st.focus) else { return Act::Done };
    let to = i as i64 + delta;
    if !o.editable || to < 0 || to as usize >= o.items.len() {
        return Act::Done;
    }
    // Focus follows the moved item: ids are positions, and the host keeps the order.
    st.focus = format!("{}:{to}", ord_id(&o.id));
    Act::Send(with(&o.op, &[("action", json!(if delta < 0 { "up" } else { "down" })), ("index", json!(i))]))
}

fn remove(page: &Page, st: &mut PageState) -> Act {
    let Some(Target::OrderedItem(o, i)) = page.find(&st.focus) else { return Act::Pass };
    if !o.editable {
        return Act::Done;
    }
    if i + 1 == o.items.len() && i > 0 {
        st.focus = format!("{}:{}", ord_id(&o.id), i - 1);
    }
    Act::Send(with(&o.op, &[("action", json!("remove")), ("index", json!(i))]))
}

/// A left click at (x, y). Returns what to do; sets focus to what was clicked.
pub fn click(page: &Page, st: &mut PageState, x: u16, y: u16) -> Act {
    let Some((id, part)) = st.hits.at(x, y).map(|(i, p)| (i.to_string(), p.clone())) else {
        if st.popup.take().is_some() {
            return Act::Done;
        }
        return Act::Pass;
    };
    // A click on a popup row picks it.
    if let Some(rest) = id.strip_prefix("popup:") {
        let (row, index) = rest.rsplit_once(':').unwrap_or((rest, "0"));
        let index: usize = index.parse().unwrap_or(0);
        st.popup = None;
        if let Some(Target::Row(Row { control: Control::Select { options, op, .. }, .. })) = page.find(row) {
            if let Some((_, v)) = options.get(index) {
                return Act::Send(with(op, &[("value", v.clone())]));
            }
        }
        return Act::Done;
    }
    st.popup = None;
    st.edit = None;
    st.focus = id.clone();
    let named = match &part {
        Part::Named(n) => Some(n.as_str()),
        Part::Body => None,
    };
    match page.find(&id) {
        Some(Target::Row(r)) => match (&r.control, named) {
            (Control::Segmented { values, options, op, .. }, Some(n)) => {
                let i: usize = n.strip_prefix("segment:").and_then(|x| x.parse().ok()).unwrap_or(0);
                Act::Send(with(op, &[("value", values.get(i).cloned().unwrap_or_else(|| json!(options.get(i).cloned().unwrap_or_default())))]))
            }
            (Control::Stepper { value, min, max, step, op, .. }, Some(n)) => {
                let d = if n == "inc" { *step } else { -*step };
                Act::Send(with(op, &[("value", num(stepped(*value, d, *min, *max)))]))
            }
            _ => activate(page, st),
        },
        Some(Target::Tabs(t)) => match named.and_then(|n| n.strip_prefix("tab:")).and_then(|i| i.parse::<usize>().ok()) {
            Some(i) if i != t.active && i < t.items.len() => Act::Send(with(&t.op, &[("value", json!(i))])),
            _ => Act::Done,
        },
        Some(Target::OrderedItem(o, i)) if o.editable => match named {
            Some("up") if i > 0 => Act::Send(with(&o.op, &[("action", json!("up")), ("index", json!(i))])),
            Some("down") if i + 1 < o.items.len() => Act::Send(with(&o.op, &[("action", json!("down")), ("index", json!(i))])),
            Some("remove") => Act::Send(with(&o.op, &[("action", json!("remove")), ("index", json!(i))])),
            _ => Act::Done,
        },
        Some(Target::Scope(sc)) => match named.and_then(|n| n.strip_prefix("segment:")).and_then(|i| i.parse::<usize>().ok()) {
            Some(i) if i != sc.value => Act::Send(with(&sc.op, &[("value", json!(i))])),
            _ => Act::Done,
        },
        Some(_) => activate(page, st),
        None => Act::Done,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::settings_page::model::tests::sample;
    use ratatui::crossterm::event::KeyEvent;

    fn page() -> Page {
        Page::from_value(&sample())
    }
    fn state(page: &Page) -> PageState {
        // The drawn order of the sample page: toggle, segmented, select, tabs, two ordered rows, add, section.
        let _ = page;
        PageState {
            order: ["row:titles", "row:eff", "row:dev", "tabs:tiers", "ord:tier:low:0", "ord:tier:low:1", "ord:tier:low:add", "sec:prov"]
                .iter()
                .map(|s| s.to_string())
                .collect(),
            focus: "row:titles".into(),
            ..Default::default()
        }
    }
    fn press(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }
    fn alt(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::ALT)
    }

    #[test]
    fn up_down_walk_the_stops_and_clamp() {
        let (p, mut st) = (page(), state(&page()));
        key(&p, &mut st, press(KeyCode::Down));
        assert_eq!(st.focus, "row:eff");
        for _ in 0..20 {
            key(&p, &mut st, press(KeyCode::Down));
        }
        assert_eq!(st.focus, "sec:prov");
        key(&p, &mut st, press(KeyCode::Home));
        assert_eq!(st.focus, "row:titles");
    }

    #[test]
    fn space_toggles_with_the_negated_value_and_a_locked_toggle_does_nothing() {
        let (p, mut st) = (page(), state(&page()));
        let act = key(&p, &mut st, press(KeyCode::Char(' ')));
        assert_eq!(act, Act::Send(json!({"kind": "sp_models", "key": "titles", "value": false})));
    }

    #[test]
    fn left_right_change_a_segmented_control_and_left_elsewhere_goes_to_the_area_list() {
        let (p, mut st) = (page(), state(&page()));
        st.focus = "row:eff".into();
        assert_eq!(key(&p, &mut st, press(KeyCode::Left)), Act::Send(json!({"kind": "sp_models", "key": "eff", "value": "low"})));
        assert_eq!(key(&p, &mut st, press(KeyCode::Right)), Act::Done, "already on the last option");
        st.focus = "row:titles".into();
        assert_eq!(key(&p, &mut st, press(KeyCode::Left)), Act::Nav);
    }

    #[test]
    fn select_opens_a_popup_and_enter_sends_the_highlighted_value() {
        let (p, mut st) = (page(), state(&page()));
        st.focus = "row:dev".into();
        assert_eq!(key(&p, &mut st, press(KeyCode::Enter)), Act::Done);
        assert!(st.popup.is_some());
        key(&p, &mut st, press(KeyCode::Down));
        assert_eq!(key(&p, &mut st, press(KeyCode::Enter)), Act::Send(json!({"kind": "sp_models", "key": "dev", "value": "cpu"})));
        assert!(st.popup.is_none());
        // Escape closes the popup first and does not close Settings.
        key(&p, &mut st, press(KeyCode::Enter));
        assert_eq!(key(&p, &mut st, press(KeyCode::Esc)), Act::Done);
        assert_eq!(key(&p, &mut st, press(KeyCode::Esc)), Act::Close);
    }

    #[test]
    fn alt_arrows_reorder_and_focus_follows_the_moved_item() {
        let (p, mut st) = (page(), state(&page()));
        st.focus = "ord:tier:low:0".into();
        let act = key(&p, &mut st, alt(KeyCode::Down));
        assert_eq!(act, Act::Send(json!({"kind": "sp_models", "key": "tier", "tier": "low", "action": "down", "index": 0})));
        assert_eq!(st.focus, "ord:tier:low:1");
        assert_eq!(key(&p, &mut st, alt(KeyCode::Down)), Act::Done, "cannot move past the end");
    }

    #[test]
    fn delete_removes_and_the_add_row_asks_to_add() {
        let (p, mut st) = (page(), state(&page()));
        st.focus = "ord:tier:low:1".into();
        assert_eq!(key(&p, &mut st, press(KeyCode::Delete)), Act::Send(json!({"kind": "sp_models", "key": "tier", "tier": "low", "action": "remove", "index": 1})));
        assert_eq!(st.focus, "ord:tier:low:0", "focus moves to the surviving neighbour");
        st.focus = "ord:tier:low:add".into();
        assert_eq!(key(&p, &mut st, press(KeyCode::Enter)), Act::Send(json!({"kind": "sp_models", "key": "tier", "tier": "low", "action": "add", "index": 2})));
    }

    #[test]
    fn tabs_switch_with_arrows_and_ctrl_page_keys() {
        let (p, mut st) = (page(), state(&page()));
        st.focus = "tabs:tiers".into();
        assert_eq!(key(&p, &mut st, press(KeyCode::Right)), Act::Send(json!({"kind": "sp_models", "key": "tab", "value": 1})));
        st.focus = "row:titles".into();
        let ctrl = KeyEvent::new(KeyCode::PageDown, KeyModifiers::CONTROL);
        assert_eq!(key(&p, &mut st, ctrl), Act::Send(json!({"kind": "sp_models", "key": "tab", "value": 1})), "from anywhere in the page");
    }

    #[test]
    fn sections_toggle_locally_and_keep_their_state() {
        let (p, mut st) = (page(), state(&page()));
        st.focus = "sec:prov".into();
        let sec = match &p.blocks[6] {
            Block::Section(s) => s.clone(),
            _ => unreachable!(),
        };
        assert!(!st.is_open(&sec));
        key(&p, &mut st, press(KeyCode::Right));
        assert!(st.is_open(&sec));
        key(&p, &mut st, press(KeyCode::Left));
        assert!(!st.is_open(&sec));
        assert_eq!(key(&p, &mut st, press(KeyCode::Left)), Act::Nav, "a closed section's Left leaves for the area list");
    }

    #[test]
    fn keys_the_page_does_not_use_pass_through() {
        let (p, mut st) = (page(), state(&page()));
        assert_eq!(key(&p, &mut st, KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)), Act::Pass);
    }

    #[test]
    fn text_fields_edit_locally_and_send_once_on_enter() {
        let v = json!({"area": "x", "blocks": [{"t": "row", "id": "k", "label": "Key", "control": {"c": "text", "value": "ab", "secret": true, "placeholder": "", "operation": {"kind": "sp_x", "key": "k"}}}]});
        let p = Page::from_value(&v);
        let mut st = PageState { order: vec!["row:k".into()], focus: "row:k".into(), ..Default::default() };
        key(&p, &mut st, press(KeyCode::Enter));
        assert!(st.edit.is_some());
        key(&p, &mut st, press(KeyCode::Char('c')));
        key(&p, &mut st, press(KeyCode::Backspace));
        key(&p, &mut st, press(KeyCode::Char('z')));
        assert_eq!(key(&p, &mut st, press(KeyCode::Enter)), Act::Send(json!({"kind": "sp_x", "key": "k", "value": "abz"})));
        key(&p, &mut st, press(KeyCode::Enter));
        assert_eq!(key(&p, &mut st, press(KeyCode::Esc)), Act::Done, "Escape cancels the edit without sending or closing");
        assert!(st.edit.is_none());
    }

    #[test]
    fn a_new_area_starts_with_fresh_focus_and_scroll() {
        let mut st = PageState { focus: "row:x".into(), area: "models".into(), ..Default::default() };
        st.scroll.offset = 12;
        st.reset_for("models");
        assert_eq!(st.scroll.offset, 12, "same area keeps its place across host rebuilds");
        st.reset_for("voice");
        assert!(st.focus.is_empty() && st.scroll.offset == 0 && st.area == "voice");
    }
}
