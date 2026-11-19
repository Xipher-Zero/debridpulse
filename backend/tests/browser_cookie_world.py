"""One scenario of the browser video sign-in holder (``external.browser_cookies``)
against the canonical engine and repository, run as its own process by
``test_v114_browser_cookie_holder.py``.

The process is the isolation: ``DB_PATH`` names this scenario's database, and
the holder is the module's own singleton -- nothing is patched. Accounts and
browser tokens are the real ones (``auth.accounts``); the provider is the
ledger fake, recording what the holder hands the resolution unit it runs in.

Usage: python browser_cookie_world.py <scenario> <work-dir>   (prints one JSON object)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

scenario, work = sys.argv[1], Path(sys.argv[2])
os.environ["DB_PATH"] = str(work / "state.db")
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

import db.database as database  # noqa: E402
from auth import accounts  # noqa: E402
from executor_fakes import LedgerExecutor, LedgerProvider  # noqa: E402
from external.browser_cookies import CookieCapacity, CAPACITY, browser_cookies, parse  # noqa: E402
from transfers.convergence_engine import TransferEngine  # noqa: E402
from transfers.models import SubmissionDemand, TransferRequest  # noqa: E402
from transfers.policy import TransferPolicy  # noqa: E402
from transfers.recovery_repository import TransferRepository  # noqa: E402
from transfers.registry import IntegrationRegistry  # noqa: E402

VALUE = "held-value-for-one-job"
COOKIES = parse([{"name": "SID", "value": VALUE, "domain": ".video.test"}], ["https://www.video.test/v/1"])


class SignInProvider(LedgerProvider):
    """Records, for each resolution, whether the holder handed it the sign-in."""

    seen: list = []

    async def resolve(self, request):
        held = await browser_cookies.current()
        self.seen.append(bool(held) and held[0].value == VALUE)
        return await super().resolve(request)


async def person(name):
    account = await accounts.create_local_account(name, "correct horse battery staple", role="user")
    token, _raw = await accounts.issue_browser_token(account.id, f"{name}'s browser")
    return account.id, token["id"]


async def main() -> None:
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    provider = SignInProvider()
    registry.register_provider(provider)
    executor = LedgerExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(work / "payloads"),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0))
    await engine.initialize()
    report: dict = {"outside a unit": await browser_cookies.current()}

    if scenario == "capacity":
        for index in range(CAPACITY):
            browser_cookies.register(f"d-{index}", "a", "t", COOKIES)
        try:
            browser_cookies.register("one-more", "a", "t", COOKIES)
            report["over capacity"] = "accepted"
        except CookieCapacity:
            report["over capacity"] = "refused"
        report["settled"] = await browser_cookies.settle()   # none was ever reserved or admitted
        report["held after settle"] = len(browser_cookies)
        print(json.dumps(report, default=str))
        return

    una, una_token = await person("una")
    demand = SubmissionDemand(una, una_token)
    if scenario != "unregistered":
        browser_cookies.register(demand.id, una, una_token, COOKIES)
    request = TransferRequest("ledger", "clip", name="clip")
    transfer = await engine.submit((request,), demand=demand)
    if scenario == "merged":
        bo, bo_token = await person("bo")
        merged = await engine.submit((request,), demand=SubmissionDemand(bo, bo_token))
        report["merged into the same transfer"] = merged.id == transfer.id
    if scenario == "revoked":
        await accounts.revoke_token(una, una_token, kind="browser")
    await engine.tick()
    report["resolution saw the sign-in"] = provider.seen
    artifact = (await repository.artifacts(transfer.id))[0]
    held = await browser_cookies.for_attempt(artifact.execution.attempt_id) if artifact.execution else None
    report["attempt gets the sign-in"] = bool(held)
    report["unknown attempt gets"] = await browser_cookies.for_attempt("no-such-attempt")
    report["settled while running"] = await browser_cookies.settle()
    if scenario == "one":
        await engine.withdraw_demand([demand.id], by=una)
        report["settled after withdrawal"] = await browser_cookies.settle()
        report["held"] = browser_cookies.held(demand.id)
        report["attempt gets after withdrawal"] = bool(await browser_cookies.for_attempt(
            artifact.execution.attempt_id)) if artifact.execution else None
    print(json.dumps(report, default=str))


asyncio.run(main())
