"""HTTP composition seam; tests may supply an unrelated integration registry."""
from fastapi import Request

from application.service import ApplicationService


def get_application(request: Request) -> ApplicationService:
    service = getattr(request.app.state, "application", None)
    if service is None:
        from application.composition import application
        service = application
        request.app.state.application = service
    return service
