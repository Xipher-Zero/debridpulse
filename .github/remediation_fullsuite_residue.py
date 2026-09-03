from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {text.count(old)}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# Test-only HTTP fake: URL participation is explicit applicability, never inferred
# from descriptor.request_types.
replace_once(
    "backend/tests/test_application_runtime.py",
    "from fake_integrations import MemoryExecutor, ParcelProvider\nfrom transfers.engine import TransferEngine\n",
    "from fake_integrations import MemoryExecutor, ParcelProvider\nfrom transfers.applicability import ProviderApplicability\nfrom transfers.engine import TransferEngine\n",
)
replace_once(
    "backend/tests/test_application_runtime.py",
    "    provider = ParcelProvider()\n    provider.descriptor = replace(provider.descriptor, request_types=frozenset({\"parcel\", \"http\", \"https\", \"magnet\", \"torrent\"}))\n",
    "    provider = ParcelProvider()\n    monkeypatch.setattr(\n        ParcelProvider,\n        \"applicability\",\n        property(lambda _provider: ProviderApplicability(generic_schemes=frozenset({\"http\", \"https\"}))),\n    )\n    provider.descriptor = replace(provider.descriptor, request_types=frozenset({\"parcel\", \"http\", \"https\", \"magnet\", \"torrent\"}))\n",
)

# Opaque synthetic routing must opt into the explicit static applicability path.
replace_once(
    "backend/tests/test_universal_contracts.py",
    "from transfers.errors import (\n",
    "from transfers.applicability import ProviderApplicability\nfrom transfers.errors import (\n",
)
replace_once(
    "backend/tests/test_universal_contracts.py",
    "    async def resolve(self, request):\n        return ResolutionResult(ResourceState.AVAILABLE)\n\n\ndef test_routing_requires_enabled_healthy_capability_and_request_support():\n",
    "    @property\n    def applicability(self):\n        return ProviderApplicability()\n\n    async def resolve(self, request):\n        return ResolutionResult(ResourceState.AVAILABLE)\n\n\ndef test_routing_requires_enabled_healthy_capability_and_request_support():\n",
)

# UIARCH-001: canonical CSS/app.js own presentation; accessibility runtime must
# not repair footer paint or provider premium labels after render.
replace_once(
    "backend/tests/test_ui_cross_page_consistency_contract.py",
    "    assert \"#torrent-pagination\" in downloads\n    assert \"border-top: 0;\" in downloads\n    assert \"removeProperty('border-top')\" in runtime\n",
    "    assert \"#torrent-pagination\" in downloads\n    assert \"border-top: 0;\" in downloads\n    assert \"removeProperty('border-top')\" not in runtime\n",
)
replace_once(
    "backend/tests/test_ui_cross_page_consistency_contract.py",
    "    provider = read_static(\"ui-shell-provider-status.css\")\n    runtime = read_static(\"ui-accessibility-runtime.js\")\n",
    "    provider = read_static(\"ui-shell-provider-status.css\")\n    app = read_static(\"app.js\")\n    runtime = read_static(\"ui-accessibility-runtime.js\")\n",
)
replace_once(
    "backend/tests/test_ui_cross_page_consistency_contract.py",
    "    assert \"#lbl-premium::before\" in provider\n    assert \"content: none !important\" in provider\n    assert \"AllDebrid Premium until \" in runtime\n    assert \"days remaining)\" in runtime\n    assert \"MutationObserver(normalizeProviderPremiumLabel)\" in runtime\n",
    "    assert \"#lbl-premium::before\" in provider\n    assert \"content: none !important\" in provider\n    assert \"dp-provider-premium-until\" in app\n    assert \"dp-provider-premium-days\" in app\n    assert \"normalizeProviderPremiumLabel\" not in runtime\n    assert \"AllDebrid Premium until \" not in runtime\n",
)

# The mixed submission contract is behavioral/structural, not frozen flavor copy
# or inline geometry from an earlier UI generation.
replace_once(
    "backend/tests/test_v106_audit_contracts.py",
    "    assert 'One item per line · Empty + Add opens a .torrent file' in html\n    assert 'column-gap:14px' in html\n    assert 'font-size:11px;font-weight:400;color:var(--text3)' in html\n    assert 'style=\"display:flex;gap:6px;margin-left:auto\"' in html\n    assert 'One item per line. Leave empty and click Add to choose a .torrent file.' not in html\n",
    "    assert 'Add links, magnets, or torrent files to the queue.' in html\n    assert 'when empty, choose a .torrent file' in html\n",
)

# Current architecture records permanent real-browser validation without reviving
# obsolete presentation-loader/finalization ownership.
replace_once(
    "docs/UI_FRONTEND_ARCHITECTURE.md",
    "## Compatibility retained intentionally\n",
    "## Permanent browser validation\n\nThe `Browser Runtime` workflow is permanent CI and provides the real-browser smoke contract across the six canonical navigation surfaces. It validates the canonical render owners directly, including responsive/theme behavior and visual checkpoints; it does not depend on retired presentation-loader/finalization dependencies or any post-render corrective pass. Removed correction runtimes must not be reintroduced as a corrective mechanism to satisfy browser validation.\n\n## Compatibility retained intentionally\n",
)
replace_once(
    "backend/tests/test_ui_release_cleanup_contract.py",
    "    assert \"permanent ci\" in architecture\n    assert \"real-browser smoke contract\" in architecture\n    assert \"six canonical navigation surfaces\" in architecture\n    assert \"retired presentation-loader/finalization dependencies\" in architecture\n    assert \"live calibration\" not in architecture\n    assert \"there is no live presentation-loader/finalization bootstrap\" in architecture\n    assert \"retired `ui-presentation-loader.*`\" in architecture\n    assert \"`ui-page-finalization.*`\" in architecture\n    assert \"must not be reintroduced as a corrective mechanism\" in architecture\n",
    "    assert \"permanent ci\" in architecture\n    assert \"browser runtime\" in architecture\n    assert \"real-browser smoke contract\" in architecture\n    assert \"six canonical navigation surfaces\" in architecture\n    assert \"retired presentation-loader/finalization dependencies\" in architecture\n    assert \"live calibration\" not in architecture\n    assert \"`ui-runtime.js` and `ui-downloads-runtime.js` are physically absent\" in architecture\n    assert \"must not be reintroduced as a corrective mechanism\" in architecture\n",
)

# Retained runtime description must match its post-audit scope.
replace_once(
    "frontend/static/ui-accessibility-runtime.js",
    "/* DebridPulse v1.0.11 cross-cutting interaction accessibility.\n *\n * Presentation/interaction semantics only. This module does not call the API,\n * alter transfer state, or replace established app.js behavior; it makes the\n * inherited clickable-div surfaces keyboard-operable, keeps ARIA state in\n * sync with the existing active-class contract, and normalizes a small number\n * of legacy presentation-only DOM details that cannot live in CSS alone.\n */\n",
    "/* DebridPulse v1.0.12 cross-cutting accessibility and dropdown semantics.\n *\n * This module does not call the API, alter transfer state, replace established\n * app.js behavior, or repair canonical presentation. It makes inherited\n * clickable controls keyboard-operable, keeps ARIA state synchronized with the\n * active-class contract, and projects native-select dropdown semantics for\n * dynamically rendered controls.\n */\n",
)
