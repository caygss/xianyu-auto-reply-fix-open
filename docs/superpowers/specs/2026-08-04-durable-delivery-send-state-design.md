# Durable Delivery Send State Design

## Goal

Prevent bound deliveries from being automatically resent or falsely finalized when an external sender has started but its outcome is not proven, while preserving retry behavior for confirmed sender failures and legacy `configured=False` deliveries.

## State model

`delivery_orchestration_states` gains three durable fields:

- `item_id`: the trusted item scope captured when the orchestration row is created.
- `send_started_at`: written atomically before invoking an external sender. A non-null value permanently excludes the row from stale claim reclamation until a terminal transition explicitly clears it.
- `verification_required`: set with the durable pre-send barrier and kept until successful completion or a confirmed sender failure. Such rows require human verification and cannot be automatically resent, marked sent, platform-confirmed, or finalized while the outcome remains unknown.

The existing `status='sending'` remains the active claim state. This avoids rebuilding the status CHECK constraint and keeps historical status consumers compatible.

## Public orchestration API

`DeliveryOrchestrationService.begin_send(request, claim_token)` is the only configured-send preflight API. It normalizes the request, verifies the exact persisted claim payload (`token`, `quantity`, `mode`, `idempotency_key`, `item_id`, and existing order/card/account/line scope), and atomically writes `send_started_at` plus `verification_required=1` before returning success.

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

The canonical table definition includes all three fields. Existing databases add missing columns inside the existing `init_db` transaction. Historical uncertain finalization metadata is parsed in Python and updates matching orchestration rows before the transaction commits. Migration failures roll back initialization.

## Tests

Tests use real SQLite state and only mock external sender or injected storage failures. Required coverage includes:

- canonical and legacy schema migration;
- begin-send False/exception with zero sender calls;
- lease expiry cannot reclaim any send-started row;
- unknown sender result cannot confirm, finalize, ship, or resend;
- completed sender plus heartbeat failure remains non-retriable and may recover through a normal sent anchor;
- exact payload mismatches for quantity, mode, idempotency key, and item scope;
- confirmed sender failure clears send-started state and reuses the same reservation;
- legacy behavior and token secrecy.
