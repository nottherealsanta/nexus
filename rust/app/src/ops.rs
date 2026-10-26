//! Pure applier for `session-poll` / `session-events` patch frames
//! (docs/nsh-client.md, `nexus/view/browser_view.py::json_patch`).
//!
//! A frame is `{schema_version, session, seq, ops:[...]}` or `{..., resync:true}`.
//! Ops are RFC 6902 `add`/`replace`/`remove` plus `append` (string concat),
//! with RFC 6901 pointers; `add` at `/-` pushes onto an array and `""` replaces
//! the whole view. Any unknown op, missing path or type mismatch is an error
//! and the caller must re-snapshot. Frames with `seq <= cursor` are dropped.

use serde_json::Value;

/// Why an op could not be applied.
#[derive(Clone, Debug, PartialEq)]
pub struct OpError(pub String);

/// What [`apply_frame`] did.
#[derive(Clone, Debug, PartialEq)]
pub enum Applied {
    /// Ops applied; the cursor moved to the frame's `seq`.
    Applied,
    /// `seq <= cursor`: dropped.
    Skipped,
    /// The server asked for a re-snapshot, or an op failed.
    Resync(String),
}

fn err<T>(msg: impl Into<String>) -> Result<T, OpError> {
    Err(OpError(msg.into()))
}

fn tokens(path: &str) -> Result<Vec<String>, OpError> {
    if path.is_empty() {
        return Ok(Vec::new());
    }
    let Some(rest) = path.strip_prefix('/') else {
        return err(format!("bad pointer `{path}`"));
    };
    Ok(rest.split('/').map(|t| t.replace("~1", "/").replace("~0", "~")).collect())
}

fn index(token: &str, len: usize, allow_end: bool) -> Result<usize, OpError> {
    let i: usize = token.parse().map_err(|_| OpError(format!("bad array index `{token}`")))?;
    if i < len || (allow_end && i == len) { Ok(i) } else { err(format!("index {i} out of range ({len})")) }
}

fn walk<'a>(mut cur: &'a mut Value, tokens: &[String]) -> Result<&'a mut Value, OpError> {
    for t in tokens {
        cur = match cur {
            Value::Object(m) => m.get_mut(t).ok_or_else(|| OpError(format!("missing key `{t}`")))?,
            Value::Array(a) => {
                let i = index(t, a.len(), false)?;
                &mut a[i]
            }
            _ => return err(format!("cannot descend into scalar at `{t}`")),
        };
    }
    Ok(cur)
}

/// Applies one op to `view` in place.
pub fn apply_op(view: &mut Value, op: &Value) -> Result<(), OpError> {
    let kind = op.get("op").and_then(Value::as_str).unwrap_or("");
    let path = op.get("path").and_then(Value::as_str).ok_or_else(|| OpError("op without path".into()))?;
    let toks = tokens(path)?;
    let value = op.get("value");
    let need = || value.cloned().ok_or_else(|| OpError(format!("`{kind}` without value")));
    let Some((last, parent_toks)) = toks.split_last() else {
        return match kind {
            "add" | "replace" => {
                *view = need()?;
                Ok(())
            }
            _ => err(format!("`{kind}` at root")),
        };
    };
    let parent = walk(view, parent_toks)?;
    match (kind, parent) {
        ("add", Value::Object(m)) => {
            m.insert(last.clone(), need()?);
        }
        ("add", Value::Array(a)) => {
            if last == "-" {
                a.push(need()?);
            } else {
                let i = index(last, a.len(), true)?;
                a.insert(i, need()?);
            }
        }
        ("replace", Value::Object(m)) => match m.get_mut(last) {
            Some(slot) => *slot = need()?,
            None => return err(format!("replace of missing key `{last}`")),
        },
        ("replace", Value::Array(a)) => {
            let i = index(last, a.len(), false)?;
            a[i] = need()?;
        }
        ("remove", Value::Object(m)) => {
            if m.remove(last).is_none() {
                return err(format!("remove of missing key `{last}`"));
            }
        }
        ("remove", Value::Array(a)) => {
            let i = index(last, a.len(), false)?;
            a.remove(i);
        }
        ("append", parent) => {
            let Some(Value::String(extra)) = value else {
                return err("append needs a string value");
            };
            let slot = match parent {
                Value::Object(m) => m.get_mut(last),
                Value::Array(a) => index(last, a.len(), false).ok().map(|i| &mut a[i]),
                _ => None,
            };
            match slot {
                Some(Value::String(s)) => s.push_str(extra),
                _ => return err(format!("append target `{path}` is not a string")),
            }
        }
        (other, _) => return err(format!("unsupported op `{other}` at `{path}`")),
    }
    Ok(())
}

/// Applies a frame's ops in order. On error the view may be partly patched:
/// the caller must replace it with a fresh snapshot.
pub fn apply_ops(view: &mut Value, ops: &[Value]) -> Result<(), OpError> {
    ops.iter().try_for_each(|op| apply_op(view, op))
}

