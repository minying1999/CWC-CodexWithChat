# CWC — focused reasoning, local execution

CWC lets an Executor consult a user-selected ChatGPT Reasoner or Reviewer.
Models are interchangeable. Pairing is a local selection, with no model handshake.
Entry commands accept both `cwc` and `/cwc`. The fixed launcher shows Chat choices before business-work instructions. Choosing a target saves the binding and immediately offers optional input **1** for one verification exchange. Direct business text closes that menu without an extra probe. Numeric replies require the active menu token and never select a different target from a reordered list.
An idle, already-bound `cwc refresh` offers the same input **1** test. Refresh itself sends nothing and keeps Pair/Exchange records unchanged; only local menu state is replaced. Menu tokens include the plugin version, so obsolete menus cannot authorize new probes after an upgrade. Existing requests remain available through `cwc resume`.

Contract v3 emphasizes Chat's careful reasoning, planning and review. Codex proactively consults on initial approaches, material uncertainties, difficult choices, obstacles and intermediate evidence, then executes and verifies useful next steps; the user need not request each consultation.

## Use

Activate CWC in a new Codex task, select an existing Chat, and describe the work.
The Executor continues locally and consults only when a decision or review is useful.

- `/cwc bind`: select an existing Chat.
- `/cwc status`: show the selected Chat and pending work.
- `/cwc rebind` / `/cwc disconnect`: change or disconnect an idle pair.
- `/cwc sync`: read the pending response once without resending.
- `/cwc resume`: recover the pending/latest consultation without sending; return saved answers with a reminder to check prior actions.
- `/cwc stop`: explicitly stop waiting, preserve the request and then allow a different target; it does not cancel remote generation.
- `/cwc doctor`: inspect local health and supplied tool availability, with at most one optional Host read.
- `/cwc refresh`: run the read-only refresh action, reload local rules and show version/binding status; no handshake or state writes.
- Review pool: opt-in only for independent high-risk review dimensions; the default remains one Pair and one Exchange.
- `/cwc audit`: inspect the latest locally recorded consultation.
- `/cwc progress`: check the existing project checklist after a reply or significant action; continue authorized work until acceptance or an explicit blocking/stopping condition.
- `归档登记簿` / `归档该cwc项目`: natural-language registration shortcuts; use the global policy linked from the Skill, with read-only pair verification for the CWC form.

The installed [Skill](skills/cwc/SKILL.md) describes the workflow. Its
[Host reference](skills/cwc/references/HOST_WORKFLOW.md) gives the controller inputs.
A short [Reasoner prompt](skills/cwc/REASONER.md) accompanies each question.

Large-project continuity uses the local Markdown index policy linked from the Skill.
The Executor maintains the user's project indexes; Reasoner advice and actual execution
remain distinct. The current Skill, Reasoner prompt and Host reference carry this work
agreement; historical Work Contract snapshots remain historical.

Refresh has both a JSON controller action and a direct `python <installed-skill>/scripts/cwc.py refresh`
entrypoint, run from the user's workspace. It works without a binding and preserves
pending requests. It reloads local instruction files; native Host/tool hot-reload is
not claimed. The returned plugin version identifies the exact package read.

## Implementation

Standard-library modules under `skills/cwc/scripts/`:

- `cwc.py`: file-backed controller and exact Host call arguments.
- `store.py`: SQLite pairs and exchanges, transactional claim/receive/consume.
- `host.py`: strict normalization of actual official Host tool results.
- `legacy.py`: read-only check for known unresolved old requests.
- `progress.py`: a read-only completion/next-action check over the existing project checklist.
- `entry.py` and `entry_relay.js`: fixed routing, saved candidate menus, exact selection and an optional one-shot verification delegated to the existing transport.

The trusted `host_relay.js` code-mode entrypoint transports the controller's actual
argument objects and records real Host results. The Executor only writes the semantic
prepare input; it does not retype conversation IDs, prompts or receipt JSON. The relay
uses the existing official Host tools, with one send attempt and bounded reads, and
does not add a standalone server or background service. Run its regression checks with
`node tests/test_host_relay.js`.

