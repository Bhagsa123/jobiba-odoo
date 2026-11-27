"""What Odoo reads back from Jobiba: the plan, job states and applications.

Jobiba decides what this connection may do (plan, job limit, whether
applicant details are sent). Odoo shows that answer and never works it out
itself. Every call here is tied to the connection's own API key, so it can
only ever see this company's Jobiba employer account.
"""
import logging
import uuid
from datetime import timedelta

from odoo import _, api, fields, models

from ..services import retry_policy

_logger = logging.getLogger(__name__)

PLAN_REFRESH_MINUTES = 60
MAX_PAGES_PER_RUN = 5
PAGE_SIZE = 50
STATE_BATCH = 100
#: Applications asked about again in one run (Jobiba takes up to 100 ids a call).
RECHECK_BATCH = 100
#: Jobiba's job state -> the state shown on the Odoo job.
REMOTE_FROM_JOBIBA = {
    "published": "published",
    "in_review": "in_review",
    "not_approved": "not_approved",
    "closed": "closed",
}


class JobibaInstancePull(models.Model):
    _inherit = "jobiba.instance"

    plan_name = fields.Char("Jobiba plan", readonly=True, copy=False)
    plan_paid = fields.Boolean("Paid plan", readonly=True, copy=False)
    integration_enabled = fields.Boolean(
        "Switched on in Jobiba", readonly=True, copy=False,
        help="The employer switches the connection on in Jobiba: Employer dashboard, Integrations.")
    job_limit_text = fields.Char("Open jobs allowed", readonly=True, copy=False)
    jobs_open = fields.Integer("Open jobs on Jobiba", readonly=True, copy=False)
    applicant_details = fields.Boolean(
        "Jobiba sends applicant details", readonly=True, copy=False,
        help="Contact details, answers and CVs. A paid Jobiba plan with applicant sync switched on in Jobiba.")
    plan_message = fields.Char("Jobiba says", readonly=True, copy=False)
    plan_checked_at = fields.Datetime("Plan read from Jobiba", readonly=True, copy=False)
    import_details = fields.Boolean(
        "Import contact details, answers and CVs", default=True,
        help="Used when Jobiba sends them (paid plan). Off: applicants arrive with a name and a link to Jobiba.")
    applications_cursor = fields.Char(readonly=True, copy=False, groups="jobiba_recruitment_connector.group_jobiba_manager")
    last_pull_at = fields.Datetime("Applications read from Jobiba", readonly=True, copy=False)
    integrations_url = fields.Char(compute="_compute_links")
    promote_url = fields.Char(compute="_compute_links")

    def _compute_links(self):
        super()._compute_links()
        for rec in self:
            base = rec.billing_url.rsplit("/employer/", 1)[0]
            rec.integrations_url = base + "/employer/dashboard/integrations"
            rec.promote_url = base + "/employer/dashboard/promote"

    # ------------------------------------------------------------------ plan
    def _refresh_plan(self, client=None):
        """Read plan and limits from Jobiba. Returns the result (never raises for HTTP)."""
        self.ensure_one()
        client = client or self._get_client()
        result = client.entitlements(uuid.uuid4().hex)
        now = fields.Datetime.now()
        if not result.ok:
            self.sudo().write({"plan_message": (result.message or "")[:250], "plan_checked_at": now})
            return result
        data = result.data
        integration = data.get("integration") or {}
        jobs = data.get("jobs") or {}
        applicants = data.get("applicants") or {}
        limit = jobs.get("limit")
        self.sudo().write({
            "plan_name": str(data.get("plan") or "")[:40],
            "plan_paid": bool(data.get("paid_plan")),
            "integration_enabled": bool(integration.get("enabled")),
            "job_limit_text": _("No limit") if limit is None else str(limit),
            "jobs_open": int(jobs.get("open") or 0),
            "applicant_details": bool(applicants.get("details")),
            "plan_message": str(integration.get("message") or applicants.get("message") or "")[:250] or False,
            "plan_checked_at": now,
        })
        return result

    def action_refresh_plan(self):
        self.ensure_one()
        self._check_manager()
        result = self._refresh_plan()
        if not result.ok:
            return self._notify(self._explain(result), "warning")
        if not self.integration_enabled:
            return self._notify(_("Connected, but the connection is switched off in Jobiba. Switch it on there: %s", self.integrations_url), "warning", sticky=True)
        return self._notify(_("Plan read from Jobiba: %(plan)s, %(limit)s open job(s) allowed.", plan=self.plan_name, limit=self.job_limit_text), "success")

    def action_open_integrations(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": self.integrations_url, "target": "new"}

    def action_open_promote(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": self.promote_url, "target": "new"}

    def action_open_billing(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": self.billing_url, "target": "new"}

    # ------------------------------------------------------------------ jobs
    def _push_closes(self, client=None):
        """Ask Jobiba to take offline the listings whose Odoo job stopped. Failures stay pending."""
        self.ensure_one()
        Mapping = self.env["jobiba.job.mapping"].sudo()
        pending = Mapping.search([("instance_id", "=", self.id), ("close_pending", "=", True)], limit=STATE_BATCH)
        if not pending or self.state not in ("connected", "attention"):
            return 0
        try:
            client = client or self._get_client()
            result = client.close_jobs(pending.mapped("reference"), uuid.uuid4().hex)
        except Exception as error:  # a missing key or an unreachable address: try again later
            _logger.info("Jobiba close request not sent: %s", type(error).__name__)
            return 0
        if not result.ok:
            self._log("warning", "jobs_close", result.message or "Jobiba did not close the listings.", result.status)
            if result.category not in retry_policy.RETRYABLE:
                pending.write({"last_error": (result.message or "")[:500]})
            return 0
        data = result.data
        done = set(data.get("closed") or []) | set(data.get("already_closed") or []) | set(data.get("not_found") or [])
        waiting = set(data.get("not_public") or [])
        for mapping in pending:
            if mapping.reference in done:
                mapping.write({"close_pending": False, "last_error": False})
            elif mapping.reference in waiting:
                # Not public (waiting for review or not approved): nothing to close.
                mapping.write({"close_pending": False,
                               "last_error": _("This job was not public on Jobiba (waiting for review or not approved). Withdraw it in your Jobiba dashboard if it should not go live.")})
        self._log("info", "jobs_close", "Closed %s listing(s) on Jobiba." % len(data.get("closed") or []), result.status)
        return len(data.get("closed") or [])

    def _pull_job_states(self, client=None):
        """Jobiba job id, public address and state for the jobs this connection keeps in sync."""
        self.ensure_one()
        Mapping = self.env["jobiba.job.mapping"].sudo()
        mappings = Mapping.search([
            ("instance_id", "=", self.id), ("first_sent_at", "!=", False),
            ("remote_state", "in", ("in_review", "published", "closed", "not_approved")),
        ], order="remote_checked_at asc nulls first, id", limit=STATE_BATCH)
        if not mappings:
            return 0
        client = client or self._get_client()
        result = client.job_states(mappings.mapped("reference"), uuid.uuid4().hex)
        if not result.ok:
            return 0
        now = fields.Datetime.now()
        by_ref = {row.get("reference"): row for row in (result.data.get("jobs") or []) if isinstance(row, dict)}
        Link = self.env["jobiba.application.mapping"]
        for mapping in mappings:
            row = by_ref.get(mapping.reference)
            vals = {"remote_checked_at": now}
            if row:
                state = REMOTE_FROM_JOBIBA.get(row.get("state"))
                if row.get("id"):
                    vals["jobiba_job_id"] = str(row["id"])[:64]
                listing = Link._own_link(self, row.get("url"))
                if listing and state == "published":
                    vals["listing_url"] = listing
                vals["remote_applications"] = int(row.get("applications") or 0)
                if state and mapping.intent == "publish" and state != mapping.remote_state:
                    vals["remote_state"] = state
                    if state == "not_approved":
                        vals["last_error"] = _("Jobiba's review did not approve this job ad. The reason is in your Jobiba dashboard, under Jobs.")
                    elif state == "closed":
                        vals["last_error"] = _("This listing is closed on Jobiba (closing date reached, or closed there). Use 'Sync now' to send it again.")
                    else:
                        vals["last_error"] = False
            mapping.write(vals)
        return len(by_ref)

    # ------------------------------------------------------------------ applications
    def _pull_applications(self, client=None):
        """Read new applications (oldest first after the saved cursor) into native applicants."""
        self.ensure_one()
        if not self.application_import:
            return 0
        client = client or self._get_client()
        Link = self.env["jobiba.application.mapping"].sudo()
        handled = 0
        for _page in range(MAX_PAGES_PER_RUN):
            result = client.applications(self.sudo().applications_cursor, PAGE_SIZE, uuid.uuid4().hex)
            if not result.ok:
                if result.category == retry_policy.RATE_LIMITED:
                    self._register_result(result)
                break
            for row in result.data.get("applications") or []:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                try:
                    with self.env.cr.savepoint():
                        self._take_application(row, client, Link)
                        handled += 1
                except Exception as error:  # one bad row never blocks the rest
                    _logger.exception("Jobiba application %s not imported", row.get("id"))
                    self._log("error", "application_import", "An application could not be imported (%s)." % type(error).__name__)
            cursor = result.data.get("next_cursor")
            vals = {"last_pull_at": fields.Datetime.now()}
            if cursor:
                vals["applications_cursor"] = str(cursor)[:300]
            self.sudo().write(vals)
            self._commit()
            if not result.data.get("has_more"):
                break
        return handled

    def _recheck_applications(self, client=None):
        """Ask Jobiba where the applications Odoo already holds stand now.

        The list call hands each application over once. A candidate can
        withdraw, or delete their Jobiba account, long after that; Jobiba tells
        a webhook address at once, and this turn-by-turn re-check is how Odoo
        hears of it when no webhook reaches it (or one was missed). The
        applications checked longest ago go first. Returns how many answered.
        """
        self.ensure_one()
        Link = self.env["jobiba.application.mapping"].sudo()
        links = Link.search(
            [("instance_id", "=", self.id), ("withdrawn_at", "=", False), ("erased_at", "=", False)],
            order="status_checked_at asc nulls first, id", limit=RECHECK_BATCH)
        if not links:
            return 0
        client = client or self._get_client()
        result = client.application_statuses(links.mapped("application_ref"), uuid.uuid4().hex)
        if not result.ok:
            if result.category == retry_policy.RATE_LIMITED:
                self._register_result(result)
            return 0
        now = fields.Datetime.now()
        by_ref = {str(row.get("id")): row for row in (result.data.get("applications") or []) if isinstance(row, dict)}
        for link in links:
            row = by_ref.get(link.application_ref)
            if row:
                details = row.get("details")
                if details == "erased":
                    link._mark_erased()
                    continue
                if details == "withdrawn":
                    link._mark_withdrawn(row.get("status"))
                else:
                    link._note_stage(str(row.get("status") or "")[:40])
                    if details == "full" and self.import_details and link.detail_state == "link":
                        # Details became available since (plan, applicant sync): fetch them once.
                        detail = client.application(link.application_ref, uuid.uuid4().hex)
                        if detail.ok:
                            link._import_details(detail.data.get("application") or {}, client)
            # An application Jobiba no longer knows as ours is left as it is; it is only asked about later.
            link.status_checked_at = now
        return len(by_ref)

    def _take_application(self, row, client, Link):
        application_id = str(row["id"])[:64]
        link = Link.search([("instance_id", "=", self.id), ("application_ref", "=", application_id)], limit=1)
        details = row.get("details")
        if details == "erased":
            # The person deleted their Jobiba account. An application Odoo never held is not created now.
            link._mark_erased()
            return
        if details == "withdrawn":
            # Status only. An application Odoo never held is not created now.
            link._mark_withdrawn(row.get("status"))
            return
        if not link:
            job = row.get("job") or {}
            link, error = Link._create_from(
                self,
                {"id": application_id, "name": " ".join(str(row.get("name") or "").split())[:120],
                 "stage": str(row.get("status") or "")[:40], "applied_at": str(row.get("applied_at") or "")[:40]},
                {"id": str(job.get("id") or "")[:64], "title": str(job.get("title") or "")[:255],
                 "reference": str(job.get("reference") or "")[:255]},
                row.get("applicants_url"),
            )
            if error:
                self._log("warning", "application_import", error)
                return
        else:
            link._note_stage(str(row.get("status") or "")[:40])
        if details == "full" and self.import_details and link.detail_state != "full":
            detail = client.application(application_id, uuid.uuid4().hex)
            if detail.ok:
                link._import_details(detail.data.get("application") or {}, client)

    # ------------------------------------------------------------------ cron
    @api.model
    def _cron_pull(self):
        """Every few minutes: closes, job states, applications; the plan about once an hour."""
        now = fields.Datetime.now()
        for rec in self.sudo().search([("state", "in", ("connected", "attention")), ("api_key_set", "=", True)]):
            if rec.paused_until and rec.paused_until > now:
                continue
            try:
                client = rec._get_client()
                if not rec.plan_checked_at or rec.plan_checked_at < now - timedelta(minutes=PLAN_REFRESH_MINUTES):
                    rec._refresh_plan(client)
                    rec._commit()
                if not rec.integration_enabled:
                    continue
                rec._push_closes(client)
                rec._commit()
                rec._pull_job_states(client)
                rec._commit()
                rec._pull_applications(client)
                rec._commit()
                rec._recheck_applications(client)
                rec._commit()
            except Exception as error:
                _logger.exception("Jobiba pull failed for connection %s", rec.id)
                rec._log("error", "pull", "Reading from Jobiba failed (%s). It is tried again by itself." % type(error).__name__)
