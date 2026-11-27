{
    "name": "Jobiba Career Pages",
    "version": "19.0.1.0.1",
    "summary": "Career pages for your Odoo website: four layouts, perks, filters and Google-ready job data",
    "description": """
Build a careers page on your own Odoo website in minutes. Pick one of four
layouts, set your colour, headline and perks, and the page lists the job
positions you published, with search and filters. Candidates apply with
Odoo's own application form, so every applicant lands in Odoo Recruitment.
Each job page also carries JobPosting structured data for search engines.

Works with Jobiba Recruitment Sync: the same jobs can be advertised on the
Jobiba job marketplace with a link back to your career page.
""",
    "category": "Website/Website",
    "author": "Jobiba AI Labs",
    "website": "https://jobiba.com/developers",
    "support": "info@jobiba.com",
    "license": "LGPL-3",
    "depends": ["website_hr_recruitment", "jobiba_recruitment_connector"],
    "data": [
        "security/ir.model.access.csv",
        "security/career_security.xml",
        "views/career_page_views.xml",
        "views/career_templates.xml",
        "views/job_detail_templates.xml",
        "data/career_data.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "jobiba_career_pages/static/src/scss/career.scss",
        ],
    },
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
}
