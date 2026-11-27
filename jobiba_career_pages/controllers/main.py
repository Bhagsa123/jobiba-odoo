import json

from markupsafe import Markup
from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request

from ..models.career_page import JOBS_PER_PAGE


class JobibaCareers(http.Controller):
    def _website(self):
        # Odoo 20.0 moved the current website from the request to its environment.
        return getattr(request, "website", None) or request.env.website

    def _page_for(self, slug):
        website = self._website()
        Page = request.env["jobiba.career.page"].sudo()
        page = Page.search([("slug", "=", slug)] + website.website_domain(), order="website_id, sequence, id", limit=1)
        if not page:
            raise NotFound()
        if not page.website_published and not request.env.user.has_group("website.group_website_designer"):
            raise NotFound()
        return page

    def _int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @http.route(
        ["/careers", "/careers/page/<int:page_number>", "/careers/<string:slug>", "/careers/<string:slug>/page/<int:page_number>"],
        type="http", auth="public", website=True, sitemap=True)
    def careers(self, slug="careers", page_number=1, q=None, department=None, country=None, **_kwargs):
        career = self._page_for(slug)
        website = self._website()
        Job = request.env["hr.job"].sudo()
        facets = career._facets(website)
        department_rec = facets["departments"].filtered(lambda d: d.id == self._int(department))[:1]
        country_rec = facets["countries"].filtered(lambda c: c.id == self._int(country))[:1]
        search = (q or "").strip()[:80]
        domain = career._job_domain(website, search=search, department=department_rec, country=country_rec)
        total = Job.search_count(domain)
        url_args = {key: value for key, value in (
            ("q", search), ("department", department_rec.id), ("country", country_rec.id)) if value}
        pager = website.pager(url=career.website_url, total=total, page=page_number, step=JOBS_PER_PAGE, url_args=url_args)
        jobs = Job.search(domain, limit=JOBS_PER_PAGE, offset=pager["offset"], order="published_date desc, sequence, id desc")
        filtered = bool(url_args)
        base = website.get_base_url().rstrip("/")
        item_list = {
            "@context": "https://schema.org", "@type": "ItemList", "name": career.name, "numberOfItems": total,
            "itemListElement": [
                {"@type": "ListItem", "position": pager["offset"] + index + 1, "url": base + (job.website_url or "")}
                for index, job in enumerate(jobs)],
        }
        groups = []
        if career.layout == "mosaic":
            for dept in jobs.mapped("department_id").sorted("name"):
                groups.append((dept.name, jobs.filtered(lambda job, dept=dept: job.department_id == dept)))
            rest = jobs.filtered(lambda job: not job.department_id)
            if rest:
                groups.append((False, rest))
        return request.render("jobiba_career_pages.career_page", {
            "career": career,
            "main_object": career,
            "jobs": jobs,
            "job_groups": groups,
            "total": total,
            "facets": facets,
            "search": search,
            "department": department_rec,
            "country": country_rec,
            "filtered": filtered,
            "pager": pager,
            "item_list_json": Markup(json.dumps(item_list).replace("<", "\\u003c")) if jobs and not filtered else "",
            "noindex": filtered or not career.website_published,
        })
