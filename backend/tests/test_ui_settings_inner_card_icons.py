from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / 'frontend' / 'static'

# The locked Settings identity: each card's family and its Lucide glyph, the
# glyph's stroke the family colour.
FAMILY_COLOURS = {'downloads': '#2563EB', 'extraction': '#FF9D00', 'authentication': '#1DDB69',
                  'notifications': '#04D7FE', 'maintenance': '#6366F1'}
EXPECTED = {
    'Download Behavior & Limits': ('downloads', 'gauge.svg'),
    'Disk Space & Recovery': ('downloads', 'shield-alert.svg'),
    'Download Engine Activity': ('downloads', 'activity.svg'),
    'Automatic Extraction': ('extraction', 'archive-restore.svg'),
    'Authentication Status': ('authentication', 'shield-check.svg'),
    'Username & Password': ('authentication', 'user-lock.svg'),
    'OpenID Connect': ('authentication', 'id-card.svg'),
    'API Access': ('authentication', 'key-round.svg'),
    'Discord Notifications': ('notifications', 'message-square.svg'),
    'Statistics Reporting': ('notifications', 'chart-line.svg'),
    'Backups & Retention': ('maintenance', 'archive.svg'),
    'Database Reset Controls': ('maintenance', 'database-x.svg'),
}


def test_settings_inner_card_icon_map_covers_reviewed_headers_and_assets():
    source = (STATIC / 'ui-settings-page.js').read_text()
    for title, (section, filename) in EXPECTED.items():
        assert f"'{title}': ['{section}', '/icons/lucide/{filename}']" in source
        text = (STATIC / 'icons' / 'lucide' / filename).read_text()
        assert '<svg' in text and 'Lucide ' in text and '@ 23f9abc4ed0146cffededd3d7f94c1018bfdf693' in text
        assert f'stroke="{FAMILY_COLOURS[section]}"' in text
        assert '<image' not in text.lower()
        assert 'data:image' not in text.lower()
        assert 'base64' not in text.lower()


def test_settings_inner_card_icons_use_section_tab_color_families_and_shared_footprint():
    css = (STATIC / 'ui-settings-card-icons.css').read_text()
    for color in ('#D657FF', *FAMILY_COLOURS.values()):
        assert color in css
    assert 'width: 34px' in css
    assert 'height: 34px' in css
    assert 'drop-shadow' in css




def test_help_license_footer_actions_and_copy_are_centered_as_one_closing_block():
    css = (STATIC / 'ui-help-license-balance.css').read_text()
    assert '.dp-help-license-actions' in css and 'justify-content: center' in css
    assert '.dp-help-license-note' in css and 'text-align: center' in css
