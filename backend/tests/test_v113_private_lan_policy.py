"""Explicit private-LAN (RFC1918) destinations: default deny, one overridable
class, connection-time enforcement, operator-submitted lineage only."""
from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from services import network_safety as safety
from services.downloader_egress_guard import DownloaderEgressGuard, RouteScope
from transfers._engine_base import TransferEngine
from transfers.models import (
    DeliveryKind, Endpoint, ResolutionResult, ResourceState, TransferCandidate, TransferRequest,
)

RFC1918 = ("10.0.0.5", "172.16.3.4", "172.31.255.254", "192.168.1.20", "::ffff:192.168.1.20")
HARD_BLOCKED = (
    "127.0.0.1", "::1", "169.254.169.254", "fe80::1", "0.0.0.0", "::", "100.64.0.1",
    "198.18.0.1", "192.0.2.10", "240.0.0.1", "255.255.255.255", "fc00::1", "fd12:3456::1",
)
# Never part of the overridable class, whatever the existing public check says.
NOT_LAN = (*HARD_BLOCKED, "224.0.0.1", "ff02::1", "172.32.0.1", "93.184.216.34")


def test_default_deny_and_rfc1918_is_the_only_overridable_class():
    for address in RFC1918:
        assert not safety.destination_admitted(address)
        assert safety.destination_admitted(address, private_lan=True)
    for address in HARD_BLOCKED:
        assert not safety.destination_admitted(address, private_lan=True), address
    for address in NOT_LAN:
        assert not safety.private_lan_address(address), address
    assert safety.destination_admitted("93.184.216.34") and safety.destination_admitted("93.184.216.34",
                                                                                          private_lan=True)


def test_localhost_names_and_literals_stay_refused_under_a_grant():
    for uri in ("http://localhost/x", "http://box.localhost/x", "http://nas.local/x", "http://127.0.0.1/x",
                "http://[::1]/x", "http://169.254.169.254/latest/meta-data"):
        with pytest.raises(safety.UnsafeDestinationError):
            safety.validate_provider_download_url(uri, schemes=safety.PUBLIC_DESTINATION_SCHEMES, private_lan=True)
    assert safety.validate_provider_download_url("http://192.168.1.20/x", schemes=safety.PUBLIC_DESTINATION_SCHEMES,
                                                 private_lan=True)
    with pytest.raises(safety.UnsafeDestinationError):
        safety.validate_provider_download_url("http://192.168.1.20/x", schemes=safety.PUBLIC_DESTINATION_SCHEMES)


def test_mixed_or_rebinding_answer_sets_are_rejected_whole():
    safety.reject_non_public_resolution(["10.0.0.5", "93.184.216.34"], host="nas.example", private_lan=True)
    with pytest.raises(safety.UnsafeDestinationError):
        safety.reject_non_public_resolution(["10.0.0.5", "127.0.0.1"], host="nas.example", private_lan=True)
    with pytest.raises(safety.UnsafeDestinationError):
        safety.reject_non_public_resolution(["10.0.0.5", "169.254.169.254"], host="nas.example", private_lan=True)
    with pytest.raises(safety.UnsafeDestinationError):
        safety.reject_non_public_resolution(["10.0.0.5"], host="nas.example")


def _guard(answers):
    async def resolver(host, port):
        return [(socket.AF_INET6 if ":" in item else socket.AF_INET, socket.SOCK_STREAM, 6, "", (item, port))
                for item in answers[host]]
    return DownloaderEgressGuard(resolver=resolver)


def _credential(guard, uri, **kwargs):
    guard._server, guard._bound_port = object(), 1  # job_options only needs a bound proxy address
    options = guard.job_options(uri, **kwargs)
    return options["all-proxy-user"], options["all-proxy-passwd"]


