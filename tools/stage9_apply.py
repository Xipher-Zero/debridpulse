from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    path.write_text(text.replace(old, new, 1))


def append_once(path: Path, marker: str, content: str) -> None:
    text = path.read_text()
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n" + content.strip() + "\n")


repo = ROOT / "backend/transfers/repository.py"

replace_once(
    repo,
    '''    """CREATE TABLE IF NOT EXISTS transfer_outcomes (\n        id INTEGER PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),\n        attempt_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,\n        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",\n''',
    '''    """CREATE TABLE IF NOT EXISTS route_attempt_provenance (\n        resolution_attempt_id TEXT PRIMARY KEY REFERENCES resolution_attempts(id),\n        transfer_id INTEGER NOT NULL REFERENCES torrents(id),\n        request_id TEXT NOT NULL REFERENCES transfer_requests(id),\n        ordinal INTEGER NOT NULL CHECK(ordinal > 0), operation TEXT NOT NULL,\n        previous_attempt_id TEXT REFERENCES resolution_attempts(id),\n        transition_kind TEXT, transition_reason TEXT, candidate_summary TEXT NOT NULL DEFAULT '[]',\n        outcome TEXT NOT NULL DEFAULT 'started', history_quality TEXT NOT NULL DEFAULT 'recorded',\n        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,\n        UNIQUE(request_id,ordinal))""",\n    """CREATE TABLE IF NOT EXISTS execution_attempt_provenance (\n        execution_attempt_id TEXT PRIMARY KEY REFERENCES execution_attempts(id),\n        route_attempt_id TEXT REFERENCES resolution_attempts(id),\n        transfer_id INTEGER NOT NULL REFERENCES torrents(id),\n        artifact_id INTEGER NOT NULL REFERENCES download_files(id),\n        ordinal INTEGER NOT NULL CHECK(ordinal > 0), provider_id TEXT, candidate_id TEXT, candidate_source TEXT,\n        outcome TEXT NOT NULL DEFAULT 'prepared', delivered INTEGER NOT NULL DEFAULT 0,\n        history_quality TEXT NOT NULL DEFAULT 'recorded',\n        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,\n        UNIQUE(artifact_id,ordinal))""",\n    "CREATE INDEX IF NOT EXISTS idx_route_provenance_transfer ON route_attempt_provenance(transfer_id,request_id,ordinal)",\n    "CREATE INDEX IF NOT EXISTS idx_execution_provenance_transfer ON execution_attempt_provenance(transfer_id,artifact_id,ordinal)",\n    "CREATE INDEX IF NOT EXISTS idx_execution_provenance_route ON execution_attempt_provenance(route_attempt_id)",\n    """CREATE TABLE IF NOT EXISTS transfer_outcomes (\n        id INTEGER PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),\n        attempt_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,\n        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",\n''',
    "provenance schema",
)

