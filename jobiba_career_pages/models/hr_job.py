import json

from markupsafe import Markup

from odoo import fields, models
from odoo.tools import html2plaintext

from odoo.addons.jobiba_recruitment_connector.models.hr_contract_type import EMPLOYMENT_TYPE_FIELD

EMPLOYMENT_TYPE = {
    "FULL_TIME": "FULL_TIME", "PART_TIME": "PART_TIME", "CONTRACT": "CONTRACTOR",
    "INTERNSHIP": "INTERN", "FREELANCE": "CONTRACTOR",
}


class HrJob(models.Model):
    _inherit = "hr.job"

    def _career_location(self):
        self.ensure_one()
        if self.jobiba_location:
            return self.jobiba_location
        address = self.sudo().address_id
        parts = [address.city, address.state_id.name, address.country_id.name]
        return ", ".join(part for part in parts if part)

    def _career_employment_type(self):
        self.ensure_one()
        return self[EMPLOYMENT_TYPE_FIELD].name or ""

    def _career_pay(self):
        """The pay as entered for publication, or ''. Never an internal figure."""
        self.ensure_one()
        low, high = self.jobiba_salary_min, self.jobiba_salary_max
        currency = self.jobiba_salary_currency_id.name
        if not currency or not (low or high):
            return ""
        if low and high and low != high:
            return "%s %s – %s" % (currency, f"{low:,}", f"{high:,}")
        return "%s %s" % (currency, f"{(low or high):,}")

    def _career_jobposting_json(self):
        """schema.org JobPosting for the job's own page, built only from stated facts."""
        self.ensure_one()
        job = self.sudo()
        company = job.company_id or self.env.company
        text = html2plaintext(job.website_description or job.description or "").strip()
        if not text or not job.website_published:
            return ""
        data = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": job.name,
            "description": text[:8000],
            "identifier": {"@type": "PropertyValue", "name": company.name, "value": str(job.id)},
            "hiringOrganization": {"@type": "Organization", "name": company.name},
            "directApply": True,
        }
        if company.website:
            data["hiringOrganization"]["sameAs"] = company.website
        if job.published_date:
            data["datePosted"] = fields.Date.to_string(job.published_date)
        address = job.address_id
        if address.country_id:
            postal = {"@type": "PostalAddress", "addressCountry": address.country_id.code}
            if address.city:
                postal["addressLocality"] = address.city
            if address.state_id:
                postal["addressRegion"] = address.state_id.name
            data["jobLocation"] = {"@type": "Place", "address": postal}
        if job.jobiba_remote_type == "REMOTE":
            data["jobLocationType"] = "TELECOMMUTE"
            if address.country_id:
                data["applicantLocationRequirements"] = {"@type": "Country", "name": address.country_id.name}
        if job.jobiba_job_type in EMPLOYMENT_TYPE:
            data["employmentType"] = EMPLOYMENT_TYPE[job.jobiba_job_type]
        # baseSalary is left out on purpose: the pay fields carry no period
        # (month or year), and a period must never be guessed.
        raw = json.dumps(data, ensure_ascii=False)
        return Markup(raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
