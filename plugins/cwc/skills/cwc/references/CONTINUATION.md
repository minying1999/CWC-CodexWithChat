# Continue an authorized CWC task

This is for multi-step business work, never a prerequisite for entry/help/bind/refresh. Chat provides substantive planning and review; the Executor owns tool calls, authorization, real evidence and completion. Preserve the original scope, target and frozen request. Continue independent authorized work while a consultation is pending.

For a blocked required item, set `human_action_required:true` only when a concrete user action is indispensable, such as their login or a new permission decision. `blocked_by` alone can describe a temporary external condition and does not trigger a notification. Temporary external blockers and PAUSED remain quiet until the material-progress threshold; preserve them accurately instead of inventing a human action.

Use the existing project checklist with `last_progress_at` (UTC ISO timestamp) and `last_progress_evidence` (a nonempty reference). Update them only after a newly verified artifact, a newly passed required item, a resolved blocker, a Chat conclusion materially changing the route, or meaningful external-state change. Repeated reads, unchanged test results, output, sending a question and heartbeat ticks never count as progress. The controller's check-progress returns `NO_PROGRESS` after 30 minutes without such progress; preserve state and ask for the needed decision, rather than declaring failure or creating more work.

When unattended execution is authorized by this task and allowed by its project rules, use the actual host `automation_update` thread heartbeat. Reuse a matching automation instead of creating duplicates. Bind it to this exact task and the existing checklist's real path. Do not create standalone cron work, a new task, a service or another project database. The heartbeat prompt should tell the Executor to:

1. Read this task's current user instructions and the existing checklist; a newer user stop or scope correction takes precedence.
2. Use the installed CWC entry loader and current work instructions. Check actual effects before resuming; read the original pending request and never resend it or revive a discarded result. Do not manufacture a fresh observation budget.
3. Run check-progress; execute the next authorized action, ask the bound Chat a material question with new evidence, or wait for the existing request. Apply the existing rules for real human blockers and explicit execution budgets.
4. Keep unchanged/non-actionable observations quiet. Notify only completion, required human action, or the 30-minute material-progress threshold. A transient error that can be recovered without the user is not itself a notification reason.
5. At COMPLETE, explicit user stop, necessary human blocker, or NO_PROGRESS, disable this actual heartbeat through automation_update and retain the first resume action. Do not archive the user's task.

Record the real returned automation ID in the same checklist as `continuation.automation_id`; record the actual setup status, trigger evidence if observed, and stop status separately. An ID absent from an actual successful host result never permits claiming unattended continuation is configured. Tool availability alone does not prove a trigger. Keep full generated automation state out of Chat consultations.

The scheduler owns intervals and wakeups; use only its supported parameters. If the host has no functioning continuation capability, continue while active, preserve a checkpoint and report that specific limitation. A Skill cannot wake a stopped/offline host. Do not claim that a saved prompt is a running background process.