replace_once(
    repo,
    '''            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_request ON download_files(request_id) WHERE request_id IS NOT NULL")\n            await db.commit()\n\n    @staticmethod\n    def _transfer(row) -> Transfer | None:\n''',
    '''            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_request ON download_files(request_id) WHERE request_id IS NOT NULL")\n            await self._backfill_provenance(db)\n            await db.commit()\n\n    @staticmethod\n    def _safe_candidate_source(candidate):\n        source = candidate.source_identity if candidate else None\n        if source is not None:\n            return {"scope": str(source.scope), "key": str(source.key)}\n        return {"scope": "candidate", "key": str(candidate.id)} if candidate else None\n\n    @classmethod\n    def _candidate_summary(cls, candidates):\n        return codec.dump([\n            {\n                "candidate_id": str(candidate.id),\n                "provider_id": str(candidate.provider_id or ""),\n                "ordinal": ordinal,\n                "source": cls._safe_candidate_source(candidate),\n            }\n            for ordinal, candidate in enumerate(candidates, start=1)\n        ])\n\n    @staticmethod\n    def _execution_outcome(state):\n        value = str(state)\n        if value == "succeeded":\n            return "succeeded"\n        if value in {"failed", "absent"}:\n            return "failed"\n        if value == "cancelled":\n            return "cancelled"\n        if value == "unknown":\n            return "unknown"\n        return "active"\n\n    @classmethod\n    async def _candidate_route(cls, db, transfer_id, candidate):\n        if candidate is None or not candidate.id or not candidate.provider_id:\n            return None\n        rows = await db.fetchall("""SELECT p.resolution_attempt_id,p.ordinal,p.candidate_summary,a.provider_id\n            FROM route_attempt_provenance p JOIN resolution_attempts a ON a.id=p.resolution_attempt_id\n            WHERE p.transfer_id=? AND a.provider_id=? ORDER BY p.ordinal DESC,a.updated_at DESC,a.id DESC""",\n            (transfer_id, candidate.provider_id))\n        for row in rows:\n            for item in codec.load(row["candidate_summary"], []):\n                if str(item.get("candidate_id") or "") == str(candidate.id):\n                    return row["resolution_attempt_id"]\n        return None\n\n    @classmethod\n    async def _backfill_provenance(cls, db):\n        """Idempotently migrate only facts already durably present before Item 9."""\n        route_rows = await db.fetchall("""SELECT a.*,r.transfer_id FROM resolution_attempts a\n            JOIN transfer_requests r ON r.id=a.request_id\n            LEFT JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id\n            WHERE p.resolution_attempt_id IS NULL\n            ORDER BY r.transfer_id,a.request_id,a.created_at,a.id""")\n        for row in route_rows:\n            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM route_attempt_provenance WHERE request_id=?", (row["request_id"],))\n            ordinal = int(ordinal_row["n"] or 0) + 1\n            candidates = ()\n            if row.get("result"):\n                try:\n                    payload = codec.load(row["result"], {})\n                    candidates = tuple(codec.candidate(value) for value in payload.get("candidates", []))\n                except (TypeError, ValueError, KeyError):\n                    candidates = ()\n            outcome = "failed" if row["state"] == "failed" else "resolved" if row["state"] == "succeeded" else "unknown"\n            await db.execute("""INSERT OR IGNORE INTO route_attempt_provenance(\n                resolution_attempt_id,transfer_id,request_id,ordinal,operation,candidate_summary,outcome,history_quality)\n                VALUES(?,?,?,?,?,?,?,'legacy_known')""",\n                (row["id"], row["transfer_id"], row["request_id"], ordinal, "legacy", cls._candidate_summary(candidates), outcome))\n\n        execution_rows = await db.fetchall("""SELECT e.*,f.status AS artifact_status,f.execution_attempt_id AS current_execution_id,\n                f.candidates AS artifact_candidates,f.selected_candidate\n            FROM execution_attempts e JOIN download_files f ON f.id=e.artifact_id\n            LEFT JOIN execution_attempt_provenance p ON p.execution_attempt_id=e.id\n            WHERE p.execution_attempt_id IS NULL\n            ORDER BY e.transfer_id,e.artifact_id,e.created_at,e.id""")\n        for row in execution_rows:\n            candidate = None\n            if row.get("candidate"):\n                try:\n                    candidate = codec.candidate(codec.load(row["candidate"]))\n                except (TypeError, ValueError, KeyError):\n                    candidate = None\n            if candidate is None and row.get("current_execution_id") == row["id"] and row.get("artifact_candidates"):\n                try:\n                    candidates = [codec.candidate(value) for value in codec.load(row["artifact_candidates"], [])]\n                    selected = int(row.get("selected_candidate") or 0)\n                    candidate = candidates[selected] if 0 <= selected < len(candidates) else None\n                except (TypeError, ValueError, KeyError, IndexError):\n                    candidate = None\n            provider_id = str(candidate.provider_id) if candidate and candidate.provider_id else None\n            route_attempt_id = await cls._candidate_route(db, row["transfer_id"], candidate) if candidate else None\n            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM execution_attempt_provenance WHERE artifact_id=?", (row["artifact_id"],))\n            ordinal = int(ordinal_row["n"] or 0) + 1\n            delivered = bool(provider_id and row["state"] == "succeeded" and row.get("artifact_status") == "completed" and row.get("current_execution_id") == row["id"])\n            await db.execute("""INSERT OR IGNORE INTO execution_attempt_provenance(\n                execution_attempt_id,route_attempt_id,transfer_id,artifact_id,ordinal,provider_id,candidate_id,candidate_source,\n                outcome,delivered,history_quality) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",\n                (row["id"], route_attempt_id, row["transfer_id"], row["artifact_id"], ordinal, provider_id,\n                 str(candidate.id) if candidate else None, codec.dump(cls._safe_candidate_source(candidate)) if candidate else None,\n                 "completed" if delivered else cls._execution_outcome(row["state"]), int(delivered),\n                 "legacy_known" if provider_id else "legacy_unknown"))\n            if delivered and route_attempt_id:\n                await db.execute("UPDATE route_attempt_provenance SET outcome='completed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (route_attempt_id,))\n\n    @classmethod\n    async def _begin_route_provenance(cls, db, attempt_id, transfer_id, request_id, provider_id, *, operation):\n        previous = await db.fetchone("""SELECT a.id,a.provider_id,a.error,p.ordinal,p.outcome\n            FROM resolution_attempts a JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id\n            WHERE a.request_id=? AND a.id!=? ORDER BY p.ordinal DESC LIMIT 1""", (request_id, attempt_id))\n        ordinal = int(previous["ordinal"] or 0) + 1 if previous else 1\n        previous_id = previous["id"] if previous else None\n        transition_kind = None\n        transition_reason = None\n        if previous:\n            if operation == "refresh":\n                transition_kind = "candidate_refresh"\n                transition_reason = "candidate_refresh"\n            elif previous["provider_id"] != provider_id:\n                transition_kind = "provider_change"\n                transition_reason = "route_reselected"\n            else:\n                transition_kind = "resolution_retry"\n                transition_reason = "retry"\n            error = codec.error(previous.get("error"))\n            if error is not None:\n                transition_reason = str(error.category.value)\n            if previous.get("outcome") in {"started", "resolved", "unknown"}:\n                await db.execute("UPDATE route_attempt_provenance SET outcome='superseded',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (previous_id,))\n        await db.execute("""INSERT INTO route_attempt_provenance(\n            resolution_attempt_id,transfer_id,request_id,ordinal,operation,previous_attempt_id,transition_kind,transition_reason,\n            candidate_summary,outcome,history_quality) VALUES(?,?,?,?,?,?,?,?,?,'started','recorded')""",\n            (attempt_id, transfer_id, request_id, ordinal, operation, previous_id, transition_kind, transition_reason, codec.dump([])))\n\n    @staticmethod\n    def _transfer(row) -> Transfer | None:\n''',
    "provenance helpers",
)

replace_once(
    repo,
    '''            await db.execute("UPDATE transfer_requests SET state='resolving',attempts=attempts+1 WHERE id=?", (request_id,))\n            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')", (identity, request_id, provider_id))\n            await db.commit()\n''',
    '''            await db.execute("UPDATE transfer_requests SET state='resolving',attempts=attempts+1 WHERE id=?", (request_id,))\n            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')", (identity, request_id, provider_id))\n            await self._begin_route_provenance(db, identity, row["transfer_id"], request_id, provider_id, operation="resolve")\n            await db.commit()\n''',
    "begin resolution provenance",
)

