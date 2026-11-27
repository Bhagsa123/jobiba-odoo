from odoo import _, api, fields, models
from odoo.exceptions import UserError


class HrApplicant(models.Model):
    _inherit = "hr.applicant"

    jobiba_application_ids = fields.One2many("jobiba.application.mapping", "applicant_id")
    is_jobiba = fields.Boolean("From Jobiba", compute="_compute_jobiba", search="_search_is_jobiba")
    jobiba_application_ref = fields.Char("Jobiba application ID", compute="_compute_jobiba")
    jobiba_external_stage = fields.Char("Stage on Jobiba", compute="_compute_jobiba")
    jobiba_applied_at = fields.Datetime("Received from Jobiba", compute="_compute_jobiba")
    jobiba_withdrawn_at = fields.Datetime("Withdrawn on Jobiba", compute="_compute_jobiba")
    jobiba_erased_at = fields.Datetime("Jobiba account deleted", compute="_compute_jobiba")
    jobiba_url = fields.Char(compute="_compute_jobiba")
    jobiba_detail_state = fields.Selection(
        [("link", "On Jobiba (name and link)"), ("full", "Details and CV in Odoo"), ("withdrawn", "Withdrawn"),
         ("erased", "Jobiba account deleted")],
        string="Details from Jobiba", compute="_compute_jobiba")
    jobiba_detail_note = fields.Char("Last detail import", compute="_compute_jobiba")

    @api.depends("jobiba_application_ids.external_stage", "jobiba_application_ids.withdrawn_at",
                 "jobiba_application_ids.erased_at",
                 "jobiba_application_ids.detail_state", "jobiba_application_ids.detail_note")
    def _compute_jobiba(self):
        for applicant in self:
            link = applicant.sudo().jobiba_application_ids[:1]
            applicant.is_jobiba = bool(link)
            applicant.jobiba_application_ref = link.application_ref
            applicant.jobiba_external_stage = link.external_stage
            applicant.jobiba_applied_at = link.applied_at
            applicant.jobiba_withdrawn_at = link.withdrawn_at
            applicant.jobiba_erased_at = link.erased_at
            applicant.jobiba_url = link.applicants_url
            applicant.jobiba_detail_state = link.detail_state
            applicant.jobiba_detail_note = link.detail_note

    def _search_is_jobiba(self, operator, value):
        positive = (operator == "=" and value) or (operator == "!=" and not value)
        return [("jobiba_application_ids", "!=" if positive else "=", False)]

    def action_jobiba_open(self):
        self.ensure_one()
        if not self.jobiba_url:
            raise UserError(_("Jobiba sent no link for this applicant."))
        return {"type": "ir.actions.act_url", "url": self.jobiba_url, "target": "new"}

    def action_jobiba_fetch_details(self):
        """Ask Jobiba again for this application's contact details, answers and CV."""
        self.ensure_one()
        link = self.sudo().jobiba_application_ids[:1]
        if not link:
            raise UserError(_("This applicant did not come from Jobiba."))
        if link.erased_at:
            raise UserError(_("This person deleted their Jobiba account. Jobiba sends nothing more about this application."))
        if link.withdrawn_at:
            raise UserError(_("The candidate withdrew this application on Jobiba. Jobiba sends nothing more about it."))
        return link.action_fetch_details()
