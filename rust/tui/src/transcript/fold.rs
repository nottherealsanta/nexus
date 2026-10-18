//! Fold transient of a finished tool group (DESIGN OVERHAUL step 5).
//!
//! When a folded group goes from `running` to completed, its tree rows leave one per frame
//! from the bottom, then the header flashes from the accent back to its muted tone. The
//! rows stay cached and tick-free: this module only decides, for a given instant, which
//! look a group has. The caller (`render::Cache`) swaps the group for the returned overlay
//! block while the fold lives and rebuilds that block's rows; entries expire on their own.
use super::groups::TREE_ROWS;
use crate::bridge::Content;
use std::{
    collections::{HashMap, HashSet},
    time::{Duration, Instant},
};

/// One animation frame of the fold.
pub const FRAME: Duration = Duration::from_millis(80);
/// Frames the header spends fading from the accent to muted.
pub const FLASH_FRAMES: usize = 6;

struct Fold {
    at: Instant,
    /// The tree members the group showed when it finished, oldest first.
    members: Vec<Content>,
    /// Members that were already announced as `… N earlier`.
    hidden: usize,
}

#[derive(Default)]
pub struct Folds {
    /// Last status seen for each group (bounded to the groups still on screen).
    status: HashMap<String, String>,
    /// The newest members of each running, folded group, kept as light copies.
    last: HashMap<String, (Vec<Content>, usize)>,
    live: HashMap<String, Fold>,
}

/// Just what a tree row draws; the bodies of a member are not needed to fold it away.
fn light(member: &Content) -> Content {
    Content {
        id: member.id.clone(),
        kind: member.kind.clone(),
        title: member.title.clone(),
        text: member.text.clone(),
        heading: member.heading.clone(),
        status: member.status.clone(),
        elapsed_ms: member.elapsed_ms,
        operation: member.operation.clone(),
        inspect_operation: member.inspect_operation.clone(),
        ..Default::default()
    }
}

impl Folds {
    /// Whether any group is mid-fold (the frame clock must keep running).
    pub fn is_live(&self) -> bool {
        !self.live.is_empty()
    }

    /// Record every group's status; a folded group that just finished starts a fold when
    /// `enabled` and it was last seen with tree rows. Idempotent for an unchanged snapshot.
    pub fn observe(&mut self, blocks: &[Content], now: Instant, enabled: bool) {
        let mut present: HashSet<&str> = HashSet::new();
        for block in blocks.iter().filter(|block| block.kind == "tool_group") {
            present.insert(block.id.as_str());
            let before_running = self
                .status
                .get(&block.id)
                .is_some_and(|status| status == "running");
            let running = block.status == "running";
            if before_running && !running {
                if let Some((members, hidden)) = self.last.remove(&block.id) {
                    if enabled && block.collapsed && !members.is_empty() {
                        self.live.insert(
                            block.id.clone(),
                            Fold {
                                at: now,
                                members,
                                hidden,
                            },
                        );
                    }
                }
            }
            if running && block.collapsed {
                let start = block.members.len().saturating_sub(TREE_ROWS);
                let members = block.members[start..].iter().map(light).collect();
                self.last
                    .insert(block.id.clone(), (members, block.hidden + start));
            } else {
                self.last.remove(&block.id);
            }
            if self.status.get(&block.id) != Some(&block.status) {
                self.status.insert(block.id.clone(), block.status.clone());
            }
        }
        self.status.retain(|id, _| present.contains(id.as_str()));
        self.last.retain(|id, _| present.contains(id.as_str()));
        self.live.retain(|id, _| present.contains(id.as_str()));
    }

    /// The overlay blocks to draw at `now`, keyed by group id, and the ids of every fold
    /// that is live or expired in this call (their rows must be rebuilt).
    pub fn frames(
        &mut self,
        blocks: &[Content],
        now: Instant,
    ) -> (HashMap<String, Content>, Vec<String>) {
        let mut overlay = HashMap::new();
        let mut touched = Vec::new();
        let mut expired = Vec::new();
        for (id, fold) in &self.live {
            let Some(block) = blocks.iter().find(|block| &block.id == id) else {
                expired.push(id.clone());
                continue;
            };
            touched.push(id.clone());
            let frame =
                (now.saturating_duration_since(fold.at).as_millis() / FRAME.as_millis()) as usize;
            let rows = fold.members.len();
            if frame >= rows + FLASH_FRAMES {
                expired.push(id.clone());
                continue;
            }
            overlay.insert(id.clone(), look(block, fold, frame));
        }
        for id in expired {
            self.live.remove(&id);
            touched.push(id);
        }
        (overlay, touched)
    }
}

