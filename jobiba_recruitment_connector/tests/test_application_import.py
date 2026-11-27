import json
import time

from odoo.tests import HttpCase, tagged

from ..services import webhook_signature
from .common import JobibaCase

SECRET = "whsec_unit_test_secret_value"
TOKEN = "tok_unit_test_webhook_address_0001"


def body_for(event, application_id="app-1", name="Asha Verma", stage="APPLIED", job=None, delivery="d-1"):
    return {
        "id": delivery, "event": event, "created_at": "2026-11-24T10:00:00.000Z",
        "data": {
            "application": {"id": application_id, "name": name, "stage": stage, "previous_stage": None,
                            "applied_at": "2026-11-24T09:59:00.000Z", "email": "asha@example.com", "phone": "+919876543210"},
            "job": job or {"id": "job-uuid-1", "title": "Maintenance Engineer", "url": "https://jobiba.in/job/maintenance-engineer-abc"},
            "applicants_url": "https://jobiba.com/employer/dashboard/applications?job=job-uuid-1",
        },
    }


@tagged("post_install", "-at_install", "jobiba")
class TestApplicationEvents(JobibaCase):
    def deliver(self, event, **kwargs):
        body = body_for(event, **kwargs)
        record, created = self.env["jobiba.webhook.event"]._ingest(self.instance, body["id"], event, body)
        if created:
            record._process()
        return record, created

    def published_job(self):
        job = self.make_job()
        job._jobiba_enqueue()
        self.run_queue()
        return job

    def test_one_application_makes_exactly_one_applicant(self):
        job = self.published_job()
        event, created = self.deliver("application.created")
        self.assertTrue(created)
        self.assertEqual(event.state, "processed")
        applicant = event.applicant_id
        self.assertEqual(applicant.job_id, job)
        self.assertEqual(applicant.partner_name, "Asha Verma")
        self.assertEqual(applicant.source_id, self.env.ref("jobiba_recruitment_connector.utm_source_jobiba"))
        self.assertTrue(applicant.is_jobiba)
        self.assertFalse(applicant.email_from, "contact details are never taken from the message")
        self.assertFalse(event.sudo().payload, "the stored copy is cleared after use")
        mapping = self.env["jobiba.job.mapping"].search([("job_id", "=", job.id)])
        self.assertEqual(mapping.jobiba_job_id, "job-uuid-1")
        self.assertEqual(mapping.listing_url, "https://jobiba.in/job/maintenance-engineer-abc")

        # The same delivery again, and a second delivery for the same application.
        _again, created_again = self.deliver("application.created")
        self.assertFalse(created_again)
        self.deliver("application.created", delivery="d-2")
        self.assertEqual(self.env["hr.applicant"].search_count([("job_id", "=", job.id)]), 1)
        self.assertEqual(self.env["jobiba.application.mapping"].search_count([("application_ref", "=", "app-1")]), 1)

    def test_only_used_fields_are_stored(self):
        self.published_job()
        body = body_for("application.created", job={"id": "x", "title": "No such job"})
        event, _created = self.env["jobiba.webhook.event"]._ingest(self.instance, "d-9", "application.created", body)
        stored = event.sudo().payload
        self.assertNotIn("asha@example.com", stored)
        self.assertNotIn("9876543210", stored)

    def test_unknown_job_goes_to_a_person_not_to_another_job(self):
        self.published_job()
        event, _created = self.deliver("application.created", job={"id": "job-uuid-9", "title": "Chef de Partie", "url": ""})
        self.assertEqual(event.state, "error")
        self.assertFalse(self.env["hr.applicant"].search([("partner_name", "=", "Asha Verma")]))
        self.assertEqual(self.instance.event_error_count, 1)

    def test_two_jobs_with_one_title_are_never_guessed(self):
        self.published_job()
        self.published_job()
        event, _created = self.deliver("application.created")
        self.assertEqual(event.state, "error")

    def test_reference_wins_when_jobiba_sends_it(self):
        self.published_job()
        second = self.published_job()
        reference = self.env["jobiba.job.mapping"].search([("job_id", "=", second.id)]).reference
        event, _created = self.deliver("application.created", job={"id": "j2", "title": "Maintenance Engineer", "reference": reference})
        self.assertEqual(event.applicant_id.job_id, second)

    def test_archived_job_gets_no_applicant_and_reprocess_works(self):
        job = self.published_job()
        job.active = False
        event, _created = self.deliver("application.created")
        self.assertEqual(event.state, "error")
        job.active = True
        event.action_reprocess()
        self.assertEqual(event.state, "processed")
        self.assertEqual(event.applicant_id.job_id, job)

    def test_stage_and_withdrawal_never_change_the_odoo_stage(self):
        self.published_job()
        event, _created = self.deliver("application.created")
        applicant = event.applicant_id
        stage = applicant.stage_id
        self.deliver("application.stage_changed", stage="INTERVIEW", delivery="d-3")
        self.assertEqual(applicant.jobiba_external_stage, "INTERVIEW")
        self.deliver("application.withdrawn", stage="WITHDRAWN", delivery="d-4")
        self.assertTrue(applicant.jobiba_withdrawn_at)
        self.assertEqual(applicant.stage_id, stage)
        self.assertTrue(applicant.active, "an applicant is never deleted or archived by Jobiba")

    def test_foreign_links_are_dropped(self):
        self.published_job()
        body = body_for("application.created")
        body["data"]["applicants_url"] = "https://evil.example/phish"
        body["data"]["job"]["url"] = "javascript:alert(1)"
        event, _created = self.env["jobiba.webhook.event"]._ingest(self.instance, "d-7", "application.created", body)
        event._process()
        self.assertFalse(event.applicant_id.jobiba_url)
        self.assertNotIn("evil.example", event.applicant_id.applicant_notes or "")

    def test_import_switch(self):
        self.published_job()
        self.instance.application_import = False
        event, _created = self.deliver("application.created")
        self.assertEqual(event.state, "ignored")


