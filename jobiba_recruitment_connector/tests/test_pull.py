from odoo.tests import tagged

from .common import JobibaCase


def row(application_id, details="on_jobiba", status="APPLIED", name="Asha Verma", reference=None, **extra):
    data = {
        "id": application_id, "status": status, "applied_at": "2026-11-24T09:59:00.000Z", "details": details,
        "name": None if details == "withdrawn" else name,
        "job": {"id": "job-uuid-1", "reference": reference, "title": "Maintenance Engineer"},
        "applicants_url": "https://jobiba.com/employer/dashboard/applications?job=job-uuid-1",
    }
    data.update(extra)
    return data


FULL = {
    "email": "asha@example.com", "phone": "+91 98765 43210",
    "answers": [{"question": "Do you hold a licence?", "answer": "Yes <b>B</b>"}],
    "cv": {"available": True, "filename": "CV.pdf", "content_type": "application/pdf",
           "download_url": "https://jobiba.com/api/v1/integrations/applications/app-2/cv?t=1.x",
           "expires_at": "2026-11-24T10:05:00.000Z"},
}


@tagged("post_install", "-at_install", "jobiba")
class TestPull(JobibaCase):
    def published(self):
        job = self.make_job()
        job._jobiba_enqueue()
        self.run_queue()
        mapping = self.env["jobiba.job.mapping"].search([("job_id", "=", job.id)])
        return job, mapping

    def test_plan_is_read_from_jobiba_and_never_worked_out_here(self):
        self.instance._refresh_plan()
        self.assertEqual(self.instance.plan_name, "FREE")
        self.assertEqual(self.instance.job_limit_text, "3")
        self.assertTrue(self.instance.integration_enabled)
        self.assertFalse(self.instance.applicant_details)
        self.client.plan = dict(self.client.plan, plan="GROWTH", paid_plan=True,
                                jobs={"open": 4, "limit": None}, applicants={"details": True})
        self.instance._refresh_plan()
        self.assertEqual(self.instance.job_limit_text, "No limit")
        self.assertTrue(self.instance.applicant_details)

    def test_switched_off_in_jobiba_means_nothing_is_read(self):
        self.client.plan = dict(self.client.plan, integration={"enabled": False, "message": "Switch it on in Jobiba."})
        self.env["jobiba.instance"]._cron_pull()
        self.assertFalse(self.instance.integration_enabled)
        self.assertEqual(self.instance.plan_message, "Switch it on in Jobiba.")
        self.assertFalse([call for call in self.client.calls if call[0] in ("applications", "job_states", "close_jobs")])

    def test_job_state_address_and_id_come_from_jobiba(self):
        job, mapping = self.published()
        self.client.states = {mapping.reference: {"id": "job-uuid-1", "state": "in_review", "url": "https://jobiba.in/job/x", "applications": 0}}
        self.instance._pull_job_states()
        self.assertEqual(mapping.remote_state, "in_review")
        self.assertFalse(mapping.listing_url, "no public address while it waits for review")
        self.client.states[mapping.reference].update(state="published", applications=2)
        self.instance._pull_job_states()
        self.assertEqual(mapping.remote_state, "published")
        self.assertEqual(mapping.jobiba_job_id, "job-uuid-1")
        self.assertEqual(mapping.listing_url, "https://jobiba.in/job/x")
        self.assertEqual(mapping.remote_applications, 2)
        self.client.states[mapping.reference].update(state="not_approved", url="https://evil.example/job")
        self.instance._pull_job_states()
        self.assertEqual(mapping.remote_state, "not_approved")
        self.assertIn("did not approve", job.jobiba_last_error)

    def test_stop_closes_the_listing_and_a_failed_close_is_tried_again(self):
        job, mapping = self.published()
        self.client.close_fails = True
        job.action_jobiba_stop()
        self.assertTrue(mapping.close_pending, "kept for the next try")
        self.assertEqual(mapping.remote_state, "stopped")
        self.client.close_fails = False
        self.assertEqual(self.instance._push_closes(), 1)
        self.assertFalse(mapping.close_pending)
        self.assertIn(("close_jobs", [mapping.reference]), self.client.calls)

    def test_archiving_the_job_asks_jobiba_to_close_it(self):
        job, mapping = self.published()
        job.active = False
        self.assertTrue(mapping.close_pending)
        never_sent = self.make_job(name="Draft only")
        never_sent.active = False
        self.assertFalse(self.env["jobiba.job.mapping"].search([("job_id", "=", never_sent.id)]).close_pending)

    def test_free_plan_applicant_has_a_name_and_a_link_only(self):
        job, mapping = self.published()
        self.client.pages = [[row("app-1", reference=mapping.reference)]]
        self.instance._pull_applications()
        applicant = self.env["hr.applicant"].search([("job_id", "=", job.id)])
        self.assertEqual(len(applicant), 1)
        self.assertEqual(applicant.partner_name, "Asha Verma")
        self.assertFalse(applicant.email_from)
        self.assertEqual(applicant.jobiba_detail_state, "link")
        self.assertFalse([call for call in self.client.calls if call[0] in ("application", "download_cv")])
        # Read again from the start: still one applicant.
        self.instance.sudo().applications_cursor = False
        self.instance._pull_applications()
        self.assertEqual(self.env["hr.applicant"].search_count([("job_id", "=", job.id)]), 1)

    def test_paid_plan_details_answers_and_cv_land_on_the_native_applicant(self):
        job, mapping = self.published()
        self.client.pages = [[row("app-2", details="full", reference=mapping.reference)]]
        self.client.details = {"app-2": row("app-2", details="full", reference=mapping.reference, **FULL)}
        self.instance._pull_applications()
        applicant = self.env["hr.applicant"].search([("job_id", "=", job.id)])
        self.assertEqual(applicant.email_from, "asha@example.com")
        self.assertEqual(applicant.partner_phone, "+91 98765 43210")
        self.assertEqual(applicant.jobiba_detail_state, "full")
        self.assertIn("Do you hold a licence?", applicant.applicant_notes)
        self.assertIn("Yes &lt;b&gt;B&lt;/b&gt;", applicant.applicant_notes, "answers are escaped, never markup")
        link = applicant.sudo().jobiba_application_ids
        self.assertEqual(link.cv_attachment_id.res_model, "hr.applicant")
        self.assertEqual(link.cv_attachment_id.res_id, applicant.id)
        self.assertEqual(bytes(link.cv_attachment_id.raw), b"%PDF-1.4 unit test")
        # A second read imports nothing twice.
        self.instance.sudo().applications_cursor = False
        calls = len([call for call in self.client.calls if call[0] == "download_cv"])
        self.instance._pull_applications()
        self.assertEqual(len([call for call in self.client.calls if call[0] == "download_cv"]), calls)
        self.assertEqual(self.env["ir.attachment"].search_count([("res_model", "=", "hr.applicant"), ("res_id", "=", applicant.id)]), 1)

    def test_details_are_not_imported_when_the_company_switched_that_off(self):
        job, mapping = self.published()
        self.instance.import_details = False
        self.client.pages = [[row("app-2", details="full", reference=mapping.reference)]]
        self.client.details = {"app-2": row("app-2", details="full", reference=mapping.reference, **FULL)}
        self.instance._pull_applications()
        applicant = self.env["hr.applicant"].search([("job_id", "=", job.id)])
        self.assertFalse(applicant.email_from)
        self.assertEqual(applicant.jobiba_detail_state, "link")

    def test_a_recruiters_own_email_is_not_overwritten(self):
        job, mapping = self.published()
        self.client.pages = [[row("app-2", reference=mapping.reference)]]
        self.instance._pull_applications()
        applicant = self.env["hr.applicant"].search([("job_id", "=", job.id)])
        (applicant.candidate_id if "candidate_id" in applicant._fields else applicant).email_from = "typed@example.com"
        self.client.details = {"app-2": row("app-2", details="full", reference=mapping.reference, **FULL)}
        applicant.action_jobiba_fetch_details()
        self.assertEqual(applicant.email_from, "typed@example.com")
        self.assertEqual(applicant.partner_phone, "+91 98765 43210")

    def test_withdrawal_keeps_the_record_notes_it_and_imports_nothing_more(self):
        job, mapping = self.published()
        self.client.pages = [[row("app-3", reference=mapping.reference)], [row("app-3", details="withdrawn", status="WITHDRAWN")]]
        self.instance._pull_applications()
        applicant = self.env["hr.applicant"].search([("job_id", "=", job.id)])
        stage = applicant.stage_id
        link = applicant.sudo().jobiba_application_ids
        self.assertTrue(link.withdrawn_at)
        self.assertEqual(applicant.jobiba_detail_state, "withdrawn")
        self.assertTrue(applicant.exists(), "the Odoo record is kept")
        self.assertEqual(applicant.stage_id, stage, "Odoo's stage is not moved")
        self.assertTrue(applicant.message_ids.filtered(lambda m: "withdrew" in (m.body or "")), "the event is on the record")
        self.client.details = {"app-3": row("app-3", details="full", reference=mapping.reference, **FULL)}
        self.assertFalse(link._import_details(self.client.details["app-3"]), "nothing is imported after a withdrawal")
        self.assertFalse(applicant.email_from)
        with self.assertRaises(Exception):
            applicant.action_jobiba_fetch_details()

    def test_a_withdrawn_application_odoo_never_held_is_not_created(self):
        job, mapping = self.published()
        self.client.pages = [[row("app-9", details="withdrawn", status="WITHDRAWN")]]
        self.instance._pull_applications()
        self.assertFalse(self.env["hr.applicant"].search([("job_id", "=", job.id)]))

    def test_a_withdrawal_after_the_first_read_reaches_odoo_without_a_webhook(self):
        job, mapping = self.published()
        self.client.pages = [[row("app-20", reference=mapping.reference)]]
        self.instance._pull_applications()
        applicant = self.env["hr.applicant"].search([("job_id", "=", job.id)])
        link = applicant.sudo().jobiba_application_ids
        stage = applicant.stage_id
        self.assertFalse(link.withdrawn_at)
        # Jobiba still says "applied": the stage is noted, nothing else happens.
        self.client.statuses = {"app-20": {"status": "SCREENING", "details": "on_jobiba"}}
        self.assertEqual(self.instance._recheck_applications(), 1)
        self.assertEqual(link.external_stage, "SCREENING")
        self.assertTrue(link.status_checked_at)
        self.assertFalse(link.withdrawn_at)
        # The candidate withdraws on Jobiba; no webhook is set up. The next turn tells Odoo.
        self.client.statuses = {"app-20": {"status": "WITHDRAWN", "details": "withdrawn"}}
        self.instance._recheck_applications()
        self.assertTrue(link.withdrawn_at)
        self.assertEqual(applicant.jobiba_detail_state, "withdrawn")
        self.assertEqual(applicant.stage_id, stage, "Odoo's stage is not moved")
        self.assertTrue(applicant.message_ids.filtered(lambda m: "withdrew" in (m.body or "")))
        # A withdrawn application is not asked about again.
        self.client.calls.clear()
        self.assertEqual(self.instance._recheck_applications(), 0)
        self.assertFalse([call for call in self.client.calls if call[0] == "application_statuses"])

    def test_a_deleted_jobiba_account_is_noted_and_nothing_more_is_imported(self):
        job, mapping = self.published()
        self.client.pages = [[row("app-21", reference=mapping.reference)]]
        self.instance._pull_applications()
        applicant = self.env["hr.applicant"].search([("job_id", "=", job.id)])
        link = applicant.sudo().jobiba_application_ids
        self.client.statuses = {"app-21": {"status": "APPLIED", "details": "erased"}}
        self.instance._recheck_applications()
        self.assertTrue(link.erased_at)
        self.assertEqual(applicant.jobiba_detail_state, "erased")
        self.assertTrue(applicant.exists(), "the Odoo record is the company's own and is kept")
        self.assertTrue(applicant.message_ids.filtered(lambda m: "deleted their Jobiba account" in (m.body or "")))
        full = row("app-21", details="full", reference=mapping.reference, **FULL)
        self.assertFalse(link._import_details(full), "nothing is imported after the account was deleted")
        with self.assertRaises(Exception):
            applicant.action_jobiba_fetch_details()

    def test_an_erased_application_odoo_never_held_is_not_created(self):
        job, mapping = self.published()
        self.client.pages = [[row("app-22", details="erased", name=None)]]
        self.instance._pull_applications()
        self.assertFalse(self.env["hr.applicant"].search([("job_id", "=", job.id)]))

    def test_the_recheck_takes_the_applications_checked_longest_ago_first(self):
        job, mapping = self.published()
        self.client.pages = [[row("app-30", reference=mapping.reference), row("app-31", name="Ravi Nair", reference=mapping.reference)]]
        self.instance._pull_applications()
        links = self.env["jobiba.application.mapping"].sudo().search([("instance_id", "=", self.instance.id)])
        first = links.filtered(lambda link: link.application_ref == "app-30")
        first.status_checked_at = "2026-11-24 10:00:00"
        self.client.calls.clear()
        self.client.statuses = {}
        self.instance._recheck_applications()
        asked = [call for call in self.client.calls if call[0] == "application_statuses"][0][1]
        self.assertEqual(asked[0], "app-31", "never checked goes first")
        # An id Jobiba does not answer for is left as it is, and only asked about later.
        self.assertFalse(links.filtered("withdrawn_at"))
        self.assertTrue(all(links.mapped("status_checked_at")))

    def test_a_failed_recheck_changes_nothing_and_is_tried_again(self):
        job, mapping = self.published()
        self.client.pages = [[row("app-40", reference=mapping.reference)]]
        self.instance._pull_applications()
        link = self.env["jobiba.application.mapping"].sudo().search([("application_ref", "=", "app-40")])
        self.client.status_fails = True
        self.assertEqual(self.instance._recheck_applications(), 0)
        self.assertFalse(link.status_checked_at, "not marked as checked")

    def test_details_that_became_available_later_are_fetched_once(self):
        job, mapping = self.published()
        self.client.pages = [[row("app-50", reference=mapping.reference)]]
        self.instance._pull_applications()
        applicant = self.env["hr.applicant"].search([("job_id", "=", job.id)])
        self.assertEqual(applicant.jobiba_detail_state, "link")
        self.client.statuses = {"app-50": {"status": "APPLIED", "details": "full"}}
        self.client.details = {"app-50": row("app-50", details="full", reference=mapping.reference, **FULL)}
        self.instance._recheck_applications()
        self.assertEqual(applicant.jobiba_detail_state, "full")
        self.client.calls.clear()
        self.instance._recheck_applications()
        self.assertFalse([call for call in self.client.calls if call[0] == "application"], "not fetched a second time")

    def test_webhook_first_then_the_pull_adds_the_details_once(self):
        job, mapping = self.published()
        body = {"id": "d-1", "event": "application.created", "data": {
            "application": {"id": "app-2", "name": "Asha Verma", "stage": "APPLIED", "applied_at": "2026-11-24T09:59:00.000Z"},
            "job": {"id": "job-uuid-1", "title": "Maintenance Engineer", "reference": mapping.reference, "url": "https://jobiba.in/job/x"},
            "applicants_url": "https://jobiba.com/employer/dashboard/applications?job=job-uuid-1"}}
        event, _created = self.env["jobiba.webhook.event"]._ingest(self.instance, "d-1", "application.created", body)
        event._process()
        self.client.pages = [[row("app-2", details="full", reference=mapping.reference)]]
        self.client.details = {"app-2": row("app-2", details="full", reference=mapping.reference, **FULL)}
        self.instance._pull_applications()
        applicant = self.env["hr.applicant"].search([("job_id", "=", job.id)])
        self.assertEqual(len(applicant), 1, "one applicant, whichever arrives first")
        self.assertEqual(applicant.email_from, "asha@example.com")

    def test_cv_link_must_be_on_the_connections_own_address(self):
        from ..services.jobiba_client import JobibaClient
        client = JobibaClient("https://jobiba.com", "k" * 40)
        for url in ("https://evil.example/api/v1/integrations/applications/a/cv?t=1",
                    "https://jobiba.com.evil.example/api/v1/integrations/applications/a/cv?t=1",
                    "https://jobiba.com/other/path?cv", None):
            result, content = client.download_cv(url)
            self.assertIsNone(content)
            self.assertFalse(result.ok)
