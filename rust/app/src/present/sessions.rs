//! Sidebar session items from the `SessionList` result, and small id/path helpers.

use serde_json::Value;

fn str_of<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

/// One session in the sidebar.
#[derive(Clone, Debug, PartialEq)]
pub struct SessionItem {
    pub id: String,
    pub title: String,
    pub state: String,
}

/// Last path component of a workspace path.
pub fn basename(path: &str) -> String {
    path.trim_end_matches(['/', '\\'])
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or("")
        .to_owned()
}

/// `SessionList` result -> sidebar items (title, else name, else id).
pub fn parse_sessions(v: &Value) -> Vec<SessionItem> {
    let Some(list) = v.get("sessions").and_then(Value::as_array) else {
        return Vec::new();
    };
    list.iter()
        .filter_map(|s| {
            let id = str_of(s, "id");
            if id.is_empty() {
                return None;
            }
            let title = [str_of(s, "title"), str_of(s, "name"), id]
                .into_iter()
                .find(|t| !t.is_empty())
                .unwrap_or(id);
            Some(SessionItem {
                id: id.to_owned(),
                title: title.to_owned(),
                state: str_of(s, "state").to_owned(),
            })
        })
        .collect()
}

/// New session id: `session-` + 8 hex chars from a millisecond clock.
pub fn new_session_id(unix_ms: f64) -> String {
    let n = unix_ms as u64;
    let mixed = (n ^ (n >> 17)).wrapping_mul(0x9E37_79B1);
    format!("session-{:08x}", mixed as u32)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn sessions_pick_title_name_id() {
        let v = json!({"sessions":[
            {"id":"a","title":"T","name":"N","state":"idle"},
            {"id":"b","title":"","name":"N2","state":"running"},
            {"id":"c"}, {"title":"no id"}]});
        let s = parse_sessions(&v);
        assert_eq!(s.len(), 3);
        assert_eq!(s[0].title, "T");
        assert_eq!(s[1].title, "N2");
        assert_eq!(s[2].title, "c");
    }

    #[test]
    fn id_and_basename() {
        let id = new_session_id(1_700_000_000_123.0);
        assert_eq!(id.len(), 16);
        assert!(id.starts_with("session-"));
        assert_eq!(basename("/a/b/proj/"), "proj");
    }
}
