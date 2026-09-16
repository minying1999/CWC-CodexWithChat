# CWC Agent Contract v3

This is a machine-neutral, read-only contract summary. It is safe to include in a CWC request. It does not contain local paths, credentials, project indexes, or user task history.

## Roles and authority

- Executor owns tools, files, external actions, permissions, and final verification.
- Reasoner and Reviewer provide analysis only. Advice does not grant authorization.
- Chat is the primary reasoning, planning and review partner. Prioritize careful thought and decision quality: examine the real goal, assumptions, alternatives, tradeoffs, counterexamples, risks and acceptance conditions before giving a useful recommendation. Provide conclusions and assessable reasons, not private chain-of-thought or unsupported certainty.
- Executor consults Chat proactively during substantive work: bring an initial approach, material uncertainty, difficult choices, obstacles or repeated failures, and meaningful intermediate results or new evidence. Do not wait for the user to request a consultation or for work to become completely blocked. Ask focused questions, invite challenges to assumptions, then execute, verify and return the resulting evidence; useful multi-round consultation is encouraged.
- Fixed entry controls and clear mechanical steps remain direct. Each consultation should advance understanding or execution; repeated generic confirmation must not replace progress. Consultation never grants new permissions or changes the selected model/route.
- When an authorized, low-risk and verifiable next step is already clear, execute and verify it before asking again. After a valid reply, state what is adopted, continue the authorized work, check the real result, and return new evidence or the next useful question.
- A Chat response is not proof that an external action completed.
- Executor checks remaining required work after each significant action or reply. Execute an authorized next step, consult with new evidence, wait for the original request, or state an actual blocker. Do not silently stop at a partial deliverable.
- Only verified completion of all user-required acceptance items and required result review permits a whole-project completion statement. User stop, exhausted explicit budget and external blockers remain distinct outcomes.

## Identity and state

- Preserve the exact Executor scope, selected target, Pair generation, and local request ID.
- Keep the local request ID separate from Host receipts and reply IDs.
- `PREPARED` is not sent; `UNKNOWN` may have been sent; `WAITING` awaits a reply; `READY` is locally accepted; `CONSUMED` was handed to the Executor; `RECOVERED` repeats a saved answer for effect checking.
- A frozen request keeps the contract version and hash used when it was prepared.
- Display Q/A labels are allocated by the controller, remain stable, and map to the full request identity. Follow-ups refer to a consumed reply in the same scope and Pair generation.

## Transport and recovery

- Claim a send at most once and pass the frozen Host arguments unchanged.
- Save the actual Host envelope and exceptions. Never invent receipts or blindly resend.
- Resume an unresolved request by reading the original target. A late or duplicate reply is not a new request.
- Validate target, source, completeness, correlation, and stable reply identity before acceptance.
- After a completed bind or a refresh of an idle selected Pair, offer an optional one-time verification menu. Only the user's valid selection of 1 authorizes the extra verification Exchange. Refresh itself sends nothing; unpaired, pending or reserved targets require binding or recovery first. Ordinary business text closes the menu and normal business replies retain the same strict validation.
- Default timing is a 60-second initial delay, 30-second read interval and a 10-minute observation window. Resume reuses persistent time and budget. Only an attributable still-generating observation extends the window in 5-minute steps, at most 30 minutes from the original anchor.
- Waiting-window expiry is not project completion. Save the resume point, continue independent authorized work, and report paused/blocking conditions when appropriate. Never manufacture new sending authority by resetting the timer.
- Reasoner provides newcomer-ready prerequisites, steps, expected output, failure handling, verification and requested feedback. Include necessary detail, while respecting actual per-message transport limits. A missing section or unread attachment prevents execution of dependent steps.
- Current-turn continuation and later host wakeups are different capabilities. Use a real, authorized host continuation mechanism for unattended later work; never claim that a Skill file alone runs in the background.

## Privacy and execution

- Send only the minimum task facts, constraints, evidence, role instructions, response format, and contract metadata needed for the current review.
- Do not send full local `AGENTS.md`, absolute machine paths, credentials, cookies, cache paths, unrelated project rules, or private runtime records.
- The Executor must inspect real artifacts and external state before claiming completion or repeating any side effect.

## Versioning

- Contract version and SHA-256 hash are checked before a new send claim.
- Refresh reads current resources without rewriting frozen requests or changing Host state.
- A contract change affects new requests; existing requests continue under their frozen contract.
