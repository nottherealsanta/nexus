---
name: hello-world
description: A minimal example skill. Use when demonstrating progressive disclosure.
allowed-tools: [Read, Glob]
bundles: [fs]
model: inherit
version: 1
---

# Hello world

This body is loaded only when the model invokes the skill. Until then, the
context carries just `hello-world: A minimal example skill...`.

When invoked, say hello, state the current workspace, and stop.
