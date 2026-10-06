//! The typed Settings page as the host sends it (`nexus/ui_support/settings_page.py`).
//!
//! Parsing is tolerant: a missing field has a default and an unknown block becomes a
//! visible warning note instead of vanishing (nothing the host sends is hidden).
use serde_json::Value;

#[derive(Clone, Debug, Default)]
pub struct Page {
    pub area: String,
    pub title: String,
    pub intro: String,
    pub footer: String,
    pub scope: Option<Scope>,
    pub blocks: Vec<Block>,
}

#[derive(Clone, Debug)]
pub struct Scope {
    pub options: Vec<String>,
    pub value: usize,
    pub op: Value,
}

#[derive(Clone, Debug)]
pub enum Block {
    Heading(String),
    Note {
        text: String,
        tone: String,
    },
    Gap,
    Row(Row),
    Tabs(Tabs),
    Ordered(Ordered),
    Buttons(Buttons),
    Section(Section),
    Table {
        cols: Vec<(String, u16)>,
        rows: Vec<Vec<String>>,
    },
    Progress {
        fraction: f32,
        label: String,
    },
    Callout {
        level: String,
        text: String,
        action: Option<Btn>,
    },
}

#[derive(Clone, Debug)]
pub struct Row {
    pub id: String,
    pub label: String,
    pub description: String,
    pub scope: String,
    pub error: String,
    pub control: Control,
}

#[derive(Clone, Debug)]
pub enum Control {
    Toggle {
        on: bool,
        locked: String,
        op: Value,
    },
    Segmented {
        options: Vec<String>,
        values: Vec<Value>,
        active: usize,
        op: Value,
    },
    Select {
        value: String,
        options: Vec<(String, Value)>,
        op: Value,
    },
    Stepper {
        display: String,
        value: f64,
        min: f64,
        max: f64,
        step: f64,
        op: Value,
    },
    Text {
        value: String,
        secret: bool,
        placeholder: String,
        op: Value,
    },
    Button(Btn),
    Readout(String),
}

#[derive(Clone, Debug)]
pub struct Btn {
    pub label: String,
    pub variant: String,
    pub op: Value,
}

#[derive(Clone, Debug)]
pub struct Tabs {
    pub id: String,
    pub items: Vec<(String, String)>,
    pub active: usize,
    pub op: Value,
}

#[derive(Clone, Debug)]
pub struct OItem {
    pub label: String,
    pub tag: String,
    pub note: String,
}

#[derive(Clone, Debug)]
pub struct Ordered {
    pub id: String,
    pub items: Vec<OItem>,
    pub add_label: String,
    pub editable: bool,
    pub op: Value,
}

#[derive(Clone, Debug)]
pub struct Buttons {
    pub id: String,
    pub label: String,
    pub items: Vec<Btn>,
}

#[derive(Clone, Debug)]
pub struct Section {
    pub id: String,
    pub title: String,
    pub summary: String,
    pub tone: String,
    pub open: bool,
    pub collapsible: bool,
    pub blocks: Vec<Block>,
}

fn s(v: &Value, key: &str) -> String {
    v.get(key).and_then(Value::as_str).unwrap_or("").to_string()
}
fn b(v: &Value, key: &str, default: bool) -> bool {
    v.get(key).and_then(Value::as_bool).unwrap_or(default)
}
fn n(v: &Value, key: &str, default: f64) -> f64 {
    v.get(key).and_then(Value::as_f64).unwrap_or(default)
}
fn u(v: &Value, key: &str) -> usize {
    v.get(key).and_then(Value::as_u64).unwrap_or(0) as usize
}
fn op(v: &Value) -> Value {
    v.get("operation").cloned().unwrap_or(Value::Null)
}
fn arr<'a>(v: &'a Value, key: &str) -> &'a [Value] {
    v.get(key)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

fn btn(v: &Value) -> Btn {
    Btn {
        label: s(v, "label"),
        variant: s(v, "variant"),
        op: op(v),
    }
}

