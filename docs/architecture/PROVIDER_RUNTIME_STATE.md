# Provider Runtime-State Persistence

Roadmap Item 2 adds one neutral durable facility for integration/provider operational runtime state. It is intentionally separate from user configuration and from the Universal Transfer Core.

## Ownership boundary

Configuration expresses operator intent: enablement, credentials or credential references, priority, and user-selected options. It remains in the existing settings/configuration model.

Runtime state is operational data learned or maintained by an integration while running. The canonical owner is `backend/integrations/runtime_state.py`, backed by the application SQLite database through `db.database.get_db()`.

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

Malformed provider payloads are therefore not interpreted or repaired by the neutral layer. Malformed neutral metadata is reported as a bounded runtime-state corruption error. Provider payload corruption cannot mutate transfer lifecycle records because runtime state occupies a separate table and repository.

## Durable schema and isolation

The schema is generic:

`integration_runtime_state(integration_id, state_key, schema_version, payload, observed_at, stale_after, successful_at, created_at, updated_at, generation)`

The composite primary key `(integration_id, state_key)` provides deterministic restart identity, provider isolation, and independent namespaces without provider-specific columns. The schema contains no concrete provider names.

Schema creation is transactional and idempotent. The runtime-state store participates in the application lifecycle and initializes after the existing v1.0.12 transfer migration and canonical transfer repository initialization. Existing SQLite application data is left intact.

The application's JSON database-maintenance path has an explicit canonical-table allowlist rather than exporting every SQLite table automatically. `integration_runtime_state` is therefore explicitly included in that backup list, including opaque BLOB payloads through the existing base64-safe JSON encoder. Routine event cleanup does not touch runtime state. An explicit administrator database wipe deliberately removes runtime-state rows along with the other application operational data; provider disablement does not.

## Last-known-good and atomic replacement

Provider-specific validation occurs before `ProviderRuntimeStateStore.replace()` is called. A failed fetch, malformed candidate, or incompatible candidate therefore never reaches the durable replacement operation and cannot overwrite the existing record.

A successful replacement occurs in one `BEGIN IMMEDIATE` SQLite transaction and updates payload plus all metadata together. The optional `expected_generation` compare-and-swap guard prevents an older concurrent refresh from overwriting a newer successful result. A generation conflict is surfaced to the integration rather than guessed around by universal code.

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

The permanent proof fixture uses arbitrary telemetry/calibration observations unrelated to debrid hosts or supported domains. It demonstrates opaque serialization, provider-owned validation, schema incompatibility handling, restart recovery, isolation, last-known-good retention, compare-and-swap concurrency, disable/re-enable retention, and backup/wipe maintenance behavior against the real SQLite implementation.

## Deferred production consumer

AllDebrid dynamic host-support data is the planned first production consumer in a later roadmap stage. Roadmap Item 2 does **not** fetch, parse, refresh, persist, consume, classify, or route from AllDebrid host-support data. HTTP(S), classifier/applicability, authentication-flow/UI, provenance, and later routing work are also outside this stage.
