"""Transfer Trace Log: the one read-only, sanitized export of one transfer's
durable state plus the relational context needed to explain it, and bounded
observations of what is actually true outside the database right now.

This module is the single trace-export owner. It observes; it never becomes a
lifecycle, persistence, recovery, routing, canonicalization or execution
owner. Every durable read happens inside one read transaction on a connection
that SQLite itself holds ``query_only``, so a trace is a consistent snapshot
and cannot write, migrate or reconcile anything.

Four evidence domains, each collected independently and never normalized to
agree with another: durable state (the snapshot), material on disk
(``transfers.filesystem.observe_material`` / ``transfers.storage.observe_capacity``),
the referenced executors' own view of their executions
(``TransferEngine.observe_existing``, the engine's one read-only batch
observation, through the canonical ``authorize_execution`` fence), and the
current runtime/integration context (registry descriptors, the neutral
``health()`` contracts, ``integrations.configuration.public_integrations``,
the engine's effective policy). A contradiction between domains is evidence
and is exported as found. Every domain reports its own collection status, so
missing evidence is never silent and never reads as absence.

Scope is comprehensive, not curated: complete rows (``SELECT *``) of every
transfer-scoped durable table, for the requested transfer (scope
``primary``) and for the smallest relational closure that makes its
canonical/consolidation/failover/provenance/execution/recovery relationships
intelligible (scope ``context``). Rows reached only through that closure are
never followed further; each reference that leaves the exported set is listed
as ``outside_closure`` (the row exists and was not exported) or ``absent``
(the row does not exist), so "omitted" never reads as "missing".

Sanitization is field-aware and applied here, server-side, after the
relationship set is decided: rows are never dropped; only values that may
carry a credential or capability are replaced, with per-export opaque tokens
that keep repeated references correlatable within this one trace.

Format versions. 1: durable state only (``metadata``, ``inventory``,
``references``, ``data``). 2 is a strict superset: every version-1 key keeps
its meaning; it adds ``collection_status`` (one entry per evidence domain:
``complete``/``partial``/``unavailable``/``unsupported``/``not_applicable``,
with a reason and counts), ``observations.filesystem``,
``observations.executors``, ``runtime_context``, ``metadata.process`` and
``metadata.observation_boundary``; ``metadata.build_revision`` is populated
whenever the running build carries a revision; and path roots are replaced by
per-export ``<redacted-path-root-N>`` tokens (sanitization version 2). 3 is a
strict superset of 2 except that rows of the requested transfer's direct
consolidation participants are now exported under scope ``component`` (with
all of their transfer-scoped rows) instead of ``context``; it adds
``metadata.closure.component`` (the bounded component, its limits and any
truncation) and ``metadata.component_transfer_ids``. 4 is a strict superset
of 3: it exports ``artifact_material_state`` rows (DebridPulse-owned material
truth per artifact) with every transfer-scoped artifact set, and each
``observations.filesystem.targets[]`` entry gains ``material`` -- the durable
VALID frontier reconciled against what the payload on disk shows now (length,
physical identity, holes). Continuation plans ride on the exported
``execution_attempts.continuation`` column and material decisions (rollback,
invalidation, forced checkpoints, writer retirement and how it stopped) on
``application_events`` of kind ``material_audit``. 5 is a strict superset of
4: ``route_attempt_provenance.routing_decision`` carries the canonical
selector's decision that started each root route attempt -- one neutral
disposition per provider it considered -- and
``transfer_requests.routing_decision`` the decision of a root request that is
held or that nothing could take. Rows recorded before either column existed
have none; nothing is reconstructed for them.
"""
from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import json
import posixpath
import re
from urllib.parse import urlsplit

from core.config import get_settings
from core.version import process_timing, read_build_revision, read_version
from db.database import get_db
from transfers import codec
from transfers import material as mat
from transfers.contracts import Health
from transfers.filesystem import observe_material, payload_facts
from transfers.models import ExecutionState, MaterializationKind
from transfers.storage import StorageDomain, observe_capacity

TRACE_FORMAT = "debridpulse.transfer-trace"
# 2: adds collection_status, observations.filesystem, observations.executors,
# runtime_context, and metadata.process; build_revision is populated.
# 3: adds the bounded consolidation component (scope 'component',
# metadata.closure.component, metadata.component_transfer_ids).
# 4: artifact_material_state rows and per-target material observations.
# 5: routing decisions on route attempts and held/unroutable root requests.
TRACE_FORMAT_VERSION = 5
# Bounds of the consolidation component (``_component``); hitting one is
# declared in metadata.closure.component, never silent.
COMPONENT_MAX_TRANSFERS = 32
COMPONENT_MAX_ARTIFACTS = 64
# 2: adds per-export path-root tokens.
SANITIZATION_VERSION = 2
# Upper bound for any one external observation (an executor batch, a health
# probe). An observation that does not answer in time is reported
# ``unavailable``; it never delays or fails the trace beyond this.
OBSERVATION_TIMEOUT_SECONDS = 5.0