`CWC_AGENT_CONTRACT.md` is a machine-neutral contract summary. The controller hashes
it with the Skill, Reasoner rules, Host workflow, and relay. New requests freeze those
metadata; pending requests keep their original contract. Full local `AGENTS.md` is
never sent to Chat.

The normal loader resolves the enabled installed version and passes it to every
controller operation. A stale version fails before state writes. Continuations use
`resume` instead of reconstructing request IDs and state transitions. A new `prepare`
input must never be replayed as a retry after completion. Header-format diagnostics
remain separate from acceptance; they never trigger an automatic correction send.

Runtime state lives at `<CODEX_HOME>/cwc/state/cwc.sqlite3`, outside plugin caches.
The default is `~/.codex`. The Executor invokes official Host tools; the Python
process does not call private APIs, run a daemon, or create scheduled work.

Requests use one local request ID and a substantive question. Replies echo that ID
and provide an answer. Actual Host receipts remain separate and may have no message
ID. Sending is claimed before the tool call; ambiguous delivery is retained as
UNKNOWN and never automatically resent. Reading and accepting a late response works
from either UNKNOWN or WAITING. Local consumption does not prove external actions
were executed.

One global target has at most one pending request in a shared state root.
A never-claimed PREPARED request may be cancelled; other unresolved states must be
reconciled or explicitly stopped. Stopped UNKNOWN/WAITING requests become ABANDONED:
the current task can use another target, while the old target stays reserved. A late
valid answer is saved as DISCARDED, never delivered, and releases that reservation.
`status.last_exchange` and `inspect` recover a consumed answer without
granting permission to replay commands.

Manual rename suggestions use `CWC|workspace-task title|8hex`, for example
`CWC|示例项目-示例任务|A1B2C3D4`. The suffix is actually calculated
from the pair; names/model labels are display-only. Pairing and refresh show the
suggestion locally, and actual consultations include it so Chat can give a copyable
name. No automatic rename or additional handshake is performed.

Schemas 1–3 upgrade to schema 4 transactionally on a normal Store operation. Existing
requests, receipts, replies and pair identities remain intact. Doctor and refresh
read all supported versions without migration. Old runtimes must not overwrite the new schema.

Each exchange has stable Q/A display labels and an optional consumed parent. Default
waiting is 60 seconds before the first read, 30 seconds between reads, and a persistent
10-minute observation window; only evidence of continued generation extends it, up to
30 minutes from the original anchor. Resume retains the elapsed budget and never sends
again. Complete replies, adopted advice, actual execution and goal completion are separate.
Detailed Reasoner instructions cover prerequisites, steps, expected outputs, failure
handling and verification. The old 12,000-character soft ceiling is removed; actual
Host limits still apply, and explicitly incomplete answers require linked follow-ups.
The plugin provides current-turn progress checks; later unattended execution requires
a separately authorized, functioning host continuation mechanism.

## Verification

From this directory:

```text
python -B -m unittest discover -s tests -v
```

Tests use temporary databases and synthetic Host results, including independent
processes competing for the same send/target. They are separate from real Host
validation; see the modification report for the dated live test evidence.

On Windows, also run `node tests/test_host_relay_integration.js`. It executes the
real relay, PowerShell, Python controller, SQLite and Host adapter in a disposable
package/state root; only the Host boundary is simulated. It checks long Chinese
payloads, quoting, delayed reads, interrupted recovery, duplicate claims and invalid
responses. Passing it does not establish live model or service reliability.

## Upgrade from the previous core

The prior source and tests were preserved in the workspace's
`output/cwc-change-20260911/before/`, with a SHA-256 manifest.
Old state, notes and evidence are left read-only. The new core does not import old
handshakes, Contract Sync transactions, or task state. Known old pending targets
are blocked until reconciled in their original version. Custom old state locations
and simultaneously running old tasks need explicit coordination.

Legacy Contract ACKs, correction loops, Project Registry, keyword Router, backup
commands, notes lifecycle, UI menus and experimental scheduler are retired from the
active code. Existing archival files remain available. Future upgrades of this core
retain pairs and frozen pending requests without model-dependent re-pairing.

After reinstalling, use a new Codex task so the updated Skill is loaded.
