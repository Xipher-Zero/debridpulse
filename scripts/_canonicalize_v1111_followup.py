from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def write(rel, text):
    (ROOT / rel).write_text(text, encoding='utf-8')


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{label}: expected one match, found {count}')
    return text.replace(old, new, 1)


def replace_between(text, start, end, replacement, label):
    a = text.find(start)
    if a < 0:
        raise RuntimeError(f'{label}: start marker missing')
    b = text.find(end, a + len(start))
    if b < 0:
        raise RuntimeError(f'{label}: end marker missing')
    return text[:a] + replacement.rstrip() + '\n\n' + text[b:]


# Preserve the accepted v1.0.11 Settings tab order. The authentication collapse
# changes ownership, not reviewed navigation order.
settings_path = 'frontend/static/ui-settings-page.js'
settings = read(settings_path)
settings = replace_once(
    settings,
    "    ['extraction', 'Extraction', 'package-open'],\n    ['authentication', 'Authentication', 'shield-check'],\n    ['notifications', 'Notifications', 'bell'],",
    "    ['extraction', 'Extraction', 'package-open'],\n    ['notifications', 'Notifications', 'bell'],\n    ['authentication', 'Authentication', 'shield-check'],",
    'accepted Settings tab order',
)
write(settings_path, settings)


# The canonical Statistics owner must retain the accepted purple completion
# chart language that previously lived in the retired visual-correction layer.
stats_path = 'frontend/static/ui-statistics.js'
stats = read(stats_path)
stats = replace_once(
    stats,
    "period = period || (document.querySelector('#stats-period-tabs .ftab.active')||{}).dataset?.period || '24h';",
    "period = period || (document.querySelector('#stats-period-tabs .ftab.active')||{}).dataset?.period || '7d';",
    'Statistics 7-day fallback',
)
chart_palette = r'''  function statisticsPurpleGradient(chart) {
    const isLight = document.body.classList.contains('light');
    if (!chart || !chart.ctx || !chart.chartArea) {
      return isLight ? 'rgba(139, 91, 203, .46)' : 'rgba(100, 39, 165, .64)';
    }

    const area = chart.chartArea;
    const gradient = chart.ctx.createLinearGradient(0, area.bottom, 0, area.top);
    if (isLight) {
      gradient.addColorStop(0, 'rgba(210, 195, 239, .28)');
      gradient.addColorStop(0.52, 'rgba(171, 137, 221, .42)');
      gradient.addColorStop(1, 'rgba(139, 91, 203, .58)');
    } else {
      gradient.addColorStop(0, 'rgba(45, 19, 84, .46)');
      gradient.addColorStop(0.52, 'rgba(91, 38, 151, .60)');
      gradient.addColorStop(1, 'rgba(166, 70, 244, .72)');
    }
    return gradient;
  }

  function applyChartPalette() {
    const chart = document.getElementById('daily-chart')?._ci;
    const dataset = chart && chart.data && chart.data.datasets && chart.data.datasets[0];
    if (!dataset) return;

    const isLight = document.body.classList.contains('light');
    dataset.backgroundColor = function (context) {
      return statisticsPurpleGradient(context.chart);
    };
    dataset.borderColor = isLight
      ? 'rgba(126, 75, 187, .72)'
      : 'rgba(166, 70, 244, .84)';
    dataset.borderWidth = 1;
    dataset.fill = true;
    if (typeof chart.update === 'function') chart.update('none');
  }'''
stats = replace_between(
    stats,
    '  function applyChartPalette() {',
    '  function applyPresentation(period) {',
    chart_palette,
    'canonical Statistics purple palette',
)
write(stats_path, stats)


# First paint should agree with the reviewed 7-day default before data hydration.
index_path = 'frontend/static/index.html'
index = read(index_path)
index = replace_once(
    index,
    '<span id="chart-title">Completions — last 24 hours</span>',
    '<span id="chart-title">Completions — last 7 days</span>',
    'Statistics first-paint period copy',
)
write(index_path, index)


# Release/install examples follow VERSION. These are real release surfaces, not
# architecture-test accommodations.
old_image = 'ghcr.io/xipher-zero/debridpulse:v1.0.11'
new_image = 'ghcr.io/xipher-zero/debridpulse:v1.0.11.1'

compose_path = 'docker-compose.yml'
compose = read(compose_path)
compose = replace_once(compose, old_image, new_image, 'compose image version')
write(compose_path, compose)

readme_path = 'README.md'
readme = read(readme_path)
count = readme.count(old_image)
if count != 2:
    raise RuntimeError(f'README image version: expected two matches, found {count}')
write(readme_path, readme.replace(old_image, new_image))

project_page_path = 'index.html'
project_page = read(project_page_path)
count = project_page.count(old_image)
if count < 1:
    raise RuntimeError('project page image version: expected at least one v1.0.11 image reference')
write(project_page_path, project_page.replace(old_image, new_image))

print('Applied v1.0.11.1 corrective follow-up')
