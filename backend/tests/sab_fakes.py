"""Deterministic in-memory stand-in for the SABnzbd HTTP API.

Models exactly the semantics characterized against a real SABnzbd 5.1.3:

* client-supplied ``nzbname`` becomes the job's ``final_name``; the QUEUE
  publishes it as slot ``filename`` and nothing else -- a real queue slot has
  no ``name`` key, no ``nzb_name`` and no custom metadata -- while the HISTORY
  publishes ``name`` (the same value) plus ``nzb_name`` (the uploaded file's
  name, which is never queue-visible and never searchable);
* queue and history search both match the job name alone;
* ``mode=queue&name=rename`` changes the job name only, immediately, and
  answers ``{"status": false}`` for an id that is not in the queue;
* submission returns a server-minted ``nzo_id``; duplicate submission creates a
  SECOND independent job; a control acknowledgement is NOT truth; a transport
  failure is distinguishable from a valid "absent" answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import tempfile
import uuid


# The fake models the REAL client contract, including its two failure classes.
from executors.sabnzbd.client import (  # noqa: F401
    NATIVE_PRIORITY_PAUSED, SabApiError, SabTransportError,
)
from transfers.staged_input import StagedInputStore

_STAGED_STORE: StagedInputStore | None = None


def staged_store() -> StagedInputStore:
    """The neutral durable-input owner these tests hand to an executor.

    One per test process: a staged reference is only meaningful against the
    store that minted it, so sharing one keeps a candidate built by one helper
    readable by an executor built by another.
    """
    global _STAGED_STORE
    if _STAGED_STORE is None:
        _STAGED_STORE = StagedInputStore(tempfile.mkdtemp(prefix="dp-staged-"))
    return _STAGED_STORE


def staged_context(payload: bytes = b"<nzb/>") -> dict:
    """A candidate context carrying a real durable reference to ``payload``.

    The bytes are genuinely staged, so integrity verification, streaming
    submission and reclamation all behave exactly as they do in production.
    """
    return {"staged_input": staged_store().stage_bytes(payload).as_context()}


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
    # The uploaded multipart file name. Retained by the service, surfaced ONLY
    # in history, and never searchable -- so it can never carry correlation
    # through the queue phase.
    nzb_name: str = ""

    def _common(self) -> dict:
        return {
            "nzo_id": self.nzo_id, "status": self.status,
            "mb": f"{self.mb:.2f}", "mbleft": f"{self.mbleft:.2f}",
            "storage": self.storage, "path": self.path,
            "fail_message": self.fail_message, "bytes": self.bytes, "pp": self.pp,
        }

    def queue_slot(self) -> dict:
        """A real queue slot: the job name appears as ``filename``, alone."""
        return {**self._common(), "filename": self.name}

    def history_slot(self) -> dict:
        """A real history slot: ``name`` plus the uploaded file's ``nzb_name``."""
        return {**self._common(), "name": self.name, "nzb_name": self.nzb_name}

    # Retained for callers that predate the queue/history split.
    def slot(self) -> dict:
        return self.queue_slot()


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
    # Every job-name search the executor performed, in order.
    searches: list = field(default_factory=list)
    # Every (nzo_id, new_name) rename the executor issued, in order.
    renames: list = field(default_factory=list)
    # Makes rename answer ``{"status": false}``, as the real service does for a
    # job that is no longer in the queue.
    refuse_renames: bool = False
    # (nzo_id, job name) for every job the native worker was allowed to run.
    acquired_under_name: list = field(default_factory=list)
    # Models elapsed time inside a lost/ambiguous acknowledgement: the service
    # keeps working while the caller is still waiting for an answer it will
    # never get. This is the condition the submission fence exists to survive,
    # so a test that never exercises it proves nothing.
    worker_runs_during_ambiguity: bool = False

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

    async def addfile(self, data, *, job_name: str, pp: int, priority: int = -100,
                      upload_filename: str = ""):
        self._guard()
        data = data.read() if hasattr(data, "read") else data
        # Priority -2 is "paused on arrival": the real service creates the job
        # but its downloader never picks it up until something resumes it.
        job = FakeJob(uuid.uuid4().hex, job_name, pp=("R" if pp == 1 else str(pp)),
                      status=("Paused" if priority == NATIVE_PRIORITY_PAUSED else "Downloading"),
                      nzb_name=upload_filename or f"{job_name}.nzb")
        # Real SAB has NO dedupe: a repeat submission is a second job.
        self.queue[job.nzo_id] = job
        self.submissions.append((job_name, bytes(data)))
        if self.drop_next_response:
            self.drop_next_response = False
            if self.worker_runs_during_ambiguity:
                # The answer is already lost; the service does not wait for it.
                self.run_native_worker()
            raise SabTransportError("response lost after submission reached SAB")
        return job.nzo_id

    async def rename(self, nzo_id: str, name: str) -> bool:
        """Change a QUEUED job's name. False once it has left the queue."""
        self._guard()
        self.renames.append((nzo_id, name))
        if self.refuse_renames or nzo_id not in self.queue:
            return False
        self.queue[nzo_id].name = name
        return True

    async def queue_snapshot(self, limit: int = 500):
        from executors.sabnzbd.client import SabSnapshot
        self._guard()
        slots = [j.queue_slot() for j in self.queue.values()]
        total = len(slots)
        if self.truncate_queue_to is not None:
            slots = slots[: self.truncate_queue_to]
        return SabSnapshot(tuple(slots), total <= len(slots))

    async def history_snapshot(self, limit: int = 500):
        from executors.sabnzbd.client import SabSnapshot
        self._guard()
        slots = [j.history_slot() for j in self.history.values()]
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
        if search is not None:
            self.searches.append(search)
        return [j.queue_slot() for j in self.queue.values()
                if search is None or search in j.name]

    async def history_slots(self, search: str | None = None, nzo_id: str | None = None):
        self._guard()
        if search is not None:
            self.searches.append(search)
        # Search matches the job name only: ``nzb_name`` is not searchable.
        return [j.history_slot() for j in self.history.values()
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

    # --- the native worker ------------------------------------------------
    def run_native_worker(self, *, member="b082fa0beaa644d3aa01045d5b8d0b36.mp4"):
        """Let the service do what it would do, to every job it may run.

        Models the two behaviours this fence exists to survive: a job that is
        NOT paused makes progress without waiting for DebridPulse, and its
        post-processing renames an obfuscated member to the JOB name -- which
        is SABnzbd 5.1.3's ``deobfuscate(nzo, files, nzo.final_name)``.

        Every run is recorded with the name the job carried at the time, so a
        test can assert that acquisition never happened under a name it must
        never have happened under.
        """
        ran = []
        for nzo_id, job in list(self.queue.items()):
            if job.status == "Paused":
                continue
            self.acquired_under_name.append((nzo_id, job.name))
            extension = member.rsplit(".", 1)[-1]
            self.finish(nzo_id, files=((f"{job.name}.{extension}", 4096), ("rename.par2", 512)))
            ran.append(nzo_id)
        return ran

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
