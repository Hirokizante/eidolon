# Domain Docs

This repo uses a single-context domain documentation layout.

## Before exploring

- Read `GLOSSARY.md` at the repo root.
- Read ADRs in `docs/adr/` that touch the area being explored.

If these files or directories are absent, proceed silently.
The `/domain-modeling` skill creates them lazily when terms or
decisions are resolved.

## File structure

- `GLOSSARY.md`: shared domain vocabulary for Eidolon.
- `docs/adr/NNNN-<decision-slug>.md`: architectural decisions.

## Use the glossary's vocabulary

When naming a domain concept in an issue, refactor proposal,
hypothesis, or test, use the term defined in `GLOSSARY.md`.

If a concept is missing, check whether an existing term fits.
Record a real vocabulary gap for `/domain-modeling`.

## Flag ADR conflicts

If a proposal contradicts an existing ADR, identify the ADR and
explain why the decision should be reconsidered.
