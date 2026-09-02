from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"anchor not found in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "frontend/static/ui-transfer-contract.css",
    """body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal-body) .badge-uploading,\nbody.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal-body) .badge-queued {\n  --dp-badge-color: var(--dp-state-active);\n}\n""",
    """body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal-body) .badge-uploading,\nbody.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal-body) .badge-queued,\nbody.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal-body) .badge-input_required {\n  --dp-badge-color: var(--dp-state-active);\n}\n""",
)

replace_once(
    "backend/tests/test_ui_transfer_contract.py",
    """        \".badge-uploading\",\n        \".badge-queued\",\n        \"--dp-badge-color: var(--dp-state-active)\",\n""",
    """        \".badge-uploading\",\n        \".badge-queued\",\n        \".badge-input_required\",\n        \"--dp-badge-color: var(--dp-state-active)\",\n""",
)

replace_once(
    "docs/architecture/UNIVERSAL_TRANSFER_CORE.md",
    """values preserve the supported UI/API states: accepted (`pending`), resolving\n(`processing`), ready, queued, transferring (`downloading`), paused, verifying,\npost-processing (`extracting`), completed, failed (`error`), cancelled and deleted.\nProvider resource states and executor states are separate observations.\n""",
    """values preserve the supported UI/API states: accepted (`pending`), resolving\n(`processing`), ready, queued, transferring (`downloading`), paused, input required\n(`input_required`), verifying, post-processing (`extracting`), completed, failed\n(`error`), cancelled and deleted. Provider resource states and executor states are\nseparate observations. `INPUT_REQUIRED` is nonterminal: it preserves the logical\ntransfer identity while a durable non-secret challenge waits for transient input.\nSee [INPUT_REQUIRED_LIFECYCLE.md](INPUT_REQUIRED_LIFECYCLE.md).\n""",
)

replace_once(
    "docs/architecture/UNIVERSAL_TRANSFER_CORE.md",
    """The SQLite schema contains `transfer_requests`, `provider_resources`,\n`resolution_attempts`, `execution_attempts`, `transfer_outcomes`, `transfer_controls`,\n`postprocess_attempts` and `application_events`. Existing parent/artifact table\n""",
    """The SQLite schema contains `transfer_requests`, `provider_resources`,\n`resolution_attempts`, `execution_attempts`, `transfer_outcomes`, `transfer_controls`,\n`postprocess_attempts`, `application_events` and the non-secret\n`transfer_input_challenges` metadata table. Submitted authentication values are\nprocess-local and are never part of that schema or integration runtime state.\nExisting parent/artifact table\n""",
)

replace_once(
    "docs/architecture/UNIVERSAL_TRANSFER_CORE.md",
    """Providers implement `Provider.resolve` plus only the capabilities they advertise:\n`ResourceLookup`, `Manifest`, `CandidateRefresh`, `Inventory`, `Cleanup` and `Health`.\nThe registry validates advertised protocol implementations at registration.\n""",
    """Providers implement `Provider.resolve` plus only the capabilities they advertise:\n`ResourceLookup`, `Manifest`, `CandidateRefresh`, `Inventory`, `Cleanup` and `Health`.\nA provider that can suspend for external input returns the neutral `InputRequirement`\nand implements `resolve_with_input`; submitted values are delivered only to that\nprovider for the current challenge. The registry validates advertised protocol\nimplementations at registration.\n""",
)

replace_once(
    "docs/architecture/UNIVERSAL_TRANSFER_CORE.md",
    """Executors implement `prepare`, `start`, `observe`, `cancel` and `resumable_paths`.\nOptional protocols include `PauseResume`, `BatchObservation`, `CandidateSampling`\nand `Health`. Selection uses supported endpoint schemes, enabled state, registered\n""",
    """Executors implement `prepare`, `start`, `observe`, `cancel` and `resumable_paths`.\nAn executor may return the same neutral `InputRequirement` from `prepare` before\nexternal mutation and continue through `prepare_with_input`; executor credentials\nremain independent of provider credentials. Optional protocols include `PauseResume`,\n`BatchObservation`, `CandidateSampling` and `Health`. Selection uses supported\nendpoint schemes, enabled state, registered\n""",
)

replace_once(
    "CHANGELOG.md",
    """- Corrected unsafe native URL/path adoption and hidden native retries. Existing payloads require verified possession; a terminal parent cannot release a path while execution remains uncertain.\n\n## [1.0.11] — 2026-08-31\n""",
    """- Corrected unsafe native URL/path adoption and hidden native retries. Existing payloads require verified possession; a terminal parent cannot release a path while execution remains uncertain.\n- Added neutral nonterminal `INPUT_REQUIRED` with initial `AUTH_REQUIRED`, durable non-secret challenge identity/generation, transient one-transfer credential delivery, and same-transfer provider/executor continuation.\n- Added `username_password` and `username_private_key` challenge descriptors; private-key passphrase is optional and is not a separate authentication method. Authentication waits preserve retry budgets and ordinary capacity, survive restart without persisting secrets, and remain subordinate to pause/cancel/delete/capacity controls.\n- Added bounded input-submission API handling, restart/migration/leakage/concurrency proofs, and minimal browser-safe `input_required` presentation. The generic authentication modal and saved-credential work remain deferred.\n\n## [1.0.11] — 2026-08-31\n""",
)
