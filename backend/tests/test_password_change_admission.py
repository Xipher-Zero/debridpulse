"""1.0.13 WS A (SEC-A01, SEC-A04, D-A2): proving a password is a sign-in attempt.

SEC-A01: changing your own password verified the current one with a
synchronous Argon2 call on the event loop, inside the authentication lock and
a write transaction, with no admission and no failure throttle -- any signed-in
account could stall the whole process and guess at its own password without a
delay. It now takes the sign-in path's own measures: the transport peer's
admission and failure throttle (failures attributed to the account's username),
the Argon2 verification off the loop and outside the lock, and under the lock a
re-check that the stored credential is still the one just proven.

SEC-A04: a Basic client presenting exactly an already-proven credential took
an admission slot first, so failing peers could crowd it out.

D-A2: a password set from now on is at least eight characters; a stored
shorter one still signs in.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from account_support import account_database
from fastapi import HTTPException

from auth import accounts
from auth.accounts import AccountRefused, Role

PEER = "192.0.2.44"
CFG = SimpleNamespace(auth_password_enabled=True)


def _request(account_id: str, role: str = "user"):
    principal = SimpleNamespace(authenticated=True, role=role, account_id=account_id)
    return SimpleNamespace(client=SimpleNamespace(host=PEER), state=SimpleNamespace(principal=principal))


@pytest.fixture
def clean_throttle():
    from auth.passwords import basic_verification_cache
    from auth.throttle import password_failure_throttle

    password_failure_throttle.clear()
    basic_verification_cache.clear()
    yield password_failure_throttle
    password_failure_throttle.clear()
    basic_verification_cache.clear()


@pytest.mark.asyncio
async def test_the_current_password_is_proven_and_the_new_one_hashed_off_the_event_loop(tmp_path, monkeypatch,
                                                                                        clean_throttle):
    from auth import passwords

    await account_database(tmp_path, monkeypatch)
    person = await accounts.create_local_account("pat", "first-password", role=Role.USER)
    loop_thread, threads = threading.current_thread(), {"verify": [], "hash": []}
    real_verify, real_hash = passwords.verify_password_candidate, accounts.hash_password

    def verify(*args, **kwargs):
        threads["verify"].append(threading.current_thread() is loop_thread)
        return real_verify(*args, **kwargs)

    def hashing(value):
        threads["hash"].append(threading.current_thread() is loop_thread)
        return real_hash(value)

    monkeypatch.setattr(passwords, "verify_password_candidate", verify)
    monkeypatch.setattr(accounts, "hash_password", hashing)
    await accounts.change_own_password(person.id, "first-password", "second-password", peer=PEER)

    assert threads["verify"] == [False] and threads["hash"] == [False]
    found = await accounts.local_credential("pat")
    assert passwords.verify_password(found[1], "second-password")


@pytest.mark.asyncio
async def test_wrong_current_passwords_are_throttled_like_sign_in_and_attributed_to_the_account(
        tmp_path, monkeypatch, clean_throttle):
    await account_database(tmp_path, monkeypatch)
    person = await accounts.create_local_account("pat", "first-password", role=Role.USER)
    assert clean_throttle.delay_for(PEER) == 0
    for _ in range(clean_throttle.free_failures + 1):
        with pytest.raises(AccountRefused, match="current password is not correct"):
            await accounts.change_own_password(person.id, "a-wrong-guess", "second-password", peer=PEER)
    assert clean_throttle.delay_for(PEER) > 0
    assert "pat" in clean_throttle._entries[PEER].failures
    # Proving the right password clears this account's own failures only.
    await accounts.change_own_password(person.id, "first-password", "second-password", peer=PEER)
    assert PEER not in clean_throttle._entries or "pat" not in clean_throttle._entries[PEER].failures


@pytest.mark.asyncio
async def test_a_full_admission_answers_429_without_checking_anything(tmp_path, monkeypatch, clean_throttle):
    from api import account_routes
    from auth import manager

    await account_database(tmp_path, monkeypatch)
    person = await accounts.create_local_account("pat", "first-password", role=Role.USER)
    checked = []
    monkeypatch.setattr(manager.password_attempt_admission, "acquire", lambda _peer: False)
    monkeypatch.setattr("auth.passwords.verify_password_candidate", lambda *a, **k: checked.append(1) or True)
    body = account_routes.OwnPassword(current_password="first-password", new_password="second-password")
    with pytest.raises(HTTPException) as refused:
        await account_routes.change_own_password(body, _request(person.id))
    assert refused.value.status_code == 429 and refused.value.headers == {"Retry-After": "2"}
    assert checked == []


@pytest.mark.asyncio
async def test_a_password_changed_while_the_proof_ran_is_refused_and_kept(tmp_path, monkeypatch, clean_throttle):
    from auth import passwords

    await account_database(tmp_path, monkeypatch)
    person = await accounts.create_local_account("pat", "first-password", role=Role.USER)
    real_async = passwords.verify_password_candidate_async

    async def proof_then_admin_reset(stored, candidate, *, use_configured_hash):
        verified = await real_async(stored, candidate, use_configured_hash=use_configured_hash)
        await accounts.reset_password(accounts.DEFAULT_ADMIN_ID, person.id, "admin-chosen-password")
        return verified

    monkeypatch.setattr(passwords, "verify_password_candidate_async", proof_then_admin_reset)
    with pytest.raises(AccountRefused, match="changed meanwhile"):
        await accounts.change_own_password(person.id, "first-password", "second-password", peer=PEER)
    found = await accounts.local_credential("pat")
    assert passwords.verify_password(found[1], "admin-chosen-password")


@pytest.mark.asyncio
async def test_new_passwords_are_at_least_eight_characters_and_a_stored_short_one_still_signs_in(
        tmp_path, monkeypatch, clean_throttle):
    from auth.manager import verify_local_credentials
    from auth.passwords import hash_password
    from db.database import get_db

    await account_database(tmp_path, monkeypatch)
    short = "seven77"
    with pytest.raises(AccountRefused, match="at least 8 characters"):
        await accounts.create_local_account("pat", short, role=Role.USER)
    person = await accounts.create_local_account("pat", "eight888", role=Role.USER)
    with pytest.raises(AccountRefused, match="at least 8 characters"):
        await accounts.change_own_password(person.id, "eight888", short, peer=PEER)
    with pytest.raises(AccountRefused, match="at least 8 characters"):
        await accounts.reset_password(accounts.DEFAULT_ADMIN_ID, person.id, short)
    with pytest.raises(AccountRefused, match="at least 8 characters"):
        await accounts.set_own_local_credential(person.id, password=short)
    # A credential stored before the rule (here written directly) still signs in.
    async with get_db() as db:
        await db.execute("UPDATE account_local_credentials SET password_hash=? WHERE account_id=?",
                         (hash_password("abc"), person.id))
        await db.commit()
    request = SimpleNamespace(client=SimpleNamespace(host=PEER))
    assert (await verify_local_credentials(request, "pat", "abc", settings=CFG)).username == "pat"


@pytest.mark.asyncio
async def test_a_proven_basic_credential_is_not_crowded_out_by_a_full_admission(tmp_path, monkeypatch,
                                                                                 clean_throttle):
    from auth import manager
    from auth.manager import PasswordAuthenticationBusy, verify_local_credentials

    await account_database(tmp_path, monkeypatch)
    await accounts.create_local_account("pat", "first-password", role=Role.USER)
    request = SimpleNamespace(client=SimpleNamespace(host=PEER))
    assert (await verify_local_credentials(request, "pat", "first-password", allow_basic_success_cache=True,
                                           settings=CFG)).username == "pat"
    monkeypatch.setattr(manager.password_attempt_admission, "acquire", lambda _peer: False)
    # The proven credential needs no Argon2 work, so no slot...
    assert (await verify_local_credentials(request, "pat", "first-password", allow_basic_success_cache=True,
                                           settings=CFG)).username == "pat"
    # ...while anything else is still admitted first.
    with pytest.raises(PasswordAuthenticationBusy):
        await verify_local_credentials(request, "pat", "a-wrong-guess", allow_basic_success_cache=True, settings=CFG)


@pytest.mark.asyncio
async def test_a_throttled_peer_waits_before_its_proof_is_checked(tmp_path, monkeypatch, clean_throttle):
    import time

    await account_database(tmp_path, monkeypatch)
    person = await accounts.create_local_account("pat", "first-password", role=Role.USER)
    monkeypatch.setattr(clean_throttle, "delay_for", lambda _peer: 0.3)
    started = time.monotonic()
    with pytest.raises(AccountRefused):
        await accounts.change_own_password(person.id, "a-wrong-guess", "second-password", peer=PEER)
    assert time.monotonic() - started >= 0.3


@pytest.mark.asyncio
async def test_the_sign_in_slot_is_released_once_proven_never_held_while_the_lock_is_awaited(
        tmp_path, monkeypatch, clean_throttle):
    import asyncio

    from auth import manager
    from auth.transitions import authentication_configuration_lock

    await account_database(tmp_path, monkeypatch)
    person = await accounts.create_local_account("pat", "first-password", role=Role.USER)
    await authentication_configuration_lock.acquire()
    try:
        change = asyncio.create_task(
            accounts.change_own_password(person.id, "first-password", "second-password", peer=PEER))
        for _ in range(400):
            await asyncio.sleep(0.01)
            if manager.password_attempt_admission.pending == 0 and not change.done():
                break
        assert manager.password_attempt_admission.pending == 0 and not change.done()
    finally:
        authentication_configuration_lock.release()
    await change


@pytest.mark.asyncio
async def test_new_passwords_are_hashed_before_any_write_transaction_opens(tmp_path, monkeypatch, clean_throttle):
    from contextlib import asynccontextmanager

    await account_database(tmp_path, monkeypatch)
    open_transactions, during = [0], []
    real_transaction, real_hash = accounts._transaction, accounts.hash_password

    @asynccontextmanager
    async def counted():
        open_transactions[0] += 1
        try:
            async with real_transaction() as db:
                yield db
        finally:
            open_transactions[0] -= 1

    def hashing(value):
        during.append(open_transactions[0])
        return real_hash(value)

    monkeypatch.setattr(accounts, "_transaction", counted)
    monkeypatch.setattr(accounts, "hash_password", hashing)
    person = await accounts.create_local_account("pat", "first-password", role=Role.USER)
    await accounts.change_own_password(person.id, "first-password", "second-password", peer=PEER)
    await accounts.reset_password(accounts.DEFAULT_ADMIN_ID, person.id, "third-password")
    assert during == [0, 0, 0]


@pytest.mark.asyncio
async def test_no_credential_is_read_before_admission_unless_a_proven_one_is_cached(tmp_path, monkeypatch,
                                                                                    clean_throttle):
    from auth import manager
    from auth.manager import PasswordAuthenticationBusy, verify_local_credentials

    await account_database(tmp_path, monkeypatch)
    await accounts.create_local_account("pat", "first-password", role=Role.USER)
    request = SimpleNamespace(client=SimpleNamespace(host=PEER))
    reads, real_read = [], manager.local_credential

    async def counted(username):
        reads.append(username)
        return await real_read(username)

    monkeypatch.setattr(manager, "local_credential", counted)
    monkeypatch.setattr(manager.password_attempt_admission, "acquire", lambda _peer: False)
    for _ in range(5):
        with pytest.raises(PasswordAuthenticationBusy):
            await verify_local_credentials(request, "pat", "a-wrong-guess", allow_basic_success_cache=True,
                                           settings=CFG)
    assert reads == []


@pytest.mark.asyncio
async def test_a_cached_basic_credential_clears_its_own_failures_like_any_proof(tmp_path, monkeypatch,
                                                                                 clean_throttle):
    from auth.manager import verify_local_credentials

    await account_database(tmp_path, monkeypatch)
    await accounts.create_local_account("pat", "first-password", role=Role.USER)
    request = SimpleNamespace(client=SimpleNamespace(host=PEER))
    assert await verify_local_credentials(request, "pat", "first-password", allow_basic_success_cache=True,
                                          settings=CFG)
    clean_throttle.record_failure(PEER, "pat")
    assert await verify_local_credentials(request, "pat", "first-password", allow_basic_success_cache=True,
                                          settings=CFG)
    assert PEER not in clean_throttle._entries


@pytest.mark.asyncio
async def test_a_cache_miss_verifies_against_the_credential_read_after_admission_never_the_early_one(
        tmp_path, monkeypatch, clean_throttle):
    """With the Basic cache populated (another user's proven credential), a
    lookup happens before admission. The stored password is replaced right
    after that early read: the old password must not succeed through it, and
    the admitted read comes after the throttle delay."""
    import time

    from auth import manager
    from auth.manager import verify_local_credentials

    await account_database(tmp_path, monkeypatch)
    await accounts.create_local_account("other", "other-password", role=Role.USER)
    pat = await accounts.create_local_account("pat", "old-password", role=Role.USER)
    request = SimpleNamespace(client=SimpleNamespace(host=PEER))
    assert await verify_local_credentials(request, "other", "other-password", allow_basic_success_cache=True,
                                          settings=CFG)
    from auth.passwords import basic_verification_cache
    assert basic_verification_cache.size == 1

    reads, real_read = [], manager.local_credential

    async def read_then_replace(username):
        found = await real_read(username)
        reads.append(time.monotonic())
        if len(reads) == 1:       # the early, pre-admission read: replace the password just after it
            await accounts.reset_password(accounts.DEFAULT_ADMIN_ID, pat.id, "new-password")
        return found

    monkeypatch.setattr(manager, "local_credential", read_then_replace)
    monkeypatch.setattr(clean_throttle, "delay_for", lambda _peer: 0.2)
    assert await verify_local_credentials(request, "pat", "old-password", allow_basic_success_cache=True,
                                          settings=CFG) is None
    assert len(reads) == 2 and reads[1] - reads[0] >= 0.2
    monkeypatch.setattr(manager, "local_credential", real_read)
    assert (await verify_local_credentials(request, "pat", "new-password", allow_basic_success_cache=True,
                                           settings=CFG)).username == "pat"
