"""1.0.13 Gate-9 rev-5, item 4: the documented ownership must match reality.

The supported topology bundles the acquisition service INSIDE the DebridPulse
container: DebridPulse starts it, stops it, restarts it when it is unhealthy,
owns its configuration, and never exposes it. Earlier prose -- written when the
service was assumed to be an operator-run external instance -- says the
opposite, and stale architecture prose is what makes the next change reason
from a topology that no longer exists.

The invariant to state is narrower than "external" or "internal": the executor
is external to the transfer CORE's implementation, but internal to DebridPulse
PRODUCT ownership.
"""
from __future__ import annotations

from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

SOURCES = (
    BACKEND / "executors" / "sabnzbd" / "admin.py",
    BACKEND / "executors" / "sabnzbd" / "runtime.py",
    BACKEND / "integrations" / "definition.py",
    BACKEND / "integrations" / "usenet" / "definition.py",
    BACKEND / "application" / "service.py",
)


def prose(path):
    """Comments and docstrings only -- this module judges PROSE, never code."""
    import ast
    import io
    import tokenize

    text = path.read_text()
    chunks = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            chunks.append(token.string)
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node)
            if doc:
                chunks.append(doc)
    return "\n".join(chunks).lower()


def collapse(text):
    """One space between words, so a claim cannot hide behind a line break."""
    import re

    return re.sub(r"\s+", " ", text)

# Claims that are now false about the bundled service.
RETIRED_CLAIMS = (
    "does NOT own the SAB daemon lifecycle",
    "there is no start/stop here",
    "externally administered",
    "externally managed",
    "operator-managed service",
    "external SAB",
)


def test_no_source_claims_debridpulse_does_not_own_the_service_lifecycle():
    offenders = []
    for path in SOURCES:
        text = path.read_text()
        lowered = text.lower()
        for claim in RETIRED_CLAIMS:
            if claim.lower() in lowered:
                offenders.append(f"{path.name}: {claim!r}")
    assert not offenders, f"stale ownership prose: {offenders}"


def test_the_administration_module_states_the_real_ownership_invariant():
    text = (BACKEND / "executors" / "sabnzbd" / "admin.py").read_text()
    assert "external to" in text and "core" in text
    assert "internal to" in text and "product" in text.lower()


def test_the_administration_module_documents_that_it_owns_start_and_stop():
    text = (BACKEND / "executors" / "sabnzbd" / "admin.py").read_text()
    assert "start" in text and "stop" in text
    # ...and it genuinely implements them.
    from executors.sabnzbd.admin import SabnzbdAdministration
    for name in ("start", "stop", "maintain"):
        assert callable(getattr(SabnzbdAdministration, name, None)), name


def test_configuration_is_not_described_as_operator_mutation_only():
    """It also applies on start and on convergence after a restart."""
    offenders = []
    for path in SOURCES:
        text = path.read_text()
        for claim in ("only on an explicit operator action",
                      "only on an explicit configuration change"):
            if claim in text:
                offenders.append(f"{path.name}: {claim!r}")
    assert not offenders, f"stale application-trigger prose: {offenders}"


def test_neutral_terminology_is_used_for_the_service():
    """`external service` reads as "someone else's"; the seam is generic."""
    text = (BACKEND / "integrations" / "definition.py").read_text()
    assert "external service" not in text, (
        "use neutral terminology: native integration service / executor-side "
        "service / out-of-core implementation"
    )


# --- concept-level contract (rev-7) ---------------------------------------
#
# Revision 6 checked for two exact sentences, which let semantically identical
# stale wording through. These reject the CLAIM however it is phrased.

# Each entry: (description, regex over collapsed lowercase prose).
FORBIDDEN_CLAIMS = (
    ("configuration applies only on operator action",
     r"only (on|by|through|upon) (an? )?(explicit )?operator[- ]?(action|request|save|mutation)"),
    ("configuration applies only on an explicit mutation/change",
     r"only (on|by|through|upon) (an? )?explicit (configuration )?(mutation|change|save)"),
    ("drift correction requires a re-Save",
     r"(re-?save|operator (action|save))[^.]{0,60}(never a background|required|only way)"),
    ("reconciliation is an operator action only",
     r"reconciliation is an explicit operator action"),
    ("lifecycle convergence never applies configuration",
     r"(lifecycle|start|restart)[^.]{0,40}never appl(y|ies)[^.]{0,20}configuration"),
    ("a native integration service is an 'external service'",
     r"external service"),
)


def test_no_authoritative_source_makes_a_retired_ownership_claim():
    offenders = []
    for path in SOURCES:
        body = collapse(prose(path))
        for description, pattern in FORBIDDEN_CLAIMS:
            import re

            if re.search(pattern, body):
                offenders.append(f"{path.name}: {description}")
    assert not offenders, f"stale architecture prose: {offenders}"


def test_the_intended_core_boundary_wording_is_still_allowed():
    """The contract must reject ownership claims, not the real distinction."""
    import re

    allowed = collapse(
        "the acquisition service is external to the transfer core's implementation "
        "and internal to debridpulse product ownership; it is an out-of-core "
        "implementation reached over a private loopback api."
    )
    for description, pattern in FORBIDDEN_CLAIMS:
        assert not re.search(pattern, allowed), (
            f"the contract wrongly rejects legitimate wording via: {description}"
        )


def test_the_one_way_authority_invariant_is_stated_where_the_seam_lives():
    body = collapse(prose(BACKEND / "integrations" / "definition.py"))
    assert "one-way authority" in body or "one way" in body
    assert "never the reverse" in body or "never imports" in body


def test_configuration_application_documents_both_of_its_triggers():
    """A reader must not conclude that only a Save applies configuration."""
    for path, in ((BACKEND / "integrations" / "definition.py",),
                  (BACKEND / "executors" / "sabnzbd" / "admin.py",)):
        body = collapse(prose(path))
        assert "mutation" in body, path.name
        assert ("restart" in body or "start" in body), path.name
        assert "convergence" in body, path.name


def test_drift_is_documented_as_detection_only_without_claiming_operator_only_repair():
    body = collapse(prose(BACKEND / "executors" / "sabnzbd" / "admin.py"))
    assert "detection only" in body
    # ...and the repair path is not described as an operator-only act.
    assert "reconciliation is an explicit operator action" not in body