@pytest.mark.asyncio
async def test_guard_admits_rfc1918_only_for_a_granted_credential_while_the_policy_is_on():
    guard = _guard({"nas.example": ["192.168.1.20"], "mixed.example": ["192.168.1.20", "127.0.0.1"],
                    "public.example": ["93.184.216.34"]})
    plain = _credential(guard, "http://nas.example/f.bin")
    granted = _credential(guard, "http://nas.example/f.bin", private_lan=True)
    assert plain[0] == "debridpulse" and granted[0] == "debridpulse.lan" and plain[1] != granted[1]
    assert guard._admits(*plain, "nas.example", 80) == (False, None, None)  # (grant, connect bound, budget)
    assert guard._admits(*granted, "nas.example", 80) == (True, None, None)
    # A grant cannot be forged by renaming an ungranted credential.
    assert guard._admits("debridpulse.lan", plain[1], "nas.example", 80) is None
    # A grant covers exactly its own authority.
    assert guard._admits(*granted, "other.example", 80) is None

    guard.configure_private_lan(False)
    with pytest.raises(ValueError):
        await guard._approved_endpoints("nas.example", 80, lan=True)  # global policy off wins
    guard.configure_private_lan(True)
    assert await guard._approved_endpoints("nas.example", 80, lan=True)
    with pytest.raises(ValueError):
        await guard._approved_endpoints("nas.example", 80, lan=False)  # ungranted job
    with pytest.raises(ValueError):
        await guard._approved_endpoints("mixed.example", 80, lan=True)  # loopback in the answer set
    with pytest.raises(ValueError):
        await guard._approved_endpoints("127.0.0.1", 80, lan=True)
    assert await guard._approved_endpoints("public.example", 80, lan=False)

    same_host = _credential(guard, "ftp://nas.example/f.bin", scope=RouteScope.SAME_HOST, private_lan=True)
    assert same_host[0] == "debridpulse.lan.same-host.21"
    assert guard._admits(*same_host, "nas.example", 40000) == (True, None, None)
    assert guard._admits(*same_host, "nas.example", 22) is None


def test_core_stamps_the_grant_only_for_the_operator_submitted_host():
    def stamped(candidate, lan_host):
        result = TransferEngine._authoritative_provider_result(
            "general_http", ResolutionResult(ResourceState.AVAILABLE, (candidate,)), request_kind="http",
            lan_host=lan_host)
        return result.candidates[0].private_network_grant

    own = TransferCandidate("f.bin", (Endpoint("http", "http://nas.example/f.bin"),))
    assert stamped(own, "nas.example") is True
    assert stamped(own, "") is False  # no consent in the lineage
    # A provider-issued (debrid) endpoint never inherits it, even on the same name.
    assert stamped(TransferCandidate("f.bin", (Endpoint("http", "http://nas.example/f.bin"),),
                                     delivery=DeliveryKind.PROVIDER_ISSUED), "nas.example") is False
    # A provider-returned/redirected private endpoint for another host does not either.
    assert stamped(TransferCandidate("f.bin", (Endpoint("http", "http://10.0.0.9/f.bin"),)), "nas.example") is False
    # And a provider cannot set it for itself.
    assert stamped(TransferCandidate("f.bin", (Endpoint("http", "http://10.0.0.9/f.bin"),),
                                     private_network_grant=True), "") is False


@pytest.mark.asyncio
async def test_admission_follows_the_policy_matrix_and_never_writes_settings(monkeypatch):
    import application.service as service
    from transfers.policy import TransferPolicy
    application = SimpleNamespace(engine=SimpleNamespace(policy=TransferPolicy()))

    def policy(lan, skip):
        application.engine.policy = TransferPolicy(private_lan_connections=lan, skip_private_lan_confirmation=skip)
        return application.engine.policy

    urls = ["http://192.168.1.20/a.bin", "https://example.com/b.bin"]

    async def decide(links, *, allow_local_network):
        return await service.ApplicationService._local_network_consent(
            application, links, allow_local_network=allow_local_network)
    policy(False, True)
    assert await decide(urls, allow_local_network=True) == frozenset()  # OFF: never consented
    before = policy(True, False)
    with pytest.raises(service.LocalNetworkConfirmationRequired) as asked:
        await decide(urls, allow_local_network=False)
    assert asked.value.hosts == ("192.168.1.20",)
    assert before.skip_private_lan_confirmation is False  # nothing written by asking
    assert await decide(urls, allow_local_network=True) == frozenset({"192.168.1.20"})
    policy(True, True)
    assert await decide(urls, allow_local_network=False) == frozenset({"192.168.1.20"})
    assert await decide(["https://example.com/b.bin"], allow_local_network=False) == frozenset()


def test_consent_is_a_per_submission_request_fact_outside_identity():
    request = TransferRequest("http", "http://192.168.1.20/a.bin", local_network_consent=True)
    from transfers import codec
    assert codec.request(codec.load(codec.dump(request))) == request
    assert codec.request({"kind": "http", "payload": "http://x/y"}).local_network_consent is False


def test_aria2_honors_a_grant_only_while_the_guard_policy_is_on():
    from executors.aria2.executor import Aria2Executor
    executor = Aria2Executor.__new__(Aria2Executor)
    granted = TransferCandidate("f.bin", (Endpoint("http", "http://192.168.1.20/f.bin"),), private_network_grant=True)
    executor.egress = SimpleNamespace(private_lan_enabled=True)
    assert executor._private_lan(granted) is True
    executor.egress = SimpleNamespace(private_lan_enabled=False)
    assert executor._private_lan(granted) is False
    executor.egress = SimpleNamespace(private_lan_enabled=True)
    assert executor._private_lan(TransferCandidate("f.bin", (Endpoint("http", "http://192.168.1.20/f.bin"),))) is False
