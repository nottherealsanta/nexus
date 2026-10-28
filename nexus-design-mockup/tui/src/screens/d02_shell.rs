//! D02 — the shell frame: header band, sidebars on the chrome surface, centre page.
use super::{common, Shot, DLM, DL};
use crate::kit::P;
use crate::shell::{user_block, Shell};

pub fn main_view(p: &mut P) {
    let s = Shell { left_w: 34, right_w: 36, ..Shell::default() };
    let c = s.draw(p);
    let y = common::turn_one(p, c, 2);
    user_block(p, c, y, &["Go ahead, but keep the change small.", "日本語のコメントも残してください。"], 2, "3:44 PM", false, false);
}

fn main_120(p: &mut P) {
    let s = Shell::default();
    let c = s.draw(p);
    common::turn_one(p, c, 2);
}

pub fn shots() -> Vec<Shot> {
    vec![
        Shot { id: "d02-shell-200x50", caption: "Shell: header band, chrome sidebars, centre page (200×50)", w: 200, h: 50, modes: DLM, ascii: true, draw: main_view },
        Shot { id: "d02-shell-120x36", caption: "Shell at 120×36", w: 120, h: 36, modes: DL, ascii: false, draw: main_120 },
    ]
}
