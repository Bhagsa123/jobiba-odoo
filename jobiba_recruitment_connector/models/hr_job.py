from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.job_payload_mapper import build_job_payload
from .hr_contract_type import EMPLOYMENT_TYPE_FIELD, JOB_TYPE_SELECTION
from .jobiba_job_mapping import REMOTE_STATES

#: A change to one of these fields is sent to Jobiba for a job that is in sync.
SYNCED_FIELDS = {
    "name", "description", "requirements", "address_id", EMPLOYMENT_TYPE_FIELD, "website_published",
    "jobiba_location", "jobiba_description", "jobiba_apply_url", "jobiba_salary_min", "jobiba_salary_max",
    "jobiba_salary_currency_id", "jobiba_salary_period", "jobiba_job_type", "jobiba_remote_type",
    "jobiba_experience_level",
}
#: What the pay amounts are for: the values Jobiba's job API accepts as salary_period.
JOBIBA_SALARY_PERIODS = [("HOUR", "an hour"), ("DAY", "a day"), ("WEEK", "a week"), ("MONTH", "a month"), ("YEAR", "a year")]
APPROVER_GROUPS = (
    "hr_recruitment.group_hr_recruitment_manager",
    "jobiba_recruitment_connector.group_jobiba_manager",
)


class HrJob(models.Model):
    _inherit = "hr.job"

    jobiba_approved = fields.Boolean(
        "Approved for Jobiba", copy=False, tracking=True,
        help="A recruitment administrator confirms this vacancy may be advertised outside the company.")
    jobiba_location = fields.Char(
        "Location on Jobiba", help="City, Region, Country. Leave empty to use the job's Job Location address.")
    jobiba_description = fields.Html(
        "Description for Jobiba", sanitize=True,
        help="Optional. When empty, the job's own description (and its requirements) is sent.")
    jobiba_apply_url = fields.Char("Application link", help="Used only when the connection sends applicants to your own site.")
    jobiba_salary_min = fields.Integer("Lowest pay")
    jobiba_salary_max = fields.Integer("Highest pay")
    jobiba_salary_currency_id = fields.Many2one("res.currency", string="Pay currency")
    jobiba_salary_period = fields.Selection(
        JOBIBA_SALARY_PERIODS, string="Pay is for",
        help="Jobiba publishes the pay with this period, for example 45,000 to 60,000 a month. Needed when pay is filled in.")
    jobiba_job_type = fields.Selection(
        JOB_TYPE_SELECTION, string="Job type", compute="_compute_jobiba_job_type", store=True, readonly=False)
    jobiba_remote_type = fields.Selection(
        [("ONSITE", "On site"), ("HYBRID", "Hybrid"), ("REMOTE", "Remote")], string="Work mode")
    jobiba_experience_level = fields.Selection(
        [("ENTRY", "Entry"), ("MID", "Mid"), ("SENIOR", "Senior"), ("LEAD", "Lead"), ("EXECUTIVE", "Executive")],
        string="Experience level")

    jobiba_mapping_ids = fields.One2many("jobiba.job.mapping", "job_id")
    jobiba_connected = fields.Boolean(compute="_compute_jobiba_panel")
    jobiba_connection_state = fields.Selection(
        [("connected", "Connected"), ("attention", "Needs attention"), ("none", "Disconnected")],
        compute="_compute_jobiba_panel")
    jobiba_state = fields.Selection(REMOTE_STATES, compute="_compute_jobiba_panel", string="Jobiba")
    jobiba_last_synced_at = fields.Datetime(compute="_compute_jobiba_panel")
    jobiba_next_retry_at = fields.Datetime(compute="_compute_jobiba_panel")
    jobiba_last_error = fields.Char(compute="_compute_jobiba_panel")
    jobiba_problems = fields.Text(compute="_compute_jobiba_panel")
    jobiba_listing_url = fields.Char(compute="_compute_jobiba_panel")
    jobiba_reference = fields.Char(compute="_compute_jobiba_panel")
    jobiba_application_count = fields.Integer(compute="_compute_jobiba_panel")

    @api.depends(EMPLOYMENT_TYPE_FIELD)
    def _compute_jobiba_job_type(self):
        for job in self:
            mapped = job[EMPLOYMENT_TYPE_FIELD].jobiba_job_type
            if mapped or not job.jobiba_job_type:
                job.jobiba_job_type = mapped or False

    def _jobiba_instance(self):
        self.ensure_one()
        return self.env["jobiba.instance"]._for_company(self.company_id or self.env.company)

    @api.depends("jobiba_mapping_ids.remote_state", "jobiba_mapping_ids.last_error", "company_id")
    def _compute_jobiba_panel(self):
        Application = self.env["jobiba.application.mapping"].sudo()
        for job in self:
            instance = job._jobiba_instance() if job.id else self.env["jobiba.instance"]
            mapping = job.sudo().jobiba_mapping_ids.filtered(lambda m: m.instance_id == instance)[:1]
            usable = bool(instance) and instance.state in ("connected", "attention")
            job.jobiba_connected = usable
            job.jobiba_connection_state = instance.state if usable else "none"
            job.jobiba_state = mapping.remote_state or "draft"
            job.jobiba_last_synced_at = mapping.last_synced_at
            job.jobiba_next_retry_at = mapping.next_retry_at
            job.jobiba_last_error = mapping.last_error
            job.jobiba_problems = mapping.problems
            job.jobiba_listing_url = mapping.listing_url
            job.jobiba_reference = mapping.reference
            job.jobiba_application_count = Application.search_count([("job_mapping_id", "=", mapping.id)]) if mapping else 0

    # ------------------------------------------------------------------ rules
    def _jobiba_mapping(self, instance, create=False):
        self.ensure_one()
        Mapping = self.env["jobiba.job.mapping"].sudo()
        mapping = Mapping.search([("instance_id", "=", instance.id), ("job_id", "=", self.id)], limit=1)
        if not mapping and create:
            payload, _problems, _warnings = build_job_payload(self, instance)
            mapping = Mapping.create({
                "instance_id": instance.id, "job_id": self.id, "job_name": self.name,
                "reference": payload["reference"],
            })
        return mapping

    def _jobiba_publish_refusal(self, instance):
        """Why this job may not be sent now, or False."""
        self.ensure_one()
        if not instance or instance.state not in ("connected", "attention"):
            return _("This company is not connected to Jobiba.")
        if not self.active:
            return _("The job is archived.")
        if instance.require_approval and not self.jobiba_approved:
            return _("A recruitment administrator has not approved this job for Jobiba yet.")
        return False

    def _jobiba_enqueue(self, force=False):
        """Queue the jobs that may be sent. Returns (queued jobs, {job: refusal})."""
        queued, refused = self.browse(), {}
        Queue = self.env["jobiba.sync.job"].sudo()
        for job in self:
            instance = job._jobiba_instance()
            refusal = job._jobiba_publish_refusal(instance)
            if not refusal:
                _payload, problems, _warnings = build_job_payload(job, instance)
                if problems:
                    refusal = " ".join(message for _field, message in problems)
            if refusal:
                refused[job] = refusal
                continue
            mapping = job._jobiba_mapping(instance, create=True)
            mapping.write({"intent": "publish"})
            Queue._enqueue(mapping, force=force)
            queued |= job
        return queued, refused

    # ------------------------------------------------------------------ buttons
    def action_jobiba_preview(self):
        # The wizard is saved before it opens: its lines (what is sent, what blocks) are built
        # when the record is created, and a browser shows an unsaved record without them.
        wizard = self.env["jobiba.publish.wizard"].create({"job_ids": [(6, 0, self.ids)]})
        return {
            "type": "ir.actions.act_window",
            "name": _("Publish on Jobiba"),
            "res_model": "jobiba.publish.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_jobiba_sync_now(self):
        self.ensure_one()
        queued, refused = self._jobiba_enqueue(force=True)
        instance = self._jobiba_instance()
        if refused:
            raise UserError(refused[self])
        self.env["jobiba.sync.job"].sudo()._process_instance(instance, max_requests=1)
        self.invalidate_recordset()
        state = dict(REMOTE_STATES).get(self.jobiba_state, "")
        kind = "success" if self.jobiba_state in ("published", "in_review") else "warning"
        return instance._notify(_("Jobiba: %s", self.jobiba_last_error or state), kind)

    def action_jobiba_stop(self):
        for job in self:
            instance = job._jobiba_instance()
            mapping = job._jobiba_mapping(instance) if instance else False
            if mapping:
                was_live = mapping.remote_state in ("published", "in_review", "queued", "failed")
                mapping.write({"intent": "stop", "remote_state": "stopped", "last_error": False,
                               "close_pending": bool(mapping.first_sent_at) and was_live})
                self.env["jobiba.sync.job"].sudo().search(
                    [("mapping_id", "=", mapping.id), ("state", "in", ("queued", "retry"))]).write({"state": "cancelled"})
                job.message_post(body=_(
                    "Sync with Jobiba stopped. Odoo no longer updates the listing and asks Jobiba to take it offline. "
                    "Applications already made stay in Odoo and on Jobiba."))
                instance._push_closes()
        return True

    def action_jobiba_open_listing(self):
        self.ensure_one()
        if not self.jobiba_listing_url:
            raise UserError(_("Jobiba has not told Odoo the listing's address yet. It arrives once Jobiba has reviewed the job."))
        return {"type": "ir.actions.act_url", "url": self.jobiba_listing_url, "target": "new"}

    def action_jobiba_promote(self):
        """Featured placement is bought on Jobiba's own pages; Odoo only opens them."""
        self.ensure_one()
        instance = self._jobiba_instance()
        if not instance:
            raise UserError(_("This company has no Jobiba connection."))
        return instance.action_open_promote()

    def action_jobiba_applications(self):
        self.ensure_one()
        mapping = self.sudo().jobiba_mapping_ids[:1]
        return {
            "type": "ir.actions.act_window", "name": _("Applicants from Jobiba"), "res_model": "hr.applicant",
            "view_mode": "list,kanban,form",
            "domain": [("jobiba_application_ids.job_mapping_id", "=", mapping.id)],
        }

    # ------------------------------------------------------------------ ORM
    def _jobiba_check_approval_rights(self, vals):
        if "jobiba_approved" in vals and not self.env.su:
            if not any(self.env.user.has_group(group) for group in APPROVER_GROUPS):
                raise AccessError(_("Only a recruitment administrator can approve a job for Jobiba."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._jobiba_check_approval_rights(vals)
        jobs = super().create(vals_list)
        jobs._jobiba_after_change(set().union(*[set(vals) for vals in vals_list]) if vals_list else set())
        return jobs

    def write(self, vals):
        self._jobiba_check_approval_rights(vals)
        result = super().write(vals)
        if "active" in vals and not vals["active"]:
            self._jobiba_mark_closed_in_odoo()
        self._jobiba_after_change(set(vals))
        return result

    def unlink(self):
        mappings = self.env["jobiba.job.mapping"].sudo().search([("job_id", "in", self.ids)])
        for mapping in mappings:
            mapping.write({"job_name": mapping.job_id.name, "intent": "stop", "remote_state": "orphan",
                           "close_pending": bool(mapping.first_sent_at),
                           "last_error": "The Odoo job was deleted. Its Jobiba listing is being closed."})
        return super().unlink()

    def _jobiba_mark_closed_in_odoo(self):
        mappings = self.env["jobiba.job.mapping"].sudo().search(
            [("job_id", "in", self.ids), ("remote_state", "in", ("published", "in_review", "queued", "failed"))])
        if mappings:
            for mapping in mappings:
                mapping.write({"intent": "stop", "remote_state": "stopped", "last_error": False,
                               "close_pending": bool(mapping.first_sent_at)})
                mapping.instance_id._log("info", "job_closed", "A synced job was archived in Odoo; its Jobiba listing is being closed.", job=mapping.job_id)

    def _jobiba_after_change(self, changed):
        """Queue an update for jobs already in sync; auto-publish when allowed."""
        if self.env.context.get("jobiba_no_sync") or not (changed & (SYNCED_FIELDS | {"jobiba_approved"})):
            return
        Queue = self.env["jobiba.sync.job"].sudo()
        for job in self.filtered("active"):
            instance = job._jobiba_instance()
            if not instance or instance.state not in ("connected", "attention"):
                continue
            mapping = job._jobiba_mapping(instance)
            in_sync = mapping and mapping.intent == "publish" and mapping.remote_state in (
                "published", "in_review", "failed", "queued")
            if in_sync and changed & SYNCED_FIELDS:
                if not job._jobiba_publish_refusal(instance):
                    Queue._enqueue(mapping)
                continue
            if mapping and mapping.intent == "stop":
                continue  # a person stopped it: never restarted by a rule
            if instance.auto_publish and job.jobiba_approved:
                departments = instance.auto_publish_department_ids
                if departments and job.department_id not in departments:
                    continue
                job._jobiba_enqueue()
