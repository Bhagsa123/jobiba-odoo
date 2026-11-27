from odoo.tests import tagged

from ..models.hr_contract_type import EMPLOYMENT_TYPE_FIELD, EMPLOYMENT_TYPE_MODEL
from ..models.hr_job import SYNCED_FIELDS
from ..services.job_payload_mapper import build_job_payload, payload_hash
from .common import JobibaCase


@tagged("post_install", "-at_install", "jobiba")
class TestJobMapping(JobibaCase):
    def fields_of(self, problems):
        return {field for field, _message in problems}

    def test_minimum_payload(self):
        job = self.make_job()
        payload, problems, _warnings = build_job_payload(job, self.instance)
        self.assertFalse(problems)
        self.assertEqual(payload["title"], "Maintenance Engineer")
        self.assertEqual(payload["location"], "Ahmedabad, India")
        self.assertNotIn("apply_url", payload, "applications stay on Jobiba by default")
        self.assertNotIn("salary_min", payload)
        self.assertTrue(payload["reference"].startswith("odoo:"))
        self.assertTrue(payload["reference"].endswith(":%s:%s" % (self.company.id, job.id)))

    def test_reference_is_stable_when_the_title_changes(self):
        job = self.make_job()
        first = build_job_payload(job, self.instance)[0]
        job.with_context(jobiba_no_sync=True).name = "Senior Maintenance Engineer"
        second = build_job_payload(job, self.instance)[0]
        self.assertEqual(first["reference"], second["reference"])
        self.assertNotEqual(payload_hash(first), payload_hash(second))

    def test_required_fields(self):
        job = self.make_job(address_id=False, description=False)
        _payload, problems, _warnings = build_job_payload(job, self.instance)
        self.assertEqual(self.fields_of(problems), {"location", "description"})
        job.jobiba_location = "Pune, Maharashtra, India"
        job.jobiba_description = "<p>%s</p>" % ("Run the night shift safely. " * 10)
        self.assertFalse(build_job_payload(job, self.instance)[1])

    def test_html_is_reduced_to_safe_tags(self):
        job = self.make_job(description=(
            '<div class="x" style="color:red"><h1>Role</h1><script>alert(1)</script>'
            '<p onclick="x()">Fix <a href="https://evil.example/apply">pumps</a> &amp; panels</p>'
            '<img src="https://evil.example/x.png"/><ul><li>Safety first</li></ul></div>'))
        text = build_job_payload(job, self.instance)[0]["description"]
        for banned in ("<script", "alert(1)", "onclick", "href", "evil.example", "<img", "style=", "class=", "<div", "<h1"):
            self.assertNotIn(banned, text)
        self.assertIn("<h3>Role</h3>", text)
        self.assertIn("<li>Safety first</li>", text)
        self.assertIn("pumps", text)

    def test_plain_text_format_and_unicode(self):
        self.instance.description_format = "text"
        job = self.make_job(name="इलेक्ट्रीशियन – Électricien ₹", description="<p>काम: पैनल की मरम्मत।</p><p>مطلوب كهربائي</p>" + "<p>%s</p>" % ("Details. " * 30))
        payload, problems, _warnings = build_job_payload(job, self.instance)
        self.assertFalse(problems)
        self.assertNotIn("<", payload["description"])
        self.assertIn("पैनल", payload["description"])
        self.assertIn("كهربائي", payload["description"])
        self.assertEqual(payload["title"], "इलेक्ट्रीशियन – Électricien ₹")

    def test_long_content_is_refused_not_cut(self):
        job = self.make_job(name="T" * 256, description="<p>%s</p>" % ("word " * 11000))
        problems = build_job_payload(job, self.instance)[1]
        self.assertEqual(self.fields_of(problems), {"title", "description"})

    def test_salary_rules(self):
        inr = self.env.ref("base.INR")
        job = self.make_job(jobiba_salary_min=30000, jobiba_salary_max=45000)
        self.assertIn("salary_currency", self.fields_of(build_job_payload(job, self.instance)[1]))
        job.jobiba_salary_currency_id = inr
        payload, problems, _warnings = build_job_payload(job, self.instance)
        self.assertEqual(self.fields_of(problems), {"salary_period"})    # 30,000 a month or a year: never guessed
        self.assertNotIn("salary_period", payload)
        job.jobiba_salary_period = "MONTH"
        payload, problems, _warnings = build_job_payload(job, self.instance)
        self.assertFalse(problems)
        self.assertEqual(
            (payload["salary_min"], payload["salary_max"], payload["salary_currency"], payload["salary_period"]),
            (30000, 45000, "INR", "MONTH"))
        self.assertIn("jobiba_salary_period", SYNCED_FIELDS)              # a changed period is sent again
        job.jobiba_salary_max = 20000
        self.assertIn("salary_max", self.fields_of(build_job_payload(job, self.instance)[1]))
        job.write({"jobiba_salary_min": 0, "jobiba_salary_max": 200_000_000})
        self.assertIn("salary", self.fields_of(build_job_payload(job, self.instance)[1]))

    def test_enums_come_only_from_explicit_settings(self):
        job = self.make_job(name="Senior Director of Engineering")
        payload = build_job_payload(job, self.instance)[0]
        for key in ("job_type", "remote_type", "experience_level"):
            self.assertNotIn(key, payload, "nothing is inferred from the title")
        contract = self.env[EMPLOYMENT_TYPE_MODEL].create({"name": "Permanent", "jobiba_job_type": "FULL_TIME"})
        job.write({EMPLOYMENT_TYPE_FIELD: contract.id, "jobiba_remote_type": "HYBRID", "jobiba_experience_level": "SENIOR"})
        payload = build_job_payload(job, self.instance)[0]
        self.assertEqual((payload["job_type"], payload["remote_type"], payload["experience_level"]), ("FULL_TIME", "HYBRID", "SENIOR"))

    def test_own_site_needs_an_https_link(self):
        self.instance.apply_mode = "own_site"
        job = self.make_job()
        self.assertIn("apply_url", self.fields_of(build_job_payload(job, self.instance)[1]))
        job.jobiba_apply_url = "http://careers.example.com/jobs/1"
        self.assertIn("apply_url", self.fields_of(build_job_payload(job, self.instance)[1]))
        job.jobiba_apply_url = "https://careers.example.com/jobs/1"
        payload, problems, _warnings = build_job_payload(job, self.instance)
        self.assertFalse(problems)
        self.assertEqual(payload["apply_url"], "https://careers.example.com/jobs/1")

    def test_requirements_are_added_and_internal_fields_are_not(self):
        job = self.make_job(requirements="Diploma in electrical engineering\n5 years in MEP")
        payload = build_job_payload(job, self.instance)[0]
        self.assertIn("Diploma in electrical engineering", payload["description"])
        self.assertEqual(
            set(payload) - {"reference", "title", "location", "description"}, set(),
            "only documented fields are sent; no notes, recruiter or department data")
