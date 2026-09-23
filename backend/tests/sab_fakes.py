"""Deterministic in-memory stand-in for the SABnzbd HTTP API.

Models exactly the semantics characterized against a real SABnzbd 5.1.3 at
Gate 1: client-supplied ``nzbname`` persists as queue ``filename`` and history
``name``; submission returns a server-minted ``nzo_id``; duplicate submission
creates a SECOND independent job; a control acknowledgement is NOT truth; a
transport failure is distinguishable from a valid "absent" answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import uuid


# The fake models the REAL client contract, including its two failure classes.
from executors.sabnzbd.client import SabApiError, SabTransportError  # noqa: F401


@dataclass
class FakeJob:
    nzo_id: str
    name: str
    status: str = "Downloading"
    mb: float = 1.0
    mbleft: float = 1.0
    storage: str = ""
    path: str = ""
    fail_message: str = ""
    bytes: int = 0
    pp: str = "R"

    def slot(self) -> dict:
        """Exactly the shape SAB's JSON API returns for a queue/history slot."""
        return {
            "nzo_id": self.nzo_id, "name": self.name, "filename": self.name,
            "status": self.status, "mb": f"{self.mb:.2f}", "mbleft": f"{self.mbleft:.2f}",
            "storage": self.storage, "path": self.path,
            "fail_message": self.fail_message, "bytes": self.bytes, "pp": self.pp,
        }


