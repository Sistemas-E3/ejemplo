from odoo import fields, models


class AccountAnalyticLine(models.Model):
    _inherit = 'account.analytic.line'

    field_attendance_id = fields.Many2one(
        'hr.attendance', string="Asistencia en campo", index='btree_not_null',
        ondelete='set null', readonly=True, copy=False)
