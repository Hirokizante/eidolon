# Current-life notes and summaries

[Back to Eidolon](../README.md)

Current life keeps local notes about ongoing topics across daily rooms. It is
separate from external Hindsight recall and delivery; see
[optional memory](MEMORY.md) for those setup requirements and controls. Local
conversation history remains available when all memory controls are off.

## Start with a reviewable set of notes

New installations leave automatic updates and reply inclusion off. Use
`/preferences` → **Current life**, then Ctrl+S to save:

| Preference | Configuration | Behavior |
| --- | --- | --- |
| Update current life automatically | `memory.continuity_enabled` | Maintain notes from eligible completed chats in the background. Default off. |
| Use current life in replies | `memory.continuity_use_enabled` | Include active usable notes when **Memory → Use learned memories** (`memory.enabled`) is also on. Default off. |
| Maximum active notes | `memory.continuity_max_notes` | Default 12, range 1–64; archived notes do not count. |
| Completed exchanges per update | `memory.continuity_batch_exchanges` | Default 8, range 1–32. |
| Idle time before update | `memory.continuity_idle_seconds` | Default 600 seconds, range 1–86400. |

For automatic summaries, first choose **Auxiliary model → Follow dialogue model**
or **Choose a separate model** and check its configuration in
[the auxiliary-model guide](AUXILIARY_MODEL.md). Turn on **Memory → Save new
memories** before completing chats you want eligible for learning. Maintenance
uses their locally saved evidence; it can run without a configured Hindsight
bank, retention readiness, or completed external delivery. Saving also queues
those exchanges for external retention if you enable its independent readiness
gates later. Earlier chats accepted with saving off are not picked up retroactively.

Open `/memory` → **Current life** → **Refresh current life** to request one
bounded pass, even with automatic updates off. Review **Notes** and **Run history**
before turning on reply inclusion. A larger backlog can require several passes.
Manual refresh does not enable automatic updates or inclusion; it still requires
a working auxiliary model and eligible source evidence. To maintain or inspect
notes without recalling them, keep **Use learned memories** off. Reply inclusion
currently shares that switch with Hindsight recall: enabling both use controls
also attempts external recall, so it requires an available existing bank. There
is no separate local-only inclusion switch. `/retry without-memory` skips both
layers for that reply.

Automatic updates begin when the batch threshold is met or the idle interval
since the latest eligible completion expires. Foreground conversation takes
priority and can interrupt maintenance; pending work is preserved for retry.
To stop automatic inference, turn off automatic updates or set the auxiliary
model to **Off**. To stop inclusion, turn off its separate use control. Neither
choice deletes existing notes or revokes earlier external deliveries.

