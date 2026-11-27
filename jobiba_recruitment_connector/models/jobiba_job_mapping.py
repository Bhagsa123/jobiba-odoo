from odoo import api, fields, models

REMOTE_STATES = [
    ("draft", "Not sent"),
    ("queued", "Queued"),
    ("in_review", "Waiting for Jobiba review"),
    ("published", "Published"),
    ("failed", "Update failed"),
    ("stopped", "Sync stopped"),
    ("closed", "Closed on Jobiba"),
    ("not_approved", "Not approved by Jobiba"),
    ("orphan", "Odoo job deleted"),
]


class JobibaJobMapping(models.Model):
    _name = "jobiba.job.mapping"
    _description = "Job position on Jobiba"
    _order = "id desc"
    _rec_name = "job_name"

    instance_id = fields.Many2one("jobiba.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    job_id = fields.Many2one("hr.job", ondelete="set null", index=True)
    job_name = fields.Char(compute="_compute_job_name", store=True, readonly=False)
    reference = fields.Char(required=True, index=True, help="The stable reference sent to Jobiba. The same reference updates the same listing.")
    intent = fields.Selection([("publish", "Keep in sync"), ("stop", "Do not sync")], default="publish", required=True)
    remote_state = fields.Selection(REMOTE_STATES, default="draft", required=True, index=True)
    jobiba_job_id = fields.Char("Jobiba job ID", readonly=True)
    close_pending = fields.Boolean("Close on Jobiba", readonly=True, help="The listing is taken offline on Jobiba with the next request.")
    remote_checked_at = fields.Datetime("State read from Jobiba", readonly=True)
    remote_applications = fields.Integer("Applications on Jobiba", readonly=True)
    listing_url = fields.Char("Jobiba listing", readonly=True)
    first_sent_at = fields.Datetime(readonly=True)
    last_synced_at = fields.Datetime("Last successful sync", readonly=True)
    last_sent_hash = fields.Char(readonly=True)
    last_error = fields.Char(readonly=True)
    problems = fields.Text("Field problems", readonly=True)
    queue_ids = fields.One2many("jobiba.sync.job", "mapping_id")
    next_retry_at = fields.Datetime(compute="_compute_next_retry")

    # Odoo 19.0 dropped _sql_constraints for models.Constraint; both keep the same database constraint names.
    if hasattr(models, "Constraint"):
        _instance_job_unique = models.Constraint("UNIQUE (instance_id, job_id)", "This job is already linked to this Jobiba connection.")
        _instance_reference_unique = models.Constraint("UNIQUE (instance_id, reference)", "This reference is already used in this Jobiba connection.")
    else:
        _sql_constraints = [
            ("instance_job_unique", "unique(instance_id, job_id)", "This job is already linked to this Jobiba connection."),
            ("instance_reference_unique", "unique(instance_id, reference)", "This reference is already used in this Jobiba connection."),
        ]

    @api.depends("job_id.name")
    def _compute_job_name(self):
        for rec in self:
            if rec.job_id:
                rec.job_name = rec.job_id.name

    def _compute_next_retry(self):
        for rec in self:
            waiting = rec.queue_ids.filtered(lambda q: q.state == "retry" and q.next_retry_at)
            rec.next_retry_at = min(waiting.mapped("next_retry_at")) if waiting else False
