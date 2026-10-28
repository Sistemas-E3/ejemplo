import base64
import secrets

from odoo import api, fields, models


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    badge_token = fields.Char(
        string="Código de gafete",
        copy=False,
        index=True,
        readonly=True,
        groups="hr.group_hr_user",
        help="Código aleatorio que va dentro del QR del gafete. "
        "Al regenerarlo, los gafetes impresos anteriormente dejan de ser válidos.",
    )
    badge_verify_url = fields.Char(
        string="Liga de verificación",
        compute="_compute_badge_verify",
        groups="hr.group_hr_user",
    )
    badge_qr = fields.Binary(
        string="QR del gafete",
        compute="_compute_badge_verify",
        groups="hr.group_hr_user",
    )
    badge_qr_filename = fields.Char(
        compute="_compute_badge_verify",
        groups="hr.group_hr_user",
    )

    _sql_constraints = [
        ("badge_token_uniq", "unique(badge_token)", "El código de gafete debe ser único."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        # El código se asigna aquí y no con default=: al instalar, Odoo llenaría
        # la columna nueva con un mismo valor para todos los empleados existentes.
        for vals in vals_list:
            if not vals.get("badge_token"):
                vals["badge_token"] = self._new_badge_token()
        return super().create(vals_list)

    @api.model
    def _new_badge_token(self):
        return secrets.token_urlsafe(16)

    @api.depends("badge_token", "name")
    def _compute_badge_verify(self):
        for employee in self:
            if not employee.badge_token:
                employee.badge_verify_url = False
                employee.badge_qr = False
                employee.badge_qr_filename = False
                continue
            url = "%s/gafete/%s" % (employee.get_base_url(), employee.badge_token)
            employee.badge_verify_url = url
            employee.badge_qr = base64.b64encode(employee._badge_qr_png(url))
            employee.badge_qr_filename = employee._badge_qr_filename()

    def _badge_qr_png(self, url):
        return self.env["ir.actions.report"].barcode(
            "QR", url, width=600, height=600, humanreadable=0, quiet=1, barLevel="M"
        )

    def _badge_qr_filename(self):
        self.ensure_one()
        safe = "".join(c if c.isalnum() else "_" for c in (self.name or "empleado"))
        prefix = self.barcode or str(self.id)
        return "%s_%s_QR.png" % (prefix, safe.strip("_"))

    def action_regenerate_badge_token(self):
        """Invalida el QR actual (por ejemplo, si el gafete se perdió)."""
        for employee in self:
            employee.badge_token = employee._new_badge_token()
        return True
