"""FastAPI dependency providers (BRD AR-3).

Bridges FastAPI's ``Depends()`` to the composition root. Presentation code
depends on *use cases* built by the container from ports; it never imports or
instantiates an infrastructure adapter. This indirection is what the
``presentation -> infrastructure`` import ban and the AST boundary test protect,
and what lets the integration test resolve the real adapter through DI.
"""

from __future__ import annotations

from app.application.documents.use_cases import RegisterDocumentUseCase
from app.application.ports.prompts import IPromptProvider
from app.core.container import Container, get_container


def get_app_container() -> Container:
    """Provide the process-wide composition root."""
    return get_container()


def get_register_document_use_case() -> RegisterDocumentUseCase:
    """Provide the RegisterDocument use case wired to its ports by the container."""
    return get_container().register_document_use_case()


def get_prompt_provider() -> IPromptProvider:
    """Provide the versioned prompt provider built by the container (AR-4)."""
    return get_container().prompt_provider
