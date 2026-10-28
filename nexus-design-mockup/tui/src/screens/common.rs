//! The fixture session "Refactor the loader", drawn into a centre column.
//! Shared by the shell, timeline and narrow screens.
use crate::kit::{bold, Seg, P};
use crate::shell::{footer, group, prose, prose_segs, thought, user_block, code_style, Centre};

/// Draw the finished turn #1 and the start of turn #2 from row `y`. Returns the next free row.
pub fn turn_one(p: &mut P, c: Centre, mut y: u16) -> u16 {
    let t = p.t;
    group(p, c, y, &[("Context", bold(t.secondary())), (" · 4 items · 17.6k tokens", t.muted())], "", false, false);
    y += 2;
    y += user_block(p, c, y, &["The loader re-reads the manifest on every call. Can you cache it and add a test?"], 1, "3:42 PM", false, false);
    y += 1;
    thought(p, c, y, "Looking for where the manifest is parsed", "2.1s", false);
    y += 2;
    y += prose(p, c, y, "I'll start by finding where the manifest is parsed.");
    y += 1;
    group(p, c, y, &[("Read 2 files", t.secondary()), (", ", t.muted()), ("ran 1 command", t.secondary())], "1.4s", false, false);
    y += 2;
    let cs = code_style(p);
    let segs: Vec<Seg> = vec![(" parse_manifest ", cs), (" is called from ", t.primary()), (" Loader.load ", cs), (" on every request. I'll memoize it on the file's mtime", t.primary())];
    prose_segs(p, c, y, &segs);
    y += prose(p, c, y + 1, "so edits still show up, and add a regression test that counts parses.") + 1;
    y += 1;
    group(p, c, y, &[("Edited 1 file", t.secondary()), ("  ", t.muted()), ("+12", t.fg("diff.add")), (" ", t.muted()), ("−3", t.fg("diff.del")), (" · ", t.muted()), (t.g("error"), t.fg("status.error")), (" 1 failed", t.fg("status.error"))], "0.9s", false, false);
    y += 2;
    y += prose(p, c, y, "The first edit did not apply because the line had changed; I re-read the file and applied it. Both tests pass.");
    y += 1;
    footer(p, c, y, "build", "gpt-6.1-terra", &["medium", "3 tools", "4.2s", "12k in · 2k out", "$0.04"], "done");
    y + 2
}
