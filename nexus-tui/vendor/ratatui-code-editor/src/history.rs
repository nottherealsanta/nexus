use crate::code::{EditBatch, Operation};
use std::collections::VecDeque;
use std::time::{Duration, Instant};

/// A run of single-character typing or deleting within this window is one
/// undo step, as code editors group it.
const GROUP_WINDOW: Duration = Duration::from_millis(750);

pub struct History {
    index: usize,
    max_items: usize,
    edits: VecDeque<EditBatch>,
    last_at: Option<Instant>,
    /// The last push was one character, so the next one may join it.
    grouping: bool,
}

impl History {
    pub fn new(max_items: usize) -> Self {
        Self {
            index: 0,
            max_items,
            edits: VecDeque::new(),
            last_at: None,
            grouping: false,
        }
    }

    pub fn push(&mut self, batch: EditBatch) {
        let now = Instant::now();
        let in_window = self
            .last_at
            .is_some_and(|at| now.duration_since(at) <= GROUP_WINDOW);
        self.last_at = Some(now);

        while self.edits.len() > self.index {
            self.edits.pop_back();
        }

        let single = single(&batch).is_some();
        // A second keystroke right next to the last one joins its step.
        if in_window && self.grouping && self.index == self.edits.len() {
            if let Some(previous) = self.edits.back_mut() {
                if merge(previous, &batch) {
                    self.grouping = true;
                    return;
                }
            }
        }

        if self.edits.len() == self.max_items {
            self.edits.pop_front();
            self.index -= 1;
        }

        self.edits.push_back(batch);
        self.index += 1;
        self.grouping = single;
    }

    pub fn undo(&mut self) -> Option<EditBatch> {
        self.last_at = None;
        self.grouping = false;
        if self.index == 0 {
            None
        } else {
            self.index -= 1;
            self.edits.get(self.index).cloned()
        }
    }

    pub fn redo(&mut self) -> Option<EditBatch> {
        self.last_at = None;
        self.grouping = false;
        if self.index >= self.edits.len() {
            None
        } else {
            let batch = self.edits.get(self.index).cloned();
            self.index += 1;
            batch
        }
    }
}

/// One single-character edit that can join a typing run. A newline, a paste or
/// a multi-character replacement starts a step of its own.
fn single(batch: &EditBatch) -> Option<&crate::code::Edit> {
    let [edit] = batch.edits.as_slice() else {
        return None;
    };
    let chars = edit.text.chars().count();
    (chars == 1 && edit.text != "\n").then_some(edit)
}

fn merge(previous: &mut EditBatch, next: &EditBatch) -> bool {
    let Some(new) = single(next) else {
        return false;
    };
    let Some(prev) = previous.edits.last() else {
        return false;
    };
    if prev.text.contains('\n') {
        return false;
    }
    let previous_start = prev.start;
    let previous_len = prev.text.chars().count();
    match (&prev.operation, &new.operation) {
        (Operation::Insert, Operation::Insert) if new.start == previous_start + previous_len => {
            previous
                .edits
                .last_mut()
                .expect("checked above")
                .text
                .push_str(&new.text);
        }
        (Operation::Remove, Operation::Remove) if new.start + 1 == previous_start => {
            // Backspace: the newer character is before the older one.
            let last = previous.edits.last_mut().expect("checked above");
            last.start = new.start;
            last.text = format!("{}{}", new.text, last.text);
        }
        (Operation::Remove, Operation::Remove) if new.start == previous_start => {
            // Forward delete: the newer character follows the older one.
            previous
                .edits
                .last_mut()
                .expect("checked above")
                .text
                .push_str(&new.text);
        }
        _ => return false,
    }
    previous.state_after = next.state_after;
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::code::{Edit, EditState};

    fn insert(start: usize, text: &str, after: usize) -> EditBatch {
        EditBatch {
            edits: vec![Edit {
                start,
                text: text.into(),
                operation: Operation::Insert,
            }],
            state_before: None,
            state_after: Some(EditState {
                offset: after,
                selection: None,
            }),
        }
    }

    #[test]
    fn adjacent_typing_is_one_undo_step() {
        let mut history = History::new(10);
        history.push(insert(0, "a", 1));
        history.push(insert(1, "b", 2));
        history.push(insert(2, "c", 3));
        let batch = history.undo().unwrap();
        assert_eq!(batch.edits.len(), 1);
        assert_eq!(batch.edits[0].text, "abc");
        assert!(history.undo().is_none());
    }

    #[test]
    fn a_newline_starts_a_new_step() {
        let mut history = History::new(10);
        history.push(insert(0, "a", 1));
        history.push(insert(1, "\n", 2));
        let batch = history.undo().unwrap();
        assert_eq!(batch.edits[0].text, "\n");
    }
}
