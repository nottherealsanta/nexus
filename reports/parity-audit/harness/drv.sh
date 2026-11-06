#!/usr/bin/env bash
# usage: drv.sh send SESSION tok...   |  drv.sh cap SESSION NAME
# Token spellings follow interface/manifest.toml (ctrl+k, alt+1, shift+tab, esc, enter).
# Literal text: prefix with "=" (e.g. =All).
SHOTS=/private/tmp/claude-501/-Users-santa-repos-nexus/ae18cd23-3c30-45e9-8fce-4498e2bbec4d/scratchpad/audit/shots
T="tmux -L audit"
map() {
  case "$1" in
    enter) echo Enter;; esc) echo Escape;; tab) echo Tab;; shift+tab) echo BTab;;
    up) echo Up;; down) echo Down;; left) echo Left;; right) echo Right;; space) echo Space;;
    shift+right) echo S-Right;; shift+left) echo S-Left;;
    pageup) echo PageUp;; pagedown) echo PageDown;; backspace) echo BSpace;; end) echo End;; home) echo Home;;
    ctrl+*) echo "C-${1#ctrl+}";; alt+*) echo "M-${1#alt+}";; shift+enter) echo S-Enter;;
    *) echo "$1";;
  esac
}
cmd="$1"; S="$2"; shift 2
case "$cmd" in
  send)
    for t in "$@"; do
      case "$t" in
        =*) $T send-keys -t "$S" -l -- "${t#=}"; sleep 0.25;;
        ctrl+*|alt+*) k=$(map "$t"); $T send-keys -t "$S" "$k"; sleep 0.5;;
        *) k=$(map "$t"); $T send-keys -t "$S" "$k"; sleep 0.5;;
      esac
    done
    sleep 1.2;;
  cap)
    N="$1"
    $T capture-pane -t "$S" -e -p -J > "$SHOTS/$N.ansi"
    $T capture-pane -t "$S" -p -J > "$SHOTS/$N.txt"
    echo "captured $N ($(wc -l < "$SHOTS/$N.txt") rows)";;
esac
