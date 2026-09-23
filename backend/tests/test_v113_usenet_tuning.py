"""DP 1.0.13 post-Usenet corrective pass, work item F.

High-value Usenet acquisition tuning, inside the ONE canonical
``integrations.usenet`` namespace, projected ONE way onto the bundled
acquisition service.

Every native fact asserted here was characterized at Gate 1 against the
bundled SABnzbd 5.1.3 (source tree extracted from the candidate image, plus
live probes against the running service):

  misc.cache_limit          OptionStr  K/M/G string, live callback ``new_limit``
  misc.direct_write         OptionBool default True, live callback, JSON bool readback
  misc.max_art_tries        OptionNumber default 3, minval 2, read per attempt
  servers.<kw>.timeout      OptionNumber default 60, native clamp 20..240
  servers.<kw>.pipelining_requests  OptionNumber default 2, native clamp 1..20

None of them requires a restart.
"""
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from executors.sabnzbd.admin import SabnzbdAdministration
from integrations.usenet.definition import UsenetOptions, UsenetServer
from tests.sab_fakes import FakeSab

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
SETTINGS_JS = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")

ROOT = "/download"


def _options(**overrides):
    base = {"servers": [UsenetServer(id="s1", host="news.example.com", username="u", password="p")]}
    base.update(overrides)
    return UsenetOptions(**base)


def _admin(options, sab=None):
    return SabnzbdAdministration(sab or FakeSab(), options, ROOT)


# --- canonical model: defaults and validation ------------------------------

def test_canonical_defaults_match_the_characterized_native_defaults():
    options = UsenetOptions()
    assert options.article_cache_megabytes == 1024      # native seeds "1G"
    assert options.direct_write is True                 # native default True
    assert options.max_acquisition_retries == 3         # native max_art_tries default
    assert options.operation_timeout_seconds == 30      # unchanged DP-side timeout
    server = UsenetServer()
    assert server.articles_per_request == 2             # DEF_PIPELINING_REQUESTS
    assert server.timeout_seconds == 60                 # native server timeout default


@pytest.mark.parametrize("field,value", [
    ("article_cache_megabytes", -1), ("article_cache_megabytes", 4097),
    ("max_acquisition_retries", 1), ("max_acquisition_retries", 26),
])
def test_integration_tuning_rejects_values_outside_the_characterized_range(field, value):
    with pytest.raises(ValidationError):
        UsenetOptions(**{field: value})


@pytest.mark.parametrize("field,value", [
    ("articles_per_request", 0), ("articles_per_request", 21),
    ("timeout_seconds", 19), ("timeout_seconds", 241),
])
def test_server_tuning_rejects_values_the_service_would_silently_clamp(field, value):
    """DP validation is never wider than native, so a persisted value can
    never be quietly rewritten by the service."""
    with pytest.raises(ValidationError):
        UsenetServer(**{field: value})


@pytest.mark.parametrize("field,value", [
    ("article_cache_megabytes", 0), ("article_cache_megabytes", 4096),
    ("max_acquisition_retries", 2), ("max_acquisition_retries", 25),
])
def test_integration_tuning_accepts_its_boundaries(field, value):
    assert getattr(UsenetOptions(**{field: value}), field) == value


def test_the_public_projection_still_hides_only_credentials():
    public = _options().public()
    assert public["article_cache_megabytes"] == 1024
    assert public["direct_write"] is True
    assert public["max_acquisition_retries"] == 3
    assert public["servers"][0]["articles_per_request"] == 2
    assert public["servers"][0]["timeout_seconds"] == 60
    assert public["servers"][0]["password"] == ""
    assert public["servers"][0]["password_configured"] is True


# --- one-way native projection --------------------------------------------

@pytest.mark.asyncio
async def test_integration_tuning_is_projected_onto_the_characterized_native_keys():
    sab = FakeSab()
    options = _options(article_cache_megabytes=512, direct_write=False, max_acquisition_retries=7)
    assert (await _admin(options, sab).apply_configuration()).ok
    misc = (await sab.get_config("misc"))["misc"]
    assert misc["cache_limit"] == "512M"
    assert misc["direct_write"] in (0, False)
    assert misc["max_art_tries"] == 7