replace_once(
    repo,
    '''            await db.execute("UPDATE resolution_attempts SET state=?,error=?,result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, error, codec.dump(result), attempt.id))\n            resource = codec.dump(result.observation.resource) if result.observation else None\n''',
    '''            await db.execute("UPDATE resolution_attempts SET state=?,error=?,result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, error, codec.dump(result), attempt.id))\n            await db.execute("""UPDATE route_attempt_provenance SET outcome=?,candidate_summary=?,updated_at=CURRENT_TIMESTAMP\n                WHERE resolution_attempt_id=?""",\n                ("failed" if result.error else "resolved", self._candidate_summary(result.candidates), attempt.id))\n            resource = codec.dump(result.observation.resource) if result.observation else None\n''',
    "resolution outcome provenance",
)

replace_once(
    repo,
    '''    async def request_failure(self, request_id: str, error: NormalizedError, retry_at: float | None, *, retry_state="pending", consume_attempt=False) -> None:\n        async with get_db() as db:\n            await db.execute("""UPDATE transfer_requests SET state=?,error=?,retry_at=?,attempts=attempts+? WHERE id=?\n                AND transfer_id IN (SELECT id FROM torrents WHERE status!='deleted')""",\n                (retry_state if retry_at is not None else "failed", codec.dump(error), retry_at or 0, int(consume_attempt), request_id))\n            await db.commit()\n''',
    '''    async def request_failure(self, request_id: str, error: NormalizedError, retry_at: float | None, *, retry_state="pending", consume_attempt=False) -> None:\n        async with get_db() as db:\n            await db.execute("BEGIN IMMEDIATE")\n            error_blob = codec.dump(error)\n            started = await db.fetchall("SELECT id FROM resolution_attempts WHERE request_id=? AND state='started'", (request_id,))\n            for item in started:\n                await db.execute("UPDATE resolution_attempts SET state='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (error_blob, item["id"]))\n                await db.execute("UPDATE route_attempt_provenance SET outcome='failed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (item["id"],))\n            await db.execute("""UPDATE transfer_requests SET state=?,error=?,retry_at=?,attempts=attempts+? WHERE id=?\n                AND transfer_id IN (SELECT id FROM torrents WHERE status!='deleted')""",\n                (retry_state if retry_at is not None else "failed", error_blob, retry_at or 0, int(consume_attempt), request_id))\n            await db.commit()\n''',
    "interrupted resolution outcome",
)

replace_once(
    repo,
    '''    async def prepare_execution(self, artifact: Artifact, handle: ExecutionHandle, *, from_input_required: bool = False) -> bool:\n        async with get_db() as db:\n            await db.execute("BEGIN IMMEDIATE")\n            row = await db.fetchone("""SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id\n                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id\n                WHERE f.id=? AND f.status=? AND f.execution_attempt_id IS NULL\n                AND t.status NOT IN ('deleted','completed','cancelled') AND COALESCE(p.paused,0)=0""",\n                (artifact.id, "input_required" if from_input_required else "queued"))\n            if not row:\n                return False\n            await db.execute("""INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state,candidate)\n                VALUES(?,?,?,?,?,'prepared',?)""", (handle.attempt_id, artifact.transfer_id, artifact.id, handle.executor_id, codec.dump(handle),\n                codec.dump(artifact.candidates[artifact.selected]) if artifact.candidates else None))\n            await db.execute("""UPDATE download_files SET execution_attempt_id=?,download_client=?,retry_count=retry_count+1,\n                status=CASE WHEN ? THEN 'queued' ELSE status END,normalized_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",\n                (handle.attempt_id, handle.executor_id, int(from_input_required), artifact.id))\n            await db.commit()\n        return True\n''',
    '''    async def prepare_execution(self, artifact: Artifact, handle: ExecutionHandle, *, from_input_required: bool = False) -> bool:\n        async with get_db() as db:\n            await db.execute("BEGIN IMMEDIATE")\n            row = await db.fetchone("""SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id\n                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id\n                WHERE f.id=? AND f.status=? AND f.execution_attempt_id IS NULL\n                AND t.status NOT IN ('deleted','completed','cancelled') AND COALESCE(p.paused,0)=0""",\n                (artifact.id, "input_required" if from_input_required else "queued"))\n            if not row:\n                return False\n            candidate = artifact.candidates[artifact.selected] if artifact.candidates else None\n            route_attempt_id = await self._candidate_route(db, artifact.transfer_id, candidate)\n            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM execution_attempt_provenance WHERE artifact_id=?", (artifact.id,))\n            ordinal = int(ordinal_row["n"] or 0) + 1\n            await db.execute("""INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state,candidate)\n                VALUES(?,?,?,?,?,'prepared',?)""", (handle.attempt_id, artifact.transfer_id, artifact.id, handle.executor_id, codec.dump(handle),\n                codec.dump(candidate) if candidate else None))\n            await db.execute("""INSERT INTO execution_attempt_provenance(\n                execution_attempt_id,route_attempt_id,transfer_id,artifact_id,ordinal,provider_id,candidate_id,candidate_source,\n                outcome,delivered,history_quality) VALUES(?,?,?,?,?,?,?,?, 'prepared',0,'recorded')""",\n                (handle.attempt_id, route_attempt_id, artifact.transfer_id, artifact.id, ordinal,\n                 candidate.provider_id if candidate and candidate.provider_id else None, str(candidate.id) if candidate else None,\n                 codec.dump(self._safe_candidate_source(candidate)) if candidate else None))\n            await db.execute("""UPDATE download_files SET execution_attempt_id=?,download_client=?,retry_count=retry_count+1,\n                status=CASE WHEN ? THEN 'queued' ELSE status END,normalized_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",\n                (handle.attempt_id, handle.executor_id, int(from_input_required), artifact.id))\n            await db.commit()\n        return True\n''',
    "execution provenance creation",
)