/// Applies one frame to `view`, advancing `cursor` only when it was applied.
pub fn apply_frame(view: &mut Value, cursor: &mut u64, frame: &Value) -> Applied {
    if frame.get("resync").and_then(Value::as_bool).unwrap_or(false) {
        return Applied::Resync("server requested resync".into());
    }
    let Some(seq) = frame.get("seq").and_then(Value::as_u64) else {
        return Applied::Resync("frame without seq".into());
    };
    if seq <= *cursor {
        return Applied::Skipped;
    }
    let Some(ops) = frame.get("ops").and_then(Value::as_array) else {
        return Applied::Resync("frame without ops".into());
    };
    match apply_ops(view, ops) {
        Ok(()) => {
            *cursor = seq;
            Applied::Applied
        }
        Err(OpError(e)) => Applied::Resync(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn run(view: &mut Value, ops: Value) -> Result<(), OpError> {
        apply_ops(view, ops.as_array().unwrap())
    }

    #[test]
    fn add_replace_remove_append_push() {
        let mut v = json!({"phase":"idle","turns":[{"messages":[{"blocks":[{"text":"he"}]}]}],"a/b":{"c~d":1}});
        run(&mut v, json!([
            {"op":"replace","path":"/phase","value":"running"},
            {"op":"append","path":"/turns/0/messages/0/blocks/0/text","value":"llo"},
            {"op":"add","path":"/turns/0/messages/0/blocks/-","value":{"text":"x"}},
            {"op":"add","path":"/extra","value":null},
            {"op":"replace","path":"/a~1b/c~0d","value":2},
            {"op":"remove","path":"/turns/0/messages/0/blocks/1"},
        ])).unwrap();
        assert_eq!(v["phase"], "running");
        assert_eq!(v["turns"][0]["messages"][0]["blocks"], json!([{"text":"hello"}]));
        assert_eq!(v["extra"], Value::Null);
        assert_eq!(v["a/b"]["c~d"], 2);
    }

    #[test]
    fn root_replace() {
        let mut v = json!([1]);
        run(&mut v, json!([{"op":"replace","path":"","value":{"turns":[]}}])).unwrap();
        assert_eq!(v, json!({"turns":[]}));
    }

    #[test]
    fn errors_signal_resync() {
        for ops in [
            json!([{"op":"replace","path":"/nope","value":1}]),
            json!([{"op":"remove","path":"/phase/x"}]),
            json!([{"op":"append","path":"/n","value":"x"}]),
            json!([{"op":"move","path":"/phase","value":1}]),
            json!([{"op":"add","path":"/turns/9","value":1}]),
            json!([{"op":"add","path":"bad","value":1}]),
        ] {
            let mut v = json!({"phase":"idle","n":1,"turns":[]});
            let mut cursor = 3;
            let frame = json!({"seq":4,"ops":ops});
            assert!(matches!(apply_frame(&mut v, &mut cursor, &frame), Applied::Resync(_)), "{frame}");
            assert_eq!(cursor, 3);
        }
    }

    #[test]
    fn frames_respect_cursor_and_resync_flag() {
        let mut v = json!({"phase":"idle"});
        let mut cursor = 5;
        let old = json!({"seq":5,"ops":[{"op":"replace","path":"/phase","value":"x"}]});
        assert_eq!(apply_frame(&mut v, &mut cursor, &old), Applied::Skipped);
        let new = json!({"seq":9,"ops":[{"op":"replace","path":"/phase","value":"running"}]});
        assert_eq!(apply_frame(&mut v, &mut cursor, &new), Applied::Applied);
        assert_eq!((cursor, v["phase"].as_str()), (9, Some("running")));
        let rs = json!({"schema_version":1,"session":"s","seq":10,"resync":true});
        assert!(matches!(apply_frame(&mut v, &mut cursor, &rs), Applied::Resync(_)));
        assert_eq!(cursor, 9);
    }
}

#[cfg(test)]
mod fixture_tests {
    //! Real frames captured from a daemon (`session-poll?from_seq=0` on an
    //! existing session) applied to the initial wire view must reproduce the
    //! `session-view` snapshot taken from the same session.
    use super::*;

    fn load(name: &str) -> Value {
        let text = match name {
            "initial_view.json" => include_str!("../tests/fixtures/initial_view.json"),
            "frames.json" => include_str!("../tests/fixtures/frames.json"),
            _ => include_str!("../tests/fixtures/snapshot.json"),
        };
        serde_json::from_str(text).unwrap()
    }

    #[test]
    fn replayed_frames_reproduce_the_snapshot() {
        let mut view = load("initial_view.json");
        let frames = load("frames.json");
        let snapshot = load("snapshot.json");
        let mut cursor = 0;
        for frame in frames.as_array().unwrap() {
            let r = apply_frame(&mut view, &mut cursor, frame);
            assert_eq!(r, Applied::Applied, "seq {}", frame["seq"]);
        }
        assert_eq!(cursor, snapshot["seq"].as_u64().unwrap());
        // `messages`, `usage` and `pending_permissions` are wire-computed copies; compare the rest.
        assert_eq!(view, snapshot["view"]);
        // Replaying again is a no-op: every frame is at or below the cursor.
        for frame in load("frames.json").as_array().unwrap() {
            assert_eq!(apply_frame(&mut view, &mut cursor, frame), Applied::Skipped);
        }
    }
}