@tagged("post_install", "-at_install", "jobiba")
class TestWebhookEndpoint(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env["jobiba.instance"].create({"name": "Jobiba", "company_id": cls.env.company.id})
        cls.instance.sudo().write({
            "state": "connected", "api_key": "jbk_x", "api_key_set": True,
            "webhook_secret": SECRET, "webhook_secret_set": True, "webhook_token": TOKEN})

    def post(self, body, secret=SECRET, stamp=None, token=TOKEN, delivery=None, signature=None):
        raw = json.dumps(body).encode()
        stamp = int(time.time()) if stamp is None else stamp
        headers = {
            "Content-Type": "application/json",
            "X-Jobiba-Event": body.get("event", ""),
            "X-Jobiba-Delivery": delivery or body.get("id", ""),
            "X-Jobiba-Timestamp": str(stamp),
            "X-Jobiba-Signature": signature or "sha256=" + webhook_signature.sign(secret, stamp, raw),
        }
        return self.url_open("/jobiba/webhook/%s" % token, data=raw, headers=headers)

    def test_ping_confirms_the_webhook(self):
        response = self.post({"id": "ping-1", "event": "ping", "data": {"message": "A test from Jobiba."}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.instance.webhook_verified_at)
        replay = self.post({"id": "ping-1", "event": "ping", "data": {}})
        self.assertEqual(replay.json(), {"ok": True, "duplicate": True})

    def test_refusals_store_nothing(self):
        Event = self.env["jobiba.webhook.event"]
        body = {"id": "bad-1", "event": "application.created", "data": {}}
        self.assertEqual(self.post(body, secret="wrong-secret-value-000").status_code, 401)
        self.assertEqual(self.post(body, stamp=int(time.time()) - 3600).status_code, 401)
        self.assertEqual(self.post(body, signature="sha256=00").status_code, 401)
        self.assertEqual(self.post(body, token="tok_unknown_address_0000000000").status_code, 404)
        self.assertFalse(Event.search([("delivery_id", "=", "bad-1")]))
        log = self.env["jobiba.sync.log"].search([("instance_id", "=", self.instance.id), ("level", "=", "security")])
        self.assertEqual(len(log), 3)
        self.assertEqual(self.url_open("/jobiba/webhook/%s" % TOKEN).status_code, 405, "GET is not served")

    def test_oversized_body_is_refused(self):
        raw = b"x" * (webhook_signature.MAX_BODY_BYTES + 1)
        response = self.url_open("/jobiba/webhook/%s" % TOKEN, data=raw, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

    def test_disconnected_address_answers_gone(self):
        self.instance.sudo().state = "disconnected"
        self.assertEqual(self.post({"id": "g-1", "event": "ping", "data": {}}).status_code, 410)