# Every transfer-scoped durable table, in export order. A table named here but
# absent from this database is reported ``unsupported``; a database table not
# named here or in ``_OMITTED`` would be reported ``omitted`` with an unknown
# reason rather than silently ignored.
_TABLES = (
    "torrents", "transfer_requests", "provider_resources", "standby_resources", "resolution_attempts",
    "route_attempt_provenance",
    "transfer_file_manifests", "transfer_file_manifest_entries", "transfer_file_selections",
    "transfer_file_selection_entries", "download_files", "canonical_candidate_bindings",
    "canonical_candidate_origins", "artifact_consolidations", "execution_attempts", "execution_attempt_provenance",
    "artifact_recovery_state", "artifact_material_state", "transfer_outcomes", "postprocess_attempts", "transfer_pause_intents",
    "transfer_input_challenges", "deferred_provider_submissions", "events", "application_events",
    "transfer_controls",
)
# Durable tables deliberately outside a transfer trace, with the reason.
_OMITTED = {
    "integration_runtime_state": "integration-global runtime state, not transfer-scoped",
    "stats_snapshots": "application-wide statistics history, not transfer-scoped",
    "schema_migrations": "reported as schema identity in metadata",
    "sqlite_sequence": "SQLite internal allocation state",
}
# (table, column, target table, target column): the durable references the
# closure audit checks against the exported row set.
_REFERENCES = (
    ("transfer_requests", "transfer_id", "torrents", "id"),
    ("transfer_requests", "parent_id", "transfer_requests", "id"),
    ("transfer_requests", "equivalence_target_artifact_id", "download_files", "id"),
    ("resolution_attempts", "request_id", "transfer_requests", "id"),
    ("route_attempt_provenance", "request_id", "transfer_requests", "id"),
    ("standby_resources", "request_id", "transfer_requests", "id"),
    ("standby_resources", "binding_id", "provider_resources", "id"),
    ("route_attempt_provenance", "previous_attempt_id", "resolution_attempts", "id"),
    ("download_files", "torrent_id", "torrents", "id"),
    ("download_files", "request_id", "transfer_requests", "id"),
    ("download_files", "mirror_group_id", "download_files", "id"),
    ("download_files", "execution_attempt_id", "execution_attempts", "id"),
    ("canonical_candidate_bindings", "canonical_artifact_id", "download_files", "id"),
    ("canonical_candidate_origins", "binding_id", "canonical_candidate_bindings", "id"),
    ("canonical_candidate_origins", "contributing_artifact_id", "download_files", "id"),
    ("canonical_candidate_origins", "contributing_transfer_id", "torrents", "id"),
    ("canonical_candidate_origins", "request_id", "transfer_requests", "id"),
    ("canonical_candidate_origins", "resolution_attempt_id", "resolution_attempts", "id"),
    ("artifact_consolidations", "contributing_artifact_id", "download_files", "id"),
    ("artifact_consolidations", "canonical_artifact_id", "download_files", "id"),
    ("artifact_consolidations", "source_transfer_id", "torrents", "id"),
    ("artifact_consolidations", "source_request_id", "transfer_requests", "id"),
    ("execution_attempts", "artifact_id", "download_files", "id"),
    ("execution_attempt_provenance", "execution_attempt_id", "execution_attempts", "id"),
    ("execution_attempt_provenance", "artifact_id", "download_files", "id"),
    ("execution_attempt_provenance", "route_attempt_id", "resolution_attempts", "id"),
    ("artifact_recovery_state", "artifact_id", "download_files", "id"),
    ("artifact_material_state", "artifact_id", "download_files", "id"),
    ("artifact_material_state", "checkpoint_attempt_id", "execution_attempts", "id"),
)

# Values of these keys/columns are credentials or capabilities by name.
_SECRET_NAME_PARTS = (
    "password", "passwd", "passphrase", "secret", "token", "apikey", "api_key", "access_key", "private_key",
    "authorization", "cookie", "credential", "session", "bearer",
)
# Values of these keys/columns name an original or resolved resource (a link,
# magnet, uploaded file or endpoint address): never assumed safe.
_RESOURCE_NAMES = frozenset({"payload", "address", "magnet", "download_url", "source_url", "url", "link", "uri"})
_URL_RE = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]{1,15}://[^\s\"'<>]+|magnet:\?[^\s\"'<>]+)")
_AUTH_RE = re.compile(r"(?i)\b(bearer|basic|token|apikey|api_key)(\s*[:= ]\s*)([A-Za-z0-9._~+/=-]{6,})")


