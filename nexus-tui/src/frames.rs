//! Frame scheduling for the interactive loop.
//!
//! The loop draws at one place (before waiting) and applies work one item at a
//! time. Sync messages may be coalesced into a small batch so a burst of
//! streamed deltas costs a few rebuilds instead of one per event. The batch
//! policy lives here so `main.rs` and the frame tests use the same rules.
//!
//! A batch never waits to fill itself: it stops as soon as the queue is empty.
//! The 8 ms budget is a soft bound on additional reducer work, not a frame-rate
//! cap (and not a hard latency limit: one snapshot/history reducer may exceed
//! it). Input, a due tick and auxiliary replies are barriers.

use std::time::{Duration, Instant};

/// Animation, auto-load and expiry cadence. Kept at 125 ms (8 fps) so a busy
/// session does not redraw faster than the spinner needs.
pub const TICK_INTERVAL: Duration = Duration::from_millis(125);

/// Additional reducer work allowed after the first message of a batch.
pub const BATCH_BUDGET: Duration = Duration::from_millis(8);

/// Most messages one batch may apply, including the one that woke the loop.
/// A bound even when reducer work is faster than the clock says.
pub const MAX_BATCH_MESSAGES: usize = 64;

/// Why a batch stopped extending.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BatchEnd {
    /// The sync queue was empty. The common case.
    Empty,
    /// Input, a due tick, an auxiliary reply, or a halt needs service.
    Barrier,
    /// The soft budget elapsed.
    Budget,
    /// [`MAX_BATCH_MESSAGES`] was reached.
    MaxMessages,
}

/// What applying one item did.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Applied {
    /// The frame no longer matches the model.
    pub dirty: bool,
    /// The loop must exit (quit, or a handler that decided to stop).
    pub halt: bool,
}

impl Applied {
    pub fn dirty(dirty: bool) -> Self {
        Self { dirty, halt: false }
    }

    pub fn halt(dirty: bool) -> Self {
        Self { dirty, halt: true }
    }
}

/// The result of one batch: what it applied and why it stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BatchOutcome {
    pub applied: usize,
    /// OR of every item's dirty flag.
    pub dirty: bool,
    pub halt: bool,
    pub end: BatchEnd,
}

/// Apply `first`, then at most [`MAX_BATCH_MESSAGES`] messages from `next`
/// within [`BATCH_BUDGET`].
///
/// `barrier` is checked before taking another message (never after, so a
/// message is never removed and then discarded). `now` is injected so tests
/// can drive the budget deterministically. `apply` must dispatch that item's
/// effects before returning: effects are a barrier between items.
pub fn run_batch<M>(
    first: M,
    started: Instant,
    mut now: impl FnMut() -> Instant,
    mut next: impl FnMut() -> Option<M>,
    mut barrier: impl FnMut() -> bool,
    mut apply: impl FnMut(M) -> Applied,
) -> BatchOutcome {
    let mut step = apply(first);
    let mut applied = 1;
    let mut dirty = step.dirty;
    loop {
        if step.halt {
            return BatchOutcome {
                applied,
                dirty,
                halt: true,
                end: BatchEnd::Barrier,
            };
        }
        if barrier() {
            return BatchOutcome {
                applied,
                dirty,
                halt: false,
                end: BatchEnd::Barrier,
            };
        }
        if applied >= MAX_BATCH_MESSAGES {
            return BatchOutcome {
                applied,
                dirty,
                halt: false,
                end: BatchEnd::MaxMessages,
            };
        }
        if now().saturating_duration_since(started) >= BATCH_BUDGET {
            return BatchOutcome {
                applied,
                dirty,
                halt: false,
                end: BatchEnd::Budget,
            };
        }
        let Some(msg) = next() else {
            return BatchOutcome {
                applied,
                dirty,
                halt: false,
                end: BatchEnd::Empty,
            };
        };
        step = apply(msg);
        applied += 1;
        dirty |= step.dirty;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    fn burst(n: usize, dirty: bool) -> (BatchOutcome, Vec<usize>) {
        let applied = std::cell::RefCell::new(Vec::new());
        let next_id = Cell::new(1usize);
        let outcome = run_batch(
            0usize,
            Instant::now(),
            Instant::now,
            || {
                let i = next_id.get();
                if i >= n {
                    None
                } else {
                    next_id.set(i + 1);
                    Some(i)
                }
            },
            || false,
            |i| {
                applied.borrow_mut().push(i);
                Applied::dirty(dirty)
            },
        );
        let ids = applied.into_inner();
        (outcome, ids)
    }

    #[test]
    fn a_finite_burst_stops_when_the_queue_is_empty() {
        let (outcome, ids) = burst(9, true);
        assert_eq!(ids, (0..9).collect::<Vec<_>>());
        assert_eq!(outcome.applied, 9);
        assert_eq!(outcome.end, BatchEnd::Empty);
        assert!(outcome.dirty);
        assert!(!outcome.halt);
    }

    #[test]
    fn invisible_traffic_is_clean() {
        let (outcome, ids) = burst(4, false);
        assert_eq!(ids.len(), 4);
        assert!(!outcome.dirty);
    }

    #[test]
    fn the_soft_budget_stops_before_the_next_message() {
        let clock = Cell::new(Instant::now());
        let queue = Cell::new(4usize);
        let outcome = run_batch(
            0usize,
            clock.get(),
            || clock.get(),
            || {
                let left = queue.get();
                queue.set(left.saturating_sub(1));
                (left > 0).then_some(left)
            },
            || false,
            |_| {
                clock.set(clock.get() + BATCH_BUDGET);
                Applied::dirty(true)
            },
        );
        assert_eq!(outcome.applied, 1);
        assert_eq!(outcome.end, BatchEnd::Budget);
        assert_eq!(queue.get(), 4);
    }

    #[test]
    fn the_count_bound_includes_the_first_message() {
        let (outcome, ids) = burst(MAX_BATCH_MESSAGES + 10, true);
        assert_eq!(outcome.applied, MAX_BATCH_MESSAGES);
        assert_eq!(ids.len(), MAX_BATCH_MESSAGES);
        assert_eq!(outcome.end, BatchEnd::MaxMessages);
    }

    #[test]
    fn a_barrier_is_checked_before_taking_a_message() {
        let queue = Cell::new(3usize);
        let outcome = run_batch(
            0usize,
            Instant::now(),
            Instant::now,
            || {
                let left = queue.get();
                queue.set(left.saturating_sub(1));
                (left > 0).then_some(left)
            },
            || true,
            |_| Applied::dirty(true),
        );
        assert_eq!(outcome.applied, 1);
        assert_eq!(outcome.end, BatchEnd::Barrier);
        assert_eq!(queue.get(), 3, "no message was removed");
    }

    #[test]
    fn a_halt_stops_immediately() {
        let queue = Cell::new(3usize);
        let outcome = run_batch(
            0usize,
            Instant::now(),
            Instant::now,
            || {
                queue.set(queue.get().saturating_sub(1));
                Some(1)
            },
            || false,
            |_| Applied::halt(true),
        );
        assert_eq!(outcome.applied, 1);
        assert!(outcome.halt);
        assert_eq!(queue.get(), 3);
    }

    #[test]
    fn dirty_is_sticky_across_a_clean_batch() {
        let mut dirty = false;
        let (first, _) = burst(2, true);
        dirty |= first.dirty;
        let (second, _) = burst(2, false);
        dirty |= second.dirty;
        assert!(dirty, "an invisible batch must not clear an earlier draw");
    }
}
