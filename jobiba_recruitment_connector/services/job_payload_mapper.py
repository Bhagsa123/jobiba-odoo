"""hr.job -> the job object of Jobiba's jobs sync API, with the same limits.

The limits repeat the API's own validation (apps/nextjs/.../api/v1/jobs/sync)
so a problem is shown on the job in Odoo before anything is sent.
"""
import hashlib
import json
import re
from urllib.parse import urlsplit

from .html_text import clean_job_html, plain_job_text

JOB_TYPES = ("FULL_TIME", "PART_TIME", "CONTRACT", "INTERNSHIP", "FREELANCE")
REMOTE_TYPES = ("REMOTE", "HYBRID", "ONSITE")
EXPERIENCE_LEVELS = ("ENTRY", "MID", "SENIOR", "LEAD", "EXECUTIVE")

MAX_REFERENCE = 255
MAX_TITLE = 255
MAX_LOCATION = 255
MAX_DESCRIPTION = 50_000
MAX_URL = 2048
MAX_SALARY = 100_000_000
#: Shorter ads are sent, with a warning: Jobiba reviews new ads by hand.
SHORT_DESCRIPTION = 160

_CURRENCY = re.compile(r"^[A-Z]{3}$")


def job_reference(database_uuid, company_id, job_id):
    """Stable for one job of one company of one database. No personal data."""
    return ("odoo:%s:%s:%s" % (database_uuid or "db", company_id or 0, job_id))[:MAX_REFERENCE]


def location_from_partner(partner):
    """City, State, Country from the job's work address, or ''."""
    if not partner:
        return ""
    parts = [partner.city, partner.state_id.name, partner.country_id.name]
    seen, out = set(), []
    for part in parts:
        part = (part or "").strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return ", ".join(out)


def _valid_https_url(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and "." in parts.hostname


def build_job_payload(job, instance):
    """Return (payload, problems, warnings). Problems block sending."""
    problems, warnings = [], []
    env = job.env
    parameters = env["ir.config_parameter"].sudo()
    # Odoo 20.0 replaced get_param with typed getters.
    database_uuid = (parameters.get_str if hasattr(parameters, "get_str") else parameters.get_param)("database.uuid")
    company = job.company_id or instance.company_id
    payload = {"reference": job_reference(database_uuid, company.id, job.id)}

    title = (job.name or "").strip()
    if not title:
        problems.append(("title", "The job position has no name."))
    elif len(title) > MAX_TITLE:
        problems.append(("title", "The job title is longer than %s characters." % MAX_TITLE))
    payload["title"] = title

    location = (job.jobiba_location or "").strip() or location_from_partner(job.address_id)
    if not location:
        problems.append(("location", "Add a location: set the job's Job Location address or type City, Region, Country in the Jobiba tab."))
    elif len(location) > MAX_LOCATION:
        problems.append(("location", "The location is longer than %s characters." % MAX_LOCATION))
    payload["location"] = location

    source = job.jobiba_description or job.description
    if instance.description_format == "text":
        description = plain_job_text(source)
    else:
        description = clean_job_html(source)
    requirements = (job.requirements or "").strip() if "requirements" in job._fields else ""
    if requirements and not job.jobiba_description:
        if instance.description_format == "text":
            description = ("%s\n\nRequirements\n%s" % (description, requirements)).strip()
        else:
            from markupsafe import escape
            lines = "".join("<li>%s</li>" % escape(line.strip()) for line in requirements.splitlines() if line.strip())
            description = "%s<h3>Requirements</h3><ul>%s</ul>" % (description, lines)
    text_length = len(plain_job_text(description))
    if not text_length:
        problems.append(("description", "Write the job description (Job Summary) before publishing."))
    elif len(description) > MAX_DESCRIPTION:
        problems.append(("description", "The description is longer than %s characters." % f"{MAX_DESCRIPTION:,}"))
    elif text_length < SHORT_DESCRIPTION:
        warnings.append(("description", "The description is short (%s characters). Jobiba reviews new job ads; say what the work is and what you ask for." % text_length))
    payload["description"] = description

    if instance.apply_mode == "own_site":
        url = (job.jobiba_apply_url or "").strip()
        if not url and "full_url" in job._fields and getattr(job, "website_published", False):
            url = job.full_url or ""
        if not url:
            problems.append(("apply_url", "This connection sends applicants to your own site, but the job has no public page. Publish it on your website or enter an application link."))
        elif not _valid_https_url(url) or len(url) > MAX_URL:
            problems.append(("apply_url", "The application link must be a full https:// address."))
        else:
            payload["apply_url"] = url

    low, high = job.jobiba_salary_min or 0, job.jobiba_salary_max or 0
    if low < 0 or high < 0 or low > MAX_SALARY or high > MAX_SALARY:
        problems.append(("salary", "Pay must be a whole number between 1 and %s." % f"{MAX_SALARY:,}"))
    elif low or high:
        currency = (job.jobiba_salary_currency_id.name or "").upper()
        if not _CURRENCY.match(currency) or currency in ("XXX", "XTS"):
            problems.append(("salary_currency", "Choose the currency of the pay."))
        elif low and high and low > high:
            problems.append(("salary_max", "The highest pay is below the lowest pay."))
        else:
            if low:
                payload["salary_min"] = int(low)
            if high:
                payload["salary_max"] = int(high)
            payload["salary_currency"] = currency
            # Never guessed: 45,000 a month and 45,000 a year are different jobs.
            period = job.jobiba_salary_period
            if period:
                payload["salary_period"] = period
            else:
                problems.append(("salary_period", "Choose what the pay is for: an hour, a day, a week, a month or a year."))
    else:
        warnings.append(("salary", "No pay is stated. Jobs that state pay are easier to find; some countries expect it."))

    for key, value, allowed, label in (
        ("job_type", job.jobiba_job_type, JOB_TYPES, "job type"),
        ("remote_type", job.jobiba_remote_type, REMOTE_TYPES, "work mode"),
        ("experience_level", job.jobiba_experience_level, EXPERIENCE_LEVELS, "experience level"),
    ):
        if value:
            if value in allowed:
                payload[key] = value
            else:
                problems.append((key, "The %s is not one Jobiba accepts." % label))
        else:
            warnings.append((key, "No %s is set (optional)." % label))
    return payload, problems, warnings


def payload_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
