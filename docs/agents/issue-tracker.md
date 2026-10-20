# Issue tracker: GitHub Issues

Use [GitHub Issues](https://github.com/Hirokizante/eidolon/issues) for specifications,
engineering tasks, and implementation updates. Review shared content under the
[privacy guide](../PRIVACY.md#sharing-diagnostics) before publishing it.

## Creating or fetching work

- Create an issue with the problem, intended behavior, scope, acceptance criteria,
  and validation plan. Keep examples synthetic.
- Fetch an issue by its GitHub URL or repository issue number. Read its body and
  comments before starting implementation.
- Record dependencies with links to blocking issues. Link related specifications
  and pull requests rather than copying their contents.
- For a larger effort, keep the overall specification in a parent issue and
  track independently actionable work in linked child issues.
- Apply the [triage label](triage-labels.md) that describes who can act next.

## Updating and completing work

Keep acceptance checklists in the issue body. Record decisions and implementation
updates in comments. Before closing an issue, link the implementing pull request
and record completed validation with its limits. A completed implementation is
tracked by the issue's closed state; triage labels describe readiness for action.

When an engineering skill says to publish a specification or fetch a ticket,
use the corresponding GitHub issue. Research and prototype findings belong in
the relevant issue unless they establish maintained product documentation or an
architectural decision.
