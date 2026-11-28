//! One microphone owner at a time, shared by dictation, the Manager's live
//! call, and the recorder.
//!
//! Each capture acquires a lease before it asks for permission or opens a
//! device, and keeps it until the device is actually released. A lease is a
//! token in a global slot; dropping it only clears that slot when the token is
//! still current, so a stale owner can never release a successor. Acquisition
//! and release both happen under one mutex, which makes the check and the claim
//! atomic across threads and beats a separate "is it busy?" query.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, MutexGuard};

/// Which capture owns the microphone.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum MicOwner {
    // Dictation exists on Apple targets; web, Android and Linux/Windows keep
    // the variant only so busy messages can name it.
    #[cfg_attr(not(any(target_os = "ios", target_os = "macos")), allow(dead_code))]
    Dictation,
    // The iOS-only Manager live call is the only producer on non-test builds.
    #[cfg_attr(not(target_os = "ios"), allow(dead_code))]
    Live,
    Recorder,
}

#[derive(Debug)]
struct State {
    owner: MicOwner,
    token: u64,
}

static OWNER: Mutex<Option<State>> = Mutex::new(None);
static NEXT_TOKEN: AtomicU64 = AtomicU64::new(1);

/// A held microphone lease. Dropping it releases the microphone.
#[derive(Debug)]
pub(crate) struct MicLease {
    token: u64,
}

impl Drop for MicLease {
    fn drop(&mut self) {
        release(self.token);
    }
}

fn slot() -> MutexGuard<'static, Option<State>> {
    // A panic while holding the slot must not disable the microphone forever.
    OWNER
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn release(token: u64) {
    let mut slot = slot();
    if slot.as_ref().is_some_and(|state| state.token == token) {
        *slot = None;
    }
}

/// Claims the microphone for `owner`, or reports the owner already holding it.
pub(crate) fn try_acquire(owner: MicOwner) -> Result<MicLease, MicOwner> {
    let mut slot = slot();
    if let Some(state) = slot.as_ref() {
        return Err(state.owner);
    }
    let token = NEXT_TOKEN.fetch_add(1, Ordering::Relaxed);
    *slot = Some(State { owner, token });
    Ok(MicLease { token })
}

#[cfg(test)]
pub(crate) fn current() -> Option<MicOwner> {
    slot().as_ref().map(|state| state.owner)
}

#[cfg(test)]
pub(crate) static TEST_LOCK: Mutex<()> = Mutex::new(());

#[cfg(test)]
pub(crate) fn reset_for_test() {
    *slot() = None;
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Barrier};

    use super::*;

    fn acquire(owner: MicOwner) -> Result<MicLease, MicOwner> {
        try_acquire(owner)
    }

    #[test]
    fn every_owner_excludes_every_other_owner_without_races() {
        let _guard = TEST_LOCK.lock().unwrap();
        reset_for_test();
        let owners = [MicOwner::Dictation, MicOwner::Live, MicOwner::Recorder];
        for held in owners.iter() {
            let lease = acquire(*held).unwrap();
            assert_eq!(current(), Some(*held));
            for other in owners.iter() {
                let error = acquire(*other).unwrap_err();
                assert_eq!(error, *held);
            }
            drop(lease);
            assert_eq!(current(), None);
        }
        reset_for_test();
    }

    #[test]
    fn a_stale_lease_does_not_release_its_successor() {
        let _guard = TEST_LOCK.lock().unwrap();
        reset_for_test();
        let first = acquire(MicOwner::Dictation).unwrap();
        let stale = first.token;
        drop(first);
        let second = acquire(MicOwner::Live).unwrap();
        assert_eq!(current(), Some(MicOwner::Live));
        release(stale);
        assert_eq!(current(), Some(MicOwner::Live));
        drop(second);
        assert_eq!(current(), None);
        reset_for_test();
    }

    #[test]
    fn concurrent_claims_have_exactly_one_winner() {
        let _guard = TEST_LOCK.lock().unwrap();
        reset_for_test();
        let barrier = Arc::new(Barrier::new(8));
        let mut threads = Vec::new();
        for i in 0..8 {
            let barrier = barrier.clone();
            threads.push(std::thread::spawn(move || {
                barrier.wait();
                let owner = if i % 2 == 0 {
                    MicOwner::Dictation
                } else {
                    MicOwner::Recorder
                };
                try_acquire(owner).ok().map(|lease| (owner, lease))
            }));
        }
        let winners: Vec<_> = threads
            .into_iter()
            .filter_map(|thread| thread.join().unwrap())
            .collect();
        assert_eq!(winners.len(), 1);
        assert_eq!(current(), Some(winners[0].0));
        drop(winners);
        assert_eq!(current(), None);
        reset_for_test();
    }
}
