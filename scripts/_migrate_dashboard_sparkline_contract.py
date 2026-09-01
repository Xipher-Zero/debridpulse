from pathlib import Path

path = Path('backend/tests/test_v1111_canonical_frontend_contract.py')
text = path.read_text()
old = '''def test_runtime_coordination_uses_explicit_events_not_page_convergence_observation() -> None:
    runtime = read(RUNTIME)
    for event in (
        "debridpulse:navigation",
        "debridpulse:dashboard-stats-rendered",
        "debridpulse:dashboard-recent-rendered",
        "debridpulse:activity-rendered",
    ):
        assert event in runtime
    assert "new MutationObserver" not in runtime
    assert "window.loadStats =" not in runtime
'''
new = '''def test_runtime_coordination_uses_explicit_events_not_page_convergence_observation() -> None:
    runtime = read(RUNTIME)
    app = read(APP)
    downloads = read(DOWNLOADS)
    for event in (
        "debridpulse:navigation",
        "debridpulse:dashboard-recent-rendered",
        "debridpulse:activity-rendered",
    ):
        assert event in runtime
    # Dashboard statistics are emitted by the canonical data owner and may be
    # consumed by other page runtimes, but the KPI sparklines no longer depend
    # on a presentation-runtime event bridge.
    assert "debridpulse:dashboard-stats-rendered" in app
    assert "debridpulse:dashboard-stats-rendered" in downloads
    assert "debridpulse:dashboard-stats-rendered" not in runtime
    assert "new MutationObserver" not in runtime
    assert "window.loadStats =" not in runtime
'''
assert text.count(old) == 1
path.write_text(text.replace(old, new))
