//! D16 — files: the tree in the left sidebar, the file view in the centre.
use super::{Shot, DL};
use crate::kit::{bold, rev, P};
use crate::shell::{Shell, Tab};
use ratatui::style::Style;

const README: &[&str] = &[
    "# side-project", "", "A small Rust workspace used to try Nexus on a project that is", "not Nexus itself. It has one binary and no dependencies.", "",
    "## Layout", "", "- `src/main.rs`   the entry point, prints a greeting", "- `src/new.rs`    a module added in the last commit", "- `hello.txt`     fixture read by the integration test", "",
    "## Build", "", "    cargo build", "    cargo test", "", "The project has no build script and no features.", "", "## Notes", "",
    "The side project exists to exercise worktrees: each branch of the", "project gets its own checkout under `~/repos/side-project-*`.", "", "Sessions started in a worktree keep that directory for the whole", "life of the session, so a project can run several at once.", "",
    "## Licence", "", "MIT. See the project root for the full text.", "", "## Contributing", "", "Open an issue first. Small changes are welcome without one.", "The project follows the usual Rust formatting rules.", "",
    "## Contact", "", "Ask in the team channel.",
];

fn files(p: &mut P) {
    let t = p.t;
    let s = Shell { left: Some(Tab::Files), left_w: 34, right_w: 36, composer: false, crumbs: &["side-project", "README.md"], ..Shell::default() };
    let c = s.draw(p);
    // Header row: the `~` path in secondary, basename bold; the visible range right-aligned.
    p.put(c.x, 2, &[("~/repos/side-project/", t.secondary()), ("README.md", bold(t.primary()))]);
    p.put_r(c.x + c.w, 2, &[("lines 1-38 of 38", t.muted())]);
    // Gutter, rule and text; search matches on the selection tint, the current one reversed.
    let q = "project";
    let gx = c.x;
    let tx = gx + 7;
    let mut seen = 0;
    for (i, line) in README.iter().take(38).enumerate() {
        let y = 4 + i as u16;
        p.put(gx, y, &[(&format!("{:>4}", i + 1), t.muted()), (" ", t.muted()), (t.g("rail"), t.hair())]);
        let mut x = tx;
        let mut rest: &str = line;
        let heading = line.starts_with('#');
        let base = if heading { bold(t.primary()) } else { t.primary() };
        while let Some(pos) = rest.find(q) {
            x = p.put(x, y, &[(&rest[..pos], base)]);
            seen += 1;
            let st: Style = if seen == 3 { rev(base) } else { base.patch(t.bg("interactive.selection")) };
            x = p.put(x, y, &[(q, st)]);
            rest = &rest[pos + q.len()..];
        }
        p.put(x, y, &[(rest, base)]);
    }
    // Search row at the bottom of the view.
    let y = p.h - 2;
    p.fill(c.bx, y, c.bw, 1, "surface.panel");
    let pb = t.bg("surface.panel");
    p.put(c.x, y, &[("/", t.secondary().patch(pb)), (" project", t.primary().patch(pb)), (&format!("  3 of {seen}"), t.secondary().patch(pb))]);
    p.put_r(c.x + c.w, y, &[("n", bold(t.secondary()).patch(pb)), (" next · ", t.muted().patch(pb)), ("N", bold(t.secondary()).patch(pb)), (" prev · ", t.muted().patch(pb)), ("esc", bold(t.secondary()).patch(pb)), (" end", t.muted().patch(pb))]);
}

pub fn shots() -> Vec<Shot> {
    vec![Shot { id: "d16-files", caption: "Files tab and the file view with search", w: 200, h: 50, modes: DL, ascii: false, draw: files }]
}
