from unittest.mock import patch

from odoo.tests import TransactionCase, new_test_user

from ..services import retry_policy
from ..services.jobiba_client import JobibaResult

from ..models.jobiba_instance import JobibaInstance

INSTANCE_MODEL = "odoo.addons.jobiba_recruitment_connector.models.jobiba_instance.JobibaInstance"
# Taken before any test replaces it: the factory with its own checks.
REAL_GET_CLIENT = JobibaInstance._get_client


class FakeClient:
    """Stands in for JobibaClient. Answers come from a queue of results."""

    def __init__(self):
        self.calls = []
        self.answers = []

    def answer(self, status, data=None, message="", retry_after=None, remaining=11):
        self.answers.append(JobibaResult(
            status=status, category=retry_policy.classify(status), data=data or {}, message=message,
            retry_after=retry_after, rate_limit=12, rate_remaining=remaining))

    def sync_jobs(self, jobs, correlation_id=None):
        self.calls.append(jobs)
        if self.answers:
            return self.answers.pop(0)
        return JobibaResult(200, retry_policy.OK, {"success": True, "upserted": len(jobs), "waiting_for_review": 0},
                            rate_limit=12, rate_remaining=11)

    def test_connection(self, correlation_id=None):
        return JobibaResult(200, retry_policy.OK, rate_limit=12, rate_remaining=11)

    # ---- what Odoo reads back -------------------------------------------------
    plan = {
        "plan": "FREE", "paid_plan": False, "integration": {"enabled": True},
        "jobs": {"open": 1, "limit": 3}, "applicants": {"details": False, "message": "Details need a paid plan."},
    }

    def _ok(self, data):
        return JobibaResult(200, retry_policy.OK, data, rate_limit=60, rate_remaining=59)

    def entitlements(self, correlation_id=None):
        self.calls.append(("entitlements",))
        return self._ok(self.plan)

    def job_states(self, references, correlation_id=None):
        self.calls.append(("job_states", list(references)))
        rows = getattr(self, "states", {})
        return self._ok({"jobs": [dict(rows[ref], reference=ref) for ref in references if ref in rows],
                         "not_found": [ref for ref in references if ref not in rows]})

    def close_jobs(self, references, correlation_id=None):
        self.calls.append(("close_jobs", list(references)))
        if getattr(self, "close_fails", False):
            return JobibaResult(503, retry_policy.TRANSIENT, message="busy")
        return self._ok({"closed": list(references), "already_closed": [], "not_public": [], "not_found": []})

    def applications(self, cursor=None, limit=50, correlation_id=None):
        self.calls.append(("applications", cursor))
        pages = getattr(self, "pages", [])
        index = int(cursor or 0)
        rows = pages[index] if index < len(pages) else []
        return self._ok({"applications": rows, "next_cursor": str(min(index + 1, len(pages))) if pages else cursor,
                         "has_more": index + 1 < len(pages)})

    def application(self, application_id, correlation_id=None):
        self.calls.append(("application", application_id))
        row = getattr(self, "details", {}).get(application_id)
        if not row:
            return JobibaResult(404, retry_policy.REJECTED, message="Application not found.")
        return self._ok({"application": row})

    def application_statuses(self, application_ids, correlation_id=None):
        self.calls.append(("application_statuses", list(application_ids)))
        if getattr(self, "status_fails", False):
            return JobibaResult(503, retry_policy.TRANSIENT, message="busy")
        rows = getattr(self, "statuses", {})
        return self._ok({"applications": [dict(rows[ref], id=ref) for ref in application_ids if ref in rows],
                         "not_found": [ref for ref in application_ids if ref not in rows]})

    def download_cv(self, url, correlation_id=None):
        self.calls.append(("download_cv", url))
        return JobibaResult(200, retry_policy.OK, {"content_type": "application/pdf"}), b"%PDF-1.4 unit test"


class JobibaCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.instance = cls.env["jobiba.instance"].create({"name": "Jobiba", "company_id": cls.company.id})
        cls.instance.sudo().write({
            "api_key": "jbk_test_key_for_unit_tests_only", "api_key_set": True, "state": "connected",
            "webhook_secret": "whsec_unit_test_secret_value", "webhook_secret_set": True,
            "webhook_token": "tok_unit_test_webhook_address_0001",
            "data_consent_at": "2026-11-01 09:00:00",
        })
        cls.country = cls.env.ref("base.in")
        cls.address = cls.env["res.partner"].create({
            "name": "Plant", "city": "Ahmedabad", "country_id": cls.country.id})
        cls.manager = new_test_user(
            cls.env, login="jobiba_mgr", groups="hr_recruitment.group_hr_recruitment_manager,jobiba_recruitment_connector.group_jobiba_manager")
        cls.recruiter = new_test_user(cls.env, login="jobiba_rec", groups="hr_recruitment.group_hr_recruitment_user")
        cls.outsider = new_test_user(cls.env, login="jobiba_out", groups="base.group_user")

    def setUp(self):
        super().setUp()
        self.client = FakeClient()
        patcher = patch(INSTANCE_MODEL + "._get_client", lambda instance: self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_job(self, **vals):
        values = {
            "name": "Maintenance Engineer",
            "company_id": self.company.id,
            "address_id": self.address.id,
            "description": "<p>Keep the plant's electrical and HVAC systems running.</p>"
                           "<ul><li>Plan preventive maintenance</li><li>Lead a team of six technicians</li></ul>"
                           "<p>We ask for five years of experience in MEP operations and a diploma or degree.</p>",
            "jobiba_approved": True,
        }
        values.update(vals)
        return self.env["hr.job"].create(values)

    def run_queue(self, requests=3):
        return self.env["jobiba.sync.job"]._process_instance(self.instance, max_requests=requests)