replace_once(
    repo,
    '''            await db.execute("""UPDATE execution_attempts SET state=?,progress=?,error=?,authorized=CASE WHEN ? THEN 0 ELSE authorized END,\n                updated_at=CURRENT_TIMESTAMP WHERE id=?""", (observation.state, codec.dump(observation.progress), error, revoked, handle.attempt_id))\n            states = {ExecutionState.TRANSFERRING: "downloading", ExecutionState.QUEUED: "queued", ExecutionState.PAUSED: "paused",\n''',
    '''            await db.execute("""UPDATE execution_attempts SET state=?,progress=?,error=?,authorized=CASE WHEN ? THEN 0 ELSE authorized END,\n                updated_at=CURRENT_TIMESTAMP WHERE id=?""", (observation.state, codec.dump(observation.progress), error, revoked, handle.attempt_id))\n            await db.execute("UPDATE execution_attempt_provenance SET outcome=?,updated_at=CURRENT_TIMESTAMP WHERE execution_attempt_id=?",\n                             (self._execution_outcome(observation.state), handle.attempt_id))\n            states = {ExecutionState.TRANSFERRING: "downloading", ExecutionState.QUEUED: "queued", ExecutionState.PAUSED: "paused",\n''',
    "execution outcome provenance",
)

replace_once(
    repo,
    '''    async def artifact_state(self, artifact_id: int, state: str, *, error=None, retry_at=0, release=False, selected=None, expected_bytes=None):\n        async with get_db() as db:\n            await db.execute("""UPDATE download_files SET status=?,normalized_error=?,retry_at=?,\n                execution_attempt_id=CASE WHEN ? THEN NULL ELSE execution_attempt_id END,\n                selected_candidate=COALESCE(?,selected_candidate),size_bytes=COALESCE(?,size_bytes),updated_at=CURRENT_TIMESTAMP\n                WHERE id=? AND torrent_id IN (SELECT id FROM torrents WHERE status!='deleted')""",\n                (state, codec.dump(error) if error else None, retry_at, release, selected, expected_bytes, artifact_id))\n            await db.commit()\n''',
    '''    async def artifact_state(self, artifact_id: int, state: str, *, error=None, retry_at=0, release=False, selected=None, expected_bytes=None):\n        async with get_db() as db:\n            await db.execute("BEGIN IMMEDIATE")\n            current = await db.fetchone("SELECT execution_attempt_id FROM download_files WHERE id=?", (artifact_id,))\n            cursor = await db.execute("""UPDATE download_files SET status=?,normalized_error=?,retry_at=?,\n                execution_attempt_id=CASE WHEN ? THEN NULL ELSE execution_attempt_id END,\n                selected_candidate=COALESCE(?,selected_candidate),size_bytes=COALESCE(?,size_bytes),updated_at=CURRENT_TIMESTAMP\n                WHERE id=? AND torrent_id IN (SELECT id FROM torrents WHERE status!='deleted')""",\n                (state, codec.dump(error) if error else None, retry_at, release, selected, expected_bytes, artifact_id))\n            if cursor.rowcount and state == "completed" and current and current.get("execution_attempt_id"):\n                execution_id = current["execution_attempt_id"]\n                await db.execute("""UPDATE execution_attempt_provenance SET delivered=1,outcome='completed',updated_at=CURRENT_TIMESTAMP\n                    WHERE execution_attempt_id=?""", (execution_id,))\n                route = await db.fetchone("SELECT route_attempt_id FROM execution_attempt_provenance WHERE execution_attempt_id=?", (execution_id,))\n                if route and route.get("route_attempt_id"):\n                    await db.execute("UPDATE route_attempt_provenance SET outcome='completed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?",\n                                     (route["route_attempt_id"],))\n            await db.commit()\n''',
    "artifact delivery provenance",
)

replace_once(
    repo,
    '''            providers = await db.fetchall("SELECT DISTINCT a.provider_id FROM resolution_attempts a JOIN transfer_requests r ON r.id=a.request_id WHERE r.transfer_id=?", (transfer_id,))\n            events = await db.fetchall("SELECT id,torrent_id,level,message,created_at FROM events WHERE torrent_id=? ORDER BY id DESC LIMIT 50", (transfer_id,)) if details else []\n            input_challenge = await db.fetchone("SELECT * FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))\n''',
    '''            providers = await db.fetchall("SELECT DISTINCT a.provider_id FROM resolution_attempts a JOIN transfer_requests r ON r.id=a.request_id WHERE r.transfer_id=?", (transfer_id,))\n            route_attempts = await db.fetchall("""SELECT p.resolution_attempt_id AS id,p.request_id,p.ordinal,p.operation,p.previous_attempt_id,\n                p.transition_kind,p.transition_reason,p.candidate_summary,p.outcome,p.history_quality,a.provider_id,\n                a.state AS resolution_state,a.created_at,a.updated_at FROM route_attempt_provenance p\n                JOIN resolution_attempts a ON a.id=p.resolution_attempt_id WHERE p.transfer_id=?\n                ORDER BY p.created_at,p.request_id,p.ordinal,p.resolution_attempt_id""", (transfer_id,))\n            execution_history = await db.fetchall("""SELECT e.id,e.artifact_id,e.executor_id,e.state AS execution_state,e.created_at,e.updated_at,\n                p.route_attempt_id,p.provider_id,p.candidate_id,p.candidate_source,p.ordinal,p.outcome,p.delivered,p.history_quality\n                FROM execution_attempt_provenance p JOIN execution_attempts e ON e.id=p.execution_attempt_id\n                WHERE p.transfer_id=? ORDER BY p.created_at,p.artifact_id,p.ordinal,p.execution_attempt_id""", (transfer_id,))\n            events = await db.fetchall("SELECT id,torrent_id,level,message,created_at FROM events WHERE torrent_id=? ORDER BY id DESC LIMIT 50", (transfer_id,)) if details else []\n            input_challenge = await db.fetchone("SELECT * FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))\n''',
    "presentation provenance queries",
)

