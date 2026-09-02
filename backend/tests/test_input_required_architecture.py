"""Permanent semantic architecture guards for Roadmap Items 3 and 4."""
from __future__ import annotations

from pathlib import Path

from db import database
from transfers.input_required import username_password, username_private_key
from transfers.models import InputField, InputMethod, TransferState
from transfers.policy import transition_allowed


ROOT = Path(__file__).resolve().parents[2]
AUTH_RUNTIME = ROOT / "frontend" / "static" / "ui-auth-required.js"


def test_authentication_methods_are_exact_and_passphrase_is_not_a_method() -> None:
    assert set(InputMethod) == {
        InputMethod.USERNAME_PASSWORD,
        InputMethod.USERNAME_PRIVATE_KEY,
    }
    assert all(method.value != "passphrase" for method in InputMethod)


def test_username_password_descriptor_requires_only_username_and_password() -> None:
    descriptor = username_password()
    assert descriptor.method == InputMethod.USERNAME_PASSWORD
    assert [(field.name, field.required) for field in descriptor.fields] == [
        (InputField.USERNAME, True),
        (InputField.PASSWORD, True),
    ]


def test_private_key_descriptor_has_optional_passphrase_field() -> None:
    descriptor = username_private_key()
    assert descriptor.method == InputMethod.USERNAME_PRIVATE_KEY
    assert [(field.name, field.required) for field in descriptor.fields] == [
        (InputField.USERNAME, True),
        (InputField.PRIVATE_KEY, True),
        (InputField.PASSPHRASE, False),
    ]


def test_durable_challenge_and_runtime_state_schemas_have_no_credential_fields() -> None:
    forbidden = {"username", "password", "private_key", "passphrase", "credentials", "credential", "secret"}
    assert not (database._INPUT_CHALLENGE_COLUMNS & forbidden)
    assert not (database._RUNTIME_STATE_COLUMNS & forbidden)
    challenge_ddl = "\n".join(database.INPUT_CHALLENGE_SCHEMA).lower()
    assert not any(f" {name} " in challenge_ddl for name in forbidden)


def test_input_required_is_nonterminal_in_the_universal_transition_policy() -> None:
    assert transition_allowed(TransferState.INPUT_REQUIRED, TransferState.RESOLVING)
    assert transition_allowed(TransferState.INPUT_REQUIRED, TransferState.QUEUED)
    assert transition_allowed(TransferState.INPUT_REQUIRED, TransferState.PAUSED)
    assert not transition_allowed(TransferState.COMPLETED, TransferState.INPUT_REQUIRED)


def test_item4_browser_runtime_uses_only_neutral_challenge_contract() -> None:
    source = AUTH_RUNTIME.read_text(encoding="utf-8").lower()
    required = {
        "input_required",
        "auth_required",
        "username_password",
        "username_private_key",
        "challenge_id",
        "passphrase",
    }
    assert required <= {token for token in required if token in source}

    # Integration/protocol knowledge must not enter the generic modal runtime.
    forbidden = {
        "alldebrid",
        "aria2",
        "sftp",
        "rsync",
        "location.protocol",
        "url.protocol",
        "url.scheme",
        "hostname",
    }
    assert not any(token in source for token in forbidden)


def test_item4_browser_runtime_has_no_secret_persistence_apis() -> None:
    source = AUTH_RUNTIME.read_text(encoding="utf-8").lower()
    forbidden = {
        "localstorage",
        "sessionstorage",
        "indexeddb",
        "document.cookie",
        "navigator.credentials",
    }
    assert not any(token in source for token in forbidden)


def test_item4_browser_runtime_closes_from_challenge_resolution_not_transfer_activity() -> None:
    source = AUTH_RUNTIME.read_text(encoding="utf-8")
    assert "if (!isAuthTransfer(item))" in source
    assert "finishActive();" in source
    assert "downloading" not in source.lower()
    assert "transferring" not in source.lower()
