import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

LAYOUTS = [
    ("aurora", "Aurora: dark hero, job cards"),
    ("ledger", "Ledger: clean list, one row per job"),
    ("mosaic", "Mosaic: jobs grouped by department"),
    ("spotlight", "Spotlight: newest job featured, then the list"),
]
ICONS = [
    ("fa-heartbeat", "Health"), ("fa-graduation-cap", "Learning"), ("fa-clock-o", "Flexible hours"),
    ("fa-home", "Remote work"), ("fa-line-chart", "Growth"), ("fa-users", "Team"), ("fa-plane", "Travel"),
    ("fa-cutlery", "Meals"), ("fa-bus", "Transport"), ("fa-shield", "Safety"), ("fa-money", "Pay"),
    ("fa-star", "Other"),
]
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
JOBS_PER_PAGE = 24


class JobibaCareerPage(models.Model):
    _name = "jobiba.career.page"
    _description = "Career page"
    _inherit = ["website.published.multi.mixin", "website.seo.metadata"]
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True, default=lambda self: _("Careers"))
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    slug = fields.Char(
        "Address", required=True, default="careers",
        help="The page is served at /careers/<address>. The page with the address 'careers' is also served at /careers.")
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)
    layout = fields.Selection(LAYOUTS, required=True, default="aurora")
    accent_color = fields.Char("Accent colour", default="#0f9d6e", required=True)
    hero_title = fields.Char(translate=True, default=lambda self: _("Build your career with us"))
    hero_subtitle = fields.Char(translate=True, default=lambda self: _("See our open roles and apply in a few minutes."))
    hero_image = fields.Image(max_width=1920, max_height=1080)
    intro = fields.Html(translate=True, sanitize=True, help="Shown under the headline. Say who you are and how you hire.")
    department_ids = fields.Many2many("hr.department", string="Only these departments", help="Leave empty to list every published job.")
    show_search = fields.Boolean("Search box", default=True)
    show_department_filter = fields.Boolean("Department filter", default=True)
    show_location_filter = fields.Boolean("Location filter", default=True)
    show_stats = fields.Boolean("Counts of roles, locations and departments", default=True)
    show_salary = fields.Boolean("Show pay when a job states it", default=True)
    show_perks = fields.Boolean("Perks section", default=True)
    perk_ids = fields.One2many("jobiba.career.perk", "page_id", copy=True)
    cta_title = fields.Char("Closing headline", translate=True, default=lambda self: _("No role that fits today?"))
    cta_text = fields.Char("Closing text", translate=True, default=lambda self: _("Tell us about yourself and we will keep you in mind."))
    cta_url = fields.Char("Closing button link", default="/contactus")
    cta_label = fields.Char("Closing button text", translate=True, default=lambda self: _("Contact us"))
    show_jobiba_credit = fields.Boolean("Show 'Jobs also on Jobiba' line", default=True)
    job_count = fields.Integer(compute="_compute_job_count")

    # Odoo 19.0 dropped _sql_constraints for models.Constraint; both keep the same database constraint names.
    if hasattr(models, "Constraint"):
        _slug_website_unique = models.Constraint("UNIQUE (slug, website_id)", "Another career page already uses this address.")
    else:
        _sql_constraints = [
            ("slug_website_unique", "unique(slug, website_id)", "Another career page already uses this address."),
        ]

    @api.constrains("accent_color", "slug", "cta_url")
    def _check_values(self):
        for page in self:
            if not _HEX.match(page.accent_color or ""):
                raise ValidationError(_("The accent colour must look like #0f9d6e."))
            if not _SLUG.match(page.slug or ""):
                raise ValidationError(_("The address may hold lower-case letters, digits and single dashes only."))
            url = (page.cta_url or "").strip()
            if url and not (url.startswith("/") and not url.startswith("//")) and not url.startswith("https://") and not url.startswith("mailto:"):
                raise ValidationError(_("The closing button link must start with /, https:// or mailto:"))

    def _compute_website_url(self):
        for page in self:
            page.website_url = "/careers" if page.slug == "careers" else "/careers/%s" % page.slug

    def _compute_job_count(self):
        for page in self:
            page.job_count = self.env["hr.job"].sudo().search_count(page._job_domain())

    # ------------------------------------------------------------------ data
    def _job_domain(self, website=None, search=None, department=None, country=None):
        self.ensure_one()
        domain = [("website_published", "=", True)]
        if website:
            domain += website.website_domain()
        if self.company_id:
            domain.append(("company_id", "in", [False, self.company_id.id]))
        if self.department_ids:
            domain.append(("department_id", "in", self.department_ids.ids))
        if department:
            domain.append(("department_id", "=", department.id))
        if country:
            domain.append(("address_id.country_id", "=", country.id))
        if search:
            term = search.strip()[:80]
            if term:
                domain += ["|", ("name", "ilike", term), ("description", "ilike", term)]
        return domain

    def _facets(self, website):
        """Departments and countries that really have a published job on this page."""
        self.ensure_one()
        jobs = self.env["hr.job"].sudo().search(self._job_domain(website))
        departments = jobs.mapped("department_id").sorted("name")
        countries = jobs.mapped("address_id.country_id").sorted("name")
        cities = {(job.address_id.city or "").strip().lower() for job in jobs if job.address_id.city}
        return {
            "total": len(jobs),
            "departments": departments,
            "countries": countries,
            "location_count": len(cities) or len(countries),
        }

    def action_open_page(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": self.website_url, "target": "new"}

    def action_add_to_menu(self):
        """Add a 'Careers' entry to the website's main menu (once)."""
        Menu = self.env["website.menu"]
        for page in self:
            websites = page.website_id or self.env["website"].search([])
            for website in websites:
                if Menu.search([("website_id", "=", website.id), ("url", "=", page.website_url)], limit=1):
                    continue
                Menu.create({"name": page.name, "url": page.website_url, "parent_id": website.menu_id.id,
                             "website_id": website.id, "sequence": 45})
        return True


class JobibaCareerPerk(models.Model):
    _name = "jobiba.career.perk"
    _description = "Career page perk"
    _order = "sequence, id"

    page_id = fields.Many2one("jobiba.career.page", required=True, ondelete="cascade")
    sequence = fields.Integer(default=10)
    icon = fields.Selection(ICONS, default="fa-star", required=True)
    name = fields.Char("Title", required=True, translate=True)
    text = fields.Char(translate=True)
