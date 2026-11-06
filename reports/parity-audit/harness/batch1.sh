#!/usr/bin/env bash
# batch1: 160x48 overlays and tabs, same keys on both clients
D=/private/tmp/claude-501/-Users-santa-repos-nexus/ae18cd23-3c30-45e9-8fce-4498e2bbec4d/scratchpad/audit/notes/drv.sh
SZ=${SZ:-160}
for pair in tui${SZ}:tui nsh${SZ}:nsh; do
  s=${pair%%:*}; tag=${pair##*:}
  $D send $s alt+2;    $D cap $s $tag-files-$SZ
  $D send $s alt+1;    $D cap $s $tag-sessions-$SZ
  $D send $s alt+3;    $D cap $s $tag-projects-$SZ
  $D send $s alt+1
  $D send $s ctrl+p;   $D cap $s $tag-palette-$SZ
  $D send $s esc
  $D send $s ctrl+m;   $D cap $s $tag-model-$SZ
  $D send $s esc
  $D send $s ctrl+g;   $D cap $s $tag-agent-$SZ
  $D send $s esc
  $D send $s f1;       $D cap $s $tag-help-$SZ
  $D send $s esc
  $D send $s ctrl+,;   $D cap $s $tag-settings-$SZ
  $D send $s esc
done