@pytest.mark.asyncio
async def test_a_disabled_article_cache_projects_the_native_off_value():
    sab = FakeSab()
    assert (await _admin(_options(article_cache_megabytes=0), sab).apply_configuration()).ok
    assert (await sab.get_config("misc"))["misc"]["cache_limit"] == "0"


@pytest.mark.asyncio
async def test_server_tuning_is_projected_onto_the_characterized_native_server_keys():
    sab = FakeSab()
    options = _options(servers=[UsenetServer(id="s1", host="a.example.com", connections=12,
                                             priority=3, articles_per_request=6, timeout_seconds=90)])
    assert (await _admin(options, sab).apply_configuration()).ok
    entry = next(iter(sab.servers.values()))
    assert entry["connections"] == 12 and entry["priority"] == 3
    assert entry["pipelining_requests"] == 6
    assert entry["timeout"] == 90


@pytest.mark.asyncio
async def test_one_servers_advanced_settings_never_reach_another_server():
    sab = FakeSab()
    options = _options(servers=[
        UsenetServer(id="s1", host="a.example.com", articles_per_request=6, timeout_seconds=90),
        UsenetServer(id="s2", host="b.example.com"),
    ])
    assert (await _admin(options, sab).apply_configuration()).ok
    by_key = {entry["name"]: entry for entry in sab.servers.values()}
    assert by_key["dp-s1"]["pipelining_requests"] == 6 and by_key["dp-s1"]["timeout"] == 90
    assert by_key["dp-s2"]["pipelining_requests"] == 2 and by_key["dp-s2"]["timeout"] == 60
    # Stable canonical id remains the addressing authority.
    assert set(by_key) == {"dp-s1", "dp-s2"}


@pytest.mark.asyncio
async def test_native_tuning_state_is_never_adopted_back_into_canonical_settings():
    sab = FakeSab()
    sab.cache_limit, sab.direct_write, sab.max_art_tries = "64M", 0, 11
    options = _options(article_cache_megabytes=2048, direct_write=True, max_acquisition_retries=4)
    admin = _admin(options, sab)
    await admin.apply_configuration()
    assert options.article_cache_megabytes == 2048
    assert options.direct_write is True
    assert options.max_acquisition_retries == 4
    assert (await sab.get_config("misc"))["misc"]["cache_limit"] == "2048M"


# --- drift detection (report only) -----------------------------------------

@pytest.mark.asyncio
async def test_tuning_drift_is_detected_and_reported_by_field_name_only():
    sab = FakeSab()
    options = _options(article_cache_megabytes=512, max_acquisition_retries=5)
    admin = _admin(options, sab)
    await admin.apply_configuration()
    sab.cache_limit = "64M"
    sab.max_art_tries = 9
    report = await admin.drift()
    assert report.reachable and report.drifted
    assert "misc.cache_limit" in report.differences
    assert "misc.max_art_tries" in report.differences
    assert all(not difference.endswith("64M") for difference in report.differences)


@pytest.mark.asyncio
async def test_equivalent_native_cache_units_are_not_reported_as_drift():
    """Readback compares BYTES: "1G" and "1024M" are the same desired state."""
    sab = FakeSab()
    admin = _admin(_options(article_cache_megabytes=1024), sab)
    await admin.apply_configuration()
    sab.cache_limit = "1G"
    assert not (await admin.drift()).drifted


@pytest.mark.asyncio
async def test_server_advanced_drift_is_detected():
    sab = FakeSab()
    admin = _admin(_options(servers=[UsenetServer(id="s1", host="a.example.com", timeout_seconds=90)]), sab)
    await admin.apply_configuration()
    sab.servers["dp-s1"]["timeout"] = 30
    report = await admin.drift()
    assert "servers.dp-s1.timeout" in report.differences


