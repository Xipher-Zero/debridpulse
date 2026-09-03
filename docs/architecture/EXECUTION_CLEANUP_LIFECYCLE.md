# External execution cleanup lifecycle

DebridPulse treats logical transfer terminality and already-launched external execution cleanup as separate durable facts.
A transfer can become `CANCELLED` or `DELETED` immediately while an executor job remains uncertain. Logical terminality never depends on a successful remote cancellation RPC, and later executor observations cannot revive the logical transfer.

## Durable reconciliation authority

Once external executor work has launched under a durable execution handle, DebridPulse retains cleanup/reconciliation responsibility until that execution is authoritatively terminal or absent, or the executor contract returns an authoritative termination acknowledgement. Persistence alone is not sufficient: an unresolved cleanup obligation must remain automatically discoverable after process restart.

The execution attempt's existing `authorized` flag continues to mean that the core owns observation and mutation authority for that launched external execution. Authorization remains true while external state is uncertain. Cleanup completion clears authorization only after authoritative terminality, absence, or a termination acknowledgement.

## Destructive cancellation pressure

Destructive executor `cancel` calls use the normal finite retry policy. Their retry budget may be exhausted, and permanent adapter failures may stop further destructive calls. Exhausting destructive retry pressure does **not** end reconciliation ownership.

After destructive cancellation is no longer eligible, the same durable cleanup obligation remains `pending` and switches to throttled observation-only reconciliation. The retry timestamp provides bounded polling cadence. A legacy `blocked` cleanup row is a degraded unresolved obligation, not completion; it remains discoverable and is reclaimed into the live reconciliation path.

## Scheduling, leases, and restart

Cleanup reconciliation runs in the universal execution reconciliation cadence. A durable claim moves the obligation behind a lease before executor I/O, so concurrent workers cannot issue duplicate uncontrolled cleanup pressure. Failed observation or missing executor availability schedules a future reconciliation opportunity instead of hot-looping or abandoning ownership.

No second cleanup scheduler or provider-specific retry architecture exists. Restart reads the same persisted execution handle, cleanup state, retry timestamp, diagnostic, and destructive-attempt count and continues from that state.

## External terminal outcomes

`ABSENT`, `CANCELLED`, `FAILED`, and `SUCCEEDED` are authoritative external terminal observations for cleanup settlement. A successful/cancelled/skipped executor cancellation acknowledgement is also treated as authoritative termination according to the executor contract.

A late external `SUCCEEDED` observation after logical cancellation or deletion settles external uncertainty only. It does not publish delivery, materialize logical success, or resurrect the terminal parent transfer.

## Delete and filesystem safety

Delete persists the tombstone and any outstanding execution cleanup responsibility in the same repository transaction before executor I/O. Deletion therefore cannot erase responsibility for a launched external writer.

While an execution remains authorized and externally nonterminal/unknown, its target path remains conservatively reserved even if the logical transfer is deleted. Once cleanup reaches authoritative terminality or absence, authorization is cleared and that reservation can converge and be released for future work.

These rules are provider- and executor-neutral and are part of the universal transfer-core lifecycle contract.