The current browser shows note wording, topics, importance, and before/after run
versions. It keeps source evidence and diagnostic provenance private rather than
displaying raw IDs or messages in note details. For deeper inspection, use the
[evidence service operations](MEMORY_REFERENCE.md#private-context-and-evidence).
Reading or reloading a view does not request model inference. Ctrl+L reloads the
view; **Refresh current life** explicitly requests work.

## Narrative behavior

Current life supplies short, cohesive prose about ongoing situations, unfinished
threads, relationship context, and temporary emotions. The existing auxiliary
worker writes these summaries with the configured model: **Follow dialogue model**
uses the dialogue target; **Choose a separate model** uses that selected target.
**Off** performs no summary inference. Automatic updates and inclusion in replies
remain independent preferences. No model call is added to the foreground reply path.

Each note describes one developing topic in third-person prose, ideally under
600 characters and at most 1,200. The maintenance prompt asks the model to combine
related exchanges, preserve background, corrections, uncertainty and attribution,
and explain what remains open. The summarizer receives configured names in
`current_identity`: `user_name` identifies the user (`user_content`) and
`companion_name` identifies the companion (`assistant_content`). Missing settings
default to **You** and **Companion**. It uses names or neutral role terms when
pronouns are ambiguous, and preserves explicitly stated pronouns in evidence.
A chosen name implies no gender, personality, relationship, or shared history.
Machine-readable `user`/`assistant` role and origin labels retain their schema
values. The companion's suggestions, user decisions, promises and completed
actions must stay distinct.
Relative dates in prose are anchored to the original speaker's timestamp and the
configured timezone.

Every note has a **topic**, which is its title in `/memory`, and **importance**:

- **Low**: little consequence for the companion or the relationship; forgetting or cancelling
  a promise would cause little disappointment. Mundane errands often fit here.
- **Moderate**: meaningful consequences and a reason for an occasional natural
  check-in; forgetting or cancelling a promise would disappoint the companion. An anticipated
  gathering is a typical example.
- **High**: deeply consequential to their bond or how they interact; a broken
  commitment could be deeply hurtful. A meaningful anniversary is a typical example.

These examples guide judgment rather than set fixed scores. The model uses the
exchange, prior evidence, context, and reasonable implications to assess relational
impact. Urgency and repetition alone do not establish importance. Inferred importance
does not establish unstated feelings, events, or commitments. Low-importance notes
do not request routine follow-ups; moderate and high importance guide attention
without requiring repeated reminders.

The model updates a note only when its particular topic develops. Topics stay stable
after classification; unrelated information requires a separate subject. New notes
must plausibly recur, describe a persistent situation, or support a meaningful future
interaction. One-off remarks, answered questions, closed small talk, and transient
trivia should not become notes. Repeated exchanges about one topic update its existing
note rather than accumulating copies. Duplicate active topic titles are rejected.

## Active limit and retirement

**Preferences → Current life → Maximum active notes** sets `memory.continuity_max_notes`
from 1 to 64, default **12**. Archived notes consume no slots. Both proposal validation
and the saving transaction enforce admission limits; there is no automatic eviction
on insertion. The transaction checks every stored active note, even when a bounded
model request or menu omits some of them.

Maintenance first archives topics that concluded, situations that ceased to be true,
and promises that were fulfilled or broken. It does not wait for an explicit request
to archive them. The supporting development must appear in original evidence;
questions, tentative future outcomes, and a date simply passing do not prove closure.
A particular discussion ending closes that discussion's note; changing rooms or
ending a chat session does not close ongoing situations or unresolved promises.

When space is still unavailable, the model must explicitly pair an archive with a
new admission: the new note names `replaces_item_id` and a `decision_reason`, and the
old note is archived with `archive_reason: replaced` and its own explanation. The new
note must have strictly greater importance than the victim's saved rating. Equal or
higher-importance notes cannot be displaced. If no eligible victim exists, the model
must skip the proposed new topic. Archiving and admission commit atomically, and both
decisions are saved with note revisions. Note details show decision explanations;
the content-oriented run history shows the before/after versions.

Lowering the limit does not silently drop existing notes. Maintenance chooses which
eligible notes to archive, preferring lower importance and less continuing relevance.
This cleanup can run without new exchanges and can span bounded passes. User-created
and locked notes stay protected and occupy slots. If protected notes alone exceed the
limit, they remain saved and additions are blocked until space is made. A user can
archive a note through its editor or raise the limit in preferences.

`/memory` lists **active notes first**, with **archived notes below** and an explicit
Archived label. Completed topics use the archived state, including legacy `resolve`
operations. Archived content stays readable and source evidence is retained privately.
Archived notes are excluded from future dialogue continuity context. Previously saved request contexts
and conversation history remain historical records.

The dialogue prompt defines `situation`, `thread`, `relationship`, and `emotion`,
along with observation/event dates, user-authored notes, promise ownership/kind,
and follow-up cooldown labels. It identifies the prose as derived background,
prioritizes current user corrections and warns against assuming outcomes.
The full continuity envelope and complete notes share a 3,000-character limit;
historical recall receives the remainder of the existing 8,000-character allowance.
There is no raw unprocessed-exchange bridge in the continuity section. Current
room history and historical retrieval retain their independent behavior.

## Participant identity and historical evidence

Edit names in **Preferences → Identity** or private `config.yaml`; see
[identity configuration](INSTALLATION.md#display-names) for setup
flags, validation, and custom-prompt precedence. Each maintenance attempt captures
the saved current identity before budgeting and inference. A rename during a held
request leaves that attempt's names intact. Later attempts, including retries of
an unfinished batch, capture the latest names without changing the batch's source
identities or hash. Foreground replies and scheduled runs similarly keep the names
captured at their own start. No service restart is needed for a saved name change.

Exchanges and note evidence can carry their own recorded `identity`, distinct from
`current_identity`. For example, with synthetic current names `Aster` / `Vega`, an
older exchange recorded as `Aster` / `Nova` retains that attribution in maintenance
input and evidence inspection. Missing source identity means no names were recorded;
the worker must not infer them from message text. Chat/history display uses current
labels for those legacy messages without rewriting their bodies.

Changing identity does not perform name replacement in saved notes, source messages,
memories, request captures, private prompts, or queued deliveries. New summaries
may update eligible automatic notes from evidence through normal maintenance;
renaming alone is not a content migration. Existing user-created/locked-note
protections, recall/save controls, source timestamps, and context budgets still apply.

## Evidence and updates

### Manual notes

**Memory → Current life → Notes** supports **Create note** (Ctrl+N), and **Edit note**
(Ctrl+E) or **Delete note** (Ctrl+D) when reading a note. Give the note a topic and
importance, then choose **Situation**, **Open topic**, **Relationship**, or **Feeling**.
The editor also lets you change an existing note's status to Active or Archived. Ctrl+S
saves up to 4,000 characters. Esc returns with the unsaved draft kept for reopening;
deletion asks for confirmation with **Keep note** selected first. These actions
work locally even when the auxiliary model or automatic maintenance is off.

The list marks created notes **User-created**. Their persisted `user_created` flag
is independent of `origin: user_note`, which is also used for edited automatic
notes. Maintenance instructions, proposal validation, and the commit transaction
all protect created notes from updates, archival, and resolution. Editing them
preserves that protection. Created Feeling notes have no automatic expiry.

Editing an automatic note keeps it eligible for evidence-based maintenance, including
updating its wording, archiving, and resolving it. Its authored wording stands on
its own until replaced with new evidence. Changing an automatic note's label to
Feeling starts its usual 48-hour expiry; changing that label away from Feeling
removes that expiry. An explicit existing lock remains respected unless the user
edits the note's wording. Every manual mutation advances the scope and control
revisions so in-flight maintenance must regenerate its proposal; edits and deletions
also require the exact item revision shown to the user.

Deleting removes the current note. Previously saved reply contexts and run versions
remain historical records. Manual notes and edits do not create synthetic exchanges
or send content to long-term memory retention.

The interface supports ASCII/monochrome rendering. Narrow dialogs identify
the focused field in their border so scrolling never leaves an unexplained value.

### Automatic notes

For ordinary updates, the model returns `topic`, `importance`, category, prose
`content`, and request-local source references. It can cite both speakers without
repeating their messages in its output. The application supplies narrative format,
derived origin, revisions and source identities and attaches each complete original message, including
messages longer than the old 4,000-character note limit. Evidence remains available
through private service inspection and revision history; it is not copied into the dialogue
continuity prompt.

Updates may reuse the target note's supporting originals, which the worker supplies
alongside the existing summary and new exchanges. Updates must also cite the
current task's evidence. A separate legacy conversion pass uses all of a named
note's existing evidence and preserves its category, scope and optional metadata.
Sources from unrelated notes or an
unseen older speaker cannot be attached. Upserts retain only the explicitly cited
current support, up to eight messages; earlier revisions keep superseded evidence.
Source eligibility, complete snapshots, locks, scope and revisions are rechecked at
commit. Revoking an earlier source invalidates the summary, including when new
evidence is added.

Dates, promise metadata and closing a topic still depend on complete original
statements. Generated prose cannot establish those changes. These deterministic
checks establish provenance and metadata constraints, not the semantic truth of
a paraphrase. Capacity/replacement archives instead record allocation decisions;
they can use `sources: []` because they retain the note's previous evidence and
do not claim the situation ended. Tests use synthetic model responses and do not establish that a
particular selected model produces faithful prose or improves answer quality.

Topic matching recognizes bounded wording equivalents, including cutoff/closing
date/due date for a deadline, hiring conversation for an interview, house move for
relocation, and doctor visit for a medical appointment. It also uses the target
note's saved originals when a later message names a subject omitted from its
summary. Old citations alone never authorize an unrelated new development.

Ordinary completion language such as "wrapped up", "got through", or a reported
successful interview can close the matching note. For example, "That meeting at
Ember Library? All wrapped up; they offered me the catalog assistant role" can
close the earlier Ember Library interview. The named introduction and its answer
must be in the same original message. A vague "it's done", interview preparation,
questions about completion, future intentions, uncertainty, negation, conditionals,
and mixed real/fiction messages do not establish that outcome. Reported speech,
quotes, and ambiguous third-party ownership are conservatively rejected for
closure, including when attribution appears in an earlier paragraph.

These local rules use the same auxiliary proposal call and are rechecked inside
the saving transaction. They cover specific wording and reference patterns rather
than general language understanding; unfamiliar paraphrases can remain unresolved.
A rejected proposal leaves all notes and evidence progress unchanged and keeps the
batch pending, rather than recording a successful no-change. Synthetic service and
storage tests cover these outcomes; no live-model reliability rate is established.

## Failure explanations

A quiet Life indicator near the composer points to `/runs` while an unresolved
condition needs retry or configuration attention. `/runs` and `/memory` →
Current life → Run history show safe per-attempt explanations alongside model,
timing, available usage, recovery allowance used/remaining, configuration pause,
next action/time, last success, and saved note comparisons.
Reasons remain visible after a later retry succeeds. Older attempts with no
recorded reason say so; unknown usage is never shown as zero. See the
[auxiliary failure guide](AUXILIARY_MODEL.md#diagnose-and-recover) for diagnostic
categories, history retention, and current retry-policy limits.

## Proposal format and retries

Maintenance expects a JSON object containing an `operations` array. Each entry
uses the exact key `op` (`upsert`, `archive`, or `no_change`, depending on the task).
Ordinary and capacity tasks allow 1–24 entries; legacy tasks allow 1–4. Task-specific
prompts provide complete examples. The model uses request-local `note_ref` and,
for ordinary evidence changes, `sources` references. The application supplies
item IDs and expected revisions from its captured snapshot. See the
[request contracts](AUXILIARY_MODEL.md#developer-reference-routing-and-provenance)
for complete response shapes.

A missing or invalid `op` produces specific correction instructions for the next
retry, including after a service restart. Unknown fields and invalid evidence still
reject the whole proposal. Failed attempts preserve the pending batch, existing
notes and watermark. A pass defaults to six dispatched auxiliary calls. After
exhaustion, automatic calls stop and newer evidence still waits; explicit Refresh
starts a fresh bounded pass with lifetime history intact. Recognized configuration
failures pause unchanged settings until a relevant change or explicit Refresh.
See [bounded recovery](AUXILIARY_MODEL.md#bounded-recovery-and-configuration-pauses).
Diagnostics use application-owned text; raw model output and
arbitrary schema error details are not persisted or replayed as instructions.
Responses stopped by the provider's token limit are reported as output-token
exhaustion, separately from connection failures. Auxiliary settings allow up to
8,192 output tokens, including any thinking counted by the serving provider.
For supporting models/gateways, the separate thinking-mode setting can explicitly
disable thinking without changing the dialogue model's generation options.
The auxiliary timeout covers the entire request, including time spent thinking
before answer text arrives. A successful synthetic connection test does not prove
that a full maintenance batch will finish within that deadline. If a timeout occurs
after streamed reasoning begins but before answer text, Current Life reports that
the auxiliary model timed out while thinking and directs the user to `/preferences`.
Disable thinking for a gateway that supports it, or increase the auxiliary timeout.
Only a progress flag is retained; reasoning text is never emitted or stored.

Over-limit reviews permit only capacity archives before legacy classification runs.
Their responses need only `op: archive`, `note_ref` and `decision_reason`; the app
supplies the capacity reason and preserves all saved note data. They may remove
no more than the existing excess and cannot change protected notes, add notes or
claim topics concluded. Ordinary replacement archives remain paired with their
admissions. Closure archives still require fresh topic-matched evidence.

A completed legacy conversion returning `no_change` is held until the current-life
state changes or the user requests another refresh. Automatic ticks never dispatch
a completed batch again, including after a restart. A deliberate refresh can create
one fresh conversion attempt; retries of unfinished work keep their original batch.
The memory screen reads local update status while work is running and clears the
running label on completion. Status reads do not trigger model requests.

## Existing notes

Once new eligible exchanges are processed, a background update or **Refresh current
life** converts existing eligible, unlocked extractive notes using the same selected
model, even if there are no new chats. Conversion keeps the item identity, evidence,
existing optional metadata and revision history, without advancing the input
watermark. It is bounded, restartable, and subject to the existing routing and
foreground-preemption checks. Larger collections may require multiple batches.
Maintenance explicitly identifies the supplied notes awaiting conversion and treats
rewriting them as useful work even when the underlying facts have not changed.

Older notes without topic/importance remain readable with a provisional title derived
from their content and a neutral Moderate rating. Eligible unlocked notes receive a
deliberate title and rating from their originals on the next maintenance pass, even
when their content already uses narrative prose. Each conversion pass targets at
most four notes whose complete source exchanges fit its input batch. Already
narrative notes keep their established prose during classification; the model
assigns topic/importance and omits content. The app supplies every saved source
and preserves category, scope and optional date, expiry and promise fields.
Only the explicitly named eligible targets may change; new notes, unrelated
updates and archives reject the whole pass. Classification preserves identity,
source evidence, optional metadata, history, and the exchange watermark. Protected
notes keep their provisional metadata until the user edits it. This compatibility
upgrade needs no database schema migration.

Legacy copied notes are omitted from the dialogue continuity section until converted.
User-authored notes remain usable as written; locks are respected. When updates are
off, request a manual refresh to convert old notes. When the auxiliary model is off,
select a model before refreshing. New exchanges await maintenance rather than being
inserted verbatim as a fallback. Conversion writes the existing JSON item/task data;
no SQLite schema change is needed.
