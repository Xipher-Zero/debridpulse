from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / 'frontend' / 'static'

# The locked Settings identity: every Settings subsection header wears its
# family's Lucide glyph inside the one Settings header chip (familyIcon), the
# glyph's stroke the family colour.
FAMILY_COLOURS = {'sources': '#D657FF', 'downloads': '#2563EB', 'extraction': '#FF9D00',
                  'authentication': '#1DDB69', 'notifications': '#04D7FE', 'maintenance': '#6366F1'}
EXPECTED = {
    'Premium Services': ('sources', 'crown'),
    'Automatic Extraction': ('extraction', 'archive-restore'),
    'Authentication Status': ('authentication', 'shield-check'),
    'Username & Password': ('authentication', 'user-lock'),
    'OpenID Connect': ('authentication', 'id-card'),
    'API Access': ('authentication', 'key-round'),
    'Discord Notifications': ('notifications', 'message-square'),
    'Statistics Reporting': ('notifications', 'chart-line'),
    'Backups & Retention': ('maintenance', 'archive'),
    'Database Reset Controls': ('maintenance', 'database-x'),
}


def test_settings_inner_card_icon_map_covers_reviewed_headers_and_assets():
    source = (STATIC / 'ui-settings-page.js').read_text()
    assert 'CARD_ICONS' not in source and 'dp-settings-inner-card-icon' not in source
    for title, (section, glyph) in EXPECTED.items():
        assert f"familyIcon('{section}', '{glyph}')" in source, title
        text = (STATIC / 'icons' / 'lucide' / f'{glyph}.svg').read_text()
        assert '<svg' in text and 'Lucide ' in text and '@ 23f9abc4ed0146cffededd3d7f94c1018bfdf693' in text
        assert f'stroke="{FAMILY_COLOURS[section]}"' in text
        assert '<image' not in text.lower()
        assert 'data:image' not in text.lower()
        assert 'base64' not in text.lower()


# The four Downloads subsection headers wear their glyph inside the Downloads
# family chip (familyIcon), the one Transfer Method Settings established.
DOWNLOADS_CHIPS = {'Download Behavior & Limits': 'gauge', 'Transfer Method Settings': 'arrow-left-right',
                   'Disk Space & Recovery': 'shield-alert', 'Download Engine Activity': 'activity'}


def test_downloads_subsection_headers_share_the_family_chip():
    source = (STATIC / 'ui-settings-page.js').read_text()
    for title, glyph in DOWNLOADS_CHIPS.items():
        assert f"'{title}': ['downloads'" not in source, f'{title} still renders the naked inner-card icon'
        assert f"familyIcon('downloads', '{glyph}')" in source
        text = (STATIC / 'icons' / 'lucide' / f'{glyph}.svg').read_text()
        assert f'stroke="{FAMILY_COLOURS["downloads"]}"' in text
    assert "data-section=\"downloads\"" not in source


def test_settings_inner_card_icons_use_section_tab_color_families_and_shared_footprint():
    css = (STATIC / 'ui-settings-card-icons.css').read_text()
    # Each family states only its colour, on the one header chip.
    for section, colour in FAMILY_COLOURS.items():
        rule = css.split(f"#view-settings .dp-settings-protocol-chip[data-section='{section}'] {{", 1)[1].split('}', 1)[0]
        assert rule.strip() == f'--dp-protocol-color: {colour};', section
    assert '.dp-settings-inner-card-icon' not in css
    chip = css.split('#view-settings .dp-settings-protocol-chip {', 1)[1].split('}', 1)[0]
    assert 'width: 38px' in chip and 'height: 38px' in chip


def test_help_license_footer_actions_and_copy_are_centered_as_one_closing_block():
    css = (STATIC / 'ui-help-license-balance.css').read_text()
    assert '.dp-help-license-actions' in css and 'justify-content: center' in css
    assert '.dp-help-license-note' in css and 'text-align: center' in css
