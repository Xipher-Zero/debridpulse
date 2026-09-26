"""Transfer Trace Log: the one read-only, sanitized export of one transfer's
durable state plus the relational context needed to explain it.

This module is the single trace-export owner. It observes; it never becomes a
lifecycle, persistence, recovery, routing, canonicalization or execution
owner. Every read happens inside one read transaction on a connection that
SQLite itself holds ``query_only``, so a trace is a consistent snapshot and
cannot write, migrate or reconcile anything.

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
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit

from core.version import read_version
from db.database import get_db

TRACE_FORMAT = "debridpulse.transfer-trace"
TRACE_FORMAT_VERSION = 1
SANITIZATION_VERSION = 1

# Every transfer-scoped durable table, in export order. A table named here but
# absent from this database is reported ``unsupported``; a database table not
# named here or in ``_OMITTED`` would be reported ``omitted`` with an unknown
# reason rather than silently ignored.
_TABLES = (
    "torrents", "transfer_requests", "provider_resources", "resolution_attempts", "route_attempt_provenance",
    "transfer_file_manifests", "transfer_file_manifest_entries", "transfer_file_selections",
    "transfer_file_selection_entries", "download_files", "canonical_candidate_bindings",
    "canonical_candidate_origins", "artifact_consolidations", "execution_attempts", "execution_attempt_provenance",
    "artifact_recovery_state", "transfer_outcomes", "postprocess_attempts", "transfer_pause_intents",
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

    def __init__(self):
        self._tokens = {}
        self._counts = {}
        self.replaced = 0

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

    def text(self, value: str) -> str:
        value = _URL_RE.sub(lambda match: self.resource(match.group(0)), value)
        return _AUTH_RE.sub(lambda match: match.group(1) + match.group(2) + self._token("secret", match.group(3)),
                            value)

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


async def _collect(collector: _Collector, transfer_id: int) -> None:
    """The requested transfer's own rows, then its depth-one relational
    closure. Order matters only in that each step reads identities the
    previous steps exported."""
    select, values = collector.select, collector.values
    own = "primary"
    await select("torrents", "id=?", (transfer_id,), scope=own)
    for table in ("transfer_requests", "provider_resources", "route_attempt_provenance", "transfer_file_manifests",
                  "transfer_file_selections", "execution_attempts", "execution_attempt_provenance",
                  "artifact_recovery_state", "transfer_outcomes", "postprocess_attempts",
                  "transfer_input_challenges", "application_events"):
        await select(table, "transfer_id=?", (transfer_id,), scope=own)
    for table in ("download_files", "transfer_pause_intents", "deferred_provider_submissions", "events"):
        await select(table, "torrent_id=?", (transfer_id,), scope=own)
    await select("transfer_controls", "1=1", scope="global")
    clause, params = _in(values("transfer_requests", "id"))
    await select("resolution_attempts", f"request_id IN {clause}", params, scope=own)
    clause, params = _in(values("transfer_file_manifests", "id"))
    await select("transfer_file_manifest_entries", f"manifest_id IN {clause}", params, scope=own)
    clause, params = _in(values("transfer_file_selections", "id"))
    await select("transfer_file_selection_entries", f"selection_id IN {clause}", params, scope=own)
    own_artifacts = values("download_files", "id")
    clause, params = _in(own_artifacts)
    await select("canonical_candidate_bindings", f"canonical_artifact_id IN {clause}", params, scope=own)
    await select("artifact_consolidations", f"source_transfer_id=? OR canonical_artifact_id IN {clause}",
                 (transfer_id, *params), scope=own)
    bindings, _ = _in(values("canonical_candidate_bindings", "id"))
    await select("canonical_candidate_origins", f"contributing_transfer_id=? OR binding_id IN {bindings}",
                 (transfer_id, *values("canonical_candidate_bindings", "id")), scope=own)

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
    ) - own_artifacts
    clause, params = _in(foreign)
    await select("download_files", f"id IN {clause}", params, scope=context)
    await select("canonical_candidate_bindings", f"canonical_artifact_id IN {clause}", params, scope=context)
    await select("artifact_consolidations", f"canonical_artifact_id IN {clause} OR contributing_artifact_id IN {clause}",
                 params + params, scope=context)
    await select("execution_attempts", f"artifact_id IN {clause}", params, scope=context)
    await select("execution_attempt_provenance", f"artifact_id IN {clause}", params, scope=context)
    await select("artifact_recovery_state", f"artifact_id IN {clause}", params, scope=context)
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


def filename(transfer_id: int, generated_at: datetime) -> str:
    return f"debridpulse-transfer-{int(transfer_id)}-trace-{generated_at.strftime('%Y%m%dT%H%M%SZ')}.json"


async def build(transfer_id: int) -> dict | None:
    """The complete sanitized trace document, or ``None`` when no transfer
    with this identity exists."""
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
            await _collect(collector, int(transfer_id))
            references = await _reference_audit(db, collector)
            schema = await _schema_identity(db, tables)
        finally:
            await db.rollback()

    sanitizer = _Sanitizer()
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
    return {
        "metadata": {
            "trace_format": TRACE_FORMAT,
            "trace_format_version": TRACE_FORMAT_VERSION,
            "requested_transfer_id": int(transfer_id),
            "primary_transfer_id": int(transfer_id),
            "context_transfer_ids": context_transfers,
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "application_version": read_version(),
            # The build revision is recorded only as an image label; the
            # running application has no durable source for it.
            "build_revision": None,
            "schema": schema,
            "closure": {
                "depth": 1,
                "rule": "requested transfer rows (scope 'primary'), plus the foreign artifacts they reference and "
                        "those artifacts' own request, transfer, candidate provenance, consolidation, execution and "
                        "recovery rows (scope 'context'); context rows are not followed further",
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
                ],
            },
        },
        "inventory": inventory,
        "references": references,
        "data": data,
    }


async def export(transfer_id: int) -> tuple[str, bytes] | None:
    """(download filename, UTF-8 JSON body), or ``None`` for an unknown transfer."""
    trace = await build(transfer_id)
    if trace is None:
        return None
    generated_at = datetime.fromisoformat(trace["metadata"]["generated_at"].replace("Z", "+00:00"))
    body = json.dumps(trace, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    return filename(transfer_id, generated_at), body
