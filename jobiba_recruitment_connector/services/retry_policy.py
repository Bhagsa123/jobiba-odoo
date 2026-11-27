"""What one answer from Jobiba means for a queued item, and when to try again.

Pure functions: no Odoo imports, so the rules are unit-tested on their own.
The table mirrors Jobiba's documented answers for POST /api/v1/jobs/sync.
"""
import random

MAX_ATTEMPTS = 6
BASE_DELAY_SECONDS = 60
MAX_DELAY_SECONDS = 6 * 3600
DEFAULT_RETRY_AFTER_SECONDS = 60
# Consecutive transient failures of one connection before it rests.
CIRCUIT_BREAKER_FAILURES = 5
CIRCUIT_BREAKER_REST_SECONDS = 15 * 60

OK = "ok"
CREDENTIAL = "credential"
PLAN = "plan"
VALIDATION = "validation"
RATE_LIMITED = "rate_limited"
TRANSIENT = "transient"
REJECTED = "rejected"

#: Categories that are retried by the worker without a person.
RETRYABLE = (RATE_LIMITED, TRANSIENT)


def classify(status):
    """HTTP status (None = no answer: timeout, reset, DNS) -> category."""
    if status is None:
        return TRANSIENT
    if 200 <= status < 300:
        return OK
    if status == 401:
        return CREDENTIAL
    if status == 403:
        return PLAN
    if status == 422:
        return VALIDATION
    if status == 429:
        return RATE_LIMITED
    if status >= 500 or status == 408:
        return TRANSIENT
    return REJECTED


def backoff_seconds(attempt, rng=random.random):
    """Exponential backoff with +/-20 % jitter. `attempt` starts at 1."""
    attempt = max(1, int(attempt))
    delay = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    return int(delay * (0.8 + 0.4 * rng()))


def rate_limit_seconds(retry_after, rng=random.random):
    """Honour Retry-After, never less than it, plus up to 15 s of jitter."""
    try:
        wait = int(float(retry_after))
    except (TypeError, ValueError):
        wait = DEFAULT_RETRY_AFTER_SECONDS
    wait = min(max(wait, 1), MAX_DELAY_SECONDS)
    return wait + int(15 * rng())


def exhausted(attempts):
    return attempts >= MAX_ATTEMPTS
