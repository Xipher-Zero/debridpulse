"""The source icon follows the candidate in use, read as the input it came from.

A transfer's row shows where its content is coming from. When consolidation
brings another transfer's candidate into it, and that candidate is the one in
use, the icon is that candidate's own input: a torrent consolidated into an
HTTP transfer and failed over to (through AllDebrid, whose delivery host is
alldebrid.com) shows the torrent, not a generic link (the 2026-11-16 trace:
transfer 1 an HTTP link; transfer 2 a .torrent consolidated into it, its
candidate contributed by a CHILD request -- the provider's unlocked link --
of transfer 2's torrent root). Both owners agree: the bounded list
(api.operational_downloads) and the detail presentation
(transfers.presentation_repository).

Each scenario runs the real code in its own process, on a fresh database
named by DB_PATH (nothing patched).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

SCENARIO = r'''
import asyncio, json, sys
from types import SimpleNamespace
from db import database

SELECTED, ROOT_ONE, ROOT_TWO = int(sys.argv[1]), sys.argv[2], sys.argv[3]

def request(kind):
    return json.dumps({"kind": kind, "payload": "https://releases.example.org/image.iso" if kind == "https"
                       else ("magnet:?xt=urn:btih:" + "a" * 40 if kind == "magnet" else "dG9ycmVudA=="),
                       "name": "image.iso", "fingerprint": "", "preferred_provider": None})

async def main():
    await database.init_db()
    async with database.get_db() as db:
        await db.execute("INSERT INTO torrents(id,hash,name,status,source) VALUES(1,'h1','image.iso','downloading','direct_link')")
        await db.execute("INSERT INTO torrents(id,hash,name,status,source) VALUES(2,'h2','image.iso','consolidated','manual_file')")
        # Transfer 1's root; transfer 2's root and its child (the provider's unlocked link).
        await db.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES('r1',1,0,?,'resolved')", (request(ROOT_ONE),))
        await db.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES('r2',2,0,?,'resolved')", (request(ROOT_TWO),))
        await db.execute("INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload,state) VALUES('r2c',2,'r2',0,?,'resolved')", (request("https"),))
        await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES('a1','r1','general_http','succeeded')")
        await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES('a2','r2c','alldebrid','succeeded')")
        await db.execute("INSERT INTO download_files(id,torrent_id,status,mirror_state,selected_candidate,size_bytes) VALUES(1,1,'downloading','primary',?,100)", (SELECTED,))
        await db.execute("INSERT INTO download_files(id,torrent_id,status,mirror_state,selected_candidate,size_bytes) VALUES(2,2,'duplicate','standby',0,100)")
        # The canonical artifact's two candidates: its own (HTTP host) and transfer 2's (AllDebrid delivery).
        await db.execute("INSERT INTO canonical_candidate_bindings(id,canonical_artifact_id,candidate_id,provider_id,source_scope,source_key,role,candidate_order) VALUES(1,1,'c1','general_http','host','releases.example.org','canonical',1)")
        await db.execute("INSERT INTO canonical_candidate_bindings(id,canonical_artifact_id,candidate_id,provider_id,source_scope,source_key,role,candidate_order) VALUES(2,1,'c2','alldebrid','host','alldebrid.com','alternate',2)")
        await db.execute("INSERT INTO canonical_candidate_origins(binding_id,contributing_artifact_id,contributing_transfer_id,request_id,resolution_attempt_id,discovered_candidate_id) VALUES(1,1,1,'r1','a1','c1')")
        await db.execute("INSERT INTO canonical_candidate_origins(binding_id,contributing_artifact_id,contributing_transfer_id,request_id,resolution_attempt_id,discovered_candidate_id) VALUES(2,2,2,'r2c','a2','c2')")
        # The writer now running: the selected candidate's execution and its provenance.
        candidate, host = ("c2", "alldebrid.com") if SELECTED == 1 else ("c1", "releases.example.org")
        await db.execute("INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state) VALUES('e1',1,1,'aria2','{}','running')")
        await db.execute("INSERT INTO execution_attempt_provenance(execution_attempt_id,transfer_id,artifact_id,ordinal,provider_id,candidate_id,candidate_source,delivered) VALUES('e1',1,1,1,?,?,?,0)",
                         ("alldebrid" if SELECTED == 1 else "general_http", candidate, json.dumps({"scope": "host", "key": host})))
        await db.execute("UPDATE download_files SET execution_attempt_id='e1' WHERE id=1")
        await db.commit()
    from api.operational_downloads import list_operational_torrents
    from transfers.presentation_repository import TransferRepository
    repository = TransferRepository()
    listed = await list_operational_torrents(status=None, search=None, limit=25, offset=0,
                                             application=SimpleNamespace(repository=repository, definitions=()))
    row = next(item for item in listed["items"] if int(item["id"]) == 1)
    detail = await repository.presentation(1)
    print(json.dumps({"list": row.get("current_source_identity"), "detail": detail.get("current_source_identity")}))

asyncio.run(main())
'''


def _identities(tmp_path: Path, selected: int, root_one: str, root_two: str) -> dict:
    env = {**os.environ, "DB_PATH": str(tmp_path / "dp.sqlite"), "CONFIG_PATH": str(tmp_path / "config.json")}
    done = subprocess.run([sys.executable, "-c", SCENARIO, str(selected), root_one, root_two], cwd=BACKEND,
                          env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("selected, root_one, root_two, expected", [
    # The trace: an HTTP transfer failed over to the torrent consolidated into it.
    (1, "https", "torrent", {"kind": "torrent_file"}),
    # Before the failover: its own HTTP candidate, shown by its host.
    (0, "https", "torrent", {"kind": "host", "host": "releases.example.org"}),
    # A magnet consolidated in the same way reads as the magnet.
    (1, "https", "magnet", {"kind": "magnet"}),
    # The other way round: a torrent transfer using an HTTP link consolidated into it (candidate c2,
    # contributed by transfer 2) shows that link's host.
    (1, "torrent", "https", {"kind": "host", "host": "alldebrid.com"}),
    # A torrent's own candidate stays the torrent, even when a provider delivers it over HTTP.
    (0, "torrent", "torrent", {"kind": "torrent_file"}),
])
def test_the_source_icon_follows_the_candidate_in_use(tmp_path, selected, root_one, root_two, expected):
    found = _identities(tmp_path, selected, root_one, root_two)
    assert found == {"list": expected, "detail": expected}, found