replace_once(
    repo,
    '''        result["resources"] = [dict(item) for item in resources]\n        result["providers"] = sorted({item["provider_id"] for item in (*resources, *providers)})\n        result["executors"] = sorted({item["download_client"] for item in files if item["download_client"]})\n        result["input_required"] = public_challenge(input_challenge)\n''',
    '''        result["resources"] = [dict(item) for item in resources]\n        historical_providers = sorted({item["provider_id"] for item in (*resources, *providers) if item.get("provider_id")})\n        delivering_providers = sorted({item["provider_id"] for item in execution_history if item.get("delivered") and item.get("provider_id")})\n        result["historical_providers"] = historical_providers\n        result["delivering_provider_ids"] = delivering_providers\n        result["delivering_provider_id"] = delivering_providers[0] if len(delivering_providers) == 1 else None\n        result["provider_provenance_status"] = "recorded" if delivering_providers else "unknown_legacy" if result["status"] == "completed" else "pending"\n        result["providers"] = delivering_providers if result["status"] == "completed" and delivering_providers else historical_providers\n        result["executors"] = sorted({item["download_client"] for item in files if item["download_client"]})\n        result["input_required"] = public_challenge(input_challenge)\n''',
    "presentation final provider projection",
)

replace_once(
    repo,
    '''            result["events"] = [dict(item) for item in events]\n        return result\n''',
    '''            result["route_attempts"] = []\n            for row in route_attempts:\n                item = dict(row)\n                item["candidates"] = codec.load(item.pop("candidate_summary"), [])\n                result["route_attempts"].append(item)\n            result["execution_attempts"] = []\n            for row in execution_history:\n                item = dict(row)\n                item["candidate_source"] = codec.load(item.get("candidate_source"), None)\n                item["delivered"] = bool(item.get("delivered"))\n                result["execution_attempts"].append(item)\n            result["events"] = [dict(item) for item in events]\n        return result\n''',
    "presentation history payload",
)

replace_once(
    repo,
    '''    async def begin_refresh(self, record: RequestRecord, provider_id: str):\n        identity = new_identity()\n        async with get_db() as db:\n            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')",\n                             (identity, record.id, provider_id))\n            await db.commit()\n        return ResolutionAttempt(identity, record.id, provider_id, "started")\n\n    async def resolved_candidates(self, request_id: str):\n        async with get_db() as db:\n            row = await db.fetchone("SELECT result FROM resolution_attempts WHERE request_id=? AND state='succeeded' ORDER BY rowid DESC LIMIT 1", (request_id,))\n''',
    '''    async def begin_refresh(self, record: RequestRecord, provider_id: str):\n        identity = new_identity()\n        async with get_db() as db:\n            await db.execute("BEGIN IMMEDIATE")\n            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')",\n                             (identity, record.id, provider_id))\n            await self._begin_route_provenance(db, identity, record.transfer_id, record.id, provider_id, operation="refresh")\n            await db.commit()\n        return ResolutionAttempt(identity, record.id, provider_id, "started")\n\n    async def resolved_candidates(self, request_id: str):\n        async with get_db() as db:\n            row = await db.fetchone("""SELECT a.result FROM resolution_attempts a\n                LEFT JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id\n                WHERE a.request_id=? AND a.state='succeeded'\n                ORDER BY COALESCE(p.ordinal,0) DESC,a.updated_at DESC,a.id DESC LIMIT 1""", (request_id,))\n''',
    "refresh provenance and deterministic resolution order",
)

migration = ROOT / "backend/db/migrations/v112.py"
replace_once(
    migration,
    '''        await db.execute("""INSERT INTO transfer_controls(key,value) VALUES('paused',?)\n            ON CONFLICT(key) DO NOTHING""", ("1" if globally_paused else "0",))\n        violations = await db.fetchall("PRAGMA foreign_key_check")\n''',
    '''        await db.execute("""INSERT INTO transfer_controls(key,value) VALUES('paused',?)\n            ON CONFLICT(key) DO NOTHING""", ("1" if globally_paused else "0",))\n        # Item 9 backfills only provider/candidate/execution facts already present\n        # in canonical migration output; it never classifies legacy URLs.\n        await repository._backfill_provenance(db)\n        violations = await db.fetchall("PRAGMA foreign_key_check")\n''',
    "v112 provenance backfill",
)

stage5 = ROOT / "backend/tests/test_general_http_stage5_runtime.py"
replace_once(
    stage5,
    '''        assert presentation["providers"] == ["general_http"]\n        assert presentation["executors"] == ["aria2"]\n        assert state["authorized"] == 1\n''',
    '''        assert presentation["providers"] == ["general_http"]\n        assert presentation["delivering_provider_id"] == "general_http"\n        assert presentation["route_attempts"][0]["provider_id"] == "general_http"\n        assert presentation["execution_attempts"][0]["provider_id"] == "general_http"\n        assert presentation["execution_attempts"][0]["delivered"] is True\n        assert presentation["executors"] == ["aria2"]\n        assert state["authorized"] == 1\n''',
    "real general HTTP provenance assertions",
)