class _Sanitizer:
    """Per-export replacement state. Tokens are assigned in first-seen order
    and carry nothing derived from the value, so they correlate repeated
    references inside one trace and nothing across traces."""

    def __init__(self, path_roots=()):
        self._tokens = {}
        self._counts = {}
        self.replaced = 0
        # Private host roots (the download root) are replaced wherever they
        # begin a path, keeping everything beneath them: the relative layout is
        # what diagnoses collisions, suffixes and placement.
        roots = sorted({str(root).rstrip("/") for root in path_roots if str(root).strip("/")}, key=len, reverse=True)
        self._roots = re.compile(r"(?<![\w.~-])(" + "|".join(map(re.escape, roots)) + r")(?=/|$|[\s\"'<>,;)\]}])"
                                 ) if roots else None

    def _token(self, kind: str, value) -> str:
        key = (kind, value)
        if key not in self._tokens:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._tokens[key] = f"<redacted-{kind}-{self._counts[kind]}>"
        self.replaced += 1
        return self._tokens[key]

    def secret(self, value):
        if value in (None, "", 0, False) or isinstance(value, (dict, list)) and not value:
            return value
        return self._token("secret", json.dumps(value, sort_keys=True, default=str))

    def opaque(self, value) -> dict:
        data = value if isinstance(value, bytes) else str(value).encode("utf-8", "surrogatepass")
        return {"$redacted": "opaque", "type": "bytes" if isinstance(value, bytes) else "text",
                "length": len(data), "token": self._token("opaque", data)}

    def resource(self, value: str) -> str:
        """Keep scheme and host (the provider/transport identity); replace
        userinfo and every resource-specific component with one token for the
        whole original value."""
        raw = str(value)
        stripped = raw.strip()
        if stripped.casefold().startswith("magnet:"):
            return f"magnet:{self._token('resource', stripped)}"
        try:
            parts = urlsplit(stripped)
            host = parts.hostname or ""
            port = parts.port
        except ValueError:
            parts, host, port = None, "", None
        if parts is None or not parts.scheme or not host:
            return self._token("resource", stripped)
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority = f"{authority}:{port}"
        if parts.username is not None or parts.password is not None:
            authority = f"{self._token('secret', parts.netloc.rpartition('@')[0])}@{authority}"
        prefix = f"{parts.scheme.casefold()}://{authority}"
        if not (parts.path.strip("/") or parts.query or parts.fragment):
            return prefix
        return f"{prefix}/{self._token('resource', stripped)}"

    def path(self, value) -> str | None:
        """A filesystem path: a known root becomes its path-root token; any
        other absolute path keeps its final component and replaces its
        directory with one, so the filename always survives."""
        if not value:
            return value
        value = str(value)
        replaced = self.text(value)
        if replaced != value or not value.startswith("/"):
            return replaced
        directory, name = posixpath.split(value.rstrip("/") or "/")
        return f"{self._token('path-root', directory)}/{name}" if directory.strip("/") else value

    def text(self, value: str) -> str:
        value = _URL_RE.sub(lambda match: self.resource(match.group(0)), value)
        value = _AUTH_RE.sub(lambda match: match.group(1) + match.group(2) + self._token("secret", match.group(3)),
                             value)
        if self._roots is not None:
            value = self._roots.sub(lambda match: self._token("path-root", match.group(1)), value)
        return value

    def value(self, name: str, value):
        lowered = str(name or "").casefold()
        if isinstance(value, bytes):
            return self.opaque(value)
        if any(part in lowered for part in _SECRET_NAME_PARTS):
            return self.secret(value)
        if isinstance(value, dict):
            if set(value) == {"$bytes"}:
                return self.opaque(str(value["$bytes"]))
            if lowered == "headers":
                return {str(key): self.secret(item) for key, item in value.items()}
            return {str(key): self.value(str(key), item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(name, item) for item in value]
        if isinstance(value, str) and value:
            if lowered in _RESOURCE_NAMES:
                if _URL_RE.fullmatch(value.strip()):
                    return self.resource(value)
                return self.opaque(value)
            return self.text(value)
        return value


def _exported(sanitizer: _Sanitizer, column: str, value):
    """One column value as exported. TEXT holding a JSON object or array is
    sanitized field by field and re-encoded, so it stays the same kind of
    value it is in the database; everything else is sanitized directly."""
    if isinstance(value, str) and value[:1] in "{[":
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(sanitizer.value(column, parsed), separators=(",", ":"), ensure_ascii=False)
    return sanitizer.value(column, value)


class _Collector:
    def __init__(self, db, tables: set[str]):
        self.db = db
        self.tables = tables
        self.rows = {table: {} for table in _TABLES}

    async def select(self, table: str, where: str, params=(), *, scope: str) -> list[dict]:
        if table not in self.tables:
            return []
        rows = await self.db.fetchall(f"SELECT rowid AS _trace_rowid,* FROM {table} WHERE {where} ORDER BY rowid",
                                      tuple(params))
        added = []
        for row in rows:
            rowid = row.pop("_trace_rowid")
            if rowid not in self.rows[table]:
                self.rows[table][rowid] = (scope, row)
                added.append(row)
        return added

    def values(self, table: str, column: str) -> set:
        return {row[column] for _, row in self.rows[table].values() if row.get(column) is not None}


def _in(values) -> tuple[str, tuple]:
    values = tuple(sorted(values, key=str))
    return ("(" + ",".join("?" * len(values)) + ")", values) if values else ("(NULL)", ())


async def _transfer_rows(collector: _Collector, transfer_id: int, scope: str) -> None:
    """One transfer's own transfer-scoped rows (the requested transfer, or a
    participant of its consolidation component)."""
    select = collector.select
    await select("torrents", "id=?", (transfer_id,), scope=scope)
    for table in ("transfer_requests", "provider_resources", "standby_resources", "route_attempt_provenance",
                  "transfer_file_manifests",
                  "transfer_file_selections", "execution_attempts", "execution_attempt_provenance",
                  "artifact_recovery_state", "transfer_outcomes", "postprocess_attempts",
                  "transfer_input_challenges", "application_events"):
        await select(table, "transfer_id=?", (transfer_id,), scope=scope)
    for table in ("download_files", "transfer_pause_intents", "deferred_provider_submissions", "events"):
        await select(table, "torrent_id=?", (transfer_id,), scope=scope)
    await select("artifact_material_state", "artifact_id IN (SELECT id FROM download_files WHERE torrent_id=?)",
                 (transfer_id,), scope=scope)
    for table, column, parent in (("resolution_attempts", "request_id", "transfer_requests"),
                                  ("transfer_file_manifest_entries", "manifest_id", "transfer_file_manifests"),
                                  ("transfer_file_selection_entries", "selection_id", "transfer_file_selections")):
        if parent in collector.tables:
            await select(table, f"{column} IN (SELECT id FROM {parent} WHERE transfer_id=?)", (transfer_id,),
                         scope=scope)


async def _component(collector: _Collector, transfer_id: int) -> dict:
    """THE bounded consolidation-component closure.

    The canonical artifacts the requested transfer owns or references (its own
    group heads, the canonical owner of each of its members, and the canonical
    artifacts its candidate provenance or cross-transfer consolidations point
    at), and the transfers that DIRECTLY take part in them: the owner, every
    transfer consolidated into one (``artifact_consolidations``), every
    transfer whose candidate provenance was bound to one, and every standby
    holder beneath one. Each participant contributes its own transfer-scoped
    rows (scope ``component``); nothing is crawled further. Both sets are
    bounded, in ascending identity order, and whatever a bound omits is
    reported by identity."""
    db = collector.db
    heads = {int(row["mirror_group_id"] or row["id"]) for row in await db.fetchall(
        "SELECT id,mirror_group_id FROM download_files WHERE torrent_id=?", (transfer_id,))}
    if "artifact_consolidations" in collector.tables:
        heads |= {int(row["canonical_artifact_id"]) for row in await db.fetchall(
            "SELECT canonical_artifact_id FROM artifact_consolidations WHERE source_transfer_id=?", (transfer_id,))}
    if {"canonical_candidate_origins", "canonical_candidate_bindings"} <= collector.tables:
        heads |= {int(row["canonical_artifact_id"]) for row in await db.fetchall(
            """SELECT b.canonical_artifact_id FROM canonical_candidate_origins o
                JOIN canonical_candidate_bindings b ON b.id=o.binding_id WHERE o.contributing_transfer_id=?""",
            (transfer_id,))}
    heads = sorted(heads)
    artifacts, omitted_artifacts = heads[:COMPONENT_MAX_ARTIFACTS], heads[COMPONENT_MAX_ARTIFACTS:]
    clause, params = _in(artifacts)
    participants = {int(row["torrent_id"]) for row in await db.fetchall(
        f"SELECT torrent_id FROM download_files WHERE id IN {clause} OR mirror_group_id IN {clause}",
        params + params)}
    if "artifact_consolidations" in collector.tables:
        participants |= {int(row["source_transfer_id"]) for row in await db.fetchall(
            f"SELECT source_transfer_id FROM artifact_consolidations WHERE canonical_artifact_id IN {clause}", params)}
    if {"canonical_candidate_origins", "canonical_candidate_bindings"} <= collector.tables:
        participants |= {int(row["contributing_transfer_id"]) for row in await db.fetchall(
            f"""SELECT o.contributing_transfer_id FROM canonical_candidate_origins o
                JOIN canonical_candidate_bindings b ON b.id=o.binding_id WHERE b.canonical_artifact_id IN {clause}""",
            params)}
    # The requested transfer is always a member; the bound counts it.
    others = sorted(participants - {int(transfer_id)})
    room = max(0, COMPONENT_MAX_TRANSFERS - 1)
    for participant in others[:room]:
        await _transfer_rows(collector, participant, "component")
    included, omitted = sorted([int(transfer_id), *others[:room]]), others[room:]
    # The component's canonical artifacts with their full provenance.
    await collector.select("download_files", f"id IN {clause}", params, scope="component")
    await collector.select("canonical_candidate_bindings", f"canonical_artifact_id IN {clause}", params,
                           scope="component")
    await collector.select("artifact_consolidations", f"canonical_artifact_id IN {clause}", params, scope="component")
    bindings, binding_params = _in(collector.values("canonical_candidate_bindings", "id"))
    await collector.select("canonical_candidate_origins", f"binding_id IN {bindings}", binding_params,
                           scope="component")
    return {
        "type": "consolidation_component",
        "rule": "the canonical artifacts the requested transfer owns or references, and the transfers directly "
                "taking part in them (owner, consolidated contributors, bound candidate provenance, standby "
                "holders), each with its own transfer-scoped rows (scope 'component'); participants are never "
                "expanded into further components",
        "canonical_artifact_ids": artifacts,
        "artifact_count": len(artifacts),
        "transfer_ids": included,
        "transfer_count": len(included),
        "truncated": bool(omitted or omitted_artifacts),
        "omitted_transfer_ids": omitted,
        "omitted_artifact_ids": omitted_artifacts,
        "limits": {"max_transfers": COMPONENT_MAX_TRANSFERS, "max_artifacts": COMPONENT_MAX_ARTIFACTS},
    }


async def _collect(collector: _Collector, transfer_id: int) -> dict:
    """The requested transfer's own rows, its consolidation component, then
    the depth-one relational closure of everything exported. Order matters
    only in that each step reads identities the previous steps exported (and
    a row keeps the first scope it was exported under). Returns the component
    description."""
    select, values = collector.select, collector.values
    own = "primary"
    await _transfer_rows(collector, transfer_id, own)
    await select("transfer_controls", "1=1", scope="global")
    own_artifacts = values("download_files", "id")
    clause, params = _in(own_artifacts)
    await select("canonical_candidate_bindings", f"canonical_artifact_id IN {clause}", params, scope=own)
    await select("artifact_consolidations", f"source_transfer_id=? OR canonical_artifact_id IN {clause}",
                 (transfer_id, *params), scope=own)
    bindings, _ = _in(values("canonical_candidate_bindings", "id"))
    await select("canonical_candidate_origins", f"contributing_transfer_id=? OR binding_id IN {bindings}",
                 (transfer_id, *values("canonical_candidate_bindings", "id")), scope=own)
    component = await _component(collector, transfer_id)
    exported_artifacts = values("download_files", "id")

    # Closure: foreign artifacts this transfer's rows point at (its canonical
    # owners, its contributors, its unverified targets), and each one's own
    # request, transfer, candidate provenance, consolidation, execution and
    # recovery facts.
    context = "context"
    clause, params = _in(values("canonical_candidate_origins", "binding_id") - values("canonical_candidate_bindings", "id"))
    await select("canonical_candidate_bindings", f"id IN {clause}", params, scope=context)
    foreign = (
        values("download_files", "mirror_group_id") | values("artifact_consolidations", "canonical_artifact_id")
        | values("artifact_consolidations", "contributing_artifact_id")
        | values("canonical_candidate_origins", "contributing_artifact_id")
        | values("canonical_candidate_bindings", "canonical_artifact_id")
        | values("transfer_requests", "equivalence_target_artifact_id")
    ) - exported_artifacts
    clause, params = _in(foreign)
    await select("download_files", f"id IN {clause}", params, scope=context)
    await select("canonical_candidate_bindings", f"canonical_artifact_id IN {clause}", params, scope=context)
    await select("artifact_consolidations", f"canonical_artifact_id IN {clause} OR contributing_artifact_id IN {clause}",
                 params + params, scope=context)
    await select("execution_attempts", f"artifact_id IN {clause}", params, scope=context)
    await select("execution_attempt_provenance", f"artifact_id IN {clause}", params, scope=context)
    await select("artifact_recovery_state", f"artifact_id IN {clause}", params, scope=context)
    await select("artifact_material_state", f"artifact_id IN {clause}", params, scope=context)
    clause, params = _in(values("canonical_candidate_bindings", "id"))
    await select("canonical_candidate_origins", f"binding_id IN {clause}", params, scope=context)
    requests = (values("download_files", "request_id") | values("canonical_candidate_origins", "request_id")
                | values("artifact_consolidations", "source_request_id"))
    clause, params = _in(requests)
    await select("transfer_requests", f"id IN {clause}", params, scope=context)
    attempts = values("canonical_candidate_origins", "resolution_attempt_id")
    clause, params = _in(values("transfer_requests", "id"))
    attempt_clause, attempt_params = _in(attempts)
    await select("resolution_attempts", f"request_id IN {clause} OR id IN {attempt_clause}",
                 params + attempt_params, scope=context)
    clause, params = _in(values("resolution_attempts", "id"))
    await select("route_attempt_provenance", f"resolution_attempt_id IN {clause}", params, scope=context)
    transfers = (values("download_files", "torrent_id") | values("transfer_requests", "transfer_id")
                 | values("artifact_consolidations", "source_transfer_id"))
    clause, params = _in(transfers)
    await select("torrents", f"id IN {clause}", params, scope=context)
    return component


async def _reference_audit(db, collector: _Collector) -> list[dict]:
    """Every durable reference in the exported rows whose target row is not
    exported: ``outside_closure`` when it exists, ``absent`` when it does not."""
    findings = []
    for table, column, target, key in _REFERENCES:
        if table not in collector.tables or target not in collector.tables:
            continue
        exported = collector.values(target, key)
        for value in sorted(collector.values(table, column) - exported, key=str):
            exists = await db.fetchone(f"SELECT 1 AS present FROM {target} WHERE {key}=?", (value,))
            findings.append({"table": table, "column": column, "target_table": target, "target_column": key,
                             "value": value, "status": "outside_closure" if exists else "absent"})
    return findings


async def _schema_identity(db, tables: set[str]) -> dict:
    columns = {}
    for table in sorted(tables):
        info = await db.fetchall(f"PRAGMA table_info({table})")
        columns[table] = [str(item["name"]) for item in info]
    migrations = []
    if "schema_migrations" in tables:
        migrations = [row["version"] for row in await db.fetchall(
            "SELECT version FROM schema_migrations ORDER BY version")]
    digest = hashlib.sha256(json.dumps(columns, sort_keys=True).encode("utf-8")).hexdigest()
    return {"migrations": migrations, "columns_sha256": digest}


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _epoch_iso(value) -> str | None:
    return _iso(datetime.fromtimestamp(float(value), timezone.utc)) if value is not None else None


def _status(counts: dict, *, empty_reason: str) -> dict:
    """One domain's collection status from its per-item outcomes."""
    observed, unavailable = counts.get("observed", 0), counts.get("unavailable", 0)
    if not observed and not unavailable:
        return {"status": "not_applicable", "reason": empty_reason, "counts": counts}
    if not unavailable:
        return {"status": "complete", "counts": counts}
    if not observed:
        return {"status": "unavailable", "reason": "no item in this domain could be observed", "counts": counts}
    return {"status": "partial", "reason": f"{unavailable} of {observed + unavailable} items could not be observed",
            "counts": counts}


async def _bounded(awaitable):
    """``(True, result)`` or ``(False, reason)`` -- an external observation
    never raises into, or holds up, the trace for longer than the bound."""
    try:
        return True, await asyncio.wait_for(awaitable, OBSERVATION_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as exc:  # the trace reports failure; it is never the failure
        return False, f"error:{type(exc).__name__}"


def _error(error) -> dict | None:
    return error.as_dict(diagnostics=True) if error is not None else None


async def _observe_filesystem(collector: _Collector, roots) -> tuple[dict, dict]:
    """Every durable material target of every exported artifact, whatever
    its lifecycle state, as it is on disk now."""
    attempts = {}
    for _, row in collector.rows["execution_attempts"].values():
        attempts.setdefault(row["artifact_id"], []).append(row)
    materials = {row["artifact_id"]: row for _, row in collector.rows.get("artifact_material_state", {}).values()}
    targets, directories = [], {}
    counts = {"observed": 0, "unavailable": 0, "no_recorded_target": 0}
    for scope, row in collector.rows["download_files"].values():
        path = row.get("local_path")
        if not path:
            counts["no_recorded_target"] += 1
            continue
        # The member paths already durably recorded for this artifact: its
        # current attempt's verified materialization, else its latest one.
        own = attempts.get(row["id"], [])
        recorded = next((item for item in own if item["id"] == row.get("execution_attempt_id")
                         and item.get("materialization")), None) or next(
            (item for item in reversed(own) if item.get("materialization")), None)
        members = ()
        if recorded is not None:
            result = codec.materialization(recorded["materialization"])
            if result is not None and result.kind == MaterializationKind.COLLECTION:
                members = tuple(entry.relative_path for entry in result.entries)
        # A stat on a hung mount must not hold the trace either.
        answered, observed = await _bounded(asyncio.to_thread(observe_material, str(path), members))
        if not answered:
            observed = {"target": {"exists": None, "type": "unavailable", "error": observed}, "members": []}
        target = observed["target"]
        expected = row.get("size_bytes")
        comparable = target["type"] == "file" and isinstance(expected, int) and expected > 0
        outcome = "unavailable" if target["type"] == "unavailable" else "observed"
        counts[outcome] += 1
        directories.setdefault(posixpath.dirname(str(path)) or str(path), None)
        targets.append({
            "artifact_id": row["id"], "transfer_id": row.get("torrent_id"), "scope": scope,
            "durable_status": row.get("status"), "execution_attempt_id": row.get("execution_attempt_id"),
            "material_owner_attempt_id": next((item.get("material_owner_attempt_id") for item in own
                                               if item["id"] == row.get("execution_attempt_id")), None),
            "materialization_record_attempt_id": recorded["id"] if recorded is not None else None,
            "path": str(path), "observation": "observed" if outcome == "observed" else "unavailable",
            **target,
            "durable_size_bytes": expected,
            "size_matches_durable": (target["bytes"] == expected) if comparable else None,
            "members": observed["members"],
            "material": await _material_facts(materials.get(row["id"]), str(path)),
        })
    capacity, seen = [], set()
    for directory in (*roots, *directories):
        answered, measured = await _bounded(asyncio.to_thread(observe_capacity, directory))
        if not answered:
            measured = {"status": "unavailable", "reason": measured}
        identity = measured.get("filesystem_id")
        if identity is not None and identity in seen:
            continue
        seen.add(identity)
        capacity.append(measured)
    status = _status({key: value for key, value in counts.items() if key != "no_recorded_target"},
                     empty_reason="no exported artifact records a material target")
    status["counts"] = counts
    return {"targets": targets, "capacity": capacity}, status


async def _material_facts(row, path: str) -> dict | None:
    """DebridPulse material truth for one artifact, reconciled -- read-only --
    against the payload now: what DP holds VALID, how far it reaches, and
    whether the file still backs it. Observation only; nothing is changed."""
    if row is None:
        return None
    valid = mat.decode(row.get("valid_ranges"))
    members, _identities = mat.decode_members(row.get("member_ranges"))
    answered, facts = await _bounded(asyncio.to_thread(payload_facts, path, valid))
    observed = facts if answered and facts.available else None
    frontier = valid[-1][1] if valid else 0
    return {
        "material_generation": row.get("material_generation"), "writer_generation": row.get("writer_generation"),
        "geometry_version": row.get("geometry_version"), "valid_bytes": mat.total(valid),
        "safe_prefix": mat.contiguous_prefix(valid), "valid_ranges": mat.summary(valid),
        "valid_range_count": len(valid), "valid_frontier": frontier,
        "observation": "observed" if observed is not None else "unavailable",
        "observed_bytes": observed.size if observed is not None and observed.exists else None,
        "payload_present": observed.exists if observed is not None else None,
        "identity_matches_checkpoint": (observed.identity == row.get("destination_identity")
                                        if observed is not None and observed.exists and row.get("destination_identity")
                                        else None),
        "valid_beyond_observed_length": (frontier > observed.size if observed is not None and observed.exists
                                         else bool(valid) if observed is not None else None),
        "holes_in_valid_bytes": mat.total(observed.holes) if observed is not None else None,
        "member_count": len(members),
        "member_valid_bytes": sum(mat.total(ranges) for _member, ranges in members),
        "members": [{"member": member, "valid_bytes": mat.total(ranges), "safe_prefix": mat.contiguous_prefix(ranges)}
                    for member, ranges in members[:16]],
    }


async def _observe_executors(application, collector: _Collector) -> tuple[dict, dict]:
    """What each referenced executor reports NOW about exactly the execution
    attempts this trace exports -- never any other native work."""
    rows = [row for _, row in collector.rows["execution_attempts"].values()]
    if not rows:
        return {"attempts": []}, _status({}, empty_reason="no exported execution attempt")
    engine = getattr(application, "engine", None)
    repository = getattr(application, "repository", None)
    observed_at = _iso(datetime.now(timezone.utc))
    entries, batches = {}, {}
    for row in rows:
        entry = {"execution_attempt_id": row["id"], "artifact_id": row["artifact_id"],
                 "transfer_id": row["transfer_id"], "executor_id": row["executor_id"],
                 "durable_state": row.get("state"), "handle": None, "dp_owned": None,
                 "observed_at": observed_at}
        entries[row["id"]] = entry
        try:
            handle = codec.handle(codec.load(row["handle"]))
        except (TypeError, ValueError, KeyError):
            handle = None
        if handle is None:
            entry.update(observation="unsupported", reason="handle_not_decodable")
            continue
        entry["handle"] = {"correlation": dict(handle.correlation), "native": dict(handle.native or {}) or None}
        if engine is None or repository is None:
            entry.update(observation="unavailable", reason="no_application_runtime")
            continue
        # The canonical fence decides what DebridPulse may observe: only work
        # it still owns. Anything else is outside its observation rights.
        entry["dp_owned"] = bool(await repository.authorize_execution(handle, "observe"))
        if not entry["dp_owned"]:
            entry.update(observation="not_applicable", reason="attempt_not_dp_owned")
            continue
        executor = engine.registry.executor_for_handle(handle)
        if executor is None:
            entry.update(observation="unavailable", reason="executor_not_registered")
            continue
        batches.setdefault(executor.descriptor.id, (executor, []))[1].append(handle)

    async def observe(executor, handles):
        return executor, handles, await _bounded(engine.observe_existing(executor, tuple(handles)))

    for executor, handles, (answered, result) in await asyncio.gather(
            *(observe(executor, handles) for executor, handles in batches.values())):
        aggregate_only = bool(getattr(executor.capabilities, "aggregate_throughput", False))
        for index, handle in enumerate(handles):
            entry = entries[handle.attempt_id]
            if not answered:
                entry.update(observation="unavailable", reason=f"executor_{result}")
                continue
            item = result.observations[index]
            progress = item.progress
            if item.state == ExecutionState.UNKNOWN:
                entry.update(observation="unavailable", reason="executor_state_unknown")
            else:
                entry["observation"] = "observed"
            entry["executor"] = {
                "state": str(item.state),
                # ABSENT is the executor's positive answer that the native job
                # does not exist; UNKNOWN is no answer at all.
                "native_exists": None if item.state == ExecutionState.UNKNOWN else item.state != ExecutionState.ABSENT,
                "completed_bytes": progress.completed_bytes, "total_bytes": progress.total_bytes,
                "bytes_per_second": None if aggregate_only else progress.bytes_per_second,
                "speed_measured_per_execution": not aggregate_only,
                "error": _error(item.error),
            }
    counts = {}
    for entry in entries.values():
        counts[entry["observation"]] = counts.get(entry["observation"], 0) + 1
    status = _status(counts, empty_reason="no exported execution attempt is observable by DebridPulse")
    return {"attempts": list(entries.values())}, status


def _referenced_identities(collector: _Collector) -> tuple[set, set]:
    providers, executors = set(), set()
    for rows in collector.rows.values():
        for _, row in rows.values():
            if row.get("provider_id"):
                providers.add(str(row["provider_id"]))
            if row.get("executor_id"):
                executors.add(str(row["executor_id"]))
    return providers, executors


async def _runtime_context(application, collector: _Collector) -> tuple[dict, dict]:
    """The current context of exactly the providers/executors this trace
    references, plus the few global values that decide execution."""
    engine = getattr(application, "engine", None)
    if engine is None:
        return {}, {"status": "unavailable", "reason": "no_application_runtime"}
    providers, executors = _referenced_identities(collector)
    settings = get_settings()
    definitions = tuple(getattr(application, "definitions", ()) or ())
    try:
        from integrations.configuration import public_integrations
        public = public_integrations(settings, definitions)
    except Exception:
        public = {}

    async def describe(identity: str, role: str) -> dict:
        registered = (engine.registry.providers if role == "provider" else engine.registry.executors).get(identity)
        namespace = next((item.id for item in definitions if identity in item.owned_identities), None)
        configuration = public.get(namespace) if namespace else None
        entry = {
            "identity": identity, "role": role, "registered": registered is not None,
            "descriptor": ({"enabled": registered.descriptor.enabled, "priority": registered.descriptor.priority}
                           if registered is not None else None),
            "configuration_namespace": namespace,
            "configuration": ({key: configuration.get(key) for key in (
                "enabled", "effective_enabled", "configured", "verified", "verification_applicable")}
                if configuration else None),
        }
        if registered is None:
            entry["readiness"] = {"observation": "unavailable", "reason": "not_registered"}
        elif role == "provider" and not isinstance(registered, Health):
            entry["readiness"] = {"observation": "unsupported", "reason": "provider_declares_no_health_contract"}
        elif role == "provider" and not registered.descriptor.enabled:
            entry["readiness"] = {"observation": "not_applicable", "reason": "provider_disabled"}
        else:
            answered, health = await _bounded(registered.health())
            if not answered:
                entry["readiness"] = {"observation": "unavailable", "reason": f"health_{health}"}
            elif role == "provider":
                entry["readiness"] = {"observation": "observed", "healthy": bool(health.healthy),
                                      "error": _error(health.error)}
            else:
                entry["readiness"] = {"observation": "observed", "reachable": bool(health.reachable),
                                      "ready": bool(health.ready), "error": _error(health.error),
                                      "available_runtime_capabilities": sorted(
                                          str(item) for item in health.available_runtime_capabilities)}
        return entry

    integrations = await asyncio.gather(*(describe(identity, "provider") for identity in sorted(providers)),
                                        *(describe(identity, "executor") for identity in sorted(executors)))
    policy = engine.policy
    capacity = getattr(application, "capacity", None)
    try:
        storage = capacity.snapshot(StorageDomain.DOWNLOAD).as_dict() if capacity is not None else None
    except Exception:
        storage = None
    context = {
        "temporal_scope": "export_time",
        "integrations": integrations,
        "transfer_execution": {
            "download_root": engine.root,
            "policy": {item.name: getattr(policy, item.name) for item in fields(policy)
                       if not callable(getattr(policy, item.name))},
            "max_download_bytes_per_second": getattr(getattr(engine, "runtime", None), "configured", None),
            "globally_paused": bool(await application.repository.globally_paused()),
            "dispatch_permitted": bool(getattr(engine, "dispatch_permitted", True)),
            # The storage-health owner's current download snapshot (as of its
            # own ``probed_at``): the state that gates new execution.
            "download_storage": storage,
        },
    }
    # The context itself is always collected; only a readiness probe can
    # fail to answer, which makes the domain partial, never absent.
    counts = {}
    for entry in integrations:
        outcome = entry["readiness"]["observation"]
        counts[outcome] = counts.get(outcome, 0) + 1
    unavailable = counts.get("unavailable", 0)
    status = ({"status": "partial", "reason": f"{unavailable} referenced integration readiness observations "
                                              "could not be made", "counts": counts}
              if unavailable else {"status": "complete", "counts": counts})
    return context, status


def filename(transfer_id: int, generated_at: datetime) -> str:
    return f"debridpulse-transfer-{int(transfer_id)}-trace-{generated_at.strftime('%Y%m%dT%H%M%SZ')}.json"


async def build(transfer_id: int, application) -> dict | None:
    """The complete sanitized trace document, or ``None`` when no transfer
    with this identity exists. ``application`` supplies the runtime the
    non-durable observations are made through; without it those domains are
    reported ``unavailable``, never omitted."""
    generated_at = datetime.now(timezone.utc)
    async with get_db() as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        try:
            tables = {row["name"] for row in await db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "torrents" not in tables or not await db.fetchone(
                    "SELECT 1 AS present FROM torrents WHERE id=?", (int(transfer_id),)):
                return None
            collector = _Collector(db, tables)
            component = await _collect(collector, int(transfer_id))
            references = await _reference_audit(db, collector)
            schema = await _schema_identity(db, tables)
        finally:
            await db.rollback()

    # The observation set is complete before anything is sanitized.
    engine = getattr(application, "engine", None)
    roots = (engine.root,) if engine is not None else ()
    filesystem, filesystem_status = await _observe_filesystem(collector, roots)
    executors, executor_status = await _observe_executors(application, collector)
    runtime, runtime_status = await _runtime_context(application, collector)

    sanitizer = _Sanitizer(path_roots=roots)
    data, inventory = {}, []
    for table in _TABLES:
        if table not in tables:
            inventory.append({"table": table, "status": "unsupported", "rows": 0})
            continue
        exported = [
            {"scope": scope, "row": {column: _exported(sanitizer, column, value) for column, value in row.items()}}
            for scope, row in collector.rows[table].values()
        ]
        data[table] = exported
        scopes = {}
        for item in exported:
            scopes[item["scope"]] = scopes.get(item["scope"], 0) + 1
        inventory.append({"table": table, "status": "populated" if exported else "empty",
                          "rows": len(exported), "rows_by_scope": scopes})
    for table in sorted(tables - set(_TABLES)):
        inventory.append({"table": table, "status": "omitted", "rows": None,
                          "reason": _OMITTED.get(table, "not a transfer-scoped durable table")})
    context_transfers = sorted(int(row["id"]) for scope, row in collector.rows["torrents"].values()
                               if scope == "context")
    component_transfers = sorted(int(row["id"]) for scope, row in collector.rows["torrents"].values()
                                 if scope == "component")
    for target in filesystem["targets"]:
        target["path"] = sanitizer.path(target["path"])
    for measured in filesystem["capacity"]:
        if measured.get("probe_path"):
            measured["probe_path"] = sanitizer.path(measured["probe_path"])
    observations = {"filesystem": sanitizer.value("filesystem", filesystem),
                    "executors": sanitizer.value("executors", executors)}
    runtime = sanitizer.value("runtime_context", runtime)
    timing = process_timing()
    return {
        "metadata": {
            "trace_format": TRACE_FORMAT,
            "trace_format_version": TRACE_FORMAT_VERSION,
            "requested_transfer_id": int(transfer_id),
            "primary_transfer_id": int(transfer_id),
            "component_transfer_ids": component_transfers,
            "context_transfer_ids": context_transfers,
            "generated_at": _iso(generated_at),
            "application_version": read_version(),
            # ``None`` only when the running build carries no revision.
            "build_revision": read_build_revision(),
            "process": {"started_at": _epoch_iso(timing["started_at"]),
                        "uptime_seconds": round(timing["uptime_seconds"], 3)},
            "schema": schema,
            "closure": {
                "depth": 1,
                "rule": "requested transfer rows (scope 'primary'), the rows of every transfer in its bounded "
                        "consolidation component (scope 'component'), plus the foreign artifacts all of these "
                        "reference and those artifacts' own request, transfer, candidate provenance, consolidation, "
                        "execution and recovery rows (scope 'context'); context rows are not followed further",
                "component": component,
            },
            "encoding": "every column of every exported row is present; TEXT holding JSON stays JSON text, "
                        "sanitized field by field; BLOB values are never emitted",
            "sanitization": {
                "applied": True,
                "version": SANITIZATION_VERSION,
                "replaced_values": sanitizer.replaced,
                "tokens": "per-export; equal tokens within this trace denote equal original values; tokens carry "
                          "no information derived from the value and never correlate across traces",
                "rules": [
                    "credential/capability fields by name (passwords, keys, tokens, cookies, sessions, "
                    "authorization, credentials) are replaced by <redacted-secret-N>",
                    "every header value is replaced; header names are kept",
                    "URLs keep scheme and host; userinfo is replaced and the rest of the URL by one "
                    "<redacted-resource-N> for the whole original value; magnets keep only the scheme",
                    "non-URL resource payloads and BLOBs become an opaque marker with type and length",
                    "free text keeps its words; embedded URLs and authorization values are replaced as above",
                    "the download root becomes <redacted-path-root-N> wherever a path begins with it, keeping "
                    "every path component beneath it; another absolute observed path keeps its final component "
                    "and replaces its directory with a path-root token",
                    "normalized errors keep their semantic fields (domain, category, stage, origin, retryability, "
                    "permanence, operator action, native code, bounded context); only credential/capability values "
                    "inside them are replaced",
                ],
            },
            "observation_boundary": (
                "durable_state is the retained database as of generated_at. observations and runtime_context are "
                "what could be observed at generation time only: they do not describe any earlier moment, and "
                "DebridPulse does not version settings. Filesystem or executor state that changed or disappeared "
                "before generation, remote provider state DebridPulse never recorded, transient network conditions "
                "and the values of secrets are outside what a trace can contain. Executors are observed only for "
                "attempts DebridPulse still owns; directories are never walked beyond the durably recorded member "
                "paths. Contradictions between domains are exported as found."),
        },
        "collection_status": {
            "durable_state": {"status": "complete"},
            "filesystem": filesystem_status,
            "executor": executor_status,
            "runtime_context": runtime_status,
        },
        "inventory": inventory,
        "references": references,
        "data": data,
        "observations": observations,
        "runtime_context": runtime,
    }


async def export(transfer_id: int, application) -> tuple[str, bytes] | None:
    """(download filename, UTF-8 JSON body), or ``None`` for an unknown transfer."""
    trace = await build(transfer_id, application)
    if trace is None:
        return None
    generated_at = datetime.fromisoformat(trace["metadata"]["generated_at"].replace("Z", "+00:00"))
    body = json.dumps(trace, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    return filename(transfer_id, generated_at), body
