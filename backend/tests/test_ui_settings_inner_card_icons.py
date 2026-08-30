from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / 'frontend' / 'static'

EXPECTED = {
    'Download Engine': ('downloads', 'download-engine.svg'),
    'Download Safety & Recovery': ('downloads', 'download-safety-recovery.svg'),
    'Built-In Download Engine State': ('downloads', 'built-in-download-engine-state.svg'),
    'Automatic Extraction': ('extraction', 'automatic-extraction.svg'),
    'Authentication Status': ('authentication', 'authentication-status.svg'),
    'Username & Password': ('authentication', 'username-password.svg'),
    'OpenID Connect': ('authentication', 'openid-connect.svg'),
    'API Access': ('authentication', 'api-access.svg'),
    'Discord Notifications': ('notifications', 'discord-notifications.svg'),
    'Statistics Reporting': ('notifications', 'statistics-reporting.svg'),
    'Backups & Retention': ('maintenance', 'backups-retention.svg'),
    'Database Reset Controls': ('maintenance', 'database-reset-controls.svg'),
}


def test_settings_inner_card_icon_map_covers_reviewed_headers_and_assets():
    source = (STATIC / 'ui-settings-card-icons.js').read_text()
    for title, (section, filename) in EXPECTED.items():
        assert title in source
        assert section in source
        assert f'/icons/dp/settings/{filename}?v=1' in source
        asset = STATIC / 'icons' / 'dp' / 'settings' / filename
        text = asset.read_text()
        assert '<svg' in text and '<path' in text
        assert '<image' not in text.lower()
        assert 'data:image' not in text.lower()
        assert 'base64' not in text.lower()


def test_settings_inner_card_icons_use_section_tab_color_families_and_shared_footprint():
    css = (STATIC / 'ui-settings-card-icons.css').read_text()
    for color in ('#4c8fff', '#e0a02b', '#48c77e', '#39c6e8', '#3ab8a8'):
        assert color in css
    assert 'width: 34px' in css
    assert 'height: 34px' in css
    assert 'drop-shadow' in css


def test_settings_inner_card_runtime_reapplies_without_observing_its_own_mutations():
    source = (STATIC / 'ui-settings-card-icons.js').read_text()
    assert 'observer?.disconnect()' in source
    assert 'observer.observe(view, {childList: true, subtree: false})' in source
    assert 'queueMicrotask' in source
    assert 'subtree: true' not in source


def test_help_license_footer_actions_and_copy_are_centered_as_one_closing_block():
    css = (STATIC / 'ui-help-final-balance.css').read_text()
    assert '.dp-help-license-actions' in css and 'justify-content: center' in css
    assert '.dp-help-license-note' in css and 'text-align: center' in css
