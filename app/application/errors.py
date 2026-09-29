"""Application-layer error taxonomy.

Failures that arise from *orchestrating* a use case (rather than from a domain
rule) derive from :class:`ApplicationError` -- for example "requested resource
does not exist" or "prompt configuration is invalid". Domain-rule violations
continue to use :mod:`app.domain.shared.errors`.

The presentation layer maps both taxonomies to HTTP status codes by type
(:mod:`app.presentation.api.errors`), so no use case ever raises a framework
(``HTTPException``) error itself. Note the split by *fault*: resource-not-found
is a client error (404), whereas configuration failures are server faults (500)
whose internal detail is never returned to the caller.
"""

from __future__ import annotations


class ApplicationError(Exception):
    """Base class for application/use-case orchestration failures."""


class ResourceNotFoundError(ApplicationError):
    """A requested resource could not be found. Maps to HTTP 404 at the boundary."""


class JobNotFoundError(ResourceNotFoundError):
    """An async job (T7) referenced by id does not exist. Named in BRD AR-5.

    Raising logic is added by the async-jobs tickets (#20-22); declared here so
    the taxonomy and its 404 mapping exist now and downstream has a stable
    import target.
    """


class ConfigurationError(ApplicationError):
    """The application is misconfigured (bad/missing prompt, invalid settings).

    A server fault, not client input: maps to HTTP 500 with a static, safe
    message. The specific cause is logged server-side but never returned to the
    caller (SDD A.5.1 -- never expose internal details).
    """


class PromptNotFoundError(ConfigurationError):
    """A requested prompt id/version is not present in the prompt store."""


class PromptValidationError(ConfigurationError):
    """A prompt artifact is malformed or fails schema validation on load."""
