# TUI vs `nexus nsh tui` parity audit (2026-11-06, partial)

The audit was stopped before its written report. These are its raw captures at 160x48 of the Rust TUI
(reference) and the NSH terminal app against one seeded scratch Core, same keys on both
(`harness/batch1.sh`): `tui-<screen>-160.png` / `nsh-<screen>-160.png` with `.txt` text dumps.

Caveat: some NSH captures were made before the harness answered OSC 10/11 colour queries; under
tmux, NSH's terminal backend falls back to a bandless look (reverse-video selection, no raised
surfaces). Re-capture NSH with the pty harness (`harness/harness.py`, answers like Ghostty; arrow
keys as SS3; wait ~8 s for the splash) before trusting colours.

The differences found so far are listed in PLAN.md section 15.7.
