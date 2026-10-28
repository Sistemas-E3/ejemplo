import base64
import io
import zipfile

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
        values = {
            "found": bool(employee),
            "active": bool(employee) and employee.active,
            "name": employee.name if employee else "",
            "job": (employee.job_title or employee.job_id.name or "") if employee else "",
            "department": employee.department_id.name if employee else "",
            "company": employee.company_id.name if employee else "",
            "photo": (
                "data:image/png;base64,%s" % employee.image_512.decode()
                if employee and employee.image_512 else False
            ),
            "checked_at": now.strftime("%d/%m/%Y %H:%M"),
        }
        response = request.render("etres_badge_verify.verify_page", values)
        response.headers.update(dict(NO_CACHE))
        return response

    @http.route("/gafete/qr_zip", type="http", auth="user", methods=["GET"])
    def download_qr_zip(self, ids="", **kw):
        if not request.env.user.has_group("hr.group_hr_user"):
            return request.not_found()
        id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
        employees = request.env["hr.employee"].with_context(active_test=False).browse(id_list).exists()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for employee in employees:
                if not employee.badge_token:
                    continue
                archive.writestr(
                    employee._badge_qr_filename(),
                    base64.b64decode(employee.badge_qr),
                )
        return request.make_response(
            buffer.getvalue(),
            headers=[
                ("Content-Type", "application/zip"),
                ("Content-Disposition", 'attachment; filename="QR_Gafetes_Etres.zip"'),
            ] + NO_CACHE,
        )
