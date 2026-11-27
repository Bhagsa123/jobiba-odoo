import json
import re

from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install", "jobiba")
class TestCareerPage(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.page = cls.env.ref("jobiba_career_pages.career_page_default")
        cls.env["hr.job"].search([]).write({"website_published": False})
        india, uae = cls.env.ref("base.in"), cls.env.ref("base.ae")
        plant = cls.env["res.partner"].create({"name": "Plant", "city": "Ahmedabad", "country_id": india.id})
        hotel = cls.env["res.partner"].create({"name": "Hotel", "city": "Dubai", "country_id": uae.id})
        cls.engineering = cls.env["hr.department"].create({"name": "Engineering"})
        cls.hospitality = cls.env["hr.department"].create({"name": "Hospitality"})
        Job = cls.env["hr.job"]
        cls.engineer = Job.create({
            "name": "Maintenance Engineer <MEP>", "address_id": plant.id, "department_id": cls.engineering.id,
            "website_published": True, "description": "<p>Keep the plant running.</p>",
            "jobiba_salary_min": 30000, "jobiba_salary_max": 45000, "jobiba_salary_currency_id": cls.env.ref("base.INR").id,
            "jobiba_job_type": "FULL_TIME"})
        cls.supervisor = Job.create({
            "name": "Housekeeping Supervisor", "address_id": hotel.id, "department_id": cls.hospitality.id,
            "website_published": True, "description": "<p>Lead the housekeeping team.</p>"})
        cls.hidden = Job.create({"name": "Secret Internal Role", "website_published": False, "department_id": cls.engineering.id})

    def test_unpublished_page_is_not_public(self):
        self.page.is_published = False
        self.assertEqual(self.url_open("/careers").status_code, 404)
        self.assertEqual(self.url_open("/careers/no-such-page").status_code, 404)

    def test_every_layout_lists_only_published_jobs(self):
        self.page.is_published = True
        self.page.perk_ids = [(0, 0, {"name": "Shift transport", "icon": "fa-bus", "text": "Bus from the city"})]
        for layout in ("aurora", "ledger", "mosaic", "spotlight"):
            self.page.layout = layout
            response = self.url_open("/careers")
            self.assertEqual(response.status_code, 200, layout)
            html = response.text
            self.assertIn("jcp-%s" % layout, html)
            self.assertIn("Housekeeping Supervisor", html)
            self.assertIn("Maintenance Engineer &lt;MEP&gt;", html, "titles are escaped")
            self.assertNotIn("Secret Internal Role", html)
            self.assertIn("INR 30,000 – 45,000", html)
            self.assertIn("Shift transport", html)
            self.assertIn("/jobs/apply/", html)
            self.assertEqual(len(re.findall(r"<h1[ >]", html)), 1, "one H1")
            blocks = (json.loads(block) for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S))
            data = next(block for block in blocks if block.get("@type") == "ItemList")
            self.assertEqual(data["numberOfItems"], 2)
            self.assertNotIn("noindex", html)

    def test_filters_search_and_scope(self):
        self.page.is_published = True
        html = self.url_open("/careers?department=%s" % self.hospitality.id).text
        self.assertIn("Housekeeping Supervisor", html)
        self.assertNotIn("Maintenance Engineer", html)
        self.assertIn('content="noindex, follow"', html, "a filtered view is not indexed")
        html = self.url_open("/careers?q=plant").text
        self.assertIn("Maintenance Engineer", html)
        self.assertNotIn("Housekeeping Supervisor", html)
        html = self.url_open("/careers?q=zzzz-nothing").text
        self.assertIn("No open role matches that search", html)
        html = self.url_open("/careers?department=999999&country=abc").text
        self.assertIn("Housekeeping Supervisor", html, "unknown filter values are ignored")
        self.page.department_ids = [(6, 0, self.engineering.ids)]
        html = self.url_open("/careers").text
        self.assertNotIn("Housekeeping Supervisor", html)
        self.page.show_salary = False
        self.assertNotIn("30,000", self.url_open("/careers").text)

    def test_second_page_and_values(self):
        second = self.page.copy({"slug": "hotel-jobs", "name": "Hotel jobs", "is_published": True, "layout": "ledger",
                                 "department_ids": [(6, 0, self.hospitality.ids)]})
        html = self.url_open("/careers/hotel-jobs").text
        self.assertIn("Housekeeping Supervisor", html)
        self.assertNotIn("Maintenance Engineer", html)
        self.assertEqual(second.website_url, "/careers/hotel-jobs")
        for bad in ({"accent_color": "red; background:url(x)"}, {"slug": "Bad Slug"}, {"cta_url": "javascript:alert(1)"}, {"cta_url": "//evil.example"}):
            with self.assertRaises(ValidationError):
                self.page.write(bad)

    def test_job_page_carries_jobposting_data(self):
        html = self.url_open(self.engineer.website_url).text
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        # Odoo 20.0 adds its own JobPosting to the page head graph; the module's is the one with its own script tag.
        posting = next(data for data in map(json.loads, blocks) if data.get("@type") == "JobPosting")
        self.assertEqual(posting["title"], "Maintenance Engineer <MEP>")
        self.assertEqual(posting["jobLocation"]["address"]["addressCountry"], "IN")
        self.assertEqual(posting["employmentType"], "FULL_TIME")
        self.assertNotIn("baseSalary", posting, "no pay period is known, so no salary is declared")
        self.assertNotIn("<MEP>", "".join(blocks), "angle brackets are escaped inside the script")
        self.assertFalse(self.hidden._career_jobposting_json())
