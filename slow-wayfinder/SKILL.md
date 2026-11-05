---
name: slow-wayfinder
description: >-
  Personal overlay on stock /wayfinder: one question at a time, repo checkpoints,
  map-only charting, and bot-queue handoff. Use with /wayfinder, when charting a
  map, working a wayfinder:grilling ticket with the owner, or when the owner says
  how they want wayfinding run. Where this conflicts with stock wayfinder or
  grilling (including multi-question rounds), these win. Not for PM, builder, or
  researcher bot runs.
---

# Slow wayfinder

Read the project's `/wayfinder` skill and issue-tracker docs first. Then apply these overlays. Where they conflict with stock wayfinder or grilling (including multi-question rounds), these win.

## Question style

- Ask **one question at a time**. Wait for the answer before the next.
- Normal prose. No emoji question banners, no multi-Q frontier dump in one message.
- Keep each question short. State the options with **pros and cons**. When a concept is advanced (REPL vs agent loop, provider seam, fixture vs live smoke), explain it in plain language before asking.
- Recommend one option. **"Yes" accepts the recommendation.** Push back when the owner's preference fights the destination, bot line budgets, or a settled cut.
- Grill the product hard: what is delivered at each stage, and the complexity-versus-learning trade-off. Prefer thinner milestones a bot can finish in one session over landscape stages that bundle three lessons.

## Authority

- Existing ADRs, early-cuts, glossary drafts, and survey docs are **reference only** until a closed wayfinder ticket records the decision.
- Confirmed decisions live on tickets and in the map's Decisions-so-far. The chat and a checkpoint file are not the authority.

## Checkpoint

After each settled answer, update a repo file (default `docs/wayfinder/session-checkpoint.md`):

- Settled cut, open frontier, fog, out of scope, and issue links by **title**.
- Mark it as working notes, not a decision record.
- Keep it lean. Crash, outage, and usage limits must not wipe the route.

## Charting and bots

- Charting creates the map and the decision tickets, then **stops**. Do not resolve research in the charting chat when the repo has a researcher bot.
- Label researcher work `ready-for-researcher` (plus `wayfinder:research` when it is a map child). Label builder work `ready-for-agent` only after `/to-tickets`. Never put `ready-for-agent` on a wayfinder decision ticket.
- Leave `wayfinder:grilling` and HITL tickets for the owner. Do not claim or close them as a bot.
- Do not run `/to-spec` or `/to-tickets` until the map's open grilling tickets that block the cut are closed, unless the owner explicitly asks to draft the unblocked milestones only.
- Size every ticket for a small bot session. Prefer one decision or one research fact per ticket.

## After the map

When the frontier is empty: tell the owner the next step is `/to-spec`, then `/to-tickets`, then the PM assigns `ready-for-agent` / `ready-for-researcher`. Do not invent build tickets during wayfinding.
