# Durable Delivery Send State Design

## Goal

Prevent bound deliveries from being automatically resent or falsely finalized when an external sender has started but its outcome is not proven, while preserving retry behavior for confirmed sender failures and legacy `configured=False` deliveries.

## State model

`delivery_orchestration_states` gains four durable fields:

- `item_id`: the trusted item scope captured when the orchestration row is created.
- `send_started_at`: written atomically before invoking an external sender. A non-null value permanently excludes the row from stale claim reclamation until a terminal transition explicitly clears it.
- `verification_required`: set with the durable pre-send barrier and kept until successful completion or a confirmed sender failure. Such rows require human verification and cannot be automatically resent, marked sent, platform-confirmed, or finalized while the outcome remains unknown.
- `item_scope_migration_version`: records completion of historical item-scope resolution. Canonical rows start at the current version; upgraded rows start at zero and are processed once.

The existing `status='sending'` remains the active claim state. This avoids rebuilding the status CHECK constraint and keeps historical status consumers compatible.

## Public orchestration API

`DeliveryOrchestrationService.begin_send(request, claim_token)` is the only configured-send preflight API. It normalizes the request, verifies the exact persisted claim payload (`token`, `quantity`, `mode`, `idempotency_key`, `item_id`, and existing order/card/account/line scope), and atomically writes `send_started_at` plus `verification_required=1` before returning success.

The synchronous compatibility APIs `orchestrate()` and `retry()` own their sender invocation, so they call `begin_send()` exactly once after prepare and before invoking the sender. An ordinary sender `Exception` is a proven failure and goes through `mark_failed`; a `BaseException` is allowed to propagate while the durable barrier remains. The externally segmented async runtime owns its own `begin_send()` call and therefore continues to use `prepare_retry()` rather than either service-owned sender API.

`mark_failed` clears `send_started_at` and `verification_required` only for a confirmed ordinary sender failure with the current token. `mark_sent` clears those fields as part of the terminal sent transition.

## Runtime arbitration

Configured external send flow:

1. Rebuild and structurally validate the request.
2. Call `begin_send` via `asyncio.to_thread` before creating the sender task. False or exception means sender count remains zero.
3. Run sender and heartbeat concurrently.
4. If sender completes successfully, it is safe to persist a normal `sent` finalization anchor. A concurrent heartbeat failure cannot make the row reclaimable because `send_started_at` is already durable.
5. If heartbeat fails while sender is unfinished, leave the durable `verification_required` barrier in place, cancel/collect the sender, and do not create a `sent` finalization anchor.
6. Caller cancellation after sender start follows the same unknown-result path and still propagates `CancelledError`.
7. A confirmed ordinary sender exception calls `mark_failed`, preserving the reservation and allowing the established retry path.

## Recovery and entry-point behavior

All automatic, simplified, compensation, and manual recovery paths treat verification-required work as fail-closed. Historical finalization metadata containing `claim_verification_required=true` is never returned as pending-finalize work. Database initialization migrates those historical records onto the orchestration row when a matching row exists.

User-facing dispositions and reasons use token-free Chinese text such as “发货结果待人工核实，已阻止自动重发和确认发货”. No manual resolution endpoint is added in this task.

## Migration

The canonical table definition includes all four fields. Existing databases add missing columns inside the existing `init_db` transaction. Historical uncertain finalization metadata is parsed in Python and updates matching orchestration rows before the transaction commits. Migration failures roll back initialization.

For every historical row whose migration marker is missing or older than the current version, migration uses only independent persisted scope evidence. A pre-marker `item_id` is untrusted input and never proves itself: it cannot advance the marker or join the evidence candidate set. The existing value is confirmed only when unique independent evidence resolves the same item; disagreement, missing evidence, or ambiguous evidence quarantines the row. A pre-existing `verification_required=1` remains fail-closed and may advance its marker without being automatically cleared.

The migration loads states, finalization anchors, orders, and bindings once and builds indexes by account/order, idempotency key, complete card/line scope, and binding scope. A finalization anchor belongs to a state only when the anchor's own account/order matches and either its idempotency key uniquely identifies that state or its metadata contains both `card_id` and `order_line_id` and uniquely maps to one state. Metadata cannot override the anchor's account/order ownership. Missing line scope, partial scope, cross-owner keys, multiple candidates, and conflicting item evidence are never accepted.

Exact evidence is locked before ambiguous anchors are considered. A normally resolved sibling cannot later be quarantined by another partial anchor. Ambiguous anchors may quarantine only unresolved candidate states in the anchor's own account/order; when candidate ownership is not reliable, only each already-pending orchestration row fails closed. Finalization metadata is changed only for one uniquely owned anchor, by merging token-free verification fields while preserving all original fields. Ambiguous anchor metadata is left byte-for-byte unchanged.

An order item matched by `(order_id, account_id)` and one uniquely owned finalization anchor are order-level evidence; disagreement is a conflict. Only when no order-level evidence exists may a unique `(user_id, account_id, card_id)` binding supply the item. The schema has no persisted orchestration request payload, and `order_line_id` is not treated as an item identifier. Unique evidence backfills the item for `failed`, `paused`, and `sent`; historical `sending` is still quarantined because the old row cannot prove whether its sender completed. Missing or conflicting evidence retains the reservation and writes a token-free Chinese verification reason.

Every processed row advances `item_scope_migration_version`, including isolated rows whose `item_id` remains null. A partial index selects only old-version rows. If no such row exists on a later startup, migration does not load finalization anchors, orders, or bindings. The marker column, partial index, data changes, and schema upgrades remain inside the existing atomic savepoint.

## Tests

Tests use real SQLite state and only mock external sender or injected storage failures. Required coverage includes:

- canonical and legacy schema migration;
- exact finalization ownership, cross-account rejection, complete-scope uniqueness, and ambiguous metadata preservation;
- sibling isolation across two database startups;
- 10,000-row migration with marker-based second-startup short circuit and bounded query count;
- trusted historical item backfill for failed/paused/sent and safe quarantine for sending;
- unresolved/conflicting historical item scope, reservation retention, and finalization blocking;
- service-owned `orchestrate()`/`retry()` begin-send coverage including ordinary failure and `BaseException` interruption;
- begin-send False/exception with zero sender calls;
- lease expiry cannot reclaim any send-started row;
- unknown sender result cannot confirm, finalize, ship, or resend;
- completed sender plus heartbeat failure remains non-retriable and may recover through a normal sent anchor;
- exact payload mismatches for quantity, mode, idempotency key, and item scope;
- confirmed sender failure clears send-started state and reuses the same reservation;
- legacy behavior and token secrecy.
