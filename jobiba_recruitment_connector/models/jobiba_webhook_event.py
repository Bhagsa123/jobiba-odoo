import json
import logging
from psycopg2 import IntegrityError

from odoo import _, api, fields, models
from odoo.tools import mute_logger

_logger = logging.getLogger(__name__)

KNOWN_EVENTS = ("application.created", "application.stage_changed", "application.withdrawn", "lead.applied", "ping")
MANAGER_GROUP = "jobiba_recruitment_connector.group_jobiba_manager"


def _text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def minimal_payload(body):
    """Keep only the fields this addon uses. Nothing else from the body is stored."""
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    application = data.get("application") if isinstance(data.get("application"), dict) else {}
    job = data.get("job") if isinstance(data.get("job"), dict) else {}
    return {
        "application": {
            "id": _text(application.get("id"), 64),
            "name": _text(application.get("name"), 120),
            "stage": _text(application.get("stage"), 40),
            "previous_stage": _text(application.get("previous_stage"), 40),
            "applied_at": _text(application.get("applied_at"), 40),
        },
        "job": {
            "id": _text(job.get("id"), 64),
            "title": _text(job.get("title"), 255),
            "url": _text(job.get("url"), 500),
            "reference": _text(job.get("reference"), 255),
        },
        "applicants_url": _text(data.get("applicants_url"), 500),
    }


class JobibaWebhookEvent(models.Model):
    _name = "jobiba.webhook.event"
    _description = "Jobiba webhook event"
    _order = "id desc"
    _rec_name = "delivery_id"

    instance_id = fields.Many2one("jobiba.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    delivery_id = fields.Char(required=True, index=True)
    event_type = fields.Char(required=True, index=True)
    state = fields.Selection(
        [("received", "Received"), ("processed", "Processed"), ("ignored", "Ignored"), ("error", "Needs a person")],
        default="received", required=True, index=True)
    payload = fields.Text(groups=MANAGER_GROUP, help="Only the fields this addon uses. Cleared once processed.")
    error = fields.Char(readonly=True)
    applicant_id = fields.Many2one("hr.applicant", ondelete="set null")
    processed_at = fields.Datetime(readonly=True)

    # Odoo 19.0 dropped _sql_constraints for models.Constraint; both keep the same database constraint names.
    if hasattr(models, "Constraint"):
        _instance_delivery_unique = models.Constraint("UNIQUE (instance_id, delivery_id)", "This delivery was already received.")
    else:
        _sql_constraints = [
            ("instance_delivery_unique", "unique(instance_id, delivery_id)", "This delivery was already received."),
        ]

    # ------------------------------------------------------------------ intake
    @api.model
    def _ingest(self, instance, delivery_id, event_type, body):
        """Store a verified delivery once. Returns (event, created)."""
        existing = self.sudo().search(
            [("instance_id", "=", instance.id), ("delivery_id", "=", delivery_id)], limit=1)
        if existing:
            return existing, False
        try:
            with self.env.cr.savepoint(), mute_logger("odoo.sql_db"):
                event = self.sudo().create({
                    "instance_id": instance.id,
                    "delivery_id": delivery_id,
                    "event_type": event_type,
                    "payload": json.dumps(minimal_payload(body)),
                })
        except IntegrityError:
            # The same delivery arrived twice at the same moment: the first one counts.
            return self.sudo().search(
                [("instance_id", "=", instance.id), ("delivery_id", "=", delivery_id)], limit=1), False
        return event, True

    def _process(self):
        for event in self.sudo():
            try:
                with self.env.cr.savepoint():
                    event._process_one()
            except Exception as error:  # keep the event for a person, never lose it
                _logger.exception("Jobiba webhook event %s failed", event.id)
                event.write({"state": "error", "error": _("Unexpected error (%s). Use Reprocess after checking the job.", type(error).__name__)})

    def _done(self, state, error=False, applicant=False):
        vals = {"state": state, "error": error, "processed_at": fields.Datetime.now()}
        if applicant:
            vals["applicant_id"] = applicant.id
        if state in ("processed", "ignored"):
            vals["payload"] = False  # nothing personal is kept after use
        self.write(vals)

    def _process_one(self):
        self.ensure_one()
        instance = self.instance_id
        if self.event_type == "ping":
            instance.write({"webhook_verified_at": fields.Datetime.now()})
            return self._done("processed")
        if self.event_type not in KNOWN_EVENTS:
            return self._done("ignored", _("This kind of event is not used."))
        data = json.loads(self.payload or "{}")
        application = data.get("application") or {}
        if not application.get("id"):
            return self._done("error", _("The event names no application."))
        Mapping = self.env["jobiba.application.mapping"].sudo()
        link = Mapping.search(
            [("instance_id", "=", instance.id), ("application_ref", "=", application["id"])], limit=1)
        if self.event_type in ("application.created", "lead.applied"):
            if link:
                # A second event for the same application never makes a second applicant.
                return self._done("processed", applicant=link.applicant_id)
            if not instance.application_import:
                return self._done("ignored", _("Creating applicants is switched off for this connection."))
            return self._create_applicant(data)
        if not link:
            return self._done("error", _("The application is not known in Odoo (its 'created' event was not received)."))
        stage = application.get("stage") or ""
        if self.event_type == "application.withdrawn":
            # Status only: the Odoo record stays, nothing more is imported for it.
            link._mark_withdrawn(stage)
        else:
            link._note_stage(stage)
        return self._done("processed", applicant=link.applicant_id)

    def _create_applicant(self, data):
        """Create the native applicant. Details (if the plan sends them) follow with the next pull."""
        link, error = self.env["jobiba.application.mapping"]._create_from(
            self.instance_id, data["application"], data.get("job") or {}, data.get("applicants_url"))
        if error:
            return self._done("error", error)
        return self._done("processed", applicant=link.applicant_id)

    def action_reprocess(self):
        events = self.filtered(lambda e: e.state == "error" and e.sudo().payload)
        events._process()
        return True