fn control(v: &Value) -> Control {
    match s(v, "c").as_str() {
        "toggle" => Control::Toggle {
            on: b(v, "on", false),
            locked: s(v, "locked"),
            op: op(v),
        },
        "segmented" => Control::Segmented {
            options: arr(v, "options")
                .iter()
                .map(|o| o.as_str().unwrap_or("").to_string())
                .collect(),
            values: arr(v, "values").to_vec(),
            active: u(v, "active"),
            op: op(v),
        },
        "select" => Control::Select {
            value: s(v, "value"),
            options: arr(v, "options")
                .iter()
                .map(|o| {
                    (
                        o.get(0).and_then(Value::as_str).unwrap_or("").to_string(),
                        o.get(1).cloned().unwrap_or(Value::Null),
                    )
                })
                .collect(),
            op: op(v),
        },
        "stepper" => Control::Stepper {
            display: s(v, "display"),
            value: n(v, "value", 0.0),
            min: n(v, "min", f64::MIN),
            max: n(v, "max", f64::MAX),
            step: n(v, "step", 1.0),
            op: op(v),
        },
        "text" => Control::Text {
            value: s(v, "value"),
            secret: b(v, "secret", false),
            placeholder: s(v, "placeholder"),
            op: op(v),
        },
        "button" => Control::Button(btn(v)),
        _ => Control::Readout(s(v, "value")),
    }
}

pub fn blocks(list: &[Value]) -> Vec<Block> {
    list.iter().take(400).map(block).collect()
}

fn block(v: &Value) -> Block {
    match s(v, "t").as_str() {
        "heading" => Block::Heading(s(v, "text")),
        "note" => Block::Note {
            text: s(v, "text"),
            tone: s(v, "tone"),
        },
        "gap" => Block::Gap,
        "row" => Block::Row(Row {
            id: s(v, "id"),
            label: s(v, "label"),
            description: s(v, "description"),
            scope: s(v, "scope"),
            error: s(v, "error"),
            control: control(v.get("control").unwrap_or(&Value::Null)),
        }),
        "tabs" => Block::Tabs(Tabs {
            id: s(v, "id"),
            items: arr(v, "items")
                .iter()
                .map(|i| {
                    (
                        i.get(0).and_then(Value::as_str).unwrap_or("").to_string(),
                        i.get(1).and_then(Value::as_str).unwrap_or("").to_string(),
                    )
                })
                .collect(),
            active: u(v, "active"),
            op: op(v),
        }),
        "ordered" => Block::Ordered(Ordered {
            id: s(v, "id"),
            items: arr(v, "items")
                .iter()
                .map(|i| OItem {
                    label: i.get(0).and_then(Value::as_str).unwrap_or("").to_string(),
                    tag: i.get(1).and_then(Value::as_str).unwrap_or("").to_string(),
                    note: i.get(2).and_then(Value::as_str).unwrap_or("").to_string(),
                })
                .collect(),
            add_label: s(v, "add_label"),
            editable: b(v, "editable", true),
            op: op(v),
        }),
        "buttons" => Block::Buttons(Buttons {
            id: s(v, "id"),
            label: s(v, "label"),
            items: arr(v, "items").iter().map(btn).collect(),
        }),
        "section" => Block::Section(Section {
            id: s(v, "id"),
            title: s(v, "title"),
            summary: s(v, "summary"),
            tone: s(v, "tone"),
            open: b(v, "open", true),
            collapsible: b(v, "collapsible", true),
            blocks: blocks(arr(v, "blocks")),
        }),
        "table" => Block::Table {
            cols: arr(v, "cols")
                .iter()
                .map(|c| {
                    (
                        c.get(0).and_then(Value::as_str).unwrap_or("").to_string(),
                        c.get(1).and_then(Value::as_u64).unwrap_or(0) as u16,
                    )
                })
                .collect(),
            rows: arr(v, "rows")
                .iter()
                .map(|r| {
                    r.as_array()
                        .map(|c| {
                            c.iter()
                                .map(|x| x.as_str().unwrap_or("").to_string())
                                .collect()
                        })
                        .unwrap_or_default()
                })
                .collect(),
        },
        "progress" => Block::Progress {
            fraction: n(v, "fraction", 0.0) as f32,
            label: s(v, "label"),
        },
        "callout" => Block::Callout {
            level: s(v, "level"),
            text: s(v, "text"),
            action: v.get("action").filter(|a| !a.is_null()).map(btn),
        },
        other => Block::Note {
            text: format!("[unsupported settings block: {other}]"),
            tone: "warning".into(),
        },
    }
}

