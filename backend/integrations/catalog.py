"""The production registration point; core routing remains in IntegrationRegistry."""
from executors.aria2.definition import definition as aria2
from providers.alldebrid.definition import definition as alldebrid
from providers.general_ftp.definition import definition as general_ftp
from providers.general_http.definition import definition as general_http

definitions = (alldebrid, general_http, general_ftp, aria2)


def register(registry, settings, environment, selected=definitions):
    for definition in selected:
        configured = settings.integrations[definition.id]
        implementation = definition.build(configured, environment)
        if definition.kind == "provider":
            registry.register_provider(implementation)
        elif definition.kind == "executor":
            registry.register_executor(implementation)
        else:
            raise ValueError("Unsupported integration definition kind")
