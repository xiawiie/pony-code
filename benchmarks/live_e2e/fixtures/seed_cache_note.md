---
name: cache-invariant
type: feedback
description: prompt-cache invariant — stable prefix must not include mtime content
tags: [context, cache]
aliases: []
supersedes: []
---

Pony's cache anchor lives in Layer 1 (system content block). Anything
that changes per turn (workspace state, memory index) must go through
<system-reminder> injection on the user message, not into system.
