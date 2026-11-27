import logging
import uuid
from datetime import timedelta

from odoo import _, api, fields, models

from ..services import retry_policy
from ..services.job_payload_mapper import build_job_payload, payload_hash

_logger = logging.getLogger(__name__)

LEASE_MINUTES = 10
OPEN_STATES = ("queued", "retry", "processing")


class JobibaSyncJob(models.Model):
    _name = "jobiba.sync.job"
    _description = "Jobiba outbound queue item"
    _order = "id desc"
    _rec_name = "mapping_id"

    instance_id = fields.Many2one("jobiba.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    mapping_id = fields.Many2one("jobiba.job.mapping", required=True, ondelete="cascade", index=True)
    job_id = fields.Many2one(related="mapping_id.job_id")
    operation = fields.Selection([("upsert", "Create or update")], default="upsert", required=True)
    state = fields.Selection(
        [("queued", "Queued"), ("processing", "Sending"), ("done", "Done"), ("retry", "Will retry"),
         ("blocked", "Blocked"), ("dead_letter", "Gave up"), ("cancelled", "Cancelled")],
        default="queued", required=True, index=True)
    force = fields.Boolean(help="Send even when nothing changed since the last successful sync.")
    solo = fields.Boolean(help="Sent alone, so a field error is tied to this job.")
    attempts = fields.Integer(readonly=True)
    next_retry_at = fields.Datetime(index=True)
    lease_until = fields.Datetime(readonly=True)
    payload_hash = fields.Char(readonly=True)
    error_category = fields.Char(readonly=True)
    last_error = fields.Char(readonly=True)
    http_status = fields.Integer(readonly=True)
    correlation_id = fields.Char(readonly=True, index=True)
    done_at = fields.Datetime(readonly=True)

    # ------------------------------------------------------------------ enqueue
    @api.model
    def _enqueue(self, mapping, force=False):
        """One open item per job: a second request only raises `force`."""
        existing = self.sudo().search(
            [("mapping_id", "=", mapping.id), ("state", "in", ("queued", "retry"))], limit=1)
        if existing:
            vals = {"state": "queued", "next_retry_at": False} if existing.state == "retry" and force else {}
            if force and not existing.force:
                vals["force"] = True
            if vals:
                existing.write(vals)
            return existing
        item = self.sudo().create({"instance_id": mapping.instance_id.id, "mapping_id": mapping.id, "force": force})
        if mapping.remote_state in ("draft", "failed", "stopped"):
            mapping.sudo().write({"remote_state": "queued"})
        return item

    # ------------------------------------------------------------------ worker
    @api.model
    def _cron_process(self):
        instances = self.env["jobiba.instance"].sudo().search([("state", "in", ("connected", "attention"))])
        for instance in instances:
            try:
                self._process_instance(instance)
            except Exception:  # one connection must not stop the others
                _logger.exception("Jobiba queue failed for connection %s", instance.id)
                self.env.cr.rollback()

    def _release_stale(self, instance, now):
        stale = self.search([
            ("instance_id", "=", instance.id), ("state", "=", "processing"), ("lease_until", "<", now)])
        if stale:
            # The answer was lost. The reference makes a repeat safe: it updates.
            stale.write({"state": "retry", "next_retry_at": now, "last_error": "No answer was recorded; sending again."})

    def _claim(self, instance, now, solo, limit):
        self.flush_model()
        self.env.cr.execute(
            """
            SELECT id FROM jobiba_sync_job
             WHERE instance_id = %s AND state IN ('queued', 'retry') AND COALESCE(solo, FALSE) = %s
               AND (next_retry_at IS NULL OR next_retry_at <= %s)
             ORDER BY id
             LIMIT %s
               FOR UPDATE SKIP LOCKED
            """,
            (instance.id, solo, now, limit),
        )
        return self.browse([row[0] for row in self.env.cr.fetchall()])

    @api.model
    def _process_instance(self, instance, max_requests=3):
        """Send what is due for one connection. Returns the number of requests made."""
        instance = instance.sudo()
        now = fields.Datetime.now()
        if instance.state not in ("connected", "attention") or not instance._stored_api_key():
            return 0
        if instance.paused_until and instance.paused_until > now:
            return 0
        self = self.sudo()
        self._release_stale(instance, now)
        requests_made = 0
        for _round in range(max_requests):
            items = self._claim(instance, now, True, 1) or self._claim(instance, now, False, instance.batch_size)
            if not items:
                break
            batch = []
            for item in items:
                prepared = item._prepare()
                if prepared:
                    batch.append((item, prepared[0], prepared[1]))
            if not batch:
                instance._commit()
                continue
            correlation = uuid.uuid4().hex
            batch_items = self.browse([item.id for item, _p, _h in batch])
            batch_items.write({
                "state": "processing", "correlation_id": correlation,
                "lease_until": now + timedelta(minutes=LEASE_MINUTES),
            })
            instance._commit()
            result = instance._get_client().sync_jobs([payload for _i, payload, _h in batch], correlation)
            requests_made += 1
            instance._register_result(result)
            self._apply_result(instance, batch, result, correlation)
            instance._commit()
            if not result.ok and result.category != retry_policy.VALIDATION:
                break
            if result.rate_remaining is not None and result.rate_remaining <= 1:
                break
        return requests_made

    def _prepare(self):
        """Build the payload again at send time. None = nothing to send."""
        self.ensure_one()
        mapping, instance = self.mapping_id, self.instance_id
        job = mapping.job_id
        if not job or mapping.intent != "publish":
            self.write({"state": "cancelled", "last_error": "The job is no longer synced."})
            return None
        payload, problems, _warnings = build_job_payload(job.with_company(instance.company_id), instance)
        if problems:
            text = "\n".join(message for _field, message in problems)
            self.write({"state": "blocked", "error_category": retry_policy.VALIDATION, "last_error": text[:500]})
            mapping.write({"remote_state": "failed", "problems": text, "last_error": _("Fix the fields listed, then publish again.")})
            return None
        digest = payload_hash(payload)
        if not self.force and digest == mapping.last_sent_hash and mapping.remote_state in ("published", "in_review"):
            self.write({"state": "done", "done_at": fields.Datetime.now(), "last_error": "Nothing changed since the last sync."})
            return None
        self.payload_hash = digest
        return payload, digest

    def _apply_result(self, instance, batch, result, correlation):
        now = fields.Datetime.now()
        items = self.browse([item.id for item, _p, _h in batch])
        category = result.category
        common = {"http_status": result.status or 0, "lease_until": False, "error_category": False if result.ok else category}
        if result.ok:
            waiting = int(result.data.get("waiting_for_review") or 0)
            for item, _payload, digest in batch:
                mapping = item.mapping_id
                is_new = not mapping.first_sent_at
                if waiting >= len(batch) or (waiting and is_new):
                    state = "in_review"
                elif waiting and mapping.remote_state == "in_review":
                    state = "in_review"
                else:
                    state = "published"
                mapping.write({
                    "remote_state": state, "last_sent_hash": digest, "last_synced_at": now,
                    "first_sent_at": mapping.first_sent_at or now, "last_error": False, "problems": False,
                })
            items.write(dict(common, state="done", done_at=now, last_error=False, force=False))
            instance._log("info", "jobs_sync", "Saved %s job(s); %s waiting for Jobiba review." % (
                result.data.get("upserted", len(batch)), waiting), result.status, correlation)
            return
        message = (result.message or "")[:500]
        if category == retry_policy.VALIDATION and len(batch) > 1:
            # Jobiba names the fields, not the job: send each job alone to find it.
            items.write(dict(common, state="retry", solo=True, next_retry_at=now, last_error="Sending one by one to find the job with a field problem."))
            instance._log("warning", "jobs_sync", "A batch was refused for a field problem; its jobs are sent one by one.", result.status, correlation)
            return
        if category in (retry_policy.VALIDATION, retry_policy.CREDENTIAL, retry_policy.PLAN, retry_policy.REJECTED):
            items.write(dict(common, state="blocked", last_error=message))
            explain = instance._explain(result)
            for item in items:
                vals = {"remote_state": "failed", "last_error": explain[:500]}
                if category == retry_policy.VALIDATION:
                    vals["problems"] = message
                item.mapping_id.write(vals)
            instance._log("error", "jobs_sync", message, result.status, correlation)
            return
        if category == retry_policy.RATE_LIMITED:
            # Not the job's fault: wait for the window, attempts unchanged.
            items.write(dict(common, state="retry", next_retry_at=instance.paused_until or now, last_error=message))
            instance._log("warning", "jobs_sync", "Rate limit reached; the queue waits.", result.status, correlation)
            return
        for item in items:  # transient
            attempts = item.attempts + 1
            if retry_policy.exhausted(attempts):
                item.write(dict(common, state="dead_letter", attempts=attempts, last_error=message))
                item.mapping_id.write({"remote_state": "failed", "last_error": _("Jobiba could not be reached after several tries. Use Retry in the publishing queue.")})
            else:
                item.write(dict(common, state="retry", attempts=attempts, last_error=message,
                                next_retry_at=now + timedelta(seconds=retry_policy.backoff_seconds(attempts))))
        instance._log("warning", "jobs_sync", message, result.status, correlation)

    # ------------------------------------------------------------------ actions
    def action_retry(self):
        """Manual replay of a blocked or abandoned item. Safe: the reference updates."""
        for item in self.filtered(lambda i: i.state in ("blocked", "dead_letter", "retry", "cancelled")):
            item.sudo().write({"state": "queued", "attempts": 0, "next_retry_at": False, "last_error": False, "force": True})
            if item.mapping_id.intent == "publish":
                item.mapping_id.sudo().write({"remote_state": "queued"})
        return True

    def action_cancel(self):
        self.filtered(lambda i: i.state in ("queued", "retry", "blocked", "dead_letter")).sudo().write({"state": "cancelled"})
        return True