@pytest.mark.asyncio
async def test_tuning_is_applied_before_servers_so_drift_is_evaluated_on_the_whole_desired_state():
    sab = FakeSab()
    result = await _admin(_options(article_cache_megabytes=256), sab).apply_configuration()
    assert result.ok is True
    assert (await sab.get_config("misc"))["misc"]["cache_limit"] == "256M"


# --- UI surface -------------------------------------------------------------

def _strip_comments(source: str) -> str:
    """Rendered markup and control identifiers only.

    Absence is asserted against what the card actually RENDERS, never against
    its prose: the card may legitimately explain where global admission lives
    without that explanation reading as an exposed control.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def _usenet_tuning_block():
    return _strip_comments(SETTINGS_JS[SETTINGS_JS.index("function usenetTuning("):
                                       SETTINGS_JS.index("function downloadsPanel(")])


def _usenet_server_card_block():
    return _strip_comments(SETTINGS_JS[SETTINGS_JS.index("function usenetServerCard("):
                                       SETTINGS_JS.index("function usenetAddTile(")])


def test_executor_tuning_exposes_exactly_the_approved_executor_wide_controls():
    block = _usenet_tuning_block()
    controls = set(re.findall(r"input\('([a-z0-9_]+)'", block))
    controls |= set(re.findall(r"tuningToggle\(\s*'([a-z0-9_]+)'", block))
    controls |= set(re.findall(r"selectField\('([a-z0-9_]+)'", block))
    assert controls == {
        "usenet_operation_timeout_seconds",
        "usenet_article_cache_megabytes",
        "usenet_direct_write",
        "usenet_max_acquisition_retries",
    }


def test_no_excluded_native_setting_appears_in_the_tuning_surface():
    block = _usenet_tuning_block()
    for forbidden in ("unpack", "queue", "max_active", "concurrent", "bandwidth",
                      "speedlimit", "folder", "complete_dir", "download_dir",
                      "category", "script", "rss", "schedule", "api_key",
                      "max_url_retries", "line speed", "percentage"):
        assert forbidden not in block.lower(), forbidden


def test_maximum_retries_is_labelled_as_acquisition_retry_not_transfer_retry():
    block = _usenet_tuning_block()
    assert "Maximum Retries" in block
    lowered = block.lower()
    assert "news server" in lowered
    assert "not the debridpulse download retry" in lowered


def test_per_server_advanced_disclosure_holds_the_four_approved_controls():
    block = _usenet_server_card_block()
    assert 'class="dp-usenet-advanced"' in block
    advanced = block[block.index('class="dp-usenet-advanced"'):]
    for field in ("connections", "priority", "articles_per_request", "timeout_seconds"):
        assert f'data-usenet-field="{field}"' in advanced, field
    # Connections and Priority moved INTO Advanced; they are not duplicated.
    assert block.count('data-usenet-field="connections"') == 1
    assert block.count('data-usenet-field="priority"') == 1


def test_the_normal_server_card_stays_compact():
    block = _usenet_server_card_block()
    head = block[:block.index('class="dp-usenet-advanced"')]
    for field in ("host", "port", "ssl", "username", "password"):
        assert f'data-usenet-field="{field}"' in head, field
    for field in ("articles_per_request", "timeout_seconds"):
        assert f'data-usenet-field="{field}"' not in head, field


def test_no_implementation_name_reaches_the_tuning_copy():
    block = _usenet_tuning_block() + _usenet_server_card_block()
    # Native keys never surface. (``direct_write`` is absent from this list on
    # purpose: it is a DebridPulse CANONICAL field name that happens to share a
    # spelling with the native key, and the operator-facing label is the plain
    # semantic "Direct Write".)
    for name in ("SABnzbd", "sabnzbd", "NNTP", "nntp", "cache_limit", "max_art_tries",
                 "pipelining_requests", "max_url_retries"):
        assert name not in block, name
