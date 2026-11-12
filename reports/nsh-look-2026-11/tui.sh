#!/bin/sh
# usage: tui.sh <label> ; TUI frames of the same seeded sessions (scratch home /tmp/nexus-look.h1)
H=/tmp/nexus-look.h1; R=../..; L=$1
for sz in 120x40 160x48; do for m in dark light; do
  f() { NEXUS10_HOME=$H NEXUS_TUI_NO_START=1 NEXUS10_REDUCED_MOTION=1 $R/nexus-tui/target/debug/nexus-tui --$m --open first --dump-frame $sz --out . --name tui-$sz-$m-$1-$L --svg --keys "$2" >/dev/null 2>&1; }
  f tools "ctrl+k Streaming space and space tools enter"
  f text "ctrl+k Rich space text enter"
  f question "ctrl+k Waiting space question enter"
  f approval "ctrl+k Waiting space approval enter"
  f palette "ctrl+p"
  f settings "ctrl+,"
done; done
