import pytz

from odoo import fields, http
from odoo.http import request

NO_CACHE = [
    ("Cache-Control", "no-store, max-age=0"),
    ("X-Robots-Tag", "noindex, nofollow"),
]


class BadgeVerifyController(http.Controller):

    @http.route("/gafete/<string:token>", type="http", auth="public", methods=["GET"], csrf=False, sitemap=False)
    def verify_badge(self, token, **kw):
        Employee = request.env["hr.employee"].sudo().with_context(active_test=False)
        employee = Employee.search([("badge_token", "=", token)], limit=1) if token else Employee
        tz = pytz.timezone(
            (employee and employee.tz) or request.env.user.tz or "America/Matamoros"
        )
        now = pytz.utc.localize(fields.Datetime.now()).astimezone(tz)
        photo = employee.image_512 if employee else False
        if isinstance(photo, bytes):
            photo = photo.decode("ascii")
        # Odoo guarda las fotos casi siempre como JPEG; base64 de JPEG empieza con "/9j/".
        mime = "image/jpeg" if photo and photo.startswith("/9j/") else "image/png"
        values = {
            "found": bool(employee),
            "active": bool(employee) and employee.active,
            "name": employee.name if employee else "",
            "job": (employee.job_title or employee.job_id.name or "") if employee else "",
            "department": employee.department_id.name if employee else "",
            "company": employee.company_id.name if employee else "",
            "photo": "data:%s;base64,%s" % (mime, photo) if photo else False,
            "checked_at": now.strftime("%d/%m/%Y %H:%M"),
        }
        response = request.render("etres_badge_verify.verify_page", values)
        response.headers.update(dict(NO_CACHE))
        return response
