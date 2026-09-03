# Provider Runtime-State Persistence

Roadmap Item 2 adds one neutral durable facility for integration/provider operational runtime state. It is intentionally separate from user configuration and from the Universal Transfer Core.

## Ownership boundary

Configuration expresses operator intent: enablement, credentials or credential references, priority, and user-selected options. It remains in the existing settings/configuration model.

Runtime state is operational data learned or maintained by an integration while running. The canonical data-access owner is `backend/integrations/runtime_state.py`, backed by the application SQLite database through `db.database.get_db()`. The generic table/index DDL belongs to the canonical SQLite schema in `backend/db/database.py`, so a fresh database or compatible upgrade receives the schema during ordinary database initialization before provider activity. The store defensively runs the same idempotent DDL when used independently in tests or tooling; there is one shared schema definition, not two competing schema owners.

The neutral store knows only:

- canonical integration identity (`IntegrationDescriptor.id` / `IntegrationDefinition.id`);
- an integration-private state key/namespace;
- opaque bytes;
- a provider-declared schema/version string;
- neutral UTC epoch timestamps (`observed_at`, `successful_at`, `created_at`, `updated_at`);
- an optional provider-declared absolute `stale_after` timestamp;
- a monotonically increasing generation used for atomic compare-and-swap replacement.

The neutral store does **not** know provider payload fields, native service/host concepts, provider status codes, provider-specific migration rules, refresh cadence, TTL policy, routing semantics, or payload compatibility rules.

## Opaque payload and schema compatibility

Payloads are stored as SQLite `BLOB` values. An integration serializes, deserializes, validates, and interprets those bytes. The persisted `schema_version` is returned verbatim so the integration can decide whether retained state is current, migratable, or incompatible.

Malformed provider payloads are therefore not interpreted or repaired by the neutral layer. Malformed neutral metadata is reported as a bounded runtime-state corruption error. Non-finite neutral timestamps are rejected before a replacement transaction begins. Provider payload corruption cannot mutate transfer lifecycle records because runtime state occupies a separate table and repository.

## Durable schema and isolation

The schema is generic:

`integration_runtime_state(integration_id, state_key, schema_version, payload, observed_at, stale_after, successful_at, created_at, updated_at, generation)`

The composite primary key `(integration_id, state_key)` provides deterministic restart identity, provider isolation, and independent namespaces without provider-specific columns. The schema contains no concrete provider names.

Schema creation is transactional and idempotent. Canonical SQLite initialization creates and verifies the runtime-state table and index for fresh and compatible existing databases; store initialization uses that same DDL defensively and does not invent provider-payload migration markers. Existing SQLite application data is left intact.

The application's JSON database-maintenance path has an explicit canonical-table allowlist rather than exporting every SQLite table automatically. `integration_runtime_state` is therefore explicitly included in that backup list, including opaque BLOB payloads through the existing base64-safe JSON encoder. Routine event cleanup does not touch runtime state. An explicit administrator database wipe deliberately removes runtime-state rows along with the other application operational data; provider disablement does not.

## Last-known-good and atomic replacement

Provider-specific validation occurs before `ProviderRuntimeStateStore.replace()` is called. A failed fetch, malformed candidate, or incompatible candidate therefore never reaches the durable replacement operation and cannot overwrite the existing record.

A successful replacement occurs in one `BEGIN IMMEDIATE` SQLite transaction and updates payload plus all metadata together. The optional `expected_generation` compare-and-swap guard prevents an older concurrent refresh from overwriting a newer successful result. A generation conflict is surfaced to the integration rather than guessed around by universal code. A forced SQLite failure during replacement rolls back the candidate and leaves the previous known-good payload and metadata unchanged.

## Freshness

`stale_after` is optional absolute UTC epoch metadata declared by the integration. The store can mechanically report whether that timestamp has passed, but the integration owns what staleness means and what action to take. No production-provider TTL or refresh policy exists in the neutral layer.

## Disable / restart / re-enable

Enablement remains a configuration/routing fact. Existing registry routing ignores disabled integration descriptors. Runtime-state deletion is independent:

1. disabling an integration removes it from eligible routing through existing registry semantics;
2. its runtime-state rows remain untouched;
3. restarting while disabled preserves those rows in SQLite;
4. re-enabling allows the same stable integration identity to load them again;
5. the integration decides whether the retained data is still usable.

Explicit `delete()` and `purge_integration()` operations exist for deliberate state removal. They are never invoked as a consequence of disablement.

## Extension example

A future integration should:

1. use its existing canonical integration ID;
2. choose a private state key if it needs more than one record;
3. serialize and validate provider-native state itself;
4. call `replace()` only after successful validation;
5. persist its own schema marker and neutral timing metadata;
6. on restart, `load()` the record and perform provider-owned compatibility/validation;
7. decide whether stale or incompatible state may be migrated, refreshed, ignored, or explicitly purged.

The permanent proof fixtures use arbitrary telemetry/calibration and counter observations unrelated to debrid hosts or supported domains. They demonstrate opaque serialization, provider-owned validation, cross-provider schema incompatibility, restart recovery, identity/key isolation, last-known-good retention, compare-and-swap concurrency, disable/re-enable retention, canonical fresh-database initialization, transaction rollback, and backup/wipe maintenance behavior against the real SQLite implementation.

## Current production consumer

The neutral store remains provider-agnostic, but the current v1.0.12 development tree now has a production consumer: AllDebrid's dynamic supported-host state. `backend/providers/alldebrid/host_runtime.py` fetches and validates AllDebrid-native host data, serializes provider-owned bytes into this store, restores last-known-good state after restart, owns freshness/refresh policy, and translates usable provider state into neutral applicability facts. The store never parses AllDebrid host records or makes routing decisions.

General HTTP & HTTPS does not need provider runtime-state persistence for its current generic `http`/`https` applicability. Future providers introduced after the two-provider checkpoint may reuse this boundary without adding provider-native payload knowledge to the core or store.
