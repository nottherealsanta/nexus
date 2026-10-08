//! Transient desktop notices rendered as a bounded toast stack.
//!
//! Overhaul plan §2.4.16: at most three toasts, bottom-centre above the composer.
//! Info toasts auto-dismiss (the shell owns the timer); warnings and errors
//! persist until dismissed. A notice may carry one host action button.
use std::collections::VecDeque;

pub const MAX_NOTICES: usize = 3;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum NoticeKind {
    Info,
    Warning,
    Error,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum NoticeAction {
    None,
    Reconnect,
    UpdateHelp,
    Host(u64),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Notice {
    pub id: u64,
    pub kind: NoticeKind,
    pub text: String,
    pub action: NoticeAction,
}

/// A bounded newest-first queue. Re-pushing the current newest text replaces it
/// instead of stacking duplicates (polls and retries often repeat one message).
#[derive(Default)]
pub struct NoticeStack {
    notices: VecDeque<Notice>,
    next_id: u64,
}

impl NoticeStack {
    pub fn push(&mut self, kind: NoticeKind, text: impl Into<String>, action: NoticeAction) -> u64 {
        let text = text.into();
        self.next_id = self.next_id.wrapping_add(1);
        let id = self.next_id;
        if self
            .notices
            .front()
            .is_some_and(|notice| notice.text == text)
        {
            let mut existing = self.notices.pop_front().expect("checked front");
            existing.kind = kind;
            existing.action = action;
            existing.id = id;
            self.notices.push_front(existing);
            return id;
        }
        self.notices.push_front(Notice {
            id,
            kind,
            text,
            action,
        });
        while self.notices.len() > MAX_NOTICES {
            self.notices.pop_back();
        }
        id
    }

    pub fn dismiss(&mut self, id: u64) -> bool {
        let before = self.notices.len();
        self.notices.retain(|notice| notice.id != id);
        before != self.notices.len()
    }

    pub fn iter(&self) -> impl Iterator<Item = &Notice> {
        self.notices.iter()
    }

    pub fn is_empty(&self) -> bool {
        self.notices.is_empty()
    }

    pub fn clear(&mut self) {
        self.notices.clear();
    }

    pub fn len(&self) -> usize {
        self.notices.len()
    }

    pub fn contains_text(&self, needle: &str) -> bool {
        self.notices
            .iter()
            .any(|notice| notice.text.contains(needle))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stack_is_bounded_and_drops_the_oldest() {
        let mut stack = NoticeStack::default();
        for index in 0..5 {
            stack.push(
                NoticeKind::Error,
                format!("message {index}"),
                NoticeAction::None,
            );
        }
        assert_eq!(stack.len(), MAX_NOTICES);
        let texts: Vec<_> = stack.iter().map(|notice| notice.text.as_str()).collect();
        assert_eq!(texts, ["message 4", "message 3", "message 2"]);
    }

    #[test]
    fn repeating_the_newest_message_replaces_it_instead_of_stacking() {
        let mut stack = NoticeStack::default();
        let first = stack.push(NoticeKind::Error, "boom", NoticeAction::None);
        let second = stack.push(NoticeKind::Error, "boom", NoticeAction::None);
        assert_eq!(stack.len(), 1);
        assert_ne!(first, second);
        assert_eq!(stack.iter().next().unwrap().id, second);
    }

    #[test]
    fn dismiss_only_removes_the_matching_id() {
        let mut stack = NoticeStack::default();
        let first = stack.push(NoticeKind::Error, "one", NoticeAction::None);
        let second = stack.push(NoticeKind::Error, "two", NoticeAction::None);
        assert!(stack.dismiss(first));
        assert!(!stack.dismiss(first));
        assert_eq!(stack.len(), 1);
        assert_eq!(stack.iter().next().unwrap().id, second);
    }

    #[test]
    fn action_is_carried_on_the_notice() {
        let mut stack = NoticeStack::default();
        stack.push(NoticeKind::Warning, "Disconnected", NoticeAction::Reconnect);
        assert_eq!(stack.iter().next().unwrap().action, NoticeAction::Reconnect);
    }
}