TEST = r'''"""Roadmap Item 9 durable route/provider provenance acceptance tests."""
from __future__ import annotations

from dataclasses import replace

import pytest

import db.database as database
from providers.alldebrid.provider import AllDebridProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers import codec
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import (
    Endpoint,
    ExecutionHandle,
    ExecutionObservation,
    ExecutionState,
    ResolutionResult,
    ResourceState,
    TransferCandidate,
    TransferProgress,
    TransferRequest,
)
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio


async def _repository(tmp_path, monkeypatch, name="provenance.sqlite3"):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    await database.init_db()
    repository = TransferRepository()
    await repository.initialize()
    return repository


async def _admit(repository, request):
    transfer, created = await repository.admit((request,), name=request.name or "fixture", deduplicate=False)
    assert created
    return transfer, (await repository.requests(transfer.id))[0]


def _candidate(provider_id, identity, *, secret=""):
    address = f"https://download.example/{identity}"
    if secret:
        address += f"?token={secret}"
    return TransferCandidate(
        name=f"{identity}.bin",
        endpoints=(Endpoint("https", address),),
        expected_bytes=8,
        provider_id=provider_id,
        id=identity,
    )


async def _resolve(repository, record, provider_id, candidates=(), *, error=None):
    attempt = await repository.begin_resolution(record.id, provider_id)
    assert attempt is not None
    result = ResolutionResult(ResourceState.UNKNOWN if error else ResourceState.AVAILABLE, tuple(candidates), error=error)
    await repository.resolution(attempt, result)
    return attempt


async def _materialize_and_execute(repository, record, candidate, *, attempt_id="exec-1", succeed=True):
    artifact = await repository.materialize(record, (candidate,), f"/tmp/{candidate.name}")
    assert artifact is not None
    handle = ExecutionHandle("fixture_executor", {}, attempt_id=attempt_id)
    assert await repository.prepare_execution(artifact, handle)
    observation = ExecutionObservation(
        handle,
        ExecutionState.SUCCEEDED if succeed else ExecutionState.FAILED,
        TransferProgress(total_bytes=8, completed_bytes=8 if succeed else 2),
        error=None if succeed else NormalizedError(Domain.EXECUTOR, Category.EXECUTION_FAILED, Stage.EXECUTION),
    )
    await repository.execution(observation)
    if succeed:
        await repository.artifact_state(artifact.id, "completed", expected_bytes=8)
    else:
        await repository.artifact_state(artifact.id, "error", error=observation.error)
    return artifact, handle


async def _force_completed(transfer_id):
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='completed',progress=100,completed_at=CURRENT_TIMESTAMP WHERE id=?", (transfer_id,))
        await db.commit()


async def test_provider_a_failure_provider_b_delivery_is_append_only_and_restart_durable(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch)
    transfer, record = await _admit(repository, TransferRequest("https", "https://shared.example/file", name="route.bin"))
    failed = NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION, retryability=Retryability.NEVER)
    attempt_a = await _resolve(repository, record, "provider_a", error=failed)
    await repository.retry_requests(transfer.id, request_id=record.id)
    record = (await repository.requests(transfer.id))[0]
    candidate_b = _candidate("provider_b", "candidate-b", secret="signed-secret-sentinel")
    attempt_b = await _resolve(repository, record, "provider_b", (candidate_b,))
    record = (await repository.requests(transfer.id))[0]
    _, execution_b = await _materialize_and_execute(repository, record, candidate_b, attempt_id="execution-b")
    await _force_completed(transfer.id)

    presentation = await repository.presentation(transfer.id, details=True)
    assert presentation["delivering_provider_id"] == "provider_b"
    assert presentation["providers"] == ["provider_b"]
    assert presentation["historical_providers"] == ["provider_a", "provider_b"]
    assert [item["id"] for item in presentation["route_attempts"]] == [attempt_a.id, attempt_b.id]
    assert presentation["route_attempts"][0]["outcome"] == "failed"
    assert presentation["route_attempts"][1]["outcome"] == "completed"
    assert presentation["route_attempts"][1]["previous_attempt_id"] == attempt_a.id
    assert presentation["route_attempts"][1]["transition_kind"] == "provider_change"
    assert presentation["execution_attempts"][0]["id"] == execution_b.attempt_id
    assert presentation["execution_attempts"][0]["route_attempt_id"] == attempt_b.id
    assert presentation["execution_attempts"][0]["provider_id"] == "provider_b"
    assert presentation["execution_attempts"][0]["candidate_id"] == candidate_b.id
    assert presentation["execution_attempts"][0]["delivered"] is True

    serialized = codec.dump({"routes": presentation["route_attempts"], "executions": presentation["execution_attempts"]})
    assert "signed-secret-sentinel" not in serialized
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT candidate_summary FROM route_attempt_provenance WHERE transfer_id=?", (transfer.id,))
        executions = await db.fetchall("SELECT candidate_source FROM execution_attempt_provenance WHERE transfer_id=?", (transfer.id,))
    assert "signed-secret-sentinel" not in codec.dump({"routes": rows, "executions": executions})

    restarted = TransferRepository()
    await restarted.initialize()
    after_restart = await restarted.presentation(transfer.id, details=True)
    assert after_restart["delivering_provider_id"] == "provider_b"
    assert [item["id"] for item in after_restart["route_attempts"]] == [attempt_a.id, attempt_b.id]
    assert after_restart["route_attempts"][0]["outcome"] == "failed"


async def test_candidate_change_within_provider_is_not_provider_failover(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "candidate.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://example.test/file", name="candidate.bin"))
    first = _candidate("provider_a", "candidate-1")
    second = _candidate("provider_a", "candidate-2")
    route = await _resolve(repository, record, "provider_a", (first, second))
    record = (await repository.requests(transfer.id))[0]
    artifact = await repository.materialize(record, (first, second), "/tmp/candidate.bin")

    handle1 = ExecutionHandle("fixture_executor", {}, attempt_id="candidate-exec-1")
    assert await repository.prepare_execution(artifact, handle1)
    error = NormalizedError(Domain.EXECUTOR, Category.EXECUTION_FAILED, Stage.EXECUTION)
    await repository.execution(ExecutionObservation(handle1, ExecutionState.FAILED, error=error))
    await repository.artifact_state(artifact.id, "queued", release=True, selected=1, expected_bytes=8)
    artifact = (await repository.artifacts(transfer.id))[0]
    handle2 = ExecutionHandle("fixture_executor", {}, attempt_id="candidate-exec-2")
    assert await repository.prepare_execution(artifact, handle2)
    await repository.execution(ExecutionObservation(handle2, ExecutionState.SUCCEEDED, TransferProgress(8, 8)))
    await repository.artifact_state(artifact.id, "completed", expected_bytes=8)

    presentation = await repository.presentation(transfer.id, details=True)
    assert len(presentation["route_attempts"]) == 1
    assert presentation["route_attempts"][0]["id"] == route.id
    history = presentation["execution_attempts"]
    assert [item["candidate_id"] for item in history] == [first.id, second.id]
    assert {item["route_attempt_id"] for item in history} == {route.id}
    assert {item["provider_id"] for item in history} == {"provider_a"}
    assert history[0]["outcome"] == "failed"
    assert history[1]["delivered"] is True


async def test_executor_retry_keeps_same_provider_candidate_route(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "executor-retry.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://example.test/retry", name="retry.bin"))
    candidate = _candidate("provider_a", "same-candidate")
    route = await _resolve(repository, record, "provider_a", (candidate,))
    record = (await repository.requests(transfer.id))[0]
    artifact = await repository.materialize(record, (candidate,), "/tmp/retry.bin")

    first = ExecutionHandle("fixture_executor", {}, attempt_id="retry-exec-1")
    assert await repository.prepare_execution(artifact, first)
    error = NormalizedError(Domain.EXECUTOR, Category.EXECUTION_FAILED, Stage.EXECUTION)
    await repository.execution(ExecutionObservation(first, ExecutionState.FAILED, error=error))
    await repository.artifact_state(artifact.id, "queued", release=True)
    artifact = (await repository.artifacts(transfer.id))[0]
    second = ExecutionHandle("fixture_executor", {}, attempt_id="retry-exec-2")
    assert await repository.prepare_execution(artifact, second)
    await repository.execution(ExecutionObservation(second, ExecutionState.SUCCEEDED, TransferProgress(8, 8)))
    await repository.artifact_state(artifact.id, "completed", expected_bytes=8)

    presentation = await repository.presentation(transfer.id, details=True)
    assert len(presentation["route_attempts"]) == 1
    history = presentation["execution_attempts"]
    assert len(history) == 2
    assert {item["route_attempt_id"] for item in history} == {route.id}
    assert {item["candidate_id"] for item in history} == {candidate.id}
    assert presentation["delivering_provider_id"] == "provider_a"


async def test_item8_style_rows_backfill_known_facts_idempotently_without_url_inference(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "migration.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://rapidgator.net/looks-specialized", name="legacy.bin"))
    candidate = _candidate("durably_known_provider", "legacy-candidate")
    result = ResolutionResult(ResourceState.AVAILABLE, (candidate,))
    async with database.get_db() as db:
        await db.execute("DROP TABLE execution_attempt_provenance")
        await db.execute("DROP TABLE route_attempt_provenance")
        await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state,result) VALUES('legacy-route',?,?, 'succeeded',?)", (record.id, "durably_known_provider", codec.dump(result)))
        file_id = await db.execute_returning_id("""INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,candidates,selected_candidate,execution_attempt_id,download_client)\n            VALUES(?,?,?,8,'/tmp/legacy.bin','completed',?,0,'legacy-execution','fixture_executor')""", (transfer.id, record.id, "legacy.bin", codec.dump((candidate,))))
        handle = ExecutionHandle("fixture_executor", {}, attempt_id="legacy-execution")
        await db.execute("""INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state,candidate)\n            VALUES('legacy-execution',?,?, 'fixture_executor',?,'succeeded',?)""", (transfer.id, file_id, codec.dump(handle), codec.dump(candidate)))
        await db.execute("UPDATE torrents SET status='completed' WHERE id=?", (transfer.id,))
        await db.commit()

    migrated = TransferRepository()
    await migrated.initialize()
    first = await migrated.presentation(transfer.id, details=True)
    assert first["delivering_provider_id"] == "durably_known_provider"
    assert first["route_attempts"][0]["history_quality"] == "legacy_known"
    assert first["execution_attempts"][0]["history_quality"] == "legacy_known"
    assert first["execution_attempts"][0]["route_attempt_id"] == "legacy-route"

    await migrated.initialize()
    async with database.get_db() as db:
        route_count = (await db.fetchone("SELECT COUNT(*) AS n FROM route_attempt_provenance"))["n"]
        execution_count = (await db.fetchone("SELECT COUNT(*) AS n FROM execution_attempt_provenance"))["n"]
    assert route_count == 1
    assert execution_count == 1

    unknown, _ = await _admit(migrated, TransferRequest("https", "https://rapidgator.net/no-proof", name="unknown.bin"))
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='completed' WHERE id=?", (unknown.id,))
        await db.commit()
    unknown_presentation = await migrated.presentation(unknown.id, details=True)
    assert unknown_presentation["delivering_provider_id"] is None
    assert unknown_presentation["provider_provenance_status"] == "unknown_legacy"
    assert "alldebrid" not in unknown_presentation["providers"]


async def test_general_http_provider_identity_is_persisted_at_route_time(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "general-http.sqlite3")
    provider = GeneralHttpProvider()
    request = TransferRequest("https", "https://downloads.example/file.bin?capability=secret", name="file.bin")
    transfer, record = await _admit(repository, request)
    attempt = await repository.begin_resolution(record.id, provider.descriptor.id)
    result = await provider.resolve(request)
    await repository.resolution(attempt, result)
    record = (await repository.requests(transfer.id))[0]
    await _materialize_and_execute(repository, record, result.candidates[0], attempt_id="general-http-execution")
    presentation = await repository.presentation(transfer.id, details=True)
    assert presentation["delivering_provider_id"] == "general_http"
    assert presentation["route_attempts"][0]["provider_id"] == "general_http"
    assert presentation["execution_attempts"][0]["provider_id"] == "general_http"
    assert "capability=secret" not in codec.dump(presentation["route_attempts"])


class _AllDebridUnlockClient:
    async def unlock_link(self, _url):
        return {"link": "https://cdn.example/unlocked.bin?signature=provider-secret", "filename": "unlocked.bin", "filesize": 8}


async def test_alldebrid_fixture_route_persists_provider_candidate_and_delivery(tmp_path, monkeypatch):
    import providers.alldebrid.provider as provider_module

    repository = await _repository(tmp_path, monkeypatch, "alldebrid.sqlite3")
    monkeypatch.setattr(provider_module, "validate_provider_download_url", lambda value: value)
    provider = AllDebridProvider(client=_AllDebridUnlockClient())
    request = TransferRequest("https", "https://rapidgator.net/example", name="unlocked.bin")
    transfer, record = await _admit(repository, request)
    attempt = await repository.begin_resolution(record.id, provider.descriptor.id)
    result = await provider.resolve(request)
    await repository.resolution(attempt, result)
    record = (await repository.requests(transfer.id))[0]
    await _materialize_and_execute(repository, record, result.candidates[0], attempt_id="alldebrid-execution")
    presentation = await repository.presentation(transfer.id, details=True)
    assert presentation["delivering_provider_id"] == "alldebrid"
    assert presentation["route_attempts"][0]["provider_id"] == "alldebrid"
    assert presentation["route_attempts"][0]["candidates"][0]["source"] == {"scope": "host", "key": "rapidgator.net"}
    assert presentation["execution_attempts"][0]["provider_id"] == "alldebrid"
    assert "provider-secret" not in codec.dump(presentation["route_attempts"])
'''
(ROOT / "backend/tests/test_route_provider_provenance.py").write_text(TEST)

