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

# These modules use terminal Settings-root observers only to re-run static
# correction work. The earlier resume pass can partially reshape those tails,
# so identify the terminal observer itself rather than depending on one exact
# pre-transform view declaration. Preserve every function and initial schedule
# call above it, remove only the observer tail, and bind to the owner's explicit
# settings-rendered lifecycle instead.
for rel in (
    'frontend/static/ui-settings-authentication.js',
    'frontend/static/ui-settings-authentication-polish.js',
    'frontend/static/ui-settings-authentication-oidc.js',
    'frontend/static/ui-settings-maintenance-wipe.js',
    'frontend/static/ui-settings-notifications.js',
):
    text = read(rel)
    observer_pos = text.rfind('new MutationObserver')
    if observer_pos < 0:
        continue
    if not text.rstrip().endswith('})();'):
        raise RuntimeError(f'unexpected Settings module tail in {rel}')

    view_cut = text.rfind('\n  const view', 0, observer_pos)
    observer_cut = text.rfind('\n    const observer', 0, observer_pos)
    if observer_cut < 0:
        observer_cut = text.rfind('\n  const observer', 0, observer_pos)
    cut = view_cut if view_cut >= 0 else observer_cut
    if cut < 0:
        raise RuntimeError(f'cannot locate terminal observer block start in {rel}')

    text = text[:cut].rstrip() + "\n\n  document.addEventListener('debridpulse:settings-rendered', schedule);\n})();\n"
    write(rel, text)

# The final Settings geometry layer exists as ui-settings-form-layout.css. An
# obsolete import name survived the iterative UI pass and left that accepted
# layer orphaned. Restore the canonical file to the exact same cascade position.
style = read('frontend/static/style-v11.css')
legacy_form_import = "@import url('/ui-settings-form-consistency.css?v=2');"
canonical_form_import = "@import url('/ui-settings-form-layout.css?v=2');"
if legacy_form_import in style:
    style = style.replace(legacy_form_import, canonical_form_import, 1)
write('frontend/static/style-v11.css', style)

# Transform helpers can leave whitespace-only lines when entire functions are
# removed. Normalize trailing whitespace in the transformed frontend text tree
# before git diff --check; this is formatting-only and changes no semantics.
for file in STATIC.iterdir():
    if file.is_file() and file.suffix in {'.js', '.css', '.html'}:
        content = file.read_text()
        normalized = '\n'.join(line.rstrip() for line in content.splitlines())
        if content.endswith('\n'):
            normalized += '\n'
        if normalized != content:
            file.write_text(normalized)

# Re-run the complete targeted executable-observer inventory. Comments that
# document the retired MutationObserver behavior are not runtime scaffolding.
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
    if re.search(r'\bnew\s+MutationObserver\b', read(rel)):
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