/// The group at animation `frame`: tree rows leaving from the bottom, then the flash.
fn look(block: &Content, fold: &Fold, frame: usize) -> Content {
    let mut shown = block.clone();
    let rows = fold.members.len();
    if frame < rows {
        let keep = rows - frame;
        shown.status = "running".into();
        shown.collapsed = true;
        shown.members = fold.members[..keep].to_vec();
        shown.hidden = fold.hidden;
        shown.rev.push_str(&format!(":fold{keep}"));
    } else {
        let step = frame - rows;
        shown.flash = (100 - 100 * step / FLASH_FRAMES).clamp(0, 100) as u8;
        shown.rev.push_str(&format!(":flash{}", shown.flash));
    }
    shown
}

#[cfg(test)]
mod tests {
    use super::*;

    fn member(id: &str) -> Content {
        Content {
            id: id.into(),
            kind: "tool".into(),
            title: "Read".into(),
            text: id.into(),
            status: "completed".into(),
            ..Default::default()
        }
    }

    fn group(status: &str, members: usize) -> Content {
        Content {
            id: "g".into(),
            kind: "tool_group".into(),
            status: status.into(),
            collapsed: true,
            rev: format!("r-{status}"),
            members: (0..members).map(|i| member(&format!("m{i}"))).collect(),
            ..Default::default()
        }
    }

    fn started(now: Instant) -> Folds {
        let mut folds = Folds::default();
        folds.observe(&[group("running", 3)], now, true);
        folds.observe(&[group("completed", 0)], now, true);
        folds
    }

    #[test]
    fn a_folded_group_that_finishes_starts_a_fold() {
        let now = Instant::now();
        let folds = started(now);
        assert!(folds.is_live());
        // First sight of a finished group never folds (reload, reconnect).
        let mut fresh = Folds::default();
        fresh.observe(&[group("completed", 0)], now, true);
        assert!(!fresh.is_live());
        // Animations off: nothing folds.
        let mut off = Folds::default();
        off.observe(&[group("running", 3)], now, false);
        off.observe(&[group("completed", 0)], now, false);
        assert!(!off.is_live());
    }

    #[test]
    fn rows_leave_one_per_frame_from_the_bottom_then_the_header_flashes() {
        let now = Instant::now();
        let mut folds = started(now);
        let done = [group("completed", 0)];
        let at = |frames: u32| now + FRAME * frames;
        let rows = |folds: &mut Folds, frames: u32| {
            let (overlay, _) = folds.frames(&done, at(frames));
            overlay.get("g").map(|block| {
                (
                    block.status.clone(),
                    block
                        .members
                        .iter()
                        .map(|m| m.id.clone())
                        .collect::<Vec<_>>(),
                    block.flash,
                )
            })
        };
        let all = vec!["m0".to_string(), "m1".into(), "m2".into()];
        assert_eq!(
            rows(&mut folds, 0),
            Some(("running".into(), all.clone(), 0))
        );
        assert_eq!(rows(&mut folds, 1).unwrap().1, all[..2]);
        assert_eq!(rows(&mut folds, 2).unwrap().1, all[..1]);
        let (status, members, flash) = rows(&mut folds, 3).unwrap();
        assert_eq!(
            (status.as_str(), members.len(), flash),
            ("completed", 0, 100)
        );
        assert!(rows(&mut folds, 6).unwrap().2 < 100);
        assert!(rows(&mut folds, 8).unwrap().2 < rows(&mut folds, 6).unwrap().2);
        // Past rows + flash frames the entry expires and is reported once for a rebuild.
        let (overlay, touched) = folds.frames(&done, at(9));
        assert!(overlay.is_empty() && touched.contains(&"g".to_string()));
        assert!(!folds.is_live());
    }

    #[test]
    fn earlier_count_and_overlay_revisions_change_every_frame() {
        let now = Instant::now();
        let mut folds = Folds::default();
        let mut running = group("running", 7);
        running.hidden = 1;
        folds.observe(&[running], now, true);
        folds.observe(&[group("completed", 0)], now, true);
        let (overlay, _) = folds.frames(&[group("completed", 0)], now);
        let first = &overlay["g"];
        assert_eq!((first.members.len(), first.hidden), (5, 3));
        let (later, _) = folds.frames(&[group("completed", 0)], now + FRAME);
        assert_ne!(first.rev, later["g"].rev);
    }

    #[test]
    fn groups_that_leave_the_screen_forget_their_state() {
        let now = Instant::now();
        let mut folds = started(now);
        folds.observe(&[], now, true);
        assert!(!folds.is_live());
        assert!(folds.status.is_empty() && folds.last.is_empty());
    }
}
