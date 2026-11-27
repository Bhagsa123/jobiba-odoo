from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.job_payload_mapper import build_job_payload
from ..services.html_text import plain_job_text

FIELD_LABELS = [
    ("title", "Title"), ("location", "Location"), ("apply_url", "Application link"),
    ("salary_min", "Lowest pay"), ("salary_max", "Highest pay"), ("salary_currency", "Pay currency"), ("salary_period", "Pay is for"),
    ("job_type", "Job type"), ("remote_type", "Work mode"), ("experience_level", "Experience level"),
]


class JobibaPublishWizard(models.TransientModel):
    _name = "jobiba.publish.wizard"
    _description = "Preview and publish jobs on Jobiba"

    job_ids = fields.Many2many("hr.job", string="Jobs")
    line_ids = fields.One2many("jobiba.publish.wizard.line", "wizard_id")
    ready_count = fields.Integer(compute="_compute_counts")
    blocked_count = fields.Integer(compute="_compute_counts")
    single = fields.Boolean(compute="_compute_counts")
    done = fields.Boolean()
    summary = fields.Text(readonly=True)

    @api.depends("line_ids.ready")
    def _compute_counts(self):
        for wizard in self:
            wizard.ready_count = len(wizard.line_ids.filtered("ready"))
            wizard.blocked_count = len(wizard.line_ids) - wizard.ready_count
            wizard.single = len(wizard.line_ids) == 1

    @api.model_create_multi
    def create(self, vals_list):
        wizards = super().create(vals_list)
        for wizard in wizards:
            wizard._build_lines()
        return wizards

    def _build_lines(self):
        self.ensure_one()
        lines = []
        for job in self.job_ids:
            instance = job._jobiba_instance()
            refusal = job._jobiba_publish_refusal(instance)
            payload, problems, warnings = ({}, [], [])
            if instance:
                payload, problems, warnings = build_job_payload(job, instance)
            rows = Markup("")
            for key, label in FIELD_LABELS:
                if payload.get(key) not in (None, ""):
                    rows += Markup("<tr><th>%s</th><td>%s</td></tr>") % (escape(_(label)), escape(payload[key]))
            if instance and instance.apply_mode == "jobiba":
                rows += Markup("<tr><th>%s</th><td>%s</td></tr>") % (
                    escape(_("Where people apply")), escape(_("On the Jobiba job page")))
            excerpt = plain_job_text(payload.get("description") or "")
            preview = Markup('<table class="table table-sm o_jobiba_preview"><tbody>%s</tbody></table>') % rows
            if excerpt:
                more = "…" if len(excerpt) > 600 else ""
                preview += Markup('<div class="o_jobiba_preview_text">%s%s</div>') % (escape(excerpt[:600]), more)
            issues = [message for _field, message in problems]
            if refusal:
                issues.insert(0, refusal)
            lines.append((0, 0, {
                "job_id": job.id,
                "ready": not issues,
                "problems": "\n".join(issues),
                "warnings": "\n".join(message for _field, message in warnings),
                "preview": preview,
            }))
        self.line_ids = [(5, 0, 0)] + lines

    def action_publish(self):
        self.ensure_one()
        ready = self.line_ids.filtered("ready").mapped("job_id")
        if not ready:
            raise UserError(_("No job is ready. Fix the problems listed, then open this screen again."))
        queued, refused = ready._jobiba_enqueue(force=True)
        instances = self.env["jobiba.instance"]
        for job in queued:
            instances |= job._jobiba_instance()
        for instance in instances:
            self.env["jobiba.sync.job"].sudo()._process_instance(instance, max_requests=1)
        queued.invalidate_recordset()
        lines = []
        states = dict(queued._fields["jobiba_state"]._description_selection(self.env))
        for job in queued:
            text = job.jobiba_last_error or states.get(job.jobiba_state, "")
            lines.append("%s: %s" % (job.name, text))
        for job, why in refused.items():
            lines.append("%s: %s" % (job.name, why))
        blocked = self.line_ids.filtered(lambda line: not line.ready)
        for line in blocked:
            lines.append(_("%s: not sent (see the problems above).", line.job_id.name))
        self.write({"done": True, "summary": "\n".join(lines)})
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id,
                "view_mode": "form", "target": "new"}

    def action_close(self):
        """Close the result and draw the screen behind it again: the job's state changed."""
        return {"type": "ir.actions.client", "tag": "soft_reload"}


class JobibaPublishWizardLine(models.TransientModel):
    _name = "jobiba.publish.wizard.line"
    _description = "Job to publish on Jobiba"

    wizard_id = fields.Many2one("jobiba.publish.wizard", required=True, ondelete="cascade")
    job_id = fields.Many2one("hr.job", required=True, ondelete="cascade")
    ready = fields.Boolean()
    problems = fields.Text()
    warnings = fields.Text()
    preview = fields.Html(sanitize=False)
