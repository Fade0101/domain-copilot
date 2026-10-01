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

The authentication (401) and authorization (403) subtrees are deliberately two
separate hierarchies rather than one: "we do not know who you are" and "we know
who you are and the answer is no" are different answers to the client and carry
different response headers.
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


class ProviderError(ApplicationError):
    """Base error for all LLM/Embedding provider failures."""


class ProviderUnavailableError(ProviderError):
    """Provider is down or unreachable. (Transient)"""


class ProviderRateLimitError(ProviderError):
    """Provider rate limit exceeded. (Transient)"""


class ContextWindowExceededError(ProviderError):
    """Payload exceeds provider maximum context window. (Non-transient)"""


class ProviderAuthenticationError(ProviderError):
    """Authentication failed for the provider. (Non-transient)"""


class ProviderInvalidRequestError(ProviderError):
    """Provider rejected the request as malformed. (Non-transient)"""


class ProviderConfigurationError(ProviderError):
    """Provider setup/configuration is invalid. (Non-transient)"""


class AuthenticationError(ApplicationError):
    """The caller's identity could not be established. Maps to HTTP 401.

    Every subclass is answered at the boundary with the same static message and
    a ``WWW-Authenticate: Bearer`` header. The distinctions below exist for
    server-side logging and tests, never to tell the client which of "no such
    account", "wrong password", or "bad token" applied -- that difference is an
    enumeration oracle.
    """


class MissingCredentialsError(AuthenticationError):
    """The request carried no credentials, or no usable ``Authorization`` header."""


class InvalidCredentialsError(AuthenticationError):
    """The submitted email/password pair did not match a stored credential."""


class InvalidTokenError(AuthenticationError):
    """A presented token was malformed, wrongly signed, or failed a claim check."""


class ExpiredTokenError(InvalidTokenError):
    """A presented token was well-formed and correctly signed but has expired."""


class UnknownPrincipalError(AuthenticationError):
    """A token validated, but its subject no longer resolves to a stored user.

    Raised when a token outlives the account it names. Treated as 401 rather than
    404 because the failure is "this credential no longer identifies anyone",
    not "the resource you asked for is missing".
    """


class AuthorizationError(ApplicationError):
    """The caller is authenticated but not permitted to do this. Maps to HTTP 403."""


class PermissionDeniedError(AuthorizationError):
    """The principal's role does not grant the permission this action requires."""


class ResourceOwnershipError(AuthorizationError):
    """The principal does not own the target object and has no broader grant.

    Distinct from :class:`ResourceNotFoundError`: the object exists and belongs
    to somebody else (BRD AC-8.4, SEC-1a).
    """


class JobStoreError(ApplicationError):
    """Durable storage failed. (Transient server fault; never expose driver details.)"""


class JobQueueUnavailableError(ApplicationError):
    """The broker could not accept a job ID. The PostgreSQL job is still durable."""


class JobPaused(Exception):
    """A handler checkpointed a deliberate wait; leave the job STARTED and release its worker."""


class JobCancelled(Exception):
    """Cooperative cancellation signal; the runner records CANCELLED, not FAILED."""
