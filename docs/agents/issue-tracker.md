# Issue tracker: GitHub Issues

Use [GitHub Issues](https://github.com/Hirokizante/eidolon/issues) for specifications,
engineering tasks, and implementation updates. Use the `gh` CLI from the repository
root; it infers the repository from the Git remote. When running elsewhere, pass
`--repo Hirokizante/eidolon`.

Review shared content under the
[privacy guide](../PRIVACY.md#sharing-diagnostics) before publishing it.

## Creating or fetching work

- Create an issue with the problem, intended behavior, scope, acceptance criteria,
  and validation plan. Keep examples synthetic.
- Fetch an issue by its GitHub URL or repository issue number. Read its body and
  comments before starting implementation.
- Record dependencies using native GitHub issue dependencies where available.
  Otherwise, put `Blocked by: #<number>` at the top of the issue body.
- Link related specifications and pull requests rather than copying their contents.
- For a larger effort, keep the overall specification in a parent issue and
  track independently actionable work in linked child issues.
- Apply the [triage label](triage-labels.md) that describes who can act next.

## CLI operations

Write multiline issue bodies and comments to a temporary file outside the checkout,
then pass it with `--body-file`.

| Operation | Command |
| --- | --- |
| Create | `gh issue create --title "<title>" --body-file <file>` |
| Read body and comments | `gh issue view <number> --json number,title,body,labels,comments` |
| List open issues | `gh issue list --state open --limit 100 --json number,title,body,labels,comments` |
| Update body | `gh issue edit <number> --body-file <file>` |
| Comment | `gh issue comment <number> --body-file <file>` |
| Replace triage label | `gh issue edit <number> --remove-label "<old>" --add-label "<new>"` |
| Create child issue | `gh issue create --parent <parent> --title "<title>" --body-file <file>` |
| Link existing child | `gh issue edit <parent> --add-sub-issue <child>` |
| Add blocker | `gh issue edit <number> --add-blocked-by <blocker>` |
| Close | `gh issue close <number>` |

Use appropriate `--label` and `--state` filters when listing issues. Increase
`--limit` or paginate when the result set may exceed it. Check `gh issue <command>
--help` for available flags; parent and dependency flags require a recent CLI.

Where native sub-issues are unavailable, add the child to a task list in the parent
body and put `Part of #<parent>` at the top of the child body.

## Updating and completing work

Keep acceptance checklists in the issue body. Record decisions and implementation
updates in comments. Before closing an issue, link the implementing pull request
and record completed validation with its limits. A completed implementation is
tracked by the issue's closed state; triage labels describe readiness for action.

When an engineering skill says to publish a specification or fetch a ticket,
use the corresponding GitHub issue. Research and prototype findings belong in
the relevant issue unless they establish maintained product documentation or an
architectural decision.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Wayfinding operations

Used by `/wayfinder`. The map is a single issue with child issues as tickets.

- **Map:** label the map `wayfinder:map`. Its body holds Notes,
  Decisions-so-far, and Fog.
- **Child ticket:** link it to the map as a sub-issue, using the fallback above
  when necessary. Apply `wayfinder:<type>`, where the type is `research`,
  `prototype`, `grilling`, or `task`.
- **Blocking:** use native issue dependencies, with the `Blocked by` fallback
  above. A ticket is unblocked when every blocker is closed.
- **Frontier:** inspect the map's open children and their blockers and assignees.
  Select the first unblocked, unassigned ticket in map order.
- **Claim:** assign the selected ticket to the driving developer with
  `gh issue edit <number> --add-assignee @me` as the session's first write.
- **Resolve:** comment with the answer and validation, close the child issue,
  then append a concise finding and issue link to the map's Decisions-so-far.
