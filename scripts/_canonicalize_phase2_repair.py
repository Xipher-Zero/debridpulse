from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'frontend' / 'static'

def read(path):
    return (ROOT / path).read_text()

def write(path, text):
    (ROOT / path).write_text(text)

# Downloads bulk state now has an explicit bulkAction completion lifecycle.
path = 'frontend/static/ui-downloads-runtime.js'
text = read(path)
observer = """    new MutationObserver(function () { syncBulkButtonPresentation(bar); })
      .observe(header, {childList: true, subtree: true, characterData: true});
"""
if observer in text:
    text = text.replace(observer, '', 1)
write(path, text)

# These five modules all end with a Settings-root correction observer. The body
# before that point is preserved exactly; only the observer tail becomes the
# explicit canonical-render lifecycle emitted by ui-settings-page.js.
view_marker = "\n  const view = document.getElementById('view-settings');"
for rel in (
    'frontend/static/ui-settings-authentication.js',
    'frontend/static/ui-settings-authentication-polish.js',
    'frontend/static/ui-settings-authentication-oidc.js',
    'frontend/static/ui-settings-maintenance-wipe.js',
    'frontend/static/ui-settings-notifications.js',
):
    text = read(rel)
    if 'MutationObserver' not in text:
        continue
    cut = text.rfind(view_marker)
    if cut < 0:
        raise RuntimeError(f'Settings observer marker missing in {rel}')
    if not text.rstrip().endswith('})();'):
        raise RuntimeError(f'unexpected Settings module tail in {rel}')
    text = text[:cut] + "\n  document.addEventListener('debridpulse:settings-rendered', schedule);\n})();\n"
    write(rel, text)

# Re-run the complete targeted observer inventory.
for rel in (
    'frontend/static/ui-runtime.js',
    'frontend/static/ui-downloads-runtime.js',
    'frontend/static/ui-settings-authentication.js',
    'frontend/static/ui-settings-authentication-polish.js',
    'frontend/static/ui-settings-authentication-oidc.js',
    'frontend/static/ui-settings-authentication-callback.js',
    'frontend/static/ui-settings-maintenance-wipe.js',
    'frontend/static/ui-settings-notifications.js',
    'frontend/static/ui-settings-downloads-completion.js',
    'frontend/static/ui-settings-aria2-live.js',
    'frontend/static/ui-settings-card-icons.js',
):
    if 'MutationObserver' in read(rel):
        raise RuntimeError(f'correction observer remains in {rel}')

style = read('frontend/static/style-v11.css')
for match in re.finditer(r"@import url\('/([^?']+)", style):
    if not (STATIC / match.group(1)).exists():
        raise RuntimeError(f'broken style import: {match.group(1)}')

index = read('frontend/static/index.html')
for forbidden in ('ui-shell-runtime.js', 'ui-help-chrome.js', 'ui-page-finalization.js'):
    if forbidden in index:
        raise RuntimeError(f'forbidden runtime remains loaded: {forbidden}')

print('phase2 repair guard passed')
