{
    "name": "Jobiba Recruitment Sync",
    "version": "18.0.1.3.0",
    "summary": "Publish Odoo job positions on Jobiba and receive Jobiba applicants in your recruitment pipeline",
    "description": """
Odoo stays the system of record for your vacancies and applicants. Jobiba
(https://jobiba.com) adds reach: approved job positions are sent to the Jobiba
job marketplace and kept up to date, and each application made on Jobiba
appears as an applicant in the right Odoo job position.

External service: this module needs a Jobiba employer account (free to
open; the Free plan holds 3 open jobs through the connection) and sends data
to jobiba.com only after an integration administrator has agreed on the
connection screen.

Data sent to jobiba.com: your API key with each request; for each job
position you publish, a reference (database ID, company ID, job ID), the
title, location and description and, when filled in, the application link,
the pay with its currency and period, job type, work mode and experience level; the references
of jobs you sent and the Jobiba numbers of applications already imported.
No applicant, employee or other record of your database is sent.

Data received from jobiba.com: your plan and limits, the state of your
jobs, and your own applications (name, job and stage; email, phone, answers
and CV files only on a paid Jobiba plan with applicant details switched on
in Jobiba). Privacy policy: https://jobiba.com/privacy
""",
    "category": "Human Resources/Recruitment",
    "author": "Jobiba AI Labs",
    "website": "https://jobiba.com/developers",
    "support": "info@jobiba.com",
    "license": "LGPL-3",
    "depends": ["hr_recruitment"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/jobiba_security.xml",
        "security/ir.model.access.csv",
        "data/jobiba_data.xml",
        "data/ir_cron.xml",
        "wizard/connect_views.xml",
        "wizard/publish_views.xml",
        "views/jobiba_instance_views.xml",
        "views/jobiba_queue_views.xml",
        "views/jobiba_log_views.xml",
        "views/hr_job_views.xml",
        "views/hr_applicant_views.xml",
        "views/jobiba_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "jobiba_recruitment_connector/static/src/jobiba.scss",
        ],
    },
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
}
