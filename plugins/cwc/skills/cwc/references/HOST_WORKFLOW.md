# Controller and official Host workflow

## Normal consultations: use the trusted relay

This section is for consultation sends and recovery, not archive registration, doctor, refresh or local pairing. Do not manually reconstruct Host parameters from printed JSON. For a new question, write one UTF-8 input file with `action: "prepare"`, the exact scope, substantive `body`, and optional `mode`/`task_title`. The controller freezes the contract version/hash and resource hashes into the request. To continue existing work, use `action: "resume"` and the same scope; omit request_id to recover that scope's pending request, or its latest saved exchange when idle. Resolve the installed package on each entry using the following code-mode (`functions.exec`) loader:

```js
const loaded = await tools.exec_command({
  cmd: `$ErrorActionPreference = 'Stop'
$cwcListing = codex plugin list --json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve installed CWC' }
$cwcEntries = @($cwcListing.installed | Where-Object { $_.name -eq 'cwc' -and $_.installed -and $_.enabled })
if ($cwcEntries.Count -ne 1) { throw 'Expected exactly one enabled CWC installation' }
$cwcVersion = $cwcEntries[0].version
$cwcMarket = $cwcEntries[0].marketplaceName
if ($cwcVersion -notmatch '^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$') { throw 'Invalid CWC version' }
if ($cwcMarket -notmatch '^[A-Za-z0-9_-]+$') { throw 'Invalid CWC marketplace identity' }
$cwcHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$cwcPackage = Join-Path $cwcHome ('plugins/cache/' + $cwcMarket + '/cwc/' + $cwcVersion)
$cwcManifest = Get-Content -LiteralPath (Join-Path $cwcPackage '.codex-plugin/plugin.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($cwcManifest.name -ne 'cwc' -or $cwcManifest.version -ne $cwcVersion) { throw 'Installed CWC manifest mismatch' }
$cwcSkill = Join-Path $cwcPackage 'skills/cwc'
@{version=$cwcVersion; controller=(Join-Path $cwcSkill 'scripts/cwc.py'); source=(Get-Content -LiteralPath (Join-Path $cwcSkill 'scripts/host_relay.js') -Raw -Encoding UTF8)} | ConvertTo-Json -Compress`,
  max_output_tokens: 16000
});
if (loaded.exit_code !== 0 || loaded.session_id) throw new Error("CWC installation could not be loaded");
const installed = JSON.parse(loaded.output);
try {
  text(await (0, eval)(installed.source)(tools, {
    controller: installed.controller, expectedVersion: installed.version,
    inputPath: "ABSOLUTE_INPUT_JSON_PATH",
    outputDir: "ABSOLUTE_LOCAL_EVIDENCE_DIRECTORY",
    mode: "read", maxReads: 64, onEvent: event => notify(event.message)
  }));
} catch (error) {
  text({status: "ERROR", error_code: error.code || "RELAY_ERROR", error: error.message,
    request_id: error.request_id, host_sends: error.host_sends, evidence: error.evidence,
    notice: "Inspect or resume the existing request; never repeat prepare/send to repair an error."});
}
```

Use `mode: "send"` for a newly authorized question or to finish sending a PREPARED request. The shown `mode: "read"` resumes without sending. Only the installed relay code is evaluated; never evaluate input files, Chat responses, or web content. The placeholders above are local paths, not conversation IDs or message bodies. `expectedVersion` rejects a mismatched controller before state changes. The relay passes the frozen arguments object directly to Host and mechanically preserves actual arguments/results. Never issue a separate direct send or recreate evidence. This loader picks the current installation; it does not reload Host tools or prevent a model from bypassing the workflow.

Use a short initial code-mode yield and resume the running cell in waits of at most 30 seconds; do not invoke the send entrypoint again. Default `maxReads` is 64 (absolute maximum 120), while the controller's persistent due time and deadline own the waiting policy. The relay never blocks a single sleep longer than 30 seconds and emits Q/A progress events. `host_sends` counts attempted send tool invocations, not delivery success. A thrown send is saved as an exception and stays UNKNOWN; resume it without resending. A local evidence failure likewise never authorizes a second send.

Prefer `action: "resume"` for every continuation. In send mode it can claim an existing PREPARED request; in read mode PREPARED stays unsent. UNKNOWN/WAITING/ABANDONED only read the fixed target. READY is consumed and returned; CONSUMED returns the stored answer as RECOVERED with `effect_check_required: true`. Check actual artifacts and prior effects before continuing from RECOVERED; it never authorizes repeating actions. Cancelled/discarded results return no answer. A fresh `prepare` means a new consultation and must not be reused as a retry, including after the prior request was consumed. Low-level claim/read inputs remain supported for diagnostics.

Purely local resume (IDLE, PREPARED in read mode, READY or CONSUMED) does not require Host tools or file-writing capability. Remote capability checks happen only when needed, and always before claiming a send. Controller failures retain their structured error code and available request/evidence context through the loader's error output.

`NO_MATCH` includes a `reason`: `NO_MATCH` means this page contains no attributable matching reply; `REPLY_PENDING` means the matching reply is still being generated; `RESPONSE_FORMAT_MISMATCH` means an explicit current-request first line has unsupported formatting. The relay stops polling on format mismatch and never sends a correction automatically. An uncorrelated answer stays NO_MATCH: do not infer its identity from prose or nearby messages. Host error codes distinguish wrong route, partial content, reported tool failure and unsupported data. These diagnostics never change the frozen request or relax acceptance checks.

Use the installed `scripts/cwc.py` relative to this Skill. It uses Python's standard library and stores two SQLite record types in `<CODEX_HOME>/cwc/state/cwc.sqlite3`. `CODEX_HOME` defaults to `~/.codex`. Do not use `--codex-home` in normal work; it exists for isolated tests.

Write controller inputs to UTF-8 JSON files with a file-writing tool. Run `python -B <skill-dir>/scripts/cwc.py --input <input.json>`; parse its JSON stdout. Never embed a long prompt, Host result or Unicode JSON document in a command-line argument. The controller only reads local input and manages state; the Executor calls the official Host tools.

Every scoped input includes `"scope":{"host_id":"<actual Codex host>","thread_id":"<actual root task id>","workspace":"<absolute workspace>"}`. Keep this scope identical across steps and restarts. Target host `chatgpt` denotes the observed global Chat surface: omit the Host tool's `hostId`. If the listing supplies another explicit host, retain it exactly. Do not switch surfaces after an error.

For project continuity, follow the global index policy referenced by the Skill. The recorded project root/index mapping is for documents only and must not rewrite the exact CWC scope above. Read project indexes to recover goals and decisions; use `status`, `read`/`accept` and `inspect` for actual exchange state. Index writes are ordinary Executor file work under the project's single-writer agreement, not controller actions; `status`, `doctor` and `refresh` never acquire index-write side effects. Never regenerate a frozen request or infer archive authority from a Reasoner answer.

Navigation provenance may record actual Host-listed conversation/task IDs and project metadata, with absent fields left absent. These are source locators, not Pair/Exchange state or authorization to associate/rebind a Chat. Metadata-only registration does not require reading all messages, opening a conversation, sending a request or generating a share link. A raw message locator is recorded only when a real result supplies it.

<a id="archive-pair-lookup"></a>

## Read-only pair lookup for archive registration

This is an Executor file workflow, not a new controller action. Use it only for the actual shortcut request described in the global policy. Keep the exact caller scope (`host_id`, root `thread_id`, original `workspace`) separate from canonical document paths.

1. Select the actual installed plugin using its manifest/installed listing. With Python `-B`, import that installation's `scripts/store.py` and call the existing static `Store.read_status(<actual CODEX_HOME>/cwc/state/cwc.sqlite3, scope, details=True)`. Supply context through the established UTF-8 JSON input pattern. Do not instantiate `Store`, write ad-hoc SQL or call JSON `action=status`: that path can initialize or migrate state and does not return the target. `refresh` omits target details; `doctor` removes target before returning, so neither is a replacement for this lookup.
2. The static reader returns `status`, a stored `title` and `target={host_id,kind,conversation_id}` when paired. It does not return scope: preserve the exact input scope as the Executor identity. It never creates a missing database or migrates it; WAL/unknown formats fail unchanged. Treat missing target as UNPAIRED, and read errors as a limitation rather than permission to repair state.
3. Capture the actual `list_threads` result. Match the Executor's ID/kind/host and original cwd from the Host metadata. For the Chat, reuse installed `host.candidates(raw_listing)` and match all target identity fields; its established global-Chat route normalizes an absent host to `chatgpt`, not `local`. Obtain display title, app projectId and actual URL from the corresponding original entry after matching. Keep raw absent host/project/URL fields distinguishable from normalized routing fields in the source note.
4. If the listing does not cover the selected target, expand only the relevant read-only lookup when possible. Otherwise record local-pair-known/remote-metadata-unverified under the global policy. Do not search by title to invent a replacement target or enumerate all pairs. A SELECTED record proves a local association, not remote readability, accepted advice or successful execution.
5. Before writing the association, repeat the static read and compare target identity and generation with the first observation; a changed pair must be rechecked. Only source IDs/titles, project-association basis, date and matching result belong in navigation. `generation`, `exchange`, `last_exchange`, stopped-request details and raw result packets stay out of the source index. Keep old source links when a later pair is registered.

This lookup never calls pair/prepare/consult/claim/read/accept/consume/stop or sends a Host message. Fetching project content, if necessary and authorized, is a separate bounded source read and must not consume or advance a pending exchange. Standalone status/doctor/refresh controls keep their existing behavior; archive registration uses the static reader specifically to avoid startup effects.

| Action | Additional input | Result / next step |
|---|---|---|
| `list` | `result`: unchanged official `list_threads` result; no scope needed | Internal `candidates`; show only numbered titles to the user |
| `pair` | `listing`: unchanged listing, `conversation_id`, `target_host_id`: exact selected candidate | `SELECTED`; no Host message |
| `status` | None | Selected display title, current pending exchange and latest exchange summary |
| `refresh` | Optional `expected_version`, `known_hashes`; `full_resources:true` only for explicit diagnostics | `REFRESHED`, actual thin Skill `operator_instructions`, resource paths/hashes and changed names, read-only binding; no Host call or state mutation |
| `doctor` | Optional `available_tools` inventory; optional `read_probe` with actual read `called_with` and full `result` | Concise read-only checks, issues and at most one suggested read probe; no send or state repair |
| `consult` | `body`: current question and facts; optional `mode`: `consult` or `review` | `SEND_ONCE`, request_id, official `tool` and `arguments`; state was durably claimed before output |
| `record-send` | request_id, `called_with`: exact actual Host send arguments, `result`: unchanged tool result | Delivery state plus `next` read operation; errors remain uncertain |
| `read` | request_id; optional supported Host `cursor` | Exact official read operation for the frozen target; no outgoing message |
| `resume` | Optional request_id; otherwise resolve the exact scope's pending/latest exchange | Read pending work, consume READY once, or recover CONSUMED with an effects-check notice; never claims a send itself |
| `accept` | request_id, `called_with`: exact actual read arguments, `result`: unchanged tool result | Answer consumed once, or `NO_MATCH`; no resend |
| `inspect` | request_id | Read-only retained answer and state for recovery; not an instruction to replay actions |
| `cancel` | request_id | Only cancels a never-claimed `PREPARED` request |
| `stop` | request_id, reason, `confirmed: true` derived from the user's explicit stop decision | Stops waiting while retaining the request; unresolved target stays reserved until a complete correlated reply is discarded |
| `disconnect` | None | Only disconnects an idle pair |

The action table also documents the low-level controller API for tests and diagnostics. In normal work, the trusted relay executes `SEND_ONCE`, record-send and read/accept; do not manually copy their arguments or results. Never call `wait_threads` for a ChatGPT target; the relevant current Host tools are send/read.

New prepare inputs can include `waiting={initial_delay_ms:60000,poll_interval_ms:30000,wait_budget_ms:600000,max_wait_ms:1800000}`. Defaults apply when omitted; explicit overrides must pass the controller's bounds. Time begins at claim and persists across processes; the first read is no earlier than 60 seconds after a recorded send return. `read` plans, and `read-start` reserves a due attempt before the actual Host read. Only an attributable `REPLY_PENDING` near a deadline extends it by 5 minutes, up to the original 30-minute maximum. No match, read errors and formatting failures do not extend it. `WAIT_EXPIRED` preserves the request and reports `project_complete:false`; check other independent work or keep a resume checkpoint, never tight-loop an expired request. Old records without timestamps get one explicitly marked legacy anchor on first recovery, not on every resume.

For a new follow-up, use a new prepare input with `parent_request_id` taken from the actual consumed result. It must belong to the same scope and Pair generation. Repeating the parent's exact body without new evidence is rejected. The controller returns stable `question_id`, `answer_id` and `parent_answer_id`; these are display references, while full request IDs remain the machine identity. Waiting and resume never allocate another label.

Long replies may exceed the former 12,000-character soft limit, but must remain readable under the observed 20,000-character per-message ceiling. A reply ending in `CWC CONTINUE: <remaining sections>` is delivered with `answer_complete:false` and `required_continuation`. Obtain the missing sections with linked follow-ups before executing dependent steps. This is not an invented multipart transport: the original question is never resent, and each follow-up has its own valid response. Truncated messages and unread attachment references cannot satisfy completion.

## Check progress after replies and significant operations

After the active observation window expires, a fresh explicit `resume` may make one late read of the same request. It does not reset the claim/deadlines or grant another send. The relay ends that invocation after the probe; repeated late probes share a persisted minimum 30-second interval. A complete reply is delivered normally; an explicitly stopped ABANDONED reply is only DISCARDED. A still-generating observation may extend only within the original maximum window; the next resume uses the resulting saved schedule. Ordinary automatic `read` does not acquire this late-probe allowance.

Observed elapsed time is clamped against the last saved clock value. Clock rollback cannot decrease recorded elapsed time or move saved deadlines; a prolonged operating-system clock error can still delay wall-clock scheduling until corrected.

The real Host supplies completed-turn status; the current observed reply is shorter than the requested 20,000-character limit. Messages reaching that ceiling are conservatively rejected, counting UTF-16 units so astral characters cannot hide a clipped boundary. Explicit partial/truncated flags always reject. No new ending-marker protocol is assumed, and a future Host shape/limit change requires renewed verification.

Use `action:"check-progress"`, the exact scope, and a `checklist` extracted from the user's existing project checklist. The controller does not maintain a second project registry or write indexes. An example of the input shape is:

```json
{
  "goal": "The user's authorized final outcome",
  "required": [
    {"id":"implementation","title":"Implement required behavior","passed":true,"evidence":["Verified test log or artifact reference"]},
    {"id":"result_review","title":"Return actual evidence to Chat","passed":false,"question":"Do these results satisfy the requested behavior?"}
  ],
    "review_required": true,
    "review_passed": false
}
```

Each remaining item may contain `authorized:true` and `next_action`, a concrete `question`, or `blocked_by`. The checker prefers independent authorized execution before blocked branches. It returns CONTINUE_EXECUTION, NEED_CONSULTATION, WAITING, BLOCKED, PAUSED, STOPPED or COMPLETE, with a reason and next item. User stop and explicit budget exhaustion can be represented by `user_stopped` and `budget_exhausted`; never fabricate either. A `passed:true` item requires checked evidence. `review_request_id` must identify the latest consumed, complete review of the current Pair. Only an explicit user waiver or an ordinary question justifies `review_required:false`.

This gate checks the supplied checklist and stored review; it cannot independently prove every external effect or prevent every native final response. The Executor must preserve the user's full required scope, inspect the cited evidence, perform the returned next action, and report why a real blocker or pause prevents continuation. CONSUMED and a Chat statement to finish do not by themselves permit COMPLETE.

The database verifies `review_received`, meaning the current latest complete review was consumed. The Executor must separately set `review_passed:true` only after reading the review, adding its blocking findings to required items, and verifying their resolution. A missing/false value prevents COMPLETE even if every older checklist item passed. This is an explicit assessment, not automated natural-language grading.

Low-level `prepare` then `claim`, and `receive` then `consume`, expose the same transitions for diagnostics/tests. `prepare` returns no sendable payload; `claim` emits one only once. After interruption, use the relay's resume input rather than reconstructing the transition sequence. `inspect` remains available for explicit low-level diagnostics.

There are no remote Binding ACKs or Contract ACKs. The wire is `CWC REQUEST <request_id>` plus the question and `CWC RESPONSE <same request_id>` plus the answer. Source identity and completion come from the Host result and fixed route, not from the model's claim. An unavailable Host surface is a blocker for the consultation, not a reason to use browser/private APIs or invent transport receipts.

For a direct refresh, run `python -B <installed-skill-dir>/scripts/cwc.py refresh` from the user's workspace. It uses `CODEX_THREAD_ID` and the current directory; `--thread-id`, `--host-id` and `--workspace` provide explicit context when needed. The host defaults to this local desktop's `local`; use the actual host for a remote task. Do not change the working directory to the plugin installation. Missing task identity fails safely. The JSON `refresh` action with the established scope is equivalent.

Refreshing validates all current resources and returns the actual thin Skill operator instructions with paths/hashes and changed-resource names. The Executor applies these instructions immediately, then reads the current substantive work instructions when starting business work. Machine code and full reference texts are returned only with explicit `full_resources:true`; behavior is never guessed from a hash. Refresh neither resets the conversation nor replaces Host-injected tools. Invalid resources or unsupported state fail without migration, rebinding or resend. Prepared requests keep their frozen text; new requests receive the current contract and Reasoner rules.

Refresh supports CWC's default SQLite rollback journal. If an external tool changed the database to WAL, refresh fails without opening or converting it; a read-only WAL connection can still create auxiliary files. Resolve such a custom database change explicitly, never change its journal mode during refresh.

`task_title` is optional display metadata on pair/prepare/consult/refresh/doctor. Use the current Codex task title or the user's explicit display label. Pair/consult save it for later reuse; refresh/doctor only override the displayed suggestion for that call. The next consultation should receive the current title again. The name is `CWC|workspace-task title|8hex`; labels are sanitized and shortened, and label edits do not change the pair tag or generation. Names accompany real consultation messages, without a separate naming send.

The Chat receives only the contract summary, its version/hash, resource hashes, and facts necessary for the current question. Never include the full local `AGENTS.md`, machine paths, credentials, caches, marketplace paths, unrelated project rules, or runtime state.

Direct local entrypoints also include `python <installed-skill>/scripts/cwc.py doctor` and `python <installed-skill>/scripts/cwc.py stop --request-id <id> --reason <reason> --confirm`. `--confirm` represents an already explicit user stop decision. Direct doctor has no model tool inventory and correctly reports it as untested; the Skill supplies actual visible tool names for a fuller check. All direct commands accept the same context options as refresh, plus optional `--task-title`.

Stop can release the current task for a new target while preserving the unresolved original target reservation. A complete reply to an ABANDONED request changes it to DISCARDED and releases that reservation; `accept` does not return its answer for execution. `inspect` retains the evidence and stop reason for diagnostics. A stop that races with consumption cannot undo an answer already handed off.

Schema 1/2/3 upgrades transactionally to schema 4 on the next normal Store operation. It preserves existing payloads, receipts, replies, states, targets and Pair generations; adds unique display IDs, parent links, normalized resource snapshots and timing metadata. Refresh and doctor read schemas 1–4 without migrating. Older runtimes cannot safely process schema 4: reload the new installed workflow, never overwrite or downgrade the live database. Resource hashes use UTF-8 text with normalized LF line endings; new requests include the actual contract text and Reasoner instructions. Drift blocks a never-claimed request; recovery of an already claimed request does not reinterpret it under current resources.
