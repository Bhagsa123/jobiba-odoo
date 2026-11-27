from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged

from ..services import retry_policy
from .common import JobibaCase


@tagged("post_install", "-at_install", "jobiba")
class TestJobSync(JobibaCase):
    def mapping(self, job):
        return self.env["jobiba.job.mapping"].search([("job_id", "=", job.id)])

    def test_publish_creates_then_updates_with_the_same_reference(self):
        job = self.make_job()
        queued, refused = job._jobiba_enqueue()
        self.assertEqual(queued, job)
        self.assertFalse(refused)
        self.assertEqual(self.run_queue(), 1)
        mapping = self.mapping(job)
        self.assertEqual(mapping.remote_state, "published")
        self.assertEqual(self.instance.state, "connected")
        first_reference = self.client.calls[0][0]["reference"]

        job.name = "Maintenance Engineer (MEP)"          # an edit queues an update by itself
        self.assertEqual(self.run_queue(), 1)
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.client.calls[1][0]["reference"], first_reference)
        self.assertEqual(self.client.calls[1][0]["title"], "Maintenance Engineer (MEP)")
        self.assertEqual(len(mapping), 1, "one mapping per job and connection")

    def test_unchanged_job_is_not_sent_again(self):
        job = self.make_job()
        job._jobiba_enqueue()
        self.run_queue()
        self.env["jobiba.sync.job"]._enqueue(self.mapping(job))
        self.assertEqual(self.run_queue(), 0, "same payload hash: no request")
        self.assertEqual(len(self.client.calls), 1)

    def test_new_job_waits_for_jobiba_review(self):
        self.client.answer(200, {"success": True, "upserted": 1, "waiting_for_review": 1})
        job = self.make_job()
        job._jobiba_enqueue()
        self.run_queue()
        self.assertEqual(job.jobiba_state, "in_review")

    def test_invalid_job_is_never_sent_and_shows_the_field(self):
        job = self.make_job(address_id=False)
        queued, refused = job._jobiba_enqueue()
        self.assertFalse(queued)
        self.assertIn("location", refused[job].lower())
        self.assertEqual(self.run_queue(), 0)
        self.assertFalse(self.client.calls)
        self.assertEqual(job.jobiba_state, "draft", "an invalid job never shows as published")

    def test_approval_is_required_and_reserved_to_administrators(self):
        job = self.make_job(jobiba_approved=False)
        _queued, refused = job._jobiba_enqueue()
        self.assertIn("approved", refused[job])
        with self.assertRaises(AccessError):
            job.with_user(self.recruiter).jobiba_approved = True
        job.with_user(self.manager).jobiba_approved = True
        self.assertEqual(job.with_user(self.recruiter)._jobiba_enqueue()[0], job)

    def test_batch_validation_error_is_narrowed_to_one_job(self):
        good, bad = self.make_job(name="Electrician"), self.make_job(name="Plumber")
        (good | bad)._jobiba_enqueue()
        self.client.answer(422, {"error": "Validation failed.", "details": {"jobs": ["Invalid"]}}, "Validation failed. jobs: Invalid")
        self.client.answer(200, {"success": True, "upserted": 1, "waiting_for_review": 0})
        self.client.answer(422, {"error": "Validation failed."}, "Validation failed. jobs: location")
        self.assertEqual(self.run_queue(requests=3), 3)
        self.assertEqual([len(call) for call in self.client.calls], [2, 1, 1])
        self.assertEqual(good.jobiba_state, "published")
        self.assertEqual(bad.jobiba_state, "failed")
        self.assertIn("location", bad.jobiba_problems)
        item = self.env["jobiba.sync.job"].search([("mapping_id", "=", self.mapping(bad).id)])
        self.assertEqual(item.state, "blocked", "an unchanged payload is not retried after 422")

    def test_401_and_403_block_until_fixed(self):
        for status, category in ((401, retry_policy.CREDENTIAL), (403, retry_policy.PLAN)):
            self.instance.sudo().write({"state": "connected"})
            job = self.make_job(name="Role %s" % status)
            job._jobiba_enqueue()
            self.client.answer(status, {"error": "API access is not included in your current plan."}, "API access is not included in your current plan.")
            self.run_queue()
            item = self.env["jobiba.sync.job"].search([("mapping_id", "=", self.mapping(job).id)])
            self.assertEqual((item.state, item.error_category), ("blocked", category))
            self.assertEqual(self.instance.state, "attention")
            self.assertEqual(job.jobiba_state, "failed")
            calls = len(self.client.calls)
            self.run_queue()
            self.assertEqual(len(self.client.calls), calls, "no automatic retry")

    def test_429_pauses_the_queue_for_retry_after(self):
        job = self.make_job()
        job._jobiba_enqueue()
        self.client.answer(429, {"error": "Too many sync requests."}, "Too many sync requests.", retry_after=60, remaining=0)
        self.run_queue()
        item = self.env["jobiba.sync.job"].search([("mapping_id", "=", self.mapping(job).id)])
        self.assertEqual(item.state, "retry")
        self.assertEqual(item.attempts, 0, "a rate limit is not the job's failure")
        wait = (self.instance.paused_until - fields.Datetime.now()).total_seconds()
        self.assertGreaterEqual(wait, 55)
        self.assertEqual(self.run_queue(), 0, "nothing is sent while paused")
        self.assertEqual(len(self.client.calls), 1)
        self.instance.sudo().paused_until = fields.Datetime.now() - timedelta(seconds=1)
        item.next_retry_at = fields.Datetime.now() - timedelta(seconds=1)
        self.assertEqual(self.run_queue(), 1)
        self.assertEqual(job.jobiba_state, "published")

    def test_server_errors_back_off_then_give_up(self):
        job = self.make_job()
        job._jobiba_enqueue()
        item = self.env["jobiba.sync.job"].search([("mapping_id", "=", self.mapping(job).id)])
        for attempt in range(1, retry_policy.MAX_ATTEMPTS + 1):
            self.client.answer(503, {}, "Jobiba answered HTTP 503.")
            self.instance.sudo().write({"paused_until": False, "consecutive_failures": 0})
            item.next_retry_at = False
            self.run_queue(requests=1)
            self.assertEqual(item.attempts, attempt)
        self.assertEqual(item.state, "dead_letter")
        self.assertEqual(job.jobiba_state, "failed")
        item.action_retry()
        self.assertEqual(self.run_queue(), 1)
        self.assertEqual(job.jobiba_state, "published", "manual replay is safe: the reference updates")

    def test_circuit_breaker_rests_the_connection(self):
        jobs = self.env["hr.job"]
        for index in range(retry_policy.CIRCUIT_BREAKER_FAILURES):
            jobs |= self.make_job(name="Role %s" % index)
        jobs._jobiba_enqueue()
        self.instance.batch_size = 1
        for _index in range(retry_policy.CIRCUIT_BREAKER_FAILURES):
            self.client.answer(None, {}, "Jobiba could not be reached (Timeout).")
            self.run_queue(requests=1)
        self.assertTrue(self.instance.paused_until)
        self.assertEqual(self.run_queue(), 0)

    def test_lost_answer_is_sent_again(self):
        job = self.make_job()
        job._jobiba_enqueue()
        item = self.env["jobiba.sync.job"].search([("mapping_id", "=", self.mapping(job).id)])
        item.write({"state": "processing", "lease_until": fields.Datetime.now() - timedelta(minutes=1)})
        self.assertEqual(self.run_queue(), 1)
        self.assertEqual(item.state, "done")

    def test_batch_size_and_rate_headers(self):
        jobs = self.env["hr.job"]
        for index in range(5):
            jobs |= self.make_job(name="Technician %s" % index)
        jobs._jobiba_enqueue()
        self.instance.batch_size = 2
        self.client.answer(200, {"success": True, "upserted": 2, "waiting_for_review": 0}, remaining=5)
        self.client.answer(200, {"success": True, "upserted": 2, "waiting_for_review": 0}, remaining=1)
        self.assertEqual(self.run_queue(requests=5), 2, "stops when one request is left in the window")
        self.assertEqual([len(call) for call in self.client.calls], [2, 2])
        self.assertEqual(self.instance.rate_remaining, 1)

    def test_stop_archive_and_delete(self):
        job = self.make_job()
        job._jobiba_enqueue()
        self.run_queue()
        job.action_jobiba_stop()
        self.assertEqual(job.jobiba_state, "stopped")
        job.name = "Changed after stop"
        self.assertEqual(self.run_queue(), 0, "a stopped job is never restarted by an edit")
        mapping = self.mapping(job)
        job.unlink()
        self.assertEqual(mapping.remote_state, "orphan")
        self.assertEqual(mapping.job_name, "Changed after stop")

    def test_auto_publish_is_opt_in_and_scoped(self):
        department = self.env["hr.department"].create({"name": "Engineering", "company_id": self.company.id})
        other = self.env["hr.department"].create({"name": "Sales", "company_id": self.company.id})
        job = self.make_job(department_id=department.id)
        self.assertFalse(self.mapping(job), "off by default")
        self.instance.write({"auto_publish": True, "auto_publish_department_ids": [(6, 0, department.ids)]})
        outside = self.make_job(name="Sales Executive", department_id=other.id)
        inside = self.make_job(name="Site Engineer", department_id=department.id)
        self.assertFalse(self.mapping(outside))
        self.assertEqual(self.mapping(inside).remote_state, "queued")

    def test_disconnect_removes_secrets_and_cancels_the_queue(self):
        job = self.make_job()
        job._jobiba_enqueue()
        self.instance.with_user(self.manager).action_disconnect()
        self.assertFalse(self.instance.sudo().api_key)
        self.assertEqual(self.instance.state, "disconnected")
        self.assertEqual(self.run_queue(), 0)
        self.assertEqual(self.env["jobiba.sync.job"].search([("mapping_id", "=", self.mapping(job).id)]).state, "cancelled")

    def test_preview_opens_saved_with_its_lines(self):
        """The button returns a wizard that already exists: a browser shows an unsaved one with no lines."""
        good = self.make_job()
        bad = self.make_job(name="Site Engineer", description=False)
        action = (good | bad).with_user(self.recruiter).action_jobiba_preview()
        self.assertTrue(action.get("res_id"))
        self.assertNotIn("default_job_ids", action.get("context") or {})
        wizard = self.env["jobiba.publish.wizard"].with_user(self.recruiter).browse(action["res_id"])
        self.assertEqual(wizard.line_ids.mapped("job_id"), good | bad)
        self.assertEqual((wizard.ready_count, wizard.blocked_count), (1, 1))
        self.assertIn("Maintenance Engineer", wizard.line_ids.filtered("ready").preview)
        self.assertTrue(wizard.line_ids.filtered(lambda line: not line.ready).problems)

        wizard.action_publish()
        self.assertTrue(wizard.done)
        self.assertEqual([job["title"] for call in self.client.calls for job in call], ["Maintenance Engineer"])
        self.assertFalse(self.mapping(bad))
