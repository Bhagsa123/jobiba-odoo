from odoo import fields, models
from odoo.addons.hr import models as hr_models

from ..services.job_payload_mapper import JOB_TYPES

JOB_TYPE_SELECTION = [
    ("FULL_TIME", "Full time"), ("PART_TIME", "Part time"), ("CONTRACT", "Contract"),
    ("INTERNSHIP", "Internship"), ("FREELANCE", "Freelance"),
]
assert tuple(key for key, _label in JOB_TYPE_SELECTION) == JOB_TYPES


#: Odoo 20.0 renamed hr.contract.type to hr.employee.type and hr.job.contract_type_id to employee_type_id.
EMPLOYMENT_TYPE_MODEL, EMPLOYMENT_TYPE_FIELD = (
    ("hr.employee.type", "employee_type_id") if hasattr(hr_models, "hr_employee_type")
    else ("hr.contract.type", "contract_type_id"))


class HrContractType(models.Model):
    _inherit = EMPLOYMENT_TYPE_MODEL

    jobiba_job_type = fields.Selection(
        JOB_TYPE_SELECTION, string="Jobiba job type",
        help="Jobs with this employment type are sent to Jobiba with this job type. Leave empty to send none.",
    )
