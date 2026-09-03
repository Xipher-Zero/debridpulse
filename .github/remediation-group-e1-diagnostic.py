from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"

def read(name):
    return (STATIC / name).read_text(encoding="utf-8")

index = read("index.html")
controls = read("ui-utility-controls.css")

start = index.index('id="view-dashboard"')
end = index.index('id="view-torrents"', start)
dashboard = index[start:end]

start = index.index('id="view-events"')
end = index.index('<!-- Statistics -->', start)
activity = index[start:end]

start = index.index('<!-- Statistics -->')
end = index.index('<!-- Help -->', start)
stats = index[start:end]

assets = (
    "card-download.svg", "card-checkmark.svg", "card-play.svg", "card-clock.svg",
    "card-error.svg", "card-disk.svg", "card-link.svg", "card-document-stack.svg",
)
lines = [
    f"index_sha256={hashlib.sha256(index.encode()).hexdigest()}",
    "dashboard_assets=" + repr({asset: asset in dashboard for asset in assets}),
    "dashboard_prefix=" + repr(dashboard[:1200]),
    "activity_has_dp_activity_card=" + repr("dp-activity-card" in activity),
    "activity_prefix=" + repr(activity[:1600]),
    "stats_has_history_exact=" + repr('class="dash-kpi-strip dp-stats-history-grid"' in stats),
    "stats_history_context=" + repr(stats[max(0, stats.find("dash-kpi-strip")-200):stats.find("dash-kpi-strip")+700] if "dash-kpi-strip" in stats else stats[:1200]),
    "controls_has_activity_icon_substring=" + repr(".dp-activity-refresh .dp-utility-icon" in controls),
    "controls_activity_context=" + repr(controls[max(0, controls.find("dp-activity-refresh")-250):controls.find("dp-activity-refresh")+900] if "dp-activity-refresh" in controls else controls[:1200]),
]
(Path(__file__).with_name("e1-transformed-owner-evidence.txt")).write_text("\n".join(lines) + "\n", encoding="utf-8")
