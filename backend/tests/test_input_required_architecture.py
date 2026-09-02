"""Permanent semantic architecture guards for Roadmap Item 3."""
from __future__ import annotations

from db import database
from transfers.input_required import username_password, username_private_key
from transfers.models import InputField, InputMethod, TransferState
from transfers.policy import transition_allowed


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
