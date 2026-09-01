"""The production registration point; core routing remains in IntegrationRegistry."""
from executors.aria2.definition import definition as aria2
from providers.alldebrid.definition import definition as alldebrid

definitions = (alldebrid, aria2)


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