DOC = r'''# Durable Route & Provider Provenance

Roadmap Item 9 makes provider/acquisition history a canonical durable fact without changing logical transfer identity or adding automatic cross-provider failover policy.

## Identity and ownership

- `torrents.id` remains the logical transfer identity. Provider identity is never embedded in it.
- `resolution_attempts.id` remains the provider/resolution attempt identity. `route_attempt_provenance` adds deterministic per-request ordering, transition linkage, normalized transition reason, safe candidate identities, and acquisition-route outcome.
- `execution_attempts.id` remains executor-attempt identity. `execution_attempt_provenance` links it to the provider route and selected candidate without persisting endpoint capability data in provenance.
- A verified artifact delivery is recorded only when canonical artifact verification marks a file `completed` while an execution attempt owns it. Partial bytes do not establish delivery.

## Historical truth

Provider identity is persisted when routing/acquisition occurs and is never reconstructed later from the submitted URL, current provider enablement, current applicability, or current AllDebrid host state. Pre-Item-9 rows are backfilled only from durable resolution/candidate/execution facts. If a provider cannot be proven, provenance remains unknown rather than being guessed.

A provider transition is represented as:

```text
Logical Transfer
  -> Provider A route attempt -- failed/superseded
  -> Provider B route attempt -- completed
```

Both attempts retain the same logical transfer identity. Candidate changes within one route and executor retries beneath one candidate remain distinct from provider changes.

## Safe candidate provenance

Provenance stores candidate IDs, stable provider IDs, candidate ordering, and safe `SourceIdentity` metadata when supplied. It does not copy endpoint URLs, headers, signed query strings, credentials, API keys, authentication challenges, or provider-native payloads into the provenance tables/API history.

## Delivering provider

The completed-provider projection is derived from the execution attempt that passed canonical artifact verification. It is not the first provider, latest enabled provider, current classifier winner, or a hostname guess. Historical route attempts remain available through transfer detail even when the summary projects only the delivering provider.

## Scope

Item 9 does not add general automatic cross-provider failover policy and does not add the later provenance timeline/dashboard/badge/filter UI. It makes those later capabilities safe because their data source is durable history rather than reconstruction.
'''
(ROOT / "docs/ROUTE_PROVIDER_PROVENANCE.md").write_text(DOC)

append_once(
    ROOT / "docs/UNIVERSAL_TRANSFER_CORE.md",
    "## Roadmap Item 9: durable route/provider provenance",
    '''## Roadmap Item 9: durable route/provider provenance

Provider/resolution attempts and executor attempts now have durable provider-neutral provenance links. Historical provider identity is captured at route time, candidate identity is recorded without endpoint secrets, verified artifact delivery identifies the actual delivering execution/provider, and current routing/applicability state is never used to rewrite history. See `ROUTE_PROVIDER_PROVENANCE.md`.''',
)

append_once(
    ROOT / "CHANGELOG.md",
    "Durable route/provider provenance (Roadmap Item 9)",
    '''### Durable route/provider provenance (Roadmap Item 9)

- Persist provider-route ordering, transitions, candidate identity, executor linkage, normalized outcomes, and verified delivering-provider provenance under the existing logical transfer identity.
- Preserve legacy unknown provenance without URL/classifier inference and expose safe historical route/execution attempts through transfer detail.
- Keep automatic cross-provider failover policy and the full provenance UI deferred.''',
)

print("Stage 9 patch applied")