@dataclass
class FakeSab:
    reachable: bool = True
    authorized: bool = True
    queue: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    submissions: list = field(default_factory=list)
    complete_dir: str = "/download/.dpwork/complete"
    download_dir: str = "/download/.dpwork/incomplete"
    servers: dict = field(default_factory=dict)
    drop_next_response: bool = False
    speedlimit_abs: int = 0
    bandwidth_max: str = ""
    # Executor-wide acquisition tuning, with the native defaults and the native
    # clamping characterized against SABnzbd 5.1.3.
    cache_limit: str = "1G"
    direct_write: int = 1
    max_art_tries: int = 3
    # The one service-wide download meter SAB publishes (queue.kbpersec). There
    # is deliberately no per-slot rate: the real service has none.
    download_bytes_per_second: int = 0
    # When set, a bulk snapshot reports FEWER slots than exist, exactly as a
    # paginated SAB listing would. Absence must never be inferred from one.
    truncate_queue_to: int | None = None
    truncate_history_to: int | None = None
    # Models the native refusal to move working paths while it is busy.
    refuse_path_changes: bool = False

    # --- transport -------------------------------------------------------
    def _guard(self):
        if not self.reachable:
            raise SabTransportError("connection refused")
        if not self.authorized:
            raise SabApiError("API Key Incorrect")

    # --- operations ------------------------------------------------------
    async def version(self):
        self._guard()
        return "5.1.3"

    async def addfile(self, data: bytes, *, nzbname: str, pp: int, priority: int = -100):
        self._guard()
        job = FakeJob(uuid.uuid4().hex, nzbname, pp=("R" if pp == 1 else str(pp)))
        # Real SAB has NO dedupe: a repeat submission is a second job.
        self.queue[job.nzo_id] = job
        self.submissions.append((nzbname, bytes(data)))
        if self.drop_next_response:
            self.drop_next_response = False
            raise SabTransportError("response lost after submission reached SAB")
        return job.nzo_id

    async def queue_snapshot(self, limit: int = 500):
        from executors.sabnzbd.client import SabSnapshot
        self._guard()
        slots = [j.slot() for j in self.queue.values()]
        total = len(slots)
        if self.truncate_queue_to is not None:
            slots = slots[: self.truncate_queue_to]
        return SabSnapshot(tuple(slots), total <= len(slots))

    async def history_snapshot(self, limit: int = 500):
        from executors.sabnzbd.client import SabSnapshot
        self._guard()
        slots = [j.slot() for j in self.history.values()]
        total = len(slots)
        if self.truncate_history_to is not None:
            slots = slots[: self.truncate_history_to]
        return SabSnapshot(tuple(slots), total <= len(slots))

    async def get_config(self, section: str) -> dict:
        self._guard()
        if section == "misc":
            return {"misc": {
                "download_dir": self.download_dir, "complete_dir": self.complete_dir,
                "cache_limit": self.cache_limit,
                # Real SAB answers this one as a JSON bool, not 0/1.
                "direct_write": bool(self.direct_write),
                "max_art_tries": self.max_art_tries,
            }}
        if section == "servers":
            return {"servers": list(self.servers.values())}
        return {}

    # Native per-option clamping, exactly as characterized (values outside the
    # declared range are silently corrected rather than refused).
    _SERVER_CLAMPS = {"timeout": (20, 240), "pipelining_requests": (1, 20),
                      "connections": (0, 500), "priority": (0, 99)}

    async def set_config(self, section: str, keyword: str, **values) -> dict:
        self._guard()
        if section == "misc":
            if self.refuse_path_changes and keyword in ("download_dir", "complete_dir"):
                return {"misc": {keyword: getattr(self, keyword, "")}}
            value = values.get("value", "")
            if keyword == "max_art_tries":
                value = max(2, int(value))          # OptionNumber minval=2
            elif keyword == "direct_write":
                value = 1 if value in (1, True, "1") else 0
            setattr(self, keyword, value)
            return {"misc": {keyword: getattr(self, keyword, "")}}
        entry = dict(self.servers.get(keyword) or {})
        clamped = dict(values)
        for field, (low, high) in self._SERVER_CLAMPS.items():
            if field in clamped:
                clamped[field] = max(low, min(high, int(clamped[field])))
        entry.update({"name": keyword, **clamped})
        self.servers[keyword] = entry
        return {"servers": list(self.servers.values())}

    async def download_throughput(self) -> int:
        """One service-wide instantaneous rate, in bytes/second."""
        self._guard()
        return max(0, int(self.download_bytes_per_second))

    async def del_config(self, section: str, keyword: str):
        self._guard()
        if section == "servers":
            self.servers.pop(keyword, None)
        return {"status": True}

    async def set_speedlimit(self, value: str) -> int:
        """Models SAB 5.1.3: a value resolving to 1..100 is a PERCENTAGE and,
        with no bandwidth_max configured, applies no absolute limit at all."""
        self._guard()
        text = str(value).strip()
        units = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
        if text.endswith(tuple(units)):
            resolved = int(float(text[:-1]) * units[text[-1]])
        elif "%" in text:
            resolved = 0
        else:
            resolved = int(float(text or 0))
        self.speedlimit_abs = 0 if 0 < resolved < 101 else max(0, resolved)
        return self.speedlimit_abs

    async def queue_slots(self, search: str | None = None):
        self._guard()
        return [j.slot() for j in self.queue.values() if search is None or search in j.name]

    async def history_slots(self, search: str | None = None, nzo_id: str | None = None):
        self._guard()
        return [j.slot() for j in self.history.values()
                if (search is None or search in j.name) and (nzo_id is None or j.nzo_id == nzo_id)]

    async def pause(self, nzo_id):
        self._guard()
        job = self.queue.get(nzo_id)
        if job:
            job.status = "Paused"
        return {"status": True, "nzo_ids": [nzo_id]}  # ack even for unknown ids

    async def resume(self, nzo_id):
        self._guard()
        job = self.queue.get(nzo_id)
        if job:
            job.status = "Downloading"
        return {"status": True, "nzo_ids": [nzo_id]}

    async def delete(self, nzo_id):
        self._guard()
        self.queue.pop(nzo_id, None)
        return {"status": True}

    # --- test drivers ----------------------------------------------------
    def finish(self, nzo_id, *, root=None, files=(("payload.bin", 1024),)):
        """Complete a job AND write the repaired payload SAB would have written."""
        import os
        root = root or self.complete_dir
        job = self.queue.pop(nzo_id)
        job.status = "Completed"
        directory = os.path.join(root, job.name)
        os.makedirs(directory, exist_ok=True)
        for name, size in files:
            path = os.path.join(directory, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"\0" * size)
        # SAB renames the payload to the JOB name and reports the FILE path.
        job.storage = os.path.join(directory, files[0][0])
        job.path = os.path.join(self.download_dir, job.name)
        job.bytes = sum(size for _, size in files)
        job.mbleft = 0.0
        self.history[nzo_id] = job
        return job

    def fail(self, nzo_id, message="Download failed - Not on your server(s)"):
        job = self.queue.pop(nzo_id)
        job.status = "Failed"
        job.fail_message = message
        job.storage = f"{self.download_dir}/{job.name}"
        job.bytes = 0
        self.history[nzo_id] = job
        return job
