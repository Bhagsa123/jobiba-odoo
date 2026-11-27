import secrets
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.jobiba_client import DEFAULT_BASE_URL, JobibaClient, JobibaConfigError


class JobibaConnectWizard(models.TransientModel):
    _name = "jobiba.connect.wizard"
    _description = "Connect Odoo to Jobiba"

    instance_id = fields.Many2one("jobiba.instance", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id")
    base_url = fields.Char("Jobiba address", default=DEFAULT_BASE_URL, required=True)
    api_key = fields.Char("API key", help="From your Jobiba employer dashboard. It is sent to the Odoo server once and never shown again.")
    webhook_secret = fields.Char("Webhook signing secret", help="Shown by Jobiba when you register the webhook address below.")
    api_key_page_url = fields.Char(related="instance_id.api_key_page_url")
    webhooks_setup_url = fields.Char(related="instance_id.webhooks_setup_url")
    webhook_url = fields.Char(compute="_compute_webhook_url")
    api_key_set = fields.Boolean(related="instance_id.api_key_set")
    webhook_secret_set = fields.Boolean(related="instance_id.webhook_secret_set")
    data_consent = fields.Boolean(
        "I agree that this Odoo database sends the data listed above to jobiba.com",
        compute="_compute_data_consent", store=True, readonly=False,
        help="Nothing is sent to Jobiba before this box is ticked. The connection screen shows who agreed and when.")
    result = fields.Text(readonly=True)
    result_ok = fields.Boolean(readonly=True)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        instance = self.env["jobiba.instance"].browse(values.get("instance_id"))
        if instance:
            instance._check_manager()
            values["base_url"] = instance.sudo().base_url
            if not instance.sudo().webhook_token:
                # The receiving address exists before the secret, so it can be registered on Jobiba.
                instance.sudo().write({"webhook_token": secrets.token_urlsafe(32)})
        return values

    @api.depends("instance_id")
    def _compute_data_consent(self):
        # Asked once per connection: ticked again only when somebody already agreed.
        for wizard in self:
            wizard.data_consent = bool(wizard.instance_id.sudo().data_consent_at)

    @api.depends("instance_id")
    def _compute_webhook_url(self):
        for wizard in self:
            wizard.webhook_url = wizard.instance_id.sudo().webhook_url

    def _reopen(self):
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id,
                "view_mode": "form", "target": "new"}

    def action_connect(self):
        self.ensure_one()
        instance = self.instance_id
        instance._check_manager()
        if not self.data_consent:
            raise UserError(_("Tick the box to agree to what is sent to jobiba.com. Nothing is sent before you do."))
        key = (self.api_key or "").strip()
        secret = (self.webhook_secret or "").strip()
        if not key and not instance.api_key_set:
            raise UserError(_("Paste the API key from your Jobiba employer dashboard."))
        vals = {"base_url": self.base_url}
        if key:
            try:
                JobibaClient(self.base_url, key)
            except JobibaConfigError as error:
                raise UserError(str(error)) from error
            vals.update(api_key=key, api_key_set=True, api_key_saved_at=fields.Datetime.now())
        if secret:
            if len(secret) < 16:
                raise UserError(_("The signing secret looks too short. Copy it again from Jobiba."))
            vals.update(webhook_secret=secret, webhook_secret_set=True, webhook_verified_at=False)
        if instance.state == "disconnected":
            vals["state"] = "draft"
        if not instance.data_consent_at:
            vals.update(data_consent_at=fields.Datetime.now(), data_consent_user_id=self.env.uid)
        instance.sudo().write(vals)
        # The typed secrets leave the wizard row at once.
        self.write({"api_key": False, "webhook_secret": False})
        correlation = uuid.uuid4().hex
        result = instance._get_client().test_connection(correlation)
        instance._register_result(result)
        if result.ok:
            instance._log("info", "connection_test", "Connection test passed.", result.status, correlation)
            plan = instance._refresh_plan()
            if plan.ok and instance.integration_enabled:
                message = _(
                    "Connected. Jobiba accepted the API key. Plan read from Jobiba: %(plan)s, %(limit)s open job(s) allowed.",
                    plan=instance.plan_name or "?", limit=instance.job_limit_text or "?")
            elif plan.ok:
                message = _(
                    "Jobiba accepted the API key, but the connection is not switched on in Jobiba yet. "
                    "The account owner switches it on in Jobiba: Employer dashboard, Integrations, \"Connect my hiring system\".")
            else:
                message = _("Connected. Jobiba accepted the API key. The plan could not be read just now; Odoo reads it again by itself.")
            self.write({"result_ok": True, "result": message})
        else:
            instance._log("error", "connection_test", result.message, result.status, correlation)
            self.write({"result_ok": False, "result": instance._explain(result)})
        return self._reopen()
