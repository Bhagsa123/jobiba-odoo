# Jobiba for Odoo

Two addons for Odoo Recruitment. Licence: LGPL-3 (file `LICENSE`).

| Addon | What it does |
|---|---|
| `jobiba_recruitment_connector` | Sends the job positions you approve to Jobiba (jobiba.com), keeps them up to date, and creates an Odoo applicant for each application made on Jobiba |
| `jobiba_career_pages` | A careers page on your own Odoo website: four layouts, perks, filters, JobPosting data |

One branch per Odoo series (`18.0`, `19.0`, `20.0`). Install: copy both folders into your addons path (or add this
repository to your Odoo.sh project), update the Apps list, install "Jobiba Recruitment Sync".

External service: the connector sends job data to jobiba.com and reads your own applications from it. What is sent
is listed on the connection screen and in each addon's description. Setup guide: https://jobiba.com/developers#hiring-system

Support: info@jobiba.com
