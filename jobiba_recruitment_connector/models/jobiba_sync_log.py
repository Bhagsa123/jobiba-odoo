import re

from odoo import api, fields, models

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_BEARER = re.compile(r"(?i)bearer\s+\S+")
_SECRETISH = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def redact(text):
    """Remove emails, phone numbers, bearer tokens and long key-like strings."""
    text = " ".join(str(text or "").split())
    text = _BEARER.sub("Bearer [removed]", text)
    text = _EMAIL.sub("[email removed]", text)
    text = _PHONE.sub("[number removed]", text)
    text = _SECRETISH.sub("[removed]", text)
    return text[:1000]


class JobibaSyncLog(models.Model):
    _name = "jobiba.sync.log"
    _description = "Jobiba operational log"
    _order = "id desc"
    _rec_name = "operation"

    instance_id = fields.Many2one("jobiba.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    level = fields.Selection(
        [("info", "Info"), ("warning", "Warning"), ("error", "Error"), ("security", "Security")],
        required=True, default="info", index=True)
    operation = fields.Char(required=True, index=True)
    message = fields.Char()
    http_status = fields.Integer()
    correlation_id = fields.Char(index=True)
    job_id = fields.Many2one("hr.job", ondelete="set null")

    @api.model
    def _record(self, instance, level, operation, message, http_status=None, correlation_id=None, job=None):
        return self.create({
            "instance_id": instance.id,
            "level": level,
            "operation": operation,
            "message": redact(message),
            "http_status": http_status or 0,
            "correlation_id": correlation_id,
            "job_id": job.id if job else False,
        })
