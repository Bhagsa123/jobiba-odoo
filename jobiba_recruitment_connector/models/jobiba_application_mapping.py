import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from markupsafe import Markup, escape

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

DETAIL_STATES = [
    ("link", "On Jobiba (name and link)"),
    ("full", "Details and CV in Odoo"),
    ("withdrawn", "Withdrawn"),
    ("erased", "Jobiba account deleted"),
]
MAX_ANSWERS = 20


def readable_utc(value):
    """'2026-11-26T17:29:16.609Z' reads '26 Nov 2026, 17:29 UTC'. Anything else is shown as it came."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return value
    if moment.tzinfo is None:
        return value
    return moment.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


class JobibaApplicationMapping(models.Model):
    """One Jobiba application linked to one native Odoo applicant.

    Odoo's own hr.applicant stays the record. This row only remembers which
    Jobiba application it came from, the stage Jobiba last reported, and how
    much of the application Jobiba sent: the name and a link (every plan), or
    contact details, answers and the CV (paid plan with applicant sync on, for
    an application whose apply screen showed the transfer notice).
    """

    _name = "jobiba.application.mapping"
    _description = "Jobiba application linked to an Odoo applicant"
    _order = "id desc"
    _rec_name = "application_ref"

    instance_id = fields.Many2one("jobiba.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    application_ref = fields.Char("Jobiba application ID", required=True, index=True)
    applicant_id = fields.Many2one("hr.applicant", ondelete="set null", index=True)
    job_mapping_id = fields.Many2one("jobiba.job.mapping", ondelete="set null")
    job_id = fields.Many2one(related="job_mapping_id.job_id", store=True)
    external_stage = fields.Char("Stage on Jobiba", readonly=True)
    applied_at = fields.Datetime(readonly=True)
    withdrawn_at = fields.Datetime(readonly=True)
    applicants_url = fields.Char("Open on Jobiba", readonly=True)
    detail_state = fields.Selection(DETAIL_STATES, default="link", required=True, readonly=True)
    details_imported_at = fields.Datetime(readonly=True)
    cv_attachment_id = fields.Many2one("ir.attachment", string="CV from Jobiba", ondelete="set null", readonly=True)
    detail_note = fields.Char("Last detail import", readonly=True)
    status_checked_at = fields.Datetime("Stage read from Jobiba", readonly=True, index=True)
    erased_at = fields.Datetime("Jobiba account deleted", readonly=True)

    # Odoo 19.0 dropped _sql_constraints for models.Constraint; both keep the same database constraint names.
    if hasattr(models, "Constraint"):
        _instance_application_unique = models.Constraint("UNIQUE (instance_id, application_ref)", "This Jobiba application is already linked.")
    else:
        _sql_constraints = [
            ("instance_application_unique", "unique(instance_id, application_ref)", "This Jobiba application is already linked."),
        ]

    # ------------------------------------------------------------------ helpers
    @api.model
    def _own_link(self, instance, url):
        """A link is kept only when it points to this connection's Jobiba host."""
        try:
            parts, base = urlsplit(url or ""), urlsplit(instance.sudo().base_url)
        except ValueError:
            return False
        if parts.scheme != "https" or not parts.hostname:
            return False
        host, own = parts.hostname.lower(), (base.hostname or "").lower()
        return url if host == own or host.endswith("." + own) or host == "jobiba.in" else False

    @api.model
    def _resolve_job_mapping(self, instance, job):
        """Reference first; then the Jobiba job ID learned earlier; then one exact title."""
        Mapping = self.env["jobiba.job.mapping"].sudo()
        base = [("instance_id", "=", instance.id)]
        if job.get("reference"):
            found = Mapping.search(base + [("reference", "=", job["reference"])], limit=1)
            if found:
                return found
        if job.get("id"):
            found = Mapping.search(base + [("jobiba_job_id", "=", job["id"])], limit=1)
            if found:
                return found
        title = (job.get("title") or "").strip()
        if title:
            found = Mapping.search(base + [
                ("job_name", "=ilike", title),
                ("remote_state", "in", ("published", "in_review", "failed", "stopped", "closed")),
                ("jobiba_job_id", "=", False)])
            if len(found) == 1:
                return found
        return Mapping

    # ------------------------------------------------------------------ create
    @api.model
    def _create_from(self, instance, application, job_data, applicants_url):
        """Create the native applicant for a Jobiba application.

        `application` = {id, name, stage, applied_at}. Returns (link, error):
        exactly one of them is set. Never creates a second applicant for an
        application that is already linked (the caller checks, and the unique
        constraint is the last word).
        """
        instance = instance.sudo()
        mapping = self._resolve_job_mapping(instance, job_data)
        if not mapping or not mapping.job_id:
            return self.browse(), _("No Odoo job matches the Jobiba job '%s'. Link it, then use Reprocess.", job_data.get("title") or "?")
        job = mapping.job_id.with_company(instance.company_id)
        if not job.active:
            return self.browse(), _("The Odoo job '%s' is archived. No applicant was created.", job.name)
        learned = {}
        if job_data.get("id") and not mapping.jobiba_job_id:
            learned["jobiba_job_id"] = job_data["id"]
        listing = self._own_link(instance, job_data.get("url"))
        if listing and mapping.listing_url != listing:
            learned["listing_url"] = listing
        if learned:
            if mapping.remote_state == "in_review":
                learned["remote_state"] = "published"  # it takes applications, so it is live
            mapping.write(learned)
        name = application.get("name") or _("Jobiba applicant")
        applicants_url = self._own_link(instance, applicants_url)
        note = Markup("<p>%s</p>") % escape(_("Applied on Jobiba on %s.", readable_utc(application.get("applied_at")) or _("an unknown date")))
        if applicants_url:
            note += Markup('<p><a href="%s" target="_blank" rel="noopener">%s</a></p>') % (
                applicants_url, escape(_("Open the applicant on Jobiba")))
        Applicant = self.env["hr.applicant"].sudo().with_company(instance.company_id).with_context(
            mail_create_nosubscribe=True, tracking_disable=True)
        vals = {
            "job_id": job.id,
            "company_id": instance.company_id.id,
            "source_id": instance.source_id.id,
            "applicant_notes": note,
        }
        if job.department_id:
            vals["department_id"] = job.department_id.id
        if "candidate_id" in Applicant._fields:
            candidate = self.env["hr.candidate"].sudo().with_company(instance.company_id).create(
                {"partner_name": name, "company_id": instance.company_id.id})
            vals["candidate_id"] = candidate.id
        else:
            vals["partner_name"] = name
        name_field = Applicant._fields.get("name")
        if name_field and not name_field.related and not name_field.compute:
            vals["name"] = "%s - %s" % (name, job.name)
        applicant = Applicant.create(vals)
        link = self.sudo().create({
            "instance_id": instance.id,
            "application_ref": application["id"],
            "applicant_id": applicant.id,
            "job_mapping_id": mapping.id,
            "external_stage": application.get("stage"),
            "applied_at": fields.Datetime.now(),
            "applicants_url": applicants_url,
        })
        return link, False

    # ------------------------------------------------------------------ updates
    def _mark_withdrawn(self, stage="WITHDRAWN"):
        """The candidate withdrew on Jobiba: note it, keep the Odoo record, import nothing more."""
        for link in self.sudo():
            if link.withdrawn_at:
                continue
            link.write({
                "external_stage": stage or "WITHDRAWN", "withdrawn_at": fields.Datetime.now(),
                "detail_state": "withdrawn",
            })
            if link.applicant_id:
                link.applicant_id.message_post(body=_(
                    "The candidate withdrew this application on Jobiba. Jobiba sends nothing more about it. "
                    "The Odoo record and its stage were not changed: decide here what to do with it."))
            link.instance_id._log("info", "application_withdrawn", "An application was withdrawn on Jobiba (status received).")

    def _mark_erased(self):
        """The person deleted their Jobiba account: say so on the record, import nothing more.

        Odoo's record is the company's own and is not changed here. The note
        tells the recruiter what happened so the company can decide, under its
        own retention rules, whether to keep what it holds about this person.
        """
        for link in self.sudo():
            if link.erased_at:
                continue
            now = fields.Datetime.now()
            link.write({
                "erased_at": now, "detail_state": "erased", "status_checked_at": now,
            })
            if link.applicant_id:
                link.applicant_id.message_post(body=_(
                    "This person deleted their Jobiba account. Jobiba sends nothing more about this application. "
                    "The Odoo record was not changed: decide here, under your own retention rules, "
                    "whether to keep the details you hold about this person."))
            link.instance_id._log("info", "application_erased", "A candidate deleted their Jobiba account (status received).")

    def _note_stage(self, stage):
        for link in self.sudo():
            if stage and stage != link.external_stage:
                link.write({"external_stage": stage})
                if link.applicant_id:
                    link.applicant_id.message_post(body=_("Stage on Jobiba is now: %s. The Odoo stage was not changed.", stage))

    def _import_details(self, row, client=None):
        """Write contact details, answers and the CV of one application into the applicant.

        `row` is Jobiba's answer for the application with details "full".
        Returns True when the applicant now holds the details. Never runs for
        a withdrawn application, and never overwrites what a recruiter typed.
        """
        self.ensure_one()
        link = self.sudo()
        if link.withdrawn_at or link.erased_at or row.get("details") != "full" or not link.applicant_id:
            return False
        instance = link.instance_id
        applicant = link.applicant_id.with_company(instance.company_id).with_context(tracking_disable=True)
        email = " ".join(str(row.get("email") or "").split())[:254]
        phone = " ".join(str(row.get("phone") or "").split())[:50]
        target = applicant.candidate_id if "candidate_id" in applicant._fields and applicant.candidate_id else applicant
        vals = {}
        if email and "@" in email and not target.email_from:
            vals["email_from"] = email
        if phone and not target.partner_phone:
            vals["partner_phone"] = phone
        if vals:
            target.sudo().write(vals)
        answers = [a for a in (row.get("answers") or []) if isinstance(a, dict)][:MAX_ANSWERS]
        if answers and not link.details_imported_at:
            items = Markup("").join(
                Markup("<li><strong>%s</strong><br/>%s</li>") % (
                    escape(str(a.get("question") or "")[:600]), escape(str(a.get("answer") or "")[:2000]))
                for a in answers)
            applicant.sudo().write({
                "applicant_notes": (applicant.applicant_notes or Markup("")) + Markup("<p>%s</p><ul>%s</ul>") % (
                    escape(_("Answers given on Jobiba:")), items),
            })
        note = _("Contact details and answers imported.")
        cv = row.get("cv") or {}
        if cv.get("available") and cv.get("download_url") and not link.cv_attachment_id:
            client = client or instance._get_client()
            result, content = client.download_cv(cv["download_url"], uuid.uuid4().hex)
            if content:
                filename = "".join(ch for ch in str(cv.get("filename") or "CV") if ch.isalnum() or ch in "._- ")[:80] or "CV"
                attachment = self.env["ir.attachment"].sudo().create({
                    "name": "%s - %s" % (applicant.partner_name or _("Applicant"), filename),
                    "raw": content,
                    "res_model": "hr.applicant",
                    "res_id": applicant.id,
                    "company_id": instance.company_id.id,
                    "mimetype": (result.data or {}).get("content_type") or cv.get("content_type") or "application/octet-stream",
                })
                link.cv_attachment_id = attachment.id
                note = _("Contact details, answers and the CV imported.")
            else:
                note = _("Contact details imported. The CV could not be fetched: %s", result.message or _("try again later."))
        elif not cv.get("available"):
            note = _("Contact details imported. No CV file is stored on Jobiba for this application.")
        link.write({"detail_state": "full", "details_imported_at": fields.Datetime.now(), "detail_note": note[:250]})
        return True

    def action_fetch_details(self):
        """Ask Jobiba for this application again (a button on the applicant)."""
        self.ensure_one()
        link = self.sudo()
        instance = link.instance_id
        result = instance._get_client().application(link.application_ref, uuid.uuid4().hex)
        if not result.ok:
            link.detail_note = (result.message or _("Jobiba refused the request."))[:250]
            return instance._notify(link.detail_note, "warning")
        row = result.data.get("application") or {}
        if row.get("details") == "erased":
            link._mark_erased()
            return instance._notify(_("This person deleted their Jobiba account. Jobiba sends nothing more about this application."), "warning")
        if row.get("details") == "withdrawn":
            link._mark_withdrawn(row.get("status"))
            return instance._notify(_("The candidate withdrew this application on Jobiba."), "warning")
        link._note_stage(row.get("status"))
        if link._import_details(row):
            return instance._notify(link.detail_note, "success")
        link.detail_note = _("Jobiba keeps this application's details on Jobiba (plan, applicant sync switch, or the application was sent before the transfer notice was shown).")[:250]
        return instance._notify(link.detail_note, "info")
