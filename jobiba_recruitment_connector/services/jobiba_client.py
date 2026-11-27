"""HTTP transport to Jobiba. One class, no Odoo models, no logging of secrets.

Endpoints called (all with Authorization: Bearer <employer API key>):

    POST {base}/api/v1/jobs/sync                          create or update jobs
    GET  {base}/api/v1/integrations/entitlements          plan, limits, what is sent
    GET  {base}/api/v1/integrations/jobs?reference=...    job id, address and state
    POST {base}/api/v1/integrations/jobs/close            take jobs offline
    GET  {base}/api/v1/integrations/applications          applications, oldest first
    GET  {base}/api/v1/integrations/applications/{id}     one application (+ CV link)
    POST {base}/api/v1/integrations/applications/status   where applications we hold stand now
    GET  .../applications/{id}/cv?t=...                   the CV file (short-lived link)

The integrations endpoints answer 403 INTEGRATION_OFF until the employer
switches the connection on in Jobiba (Employer dashboard, Integrations).

Every call has bounded connect and read timeouts, validates TLS, and returns
a JobibaResult instead of raising, so the queue decides what to do next.
"""
import json
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests

from . import retry_policy

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://jobiba.com"
SYNC_PATH = "/api/v1/jobs/sync"
INTEGRATION_PATH = "/api/v1/integrations"
MAX_REFERENCES = 100
MAX_CV_BYTES = 10 * 1024 * 1024
MAX_JOBS_PER_REQUEST = 500
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 25
MAX_RESPONSE_BYTES = 512 * 1024
USER_AGENT = "Jobiba-Odoo-Connector/1.0 (+https://jobiba.com/developers)"
_LOCAL_HOSTS = ("localhost", "127.0.0.1")


class JobibaConfigError(Exception):
    """The connection settings cannot be used (bad address, missing key)."""


@dataclass
class JobibaResult:
    status: int | None
    category: str
    data: dict = field(default_factory=dict)
    message: str = ""
    retry_after: int | None = None
    rate_limit: int | None = None
    rate_remaining: int | None = None

    @property
    def ok(self):
        return self.category == retry_policy.OK


def clean_base_url(raw):
    """https only (http allowed for a local test server). No path, no login."""
    text = (raw or DEFAULT_BASE_URL).strip().rstrip("/")
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.username or parts.password or not host:
        raise JobibaConfigError("The Jobiba address is not valid.")
    if parts.scheme != "https" and not (parts.scheme == "http" and host in _LOCAL_HOSTS):
        raise JobibaConfigError("The Jobiba address must start with https://")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise JobibaConfigError("Enter the Jobiba site address only, for example https://jobiba.com")
    port = ":%s" % parts.port if parts.port else ""
    return "%s://%s%s" % (parts.scheme, host, port)


def _int_header(headers, name):
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


def _safe_message(payload, fallback):
    """The server's own short sentence, never a stack trace or a page of HTML."""
    text = ""
    if isinstance(payload, dict):
        text = payload.get("error") or payload.get("message") or ""
        details = payload.get("details")
        if isinstance(details, dict) and details:
            parts = []
            for key, issues in list(details.items())[:12]:
                if isinstance(issues, (list, tuple)):
                    issues = "; ".join(str(i) for i in issues[:4])
                parts.append("%s: %s" % (key, issues))
            text = ("%s %s" % (text, " | ".join(parts))).strip()
    text = " ".join(str(text or fallback).split())
    return text[:600]


