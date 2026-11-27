import logging
import secrets
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import NO_ACCESS
from odoo.modules import module as odoo_module
from odoo.tools import config

from ..services import retry_policy
from ..services.jobiba_client import DEFAULT_BASE_URL, JobibaClient, JobibaConfigError, clean_base_url

_logger = logging.getLogger(__name__)

MANAGER_GROUP = "jobiba_recruitment_connector.group_jobiba_manager"


class JobibaInstance(models.Model):
    _name = "jobiba.instance"
    _description = "Jobiba Connection"
    _inherit = ["mail.thread"]
    _order = "company_id, id"

    name = fields.Char(required=True, default=lambda self: _("Jobiba"), tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company", required=True, index=True, default=lambda self: self.env.company,
        help="Jobs and applicants of this company use this Jobiba employer account.",
    )
    base_url = fields.Char("Jobiba address", required=True, default=DEFAULT_BASE_URL, groups=MANAGER_GROUP)
    state = fields.Selection(
        [("draft", "Not connected"), ("connected", "Connected"),
         ("attention", "Needs attention"), ("disconnected", "Disconnected")],
        default="draft", required=True, tracking=True, copy=False,
    )
    # Secrets: no group may read them through the ORM. Server code reads with sudo().
    api_key = fields.Char(groups=NO_ACCESS, copy=False)
    api_key_set = fields.Boolean("API key saved", readonly=True, copy=False)
    api_key_saved_at = fields.Datetime(readonly=True, copy=False)
    webhook_secret = fields.Char(groups=NO_ACCESS, copy=False)
    webhook_secret_set = fields.Boolean("Signing secret saved", readonly=True, copy=False)
    webhook_token = fields.Char(groups=NO_ACCESS, copy=False)
    webhook_url = fields.Char(compute="_compute_webhook_url", groups=MANAGER_GROUP)
    webhook_verified_at = fields.Datetime("Webhook test received", readonly=True, copy=False)

    # Who agreed, on the connection wizard, to what this database sends to Jobiba, and when.
    data_consent_at = fields.Datetime("Agreed to the data sent on", readonly=True, copy=False)
    data_consent_user_id = fields.Many2one("res.users", "Agreed by", readonly=True, copy=False, ondelete="set null")

    last_attempt_at = fields.Datetime(readonly=True, copy=False)
    last_success_at = fields.Datetime("Last successful request", readonly=True, copy=False)
    last_error = fields.Char(readonly=True, copy=False)
    last_error_category = fields.Char(readonly=True, copy=False)
    rate_limit = fields.Integer("Requests allowed per minute", readonly=True, copy=False)
    rate_remaining = fields.Integer("Requests left this minute", readonly=True, copy=False)
    paused_until = fields.Datetime(readonly=True, copy=False, help="The queue waits until this time (rate limit or repeated failures).")
    consecutive_failures = fields.Integer(readonly=True, copy=False)

    apply_mode = fields.Selection(
        [("jobiba", "On Jobiba (applicants arrive in Odoo)"),
         ("own_site", "On our own site (the job's public page or link)")],
        string="Where people apply", default="jobiba", required=True,
        help="On Jobiba: the candidate applies on the Jobiba job page and the application is sent to Odoo.\n"
             "On our own site: the Jobiba listing links to your own application page. "
             "On Jobiba, a job that links to the employer's own form can be a paid post, depending on your plan.",
    )
    description_format = fields.Selection(
        [("html", "Formatted (headings and lists kept)"), ("text", "Plain text")],
        default="html", required=True,
    )
    require_approval = fields.Boolean(
        "Approval before publishing", default=True,
        help="A recruitment administrator must tick 'Approved for Jobiba' on a job before it can be published.",
    )
    auto_publish = fields.Boolean(
        "Publish approved jobs automatically", default=False,
        help="Off by default. When on, an approved job that passes the checks is queued without a click.",
    )
    auto_publish_department_ids = fields.Many2many(
        "hr.department", string="Only these departments",
        help="Leave empty to allow every department of the company.",
    )
    batch_size = fields.Integer("Jobs per request", default=50)
    application_import = fields.Boolean(
        "Create applicants from Jobiba applications", default=True,
        help="Used once Jobiba's webhook test has been received with a valid signature.",
    )
    source_id = fields.Many2one(
        "utm.source", string="Applicant source", ondelete="restrict",
        default=lambda self: self.env.ref("jobiba_recruitment_connector.utm_source_jobiba", raise_if_not_found=False))
    retention_days = fields.Integer("Keep webhook events and logs (days)", default=30)

    billing_url = fields.Char(compute="_compute_links")
    webhooks_setup_url = fields.Char(compute="_compute_links")
    api_key_page_url = fields.Char(compute="_compute_links")

    job_published_count = fields.Integer(compute="_compute_counts")
    job_review_count = fields.Integer(compute="_compute_counts")
    job_failed_count = fields.Integer(compute="_compute_counts")
    queue_pending_count = fields.Integer(compute="_compute_counts")
    queue_blocked_count = fields.Integer(compute="_compute_counts")
    application_count = fields.Integer(compute="_compute_counts")
    event_error_count = fields.Integer(compute="_compute_counts")
    health = fields.Selection(
        [("ok", "Healthy"), ("waiting", "Waiting"), ("problem", "Problem"), ("off", "Off")],
        compute="_compute_health",
    )

    # Odoo 19.0 dropped _sql_constraints for models.Constraint; both keep the same database constraint names.
    if hasattr(models, "Constraint"):
        _company_unique = models.Constraint("UNIQUE (company_id)", "This company already has a Jobiba connection.")
    else:
        _sql_constraints = [
            ("company_unique", "unique(company_id)", "This company already has a Jobiba connection."),
        ]

    # ------------------------------------------------------------------ computes
    @api.depends("base_url")
    def _compute_links(self):
        for rec in self:
            base = (rec.sudo().base_url or DEFAULT_BASE_URL).rstrip("/")
            rec.billing_url = base + "/employer/dashboard/billing"
            rec.webhooks_setup_url = base + "/employer/dashboard/webhooks"
            rec.api_key_page_url = base + "/employer/dashboard/api-key"

    def _compute_webhook_url(self):
        for rec in self:
            token = rec.sudo().webhook_token
            rec.webhook_url = "%s/jobiba/webhook/%s" % (rec.get_base_url().rstrip("/"), token) if token else False

    def _compute_counts(self):
        Mapping = self.env["jobiba.job.mapping"].sudo()
        Queue = self.env["jobiba.sync.job"].sudo()
        for rec in self:
            domain = [("instance_id", "=", rec.id)]
            rec.job_published_count = Mapping.search_count(domain + [("remote_state", "=", "published")])
            rec.job_review_count = Mapping.search_count(domain + [("remote_state", "=", "in_review")])
            rec.job_failed_count = Mapping.search_count(domain + [("remote_state", "=", "failed")])
            rec.queue_pending_count = Queue.search_count(domain + [("state", "in", ("queued", "retry", "processing"))])
            rec.queue_blocked_count = Queue.search_count(domain + [("state", "in", ("blocked", "dead_letter"))])
            rec.application_count = self.env["jobiba.application.mapping"].sudo().search_count(domain)
            rec.event_error_count = self.env["jobiba.webhook.event"].sudo().search_count(domain + [("state", "=", "error")])

    @api.depends("state", "paused_until", "last_error")
    def _compute_health(self):
        now = fields.Datetime.now()
        for rec in self:
            if rec.state in ("draft", "disconnected"):
                rec.health = "off"
            elif rec.state == "attention":
                rec.health = "problem"
            elif rec.paused_until and rec.paused_until > now:
                rec.health = "waiting"
            else:
                rec.health = "ok"

    @api.constrains("base_url")
    def _check_base_url(self):
        for rec in self:
            try:
                clean_base_url(rec.base_url)
            except JobibaConfigError as error:
                raise ValidationError(str(error)) from error

    @api.constrains("batch_size", "retention_days")
    def _check_numbers(self):
        for rec in self:
            if not 1 <= rec.batch_size <= 500:
                raise ValidationError(_("Jobs per request must be between 1 and 500."))
            if not 1 <= rec.retention_days <= 365:
                raise ValidationError(_("Keep events and logs between 1 and 365 days."))

    # ------------------------------------------------------------------ helpers
    @api.model
    def _for_company(self, company):
        return self.sudo().search([("company_id", "=", company.id), ("active", "=", True)], limit=1)

    def _commit(self):
        """Commit between external calls so a claimed item is never sent twice."""
        if not odoo_module.current_test:
            self.env.cr.commit()  # pylint: disable=invalid-commit

    def _stored_api_key(self):
        self.ensure_one()
        # A deployment may keep the key out of the database: odoo.conf
        #   jobiba_api_key_<connection id> = jbk_...
        return config.get("jobiba_api_key_%s" % self.id) or self.sudo().api_key

    def _require_data_consent(self):
        """The one gate before any request to Jobiba: somebody agreed on the connection wizard."""
        self.ensure_one()
        if not self.sudo().data_consent_at:
            raise UserError(_(
                "Nobody has agreed yet to what this connection sends to Jobiba. "
                "Press Connect and tick the box. Nothing is sent before that."))

    def _get_client(self):
        self.ensure_one()
        self._require_data_consent()
        key = self._stored_api_key()
        if not key:
            raise UserError(_("No Jobiba API key is saved for this connection."))
        try:
            return JobibaClient(self.sudo().base_url, key)
        except JobibaConfigError as error:
            raise UserError(str(error)) from error

    def _log(self, level, operation, message, http_status=None, correlation_id=None, job=None):
        self.ensure_one()
        return self.env["jobiba.sync.log"].sudo()._record(
            self, level, operation, message, http_status=http_status, correlation_id=correlation_id, job=job)

    def _register_result(self, result):
        """Keep the connection's health in step with every answer from Jobiba."""
        self.ensure_one()
        now = fields.Datetime.now()
        vals = {"last_attempt_at": now}
        if result.rate_limit is not None:
            vals["rate_limit"] = result.rate_limit
        if result.rate_remaining is not None:
            vals["rate_remaining"] = result.rate_remaining
        category = result.category
        if category == retry_policy.OK:
            vals.update(last_success_at=now, last_error=False, last_error_category=False,
                        consecutive_failures=0, paused_until=False)
            if self.state in ("draft", "attention"):
                vals["state"] = "connected"
        else:
            vals.update(last_error=result.message, last_error_category=category)
            if category in (retry_policy.CREDENTIAL, retry_policy.PLAN):
                vals["state"] = "attention"
            elif category == retry_policy.RATE_LIMITED:
                wait = retry_policy.rate_limit_seconds(result.retry_after)
                vals["paused_until"] = now + timedelta(seconds=wait)
            elif category == retry_policy.TRANSIENT:
                failures = self.consecutive_failures + 1
                vals["consecutive_failures"] = failures
                if failures >= retry_policy.CIRCUIT_BREAKER_FAILURES:
                    vals["paused_until"] = now + timedelta(seconds=retry_policy.CIRCUIT_BREAKER_REST_SECONDS)
        self.sudo().write(vals)

    def _check_manager(self):
        if not self.env.su and not self.env.user.has_group(MANAGER_GROUP):
            raise UserError(_("Only a Jobiba integration administrator can change the connection."))

    # ------------------------------------------------------------------ actions
    def action_open_connect_wizard(self):
        self.ensure_one()
        self._check_manager()
        return {
            "type": "ir.actions.act_window",
            "name": _("Connect to Jobiba"),
            "res_model": "jobiba.connect.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_instance_id": self.id},
        }

    def action_test_connection(self):
        self.ensure_one()
        self._check_manager()
        correlation = uuid.uuid4().hex
        result = self._get_client().test_connection(correlation)
        self._register_result(result)
        if result.ok:
            self._log("info", "connection_test", "Connection test passed.", result.status, correlation)
            self._refresh_plan()
            message, kind = _("Jobiba accepted the API key. Plan: %(plan)s, open jobs allowed: %(limit)s.",
                              plan=self.plan_name or "?", limit=self.job_limit_text or "?"), "success"
        else:
            self._log("error", "connection_test", result.message, result.status, correlation)
            message, kind = self._explain(result), "danger"
            plan = self._refresh_plan()
            if plan.ok and not self.integration_enabled:
                message = _("Jobiba accepted the API key, but the connection is switched off in Jobiba. "
                            "Switch it on there (Employer dashboard, Integrations), then test again: %s", self.integrations_url)
                kind = "warning"
        return self._notify(message, kind)

    def _explain(self, result):
        category = result.category
        if category == retry_policy.CREDENTIAL:
            return _("Jobiba did not accept the request: the API key is missing. Save the key again.")
        if category == retry_policy.PLAN:
            return _("Jobiba refused the request: %(reason)s Check the key, or see your plan at %(url)s",
                     reason=result.message, url=self.billing_url)
        if category == retry_policy.RATE_LIMITED:
            return _("Jobiba's request limit was reached. The queue waits and tries again by itself.")
        if category == retry_policy.TRANSIENT:
            return _("Jobiba could not be reached. Nothing was changed; the queue tries again by itself.")
        return result.message or _("Jobiba refused the request.")

    def _notify(self, message, kind="info", sticky=False):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Jobiba"), "message": message, "type": kind, "sticky": sticky,
                       "next": {"type": "ir.actions.client", "tag": "soft_reload"}},
        }

    def action_disconnect(self):
        for rec in self:
            rec._check_manager()
            rec.sudo().write({
                "api_key": False, "api_key_set": False, "webhook_secret": False,
                "webhook_secret_set": False, "webhook_token": False, "webhook_verified_at": False,
                "state": "disconnected", "paused_until": False, "last_error": False,
                "data_consent_at": False, "data_consent_user_id": False,
            })
            self.env["jobiba.sync.job"].sudo().search(
                [("instance_id", "=", rec.id), ("state", "in", ("queued", "retry", "processing"))]
            ).write({"state": "cancelled", "last_error": "Connection disconnected."})
            rec._log("warning", "disconnect", "Connection disconnected. Keys removed from Odoo.")
        return True

    def action_new_webhook_address(self):
        """A new receiving address: the old one stops working at once."""
        self.ensure_one()
        self._check_manager()
        self.sudo().write({"webhook_token": secrets.token_urlsafe(32), "webhook_verified_at": False})
        self._log("warning", "webhook_rotate", "A new webhook address was made. Update it in Jobiba.")
        return True

    def action_process_queue(self):
        self.ensure_one()
        sent = self.env["jobiba.sync.job"].sudo()._process_instance(self, max_requests=2)
        return self._notify(_("%s request(s) sent to Jobiba.", sent) if sent else _("Nothing was waiting to be sent."))

    def _open(self, name, model, domain, views="list,form"):
        return {"type": "ir.actions.act_window", "name": name, "res_model": model,
                "view_mode": views, "domain": domain, "context": {"create": False}}

    def action_view_mappings(self):
        return self._open(_("Jobs on Jobiba"), "jobiba.job.mapping", [("instance_id", "in", self.ids)])

    def action_view_queue(self):
        return self._open(_("Publishing queue"), "jobiba.sync.job", [("instance_id", "in", self.ids)])

    def action_view_applications(self):
        return self._open(_("Jobiba applications"), "jobiba.application.mapping", [("instance_id", "in", self.ids)])

    def action_view_events(self):
        return self._open(_("Webhook events"), "jobiba.webhook.event", [("instance_id", "in", self.ids)])

    def action_view_logs(self):
        return self._open(_("Jobiba log"), "jobiba.sync.log", [("instance_id", "in", self.ids)])

    @api.model
    def _cron_housekeeping(self):
        """Delete old webhook events and log lines (retention setting)."""
        for rec in self.sudo().search([]):
            limit = fields.Datetime.now() - timedelta(days=rec.retention_days or 30)
            self.env["jobiba.webhook.event"].sudo().search(
                [("instance_id", "=", rec.id), ("create_date", "<", limit), ("state", "!=", "error")], limit=2000).unlink()
            self.env["jobiba.sync.log"].sudo().search(
                [("instance_id", "=", rec.id), ("create_date", "<", limit)], limit=5000).unlink()
            self.env["jobiba.sync.job"].sudo().search(
                [("instance_id", "=", rec.id), ("write_date", "<", limit), ("state", "in", ("done", "cancelled"))], limit=5000).unlink()
