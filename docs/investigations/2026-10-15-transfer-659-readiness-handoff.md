# Transfer 659: standby readiness-to-promotion handoff (diagnostic)

1.0.13, 2026-10-15.

**Confirmed gap (at `009e7949`):** readiness committed *outside* a coordinator pass did not wake the coordinator. Provider inventory reconciliation, the writer in 659, updates a known backup binding without stamping its observation. The yield then waited for the next scheduled pass, up to one `resource_poll_interval` (30 s; 14 s in 659's shape). Readiness the coordinator observes itself was, and is, acted on in the same pass.

**Correction (owner decision, §6):** inventory reconciliation that commits a live, bound, unpromoted backup's transition to AVAILABLE now wakes the coordinator through the one existing resolution wakeup.

`backend/tests/test_v113_readiness_handoff_timeline.py` reproduces both behaviours through the real scheduler loop.

## 1. What ran

- The trace `debridpulse-transfer-659-trace-20261015T215227Z.json` (sha256 `c28f07bc…7f151c3`, preserved unmodified) reports build `009e79494bd63170eb973ec84176e3f433c6b662`.
- At investigation time that commit was the branch HEAD and equal to `origin/1.0.13`. Its tree is `cba29fa29123f68dbb063c6db6f86ec383241f16`, the approved readiness/ownership candidate.
- Transfer 659 therefore ran the committed readiness-promotion implementation. No later correction exists.

## 2. Timeline (wall clock UTC; epoch seconds 1792101xxx)

| Time | Source | Event |
|---|---|---|
| 21:49:47.12 | trace `process.started_at` | process start, 83 s before acceptance |
| 21:51:10.85 | journal | transfer accepted (external, `selection_mode=all`, no preferred provider) |
| 21:51:11.39 – 11.98 | journal | AllDebrid route started, bound `preparing` (cache `miss`) |
| 21:51:13.6 / 14.6 / 16.47 | journal `resource.standby_bound` | Debrid-Link, Premiumize, Real-Debrid backups bound. `bind_standby` stamps `observed_at` = bind time, so the RD backup is next observable from ≈16.5 + 30 s |
| 21:51:48.43 | `standby_resources.observed_at` (RD), `manifest_entries=1` | the coordinator's backup observation of RD. Only `_observe_standbys` records `manifest_entries`. **Inferred:** state `preparing` with its one-file manifest, so the select-all selection was handed over (`_select_on_standby`). Its state is not recorded directly; see §3 |
| **21:52:04.13** | journal `resource.available` (RD binding, "previously preparing") | AVAILABLE first committed for the RD binding. **`observed_at` was not stamped.** |
| 21:52:12.17 | journal `routing.provider_changed` (`operator_switch`) | operator switch, 8.04 s after AVAILABLE |
| 21:52:13.25 | journal | backup promoted (takeover) |
| 21:52:18.6 / 18.87 | `observed_at` (DL, PM) | the next coordinator backup pass, 30 s after 21:51:48 |
| 21:52:19.35 | journal | aria2 execution started |

**Unobservable:** pass start/end times, the AllDebrid binding's state at 21:52:04, and the writer of the AVAILABLE row. The trace does not record pass timing or the writer.

## 3. Root-cause analysis (source at `009e7949`)

### How readiness is acted on

- **The coordinator pass:** `core/scheduler.py:71` `sync_status_loop` → `application.resolve_pending` → `transfers/engine.py:74` `resolve_pending` → `:95` `_prepare_standbys`. Inside it, in order:
  - `:649` `_observe_standbys` (`:1060`; each backup at most once per `resource_poll_interval`, gate `:1072`);
  - `_settle_released_standbys`;
  - `:651` `_readiness_promotions` (`:947`) → `_readiness_promotion` (`:971`).
- **After a yield:** a committed yield sets `resolution_deadline = now` (`:969`). The loop then waits only `max(0.05, deadline - now)` (`scheduler.py:86`); otherwise it waits `resource_poll_interval` (`:83`).
- **Same-pass consequence:** readiness the coordinator observes is evaluated in the same pass.

### Which writer committed AVAILABLE in 659

`resource.available` is journaled only by `_repository_base._resource`, and only on a real state change (`:3161`). The candidate writers for a backup's binding:

| Writer | Stamps `observed_at`? | Fits 659? |
|---|---|---|
| `_observe_standbys` → `observe_standby` (`repository.py:1065`) | yes | no: the stamp stayed 21:51:48.43 |
| `_readiness_promotion` target re-read → `observe_standby` | yes | no: also needs AVAILABLE already |
| `_promoted_standby` | (runs after the route is chosen) | no: 21:52:13 |
| `_repository_base` provider inventory reconcile, `if item.resource.id in known:` (`_engine_base.py:4573`) → `resource_observation` | no | yes, if an inventory pass ran at 21:52:04 |

On inventory specifically:
- Inventory covers backup bindings. The RD inventory identity is `uuid5(realdebrid:resource:<native id>)`, the same key as the backup.
- The configured `full_sync_interval_minutes` is not in the trace. The default 5-minute cadence with startup jitter would not land at 21:52:04. A shorter configured interval or a manual sync/recover would.

**Alternative:** the 21:51:48 observation itself returned AVAILABLE and its commit was held about 15.7 s.
- It is weakened: SQLite `busy_timeout` is 10 s, and the maintenance gate fails fast, so only event-loop starvation remains.
- Under it, the same pass's `_readiness_promotions` would have run immediately afterwards. Its target re-read would have re-stamped `observed_at`, which did not happen. It would also need the pass to have stalled until after the operator's switch.

**Confidence**
- From the trace: high that AVAILABLE was committed outside the coordinator's own backup observation, and that the operator acted before the next pass (21:52:18).
- Medium that the writer was inventory reconciliation; the writer is not recorded.

### Which of the four failure classes

1. **Detection delay:** not the cause of the gap. AVAILABLE was durably recorded at 21:52:04.
2. **Failure to evaluate after durable READY:** no defect. Readiness committed outside a pass is evaluated at the next scheduled pass (≤ `resource_poll_interval`). Nothing wakes the loop early for it, by design of the current wake contract.
3. **Evaluated and refused by a guard:** no evidence. No preferred provider, no pause (global and per-transfer 0), no committed selection or fan-out before the switch, and no route error.
4. **Committed but downstream delayed:** not applicable (operator switch).

**Conclusion:** the operator switched 8.04 s after AVAILABLE was committed and 6.3 s before the next scheduled coordinator pass, which would have yielded. The promotion logic is correct. The gap is the missing wake for readiness recorded outside a pass, corrected below.

## 4. Reproduction through the real scheduler loop

`test_v113_readiness_handoff_timeline.py` runs the production `sync_status_loop`, with only `_wait_for_work` replaced by a virtual-clock stand-in that honours the loop's own computed waits and its wakeup event.

**Fixture:** a select-all one-file torrent; a still-preparing primary; a Real-Debrid backup over its local fake client (cached: ready as soon as selected); `resource_poll_interval = 30`.

**Measured schedule (t = 0 at acceptance):**

| t (s) | Event |
|---|---|
| 0 | pass: primary bound PREPARING, RD backup created |
| 30 | pass: backup observed PREPARING with its manifest, selection handed over (`select_files("1")`) |
| 60 | pass: backup observed AVAILABLE, **yield committed in the same pass** (`readiness_promotion`, no `operator_switch`) |
| 60.05 | the deadline wake (0.05 s) runs the follow-on resolution on Real-Debrid |
| 90, 120, … | ordinary 30 s cadence; no loop wait ever exceeds 30 s |

**659 shape, before the correction:** provider inventory commits AVAILABLE at t = 46 (16 s after the hand-off). The next scheduled pass, at t = 60, yields: **14 s** latency. Further variants:
- inventory at t = 30.5 gives 29.5 s;
- inventory at t = 59.5 gives 0.5 s.

**After the correction:** the inventory commit sets the resolution wakeup and a pass runs at that same instant. This holds even while a later provider's inventory answer is still pending: in the test it is held open, the coordinator yields first, and the scan then finishes normally. Inventory at t = 46, 45 or 30.5 yields at that instant, so ready → yield is **0 s**. A wake set while a pass is still running, after the loop cleared it, is seen when the loop enters its wait, which returns at once.

**External submission:** (`source="external"` with its demand) produces the identical operations and offsets.

**Discrimination:** with `_readiness_promotions` moved before `_observe_standbys` (a stale-order defect), the same-pass test fails. The yield slips one full interval, from t = 60 to t = 90.

**Old scheduling (`009e7949`):**
- The between-pass cases (659 shape, halfway, just after a pass) and the wake-race case fail: the yield waits for the next scheduled pass.
- The paused, already-executing and stale-re-read cases fail only on the expected wake count (none).
- The same-pass, cancelled, deleted and native/external parity cases pass on both trees.

## 5. Latency budget under the current cadence and wakeup

| Interval | Bound | Source |
|---|---|---|
| Remote readiness → DP observes it (coordinator) | ≤ `resource_poll_interval` (30 s) after the backup's previous observation, plus pass duration | backup observation gate `engine.py:1072` |
| DP observes READY in its own pass → yield decision | **0 s (same pass)** | `_prepare_standbys` order `:649–:651` |
| READY committed between passes (inventory) → yield decision | before: remainder of the interval, ≤ 30 s (14 s in 659). **After: the next pass, at once (0 s virtual)** | resolution wakeup set by `ApplicationService.reconcile_inventory` |
| Yield → follow-on resolution | 0.05 s | deadline wake `engine.py:969`, `scheduler.py:86` |

**Provider calls and rate limits:**
- No new poll or cadence.
- A wake costs exactly the pass the scheduled poll would have run later, including that pass's existing readiness re-reads.
- It fires only on a committed transition to AVAILABLE, so an unchanged AVAILABLE neither wakes nor reads.

## 6. Correction (owner decision: implement the event-driven wake)

- **`TransferRepository.inventory_observation` (`transfers/_repository_base.py`).** It is the inventory form of `resource_observation`. In one `BEGIN IMMEDIATE` it writes the binding as before, then answers, after commit, whether this write moved a live (non-terminal) transfer's bound, unpromoted backup from a non-AVAILABLE state to AVAILABLE.
- **`TransferEngine.reconcile_inventory(on_readiness=...)` (`transfers/_engine_base.py`).** It uses it for known bindings. Immediately after each qualifying commit, it nudges a running cycle (`_resolution_opportunity`) and calls the caller's `on_readiness`. This happens per commit, never at the end of the scan, so a later provider whose inventory answer is slow or stalled cannot hold the wake back.
- **`ApplicationService.reconcile_inventory` (`application/service.py`).** It supplies `on_readiness=self.resolution_wakeup.set`, the one existing scheduler wake. That covers the full-sync loop, startup inventory, manual sync and Recover alike.

The wake is a hint only:
- `_prepare_standbys` keeps its `_observe_standbys` → `_readiness_promotions` order;
- `_readiness_promotions` already reads every AVAILABLE backup regardless of its observation stamp;
- `_readiness_promotion` re-reads both resources and decides pause, execution boundary, preference and route generation in its own commit;
- retired work does not even wake.

Nothing new was added: no scheduler, poller, readiness store, column or promotion path.

## 7. Guards

The correction adds a wake only, so every guard of the approved implementation is untouched:
- pause and Pause All in the replacement commit;
- execution boundary;
- preferred provider;
- the anti-oscillation readiness-promotion rule;
- terminal cleanup;
- inventory atomicity;
- the transient-takeover handling;
- the evidence ordering.