class JobibaClient:
    def __init__(self, base_url, api_key, session=None):
        self.base_url = clean_base_url(base_url)
        if not api_key or len(api_key) > 256:
            raise JobibaConfigError("The Jobiba API key is missing.")
        self._api_key = api_key
        self._session = session or requests.Session()

    def __repr__(self):  # never show the key in a traceback or a log line
        return "<JobibaClient %s>" % self.base_url

    def _headers(self, correlation_id, json_body=False):
        headers = {
            "Authorization": "Bearer %s" % self._api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-Request-Id": correlation_id or "",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _send(self, method, url, correlation_id, body=None, params=None, accept_any=False):
        headers = self._headers(correlation_id, json_body=body is not None)
        if accept_any:
            headers["Accept"] = "*/*"
        try:
            return self._session.request(
                method, url,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
                params=params,
                headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=False,
                verify=True,
                stream=True,
            )
        except requests.RequestException as error:
            # Only the class name: an exception text can repeat the address.
            _logger.info("Jobiba request %s failed: %s", correlation_id, type(error).__name__)
            return None

    def _result(self, response, raw):
        payload = {}
        if raw and len(raw) <= MAX_RESPONSE_BYTES:
            try:
                parsed = json.loads(raw.decode("utf-8", "replace"))
                payload = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                payload = {}
        status = response.status_code
        category = retry_policy.classify(status)
        if 300 <= status < 400:
            category = retry_policy.REJECTED
        return JobibaResult(
            status=status,
            category=category,
            data=payload,
            message="" if category == retry_policy.OK else _safe_message(payload, "Jobiba answered HTTP %s." % status),
            retry_after=_int_header(response.headers, "Retry-After"),
            rate_limit=_int_header(response.headers, "X-RateLimit-Limit"),
            rate_remaining=_int_header(response.headers, "X-RateLimit-Remaining"),
        )

    def _request(self, method, path, correlation_id, body=None, params=None):
        response = self._send(method, self.base_url + path, correlation_id, body=body, params=params)
        if response is None:
            return JobibaResult(None, retry_policy.TRANSIENT, message="Jobiba could not be reached.")
        try:
            raw = response.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
        except Exception:
            raw = b""
        finally:
            response.close()
        return self._result(response, raw)

    def _post(self, path, body, correlation_id):
        return self._request("POST", path, correlation_id, body=body)

    # ---- the connection's own state on Jobiba ---------------------------------
    def entitlements(self, correlation_id=None):
        """Plan, job limit and whether applicant details are sent. Works while the switch is off."""
        return self._request("GET", INTEGRATION_PATH + "/entitlements", correlation_id)

    def job_states(self, references, correlation_id=None):
        """Jobiba job id, public address and state for up to 100 of our references."""
        references = [ref for ref in references if ref][:MAX_REFERENCES]
        if not references:
            raise JobibaConfigError("Name at least one job reference.")
        return self._request("GET", INTEGRATION_PATH + "/jobs", correlation_id, params=[("reference", ref) for ref in references])

    def close_jobs(self, references, correlation_id=None):
        """Take published jobs offline on Jobiba. Applications already made stay."""
        references = [ref for ref in references if ref][:MAX_REFERENCES]
        if not references:
            raise JobibaConfigError("Name at least one job reference.")
        return self._request("POST", INTEGRATION_PATH + "/jobs/close", correlation_id, body={"references": references})

    def applications(self, cursor=None, limit=50, correlation_id=None):
        params = [("limit", str(int(limit)))]
        if cursor:
            params.append(("cursor", cursor))
        return self._request("GET", INTEGRATION_PATH + "/applications", correlation_id, params=params)

    def application(self, application_id, correlation_id=None):
        if not application_id or len(application_id) > 64 or "/" in application_id:
            raise JobibaConfigError("That application ID is not valid.")
        return self._request("GET", "%s/applications/%s" % (INTEGRATION_PATH, application_id), correlation_id)

    def application_statuses(self, application_ids, correlation_id=None):
        """Stage and level (withdrawn, erased...) of up to 100 applications we already hold.

        This is how Odoo hears that a candidate withdrew or deleted their
        Jobiba account when no webhook reaches it. No personal data comes back.
        """
        ids = [str(ref) for ref in application_ids if ref][:MAX_REFERENCES]
        if not ids:
            raise JobibaConfigError("Name at least one application.")
        return self._request("POST", INTEGRATION_PATH + "/applications/status", correlation_id, body={"ids": ids})

    def download_cv(self, url, correlation_id=None):
        """The CV behind a link Jobiba just gave. Returns (result, bytes or None).

        The link must be on this connection's own Jobiba address and under the
        applications path: the API key is never sent anywhere else.
        """
        prefix = self.base_url + INTEGRATION_PATH + "/applications/"
        if not isinstance(url, str) or not url.startswith(prefix) or "/cv?" not in url:
            return JobibaResult(None, retry_policy.REJECTED, message="The CV link is not on this Jobiba address."), None
        response = self._send("GET", url, correlation_id, accept_any=True)
        if response is None:
            return JobibaResult(None, retry_policy.TRANSIENT, message="Jobiba could not be reached."), None
        try:
            raw = response.raw.read(MAX_CV_BYTES + 1, decode_content=True)
        except Exception:
            raw = b""
        finally:
            response.close()
        if response.status_code != 200:
            return self._result(response, raw if len(raw) <= MAX_RESPONSE_BYTES else b""), None
        if not raw or len(raw) > MAX_CV_BYTES:
            return JobibaResult(200, retry_policy.REJECTED, message="The CV is empty or larger than 10 MB."), None
        result = JobibaResult(200, retry_policy.OK, data={"content_type": (response.headers.get("Content-Type") or "").split(";")[0].strip()})
        return result, raw

    def sync_jobs(self, jobs, correlation_id=None):
        """Create or update up to 500 jobs. The same reference updates a job."""
        if not jobs or len(jobs) > MAX_JOBS_PER_REQUEST:
            raise JobibaConfigError("A request carries 1 to %s jobs." % MAX_JOBS_PER_REQUEST)
        return self._post(SYNC_PATH, {"jobs": jobs}, correlation_id)

    def test_connection(self, correlation_id=None):
        """Check the key and the plan without creating anything.

        An empty job list passes Jobiba's key, account and plan checks and is
        then refused by field validation (422), before anything is written.
        401 / 403 mean the key or the plan is the problem.
        """
        result = self._post(SYNC_PATH, {"jobs": []}, correlation_id)
        if result.category == retry_policy.VALIDATION:
            return JobibaResult(
                status=result.status, category=retry_policy.OK, data={},
                rate_limit=result.rate_limit, rate_remaining=result.rate_remaining,
            )
        if result.ok:
            # Not expected for an empty list; treat as reachable.
            return result
        return result
