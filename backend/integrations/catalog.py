"""The production registration point; core routing remains in IntegrationRegistry."""
from executors.aria2.definition import definition as aria2
from executors.rsync.definition import definition as rsync
from providers.alldebrid.definition import definition as alldebrid
from providers.general_ftp.definition import definition as general_ftp
from providers.general_scp.definition import definition as general_scp
from providers.general_rsync.definition import definition as general_rsync
from providers.general_http.definition import definition as general_http
from providers.general_webdav.definition import definition as general_webdav
from providers.multimeta.definition import definition as multimeta
from providers.realdebrid.definition import definition as realdebrid
from providers.torbox.definition import definition as torbox
from integrations.configuration import effective_integration_settings
from integrations.usenet.definition import definition as usenet

definitions = (alldebrid, realdebrid, torbox, usenet, general_http, general_ftp, general_scp, general_rsync, general_webdav,
               multimeta, aria2, rsync)


def register(registry, settings, environment, selected=definitions):
    for definition in selected:
        # Effective participation is the member's own preference AND its
        # group's gate. Composed here, on a copy, so the persisted namespace
        # keeps the operator's own choice about this member untouched.
        configured = effective_integration_settings(settings, definition)
        implementation = definition.build(configured, environment)
        if definition.kind == "provider":
            registry.register_provider(implementation)
        elif definition.kind == "executor":
            registry.register_executor(implementation)
        elif definition.kind == "provider_executor":
            # One integration, one canonical enabled state, both halves.
            provider, executor = implementation
            registry.register_provider(provider)
            registry.register_executor(executor)
        else:
            raise ValueError("Unsupported integration definition kind")