impl Page {
    pub fn from_value(v: &Value) -> Page {
        Page {
            area: s(v, "area"),
            title: s(v, "title"),
            intro: s(v, "intro"),
            footer: s(v, "footer"),
            scope: v.get("scope").filter(|x| !x.is_null()).map(|x| Scope {
                options: arr(x, "options")
                    .iter()
                    .map(|o| o.as_str().unwrap_or("").to_string())
                    .collect(),
                value: u(x, "value"),
                op: op(x),
            }),
            blocks: blocks(arr(v, "blocks")),
        }
    }
}

/// What a focus stop id refers to. Ids are stable strings derived from page data.
#[derive(Debug)]
pub enum Target<'a> {
    Scope(&'a Scope),
    Row(&'a Row),
    Tabs(&'a Tabs),
    OrderedItem(&'a Ordered, usize),
    OrderedAdd(&'a Ordered),
    Button(&'a Btn),
    Section(&'a Section),
}

pub fn row_id(id: &str) -> String {
    format!("row:{id}")
}
pub fn tabs_id(id: &str) -> String {
    format!("tabs:{id}")
}
pub fn ord_id(id: &str) -> String {
    format!("ord:{id}")
}
pub fn btn_id(id: &str, k: usize) -> String {
    format!("btn:{id}:{k}")
}
/// Callout buttons are identified by their text (callouts have no id of their own).
pub fn co_id(text: &str) -> String {
    format!("co:{}", text.chars().take(40).collect::<String>())
}
pub fn sec_id(id: &str) -> String {
    format!("sec:{id}")
}

impl Page {
    /// Resolve a focus id to its target (searching sections, whatever their open state).
    pub fn find(&self, id: &str) -> Option<Target<'_>> {
        if id == "scope" {
            return self.scope.as_ref().map(Target::Scope);
        }
        find_in(&self.blocks, id)
    }
    /// The first tabs block (for Ctrl+PgUp/PgDn).
    pub fn first_tabs(&self) -> Option<&Tabs> {
        fn go(blocks: &[Block]) -> Option<&Tabs> {
            blocks.iter().find_map(|b| match b {
                Block::Tabs(t) => Some(t),
                Block::Section(s) => go(&s.blocks),
                _ => None,
            })
        }
        go(&self.blocks)
    }
}

fn find_in<'a>(blocks: &'a [Block], id: &str) -> Option<Target<'a>> {
    for block in blocks {
        match block {
            Block::Row(r) if row_id(&r.id) == id => return Some(Target::Row(r)),
            Block::Tabs(t) if tabs_id(&t.id) == id => return Some(Target::Tabs(t)),
            Block::Ordered(o) => {
                let base = ord_id(&o.id);
                if id == format!("{base}:add") {
                    return Some(Target::OrderedAdd(o));
                }
                if let Some(i) = id
                    .strip_prefix(&format!("{base}:"))
                    .and_then(|i| i.parse::<usize>().ok())
                {
                    if i < o.items.len() {
                        return Some(Target::OrderedItem(o, i));
                    }
                }
            }
            Block::Buttons(bs) => {
                for (k, item) in bs.items.iter().enumerate() {
                    if btn_id(&bs.id, k) == id {
                        return Some(Target::Button(item));
                    }
                }
            }
            Block::Callout {
                action: Some(a),
                text,
                ..
            } if id == co_id(text) => return Some(Target::Button(a)),
            Block::Section(sec) => {
                if sec_id(&sec.id) == id {
                    return Some(Target::Section(sec));
                }
                if let Some(t) = find_in(&sec.blocks, id) {
                    return Some(t);
                }
            }
            _ => {}
        }
    }
    None
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use serde_json::json;

    pub fn sample() -> Value {
        json!({
            "area": "models", "title": "Models", "intro": "i", "footer": "Saved in ~/.nexus/config.toml",
            "scope": null,
            "blocks": [
                {"t": "heading", "text": "DEFAULT"},
                {"t": "row", "id": "titles", "label": "Titles", "description": "d", "scope": "global",
                 "control": {"c": "toggle", "on": true, "locked": "", "operation": {"kind": "sp_models", "key": "titles"}}},
                {"t": "row", "id": "eff", "label": "Effort", "control": {"c": "segmented", "options": ["Low", "High"], "values": ["low", "high"], "active": 1, "operation": {"kind": "sp_models", "key": "eff"}}},
                {"t": "row", "id": "dev", "label": "Device", "control": {"c": "select", "value": "auto", "options": [["auto", "auto"], ["cpu", "cpu"]], "operation": {"kind": "sp_models", "key": "dev"}}},
                {"t": "tabs", "id": "tiers", "items": [["Low", ""], ["High", "!"]], "active": 0, "operation": {"kind": "sp_models", "key": "tab"}},
                {"t": "ordered", "id": "tier:low", "items": [["a/b", "in use", ""], ["c/d", "skipped", "no key"]], "add_label": "Add model…", "editable": true, "operation": {"kind": "sp_models", "key": "tier", "tier": "low"}},
                {"t": "section", "id": "prov", "title": "OpenAI", "summary": "ok", "open": false, "blocks": [
                    {"t": "buttons", "id": "act", "items": [{"label": "Disconnect", "operation": {"kind": "x"}, "variant": "danger"}]}]},
                {"t": "mystery"}
            ]
        })
    }

    #[test]
    fn parses_every_block_and_control() {
        let page = Page::from_value(&sample());
        assert_eq!((page.area.as_str(), page.blocks.len()), ("models", 8));
        assert!(matches!(
            &page.blocks[1],
            Block::Row(Row {
                control: Control::Toggle { on: true, .. },
                ..
            })
        ));
        assert!(matches!(
            &page.blocks[2],
            Block::Row(Row {
                control: Control::Segmented { active: 1, .. },
                ..
            })
        ));
        assert!(
            matches!(&page.blocks[3], Block::Row(Row { control: Control::Select { options, .. }, .. }) if options.len() == 2)
        );
    }

    #[test]
    fn unknown_blocks_are_visible_not_dropped() {
        let page = Page::from_value(&sample());
        assert!(
            matches!(page.blocks.last(), Some(Block::Note { text, tone }) if text.contains("mystery") && tone == "warning")
        );
    }

    #[test]
    fn ids_resolve_to_their_targets_including_inside_closed_sections() {
        let page = Page::from_value(&sample());
        assert!(matches!(page.find("row:titles"), Some(Target::Row(_))));
        assert!(matches!(page.find("tabs:tiers"), Some(Target::Tabs(_))));
        assert!(matches!(
            page.find("ord:tier:low:1"),
            Some(Target::OrderedItem(_, 1))
        ));
        assert!(matches!(
            page.find("ord:tier:low:add"),
            Some(Target::OrderedAdd(_))
        ));
        assert!(matches!(page.find("sec:prov"), Some(Target::Section(_))));
        assert!(matches!(page.find("btn:act:0"), Some(Target::Button(_))));
        assert!(page.find("ord:tier:low:9").is_none() && page.find("nope").is_none());
        assert_eq!(page.first_tabs().unwrap().items.len(), 2);
    }

    #[test]
    fn an_empty_or_null_page_is_harmless() {
        let page = Page::from_value(&Value::Null);
        assert!(page.blocks.is_empty() && page.scope.is_none());
    }
}
