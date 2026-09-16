# CWC Windows Codex Preview — refresh verification and Contract v3, 2026-09-14

This release is a cleaned, controlled-share preview for Windows Codex desktop users.

## Included

- Portable root `plugin.json` plus `.codex-plugin/plugin.json` compatibility metadata.
- Repo marketplace at `.agents/plugins/marketplace.json`.
- CWC skills, controller, relay, Host adapter, and disposable regression tests.
- No runtime state, pairing records, conversation evidence, credentials, caches, or user task history.

## Verified

- Plugin and Skill validators pass.
- 94 Python tests pass, including refresh verification eligibility, obsolete/versioned menu rejection, failed-refresh menu closure and unchanged Pair/Exchange data.
- 14 relay checks pass with zero real Host calls.
- 17 integration checks pass with disposable state and zero real Host calls, including refresh followed by one optional verification and recovery without a second send.
- The contract resource is versioned and hashed; Pair metadata and new Exchanges freeze the contract hash, and claim rejects drift.

## Continuity behavior

- Both completed bind and successful refresh of an idle selected Pair offer optional input **1** for a communication test. Unpaired, pending and reserved targets instead receive binding/recovery guidance. The core resource refresh is read-only; the entry may replace only local menu state, and never sends a test automatically.
- Menu tokens carry the plugin version. Upgrades expire unused menus; already-created requests remain recoverable through resume. Ordinary business closes the verification menu.
- Contract v3 emphasizes thoughtful Chat reasoning, planning and review, and proactive Executor inquiries at meaningful decision points and intermediate results. Clear authorized mechanical steps still proceed directly; consultations lead back to execution and verification.

- A fixed entry dispatcher accepts both `cwc` and `/cwc`. Candidate choices appear before business-work instructions; the selected identity is revalidated against a fresh official listing.
- Binding immediately offers optional input **1** for one verification Exchange. Menu tokens are scoped and expire; business text closes the menu. Repeated verification input recovers the same request, including after an uncertain send.
- Refresh validates all resources but returns the actual thin operator instructions and resource metadata. BUSINESS then loads the current installed workflow text; help/status/bind do not load that workflow.
- Installed CWC is resolved from the unique enabled official listing entry, including its marketplace identity; neither a personal-only cache nor sorted old versions are assumed.
- Ordinary waiting/paused states stay quiet. Completion, actual human blockers and 30 minutes without material progress are notification conditions. Native heartbeat configuration and an observed trigger are reported separately.
- Final-build fixed-entry timings were about 0.95 seconds for help, 1.20 seconds for refresh and 4.14 seconds for 21 candidates, with 2.23 seconds inside the Host listing call. These exclude the model's initial response and launcher discovery time; they are not a universal end-to-end latency guarantee.

- Controller-owned persistent waiting: first read after 60 seconds, then 30 seconds between attempts; 10 minutes initially, with evidence-based extensions up to the original 30-minute ceiling.
- A future explicit resume can make one late read after expiry. It neither resends nor resets the original deadlines, and repeated late reads share a minimum 30-second interval.
- Stable Q/A display labels and validated parent links connect new questions to consumed replies.
- The existing project checklist drives continued work. Required evidence, a current complete result review, and the Executor's separate `review_passed:true` assessment are all required for completion.
- Newcomer-ready reasoning instructions include prerequisites, concrete steps, failure handling and verification. The former 12,000-character editorial ceiling is removed. Replies reaching the observed 20,000 UTF-16-unit boundary are conservatively rejected; `CWC CONTINUE` requests explicit linked continuation before dependent execution.
- Schema 4 preserves old Pair and Exchange data. Current-turn work and a separately authorized Host wakeup remain distinct capabilities.
- Real Host validation of the final installed build observed one send, first-read reservation after 61.058 seconds, three total reads with subsequent intervals above 30 seconds, a matching completed reply, and the correct parent A label. Result review passed after the two identified defects were fixed. This proves the observed normal path; fault/late-probe cases use isolated simulations.

## Boundary

The supported target is Windows Codex desktop with the built-in thread tools, PowerShell, Python 3, and Node.js. This preview does not claim support for ordinary ChatGPT, macOS, Linux, Codex CLI, or Codex cloud.

The preview carries no open-source license. Public redistribution and a universal Plugins Directory submission require a separately chosen license, cross-platform work, and new platform verification.

The review pool is optional, not a default protocol. Chat review concluded that the current GitHub and contract work does not require a permanent multi-Chat pool; use two independent roles only for a clearly high-risk, multi-dimensional review.
