from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import REAL_GET_CLIENT, JobibaCase


@tagged("post_install", "-at_install", "jobiba")
class TestSecurity(JobibaCase):
    def test_secrets_cannot_be_read_by_anyone_through_the_orm(self):
        for user in (self.manager, self.recruiter, self.outsider):
            instance = self.instance.with_user(user)
            for field in ("api_key", "webhook_secret", "webhook_token"):
                with self.assertRaises(AccessError):
                    instance.read([field])
        values = self.instance.with_user(self.manager).read()[0]
        self.assertNotIn("api_key", values)
        self.assertNotIn("webhook_secret", values)
        self.assertNotIn("jbk_test_key_for_unit_tests_only", str(values))
        found = self.env["jobiba.instance"].with_user(self.manager).search_read([], [])
        self.assertNotIn("jbk_test_key_for_unit_tests_only", str(found))

    def test_only_integration_administrators_configure(self):
        with self.assertRaises(AccessError):
            self.instance.with_user(self.recruiter).write({"auto_publish": True})
        with self.assertRaises(UserError):
            self.instance.with_user(self.recruiter).action_open_connect_wizard()
        with self.assertRaises(UserError):
            self.instance.with_user(self.recruiter).action_disconnect()
        with self.assertRaises(AccessError):
            self.env["jobiba.connect.wizard"].with_user(self.recruiter).create({"instance_id": self.instance.id})
        self.instance.with_user(self.manager).write({"auto_publish": True})

    def test_recruiter_sees_state_but_not_logs_or_events(self):
        self.instance._log("info", "test", "hello")
        recruiter_env = self.env(user=self.recruiter)
        self.assertEqual(recruiter_env["jobiba.instance"].search([]).state, "connected")
        with self.assertRaises(AccessError):
            recruiter_env["jobiba.sync.log"].search([])
        with self.assertRaises(AccessError):
            recruiter_env["jobiba.webhook.event"].search([])
        with self.assertRaises(AccessError):
            self.env(user=self.outsider)["jobiba.instance"].search([])

    def test_companies_are_isolated(self):
        other = self.env["res.company"].create({"name": "Other Group Co"})
        other_instance = self.env["jobiba.instance"].create({"name": "Jobiba B", "company_id": other.id})
        other_instance.sudo().write({"api_key": "jbk_other", "api_key_set": True, "state": "connected"})
        other_job = self.env["hr.job"].create({
            "name": "Other Company Role", "company_id": other.id, "jobiba_approved": True,
            "jobiba_location": "Dubai, United Arab Emirates",
            "description": "<p>%s</p>" % ("Run the front office of the hotel. " * 8)})
        other_job._jobiba_enqueue()
        own_job = self.make_job()
        own_job._jobiba_enqueue()
        self.assertEqual(own_job._jobiba_instance(), self.instance)
        self.assertEqual(other_job._jobiba_instance(), other_instance)

        manager_env = self.env(user=self.manager)          # belongs to the first company only
        self.assertEqual(manager_env["jobiba.instance"].search([]), self.instance)
        self.assertEqual(manager_env["jobiba.job.mapping"].search([]).job_id, own_job)
        self.assertFalse(manager_env["jobiba.sync.job"].search([("company_id", "=", other.id)]))
        with self.assertRaises(AccessError):
            other_instance.with_user(self.manager).read(["name"])

        self.run_queue()                                    # only this connection's jobs travel with its key
        self.assertEqual([job["title"] for call in self.client.calls for job in call], ["Maintenance Engineer"])

    def test_one_connection_per_company(self):
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self.env["jobiba.instance"].create({"name": "Second", "company_id": self.company.id})

    def test_connect_wizard_keeps_no_secret(self):
        wizard = self.env["jobiba.connect.wizard"].with_user(self.manager).create({
            "instance_id": self.instance.id, "api_key": "jbk_new_key_value_123456", "webhook_secret": "whsec_new_secret_value_123456",
            "data_consent": True})
        wizard.action_connect()
        self.assertFalse(wizard.api_key)
        self.assertFalse(wizard.webhook_secret)
        self.assertEqual(self.instance.sudo().api_key, "jbk_new_key_value_123456")
        self.assertTrue(wizard.result_ok)
        self.assertIn("FREE", wizard.result)                 # the plan is read at once, not ten minutes later
        self.assertTrue(self.instance.plan_checked_at)
        logs = self.env["jobiba.sync.log"].search([("instance_id", "=", self.instance.id)])
        self.assertNotIn("jbk_new_key", " ".join(logs.mapped("message")))

    def test_connect_needs_the_agreement(self):
        self.instance.sudo().write({"data_consent_at": False, "data_consent_user_id": False})
        wizard = self.env["jobiba.connect.wizard"].with_user(self.manager).create({
            "instance_id": self.instance.id, "api_key": "jbk_another_key_value_654321"})
        self.assertFalse(wizard.data_consent)
        with self.assertRaises(UserError):
            wizard.action_connect()
        self.assertEqual(self.instance.sudo().api_key, "jbk_test_key_for_unit_tests_only")   # nothing was saved
        self.assertFalse(self.instance.data_consent_at)

        wizard.data_consent = True
        wizard.action_connect()
        self.assertTrue(self.instance.data_consent_at)
        self.assertEqual(self.instance.data_consent_user_id, self.manager)
        self.assertEqual(self.instance.sudo().api_key, "jbk_another_key_value_654321")
        again = self.env["jobiba.connect.wizard"].with_user(self.manager).create({"instance_id": self.instance.id})
        self.assertTrue(again.data_consent)                  # asked once per connection

        first = self.instance.data_consent_at
        again.action_connect()
        self.assertEqual(self.instance.data_consent_at, first)

        self.instance.with_user(self.manager).action_disconnect()
        self.assertFalse(self.instance.data_consent_at)
        self.assertFalse(self.instance.data_consent_user_id)

    def test_no_request_can_be_built_without_the_agreement(self):
        self.instance.sudo().write({"data_consent_at": False})
        with self.assertRaises(UserError):
            REAL_GET_CLIENT(self.instance)
        self.instance.sudo().write({"data_consent_at": "2026-11-26 10:00:00"})
        self.assertEqual(type(REAL_GET_CLIENT(self.instance)).__name__, "JobibaClient")
