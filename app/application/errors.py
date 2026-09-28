"""Application-layer error taxonomy.

Failures that arise from *orchestrating* a use case (rather than from a domain
rule) derive from :class:`ApplicationError` -- for example "requested resource
does not exist" or "operation not permitted for this actor". Domain-rule
violations continue to use :mod:`app.domain.shared.errors`.

The presentation layer maps both taxonomies to HTTP status codes by type, so no
use case ever raises a framework (``HTTPException``) error itself.

Resource-not-found failures named in BRD AR-5 (e.g. ``JobNotFoundError``) will
subclass :class:`ResourceNotFoundError` in their owning tickets.
"""

from __future__ import annotations


class ApplicationError(Exception):
    """Base class for application/use-case orchestration failures."""


class ResourceNotFoundError(ApplicationError):
    """A requested resource could not be found. Maps to HTTP 404 at the boundary."""
