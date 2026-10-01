import hashlib
import secrets

from odoo import _, fields, models
from odoo.exceptions import UserError

MAX_PENDING_DEVICES = 3


def hash_device_key(key):
    return hashlib.sha256((key or '').encode()).hexdigest()


class HrFieldDevice(models.Model):
    _name = 'hr.field.device'
    _description = "Teléfono autorizado (asistencias en campo)"
    _inherit = ['mail.thread']
    _order = 'state desc, create_date desc'

    name = fields.Char("Teléfono", required=True, tracking=True)
    employee_id = fields.Many2one(
        'hr.employee', string="Dueño del enlace", required=True, ondelete='cascade', index=True)
    key_hash = fields.Char(required=True, copy=False, index=True, groups='base.group_system')
    state = fields.Selection([
        ('pending', "Pendiente de aprobar"),
        ('approved', "Aprobado"),
        ('revoked', "Revocado"),
    ], default='pending', required=True, tracking=True)
    user_agent = fields.Char("Navegador", readonly=True)
    last_seen = fields.Datetime("Último uso", readonly=True)
    approved_by_id = fields.Many2one('res.users', "Aprobado por", readonly=True)
    approved_date = fields.Datetime("Aprobado el", readonly=True)

    def action_approve(self):
        self.write({
            'state': 'approved',
            'approved_by_id': self.env.user.id,
            'approved_date': fields.Datetime.now(),
        })

    def action_revoke(self):
        self.write({'state': 'revoked'})

    def _field_register(self, employee, name, user_agent):
        """Create a pending device for ``employee`` and return (device, clear key)."""
        pending = self.sudo().search_count([('employee_id', '=', employee.id), ('state', '=', 'pending')])
        if pending >= MAX_PENDING_DEVICES:
            raise UserError(_("Hay demasiadas solicitudes pendientes para este enlace. Pide a Operaciones que las revise."))
        key = secrets.token_urlsafe(32)
        device = self.sudo().create({
            'name': (name or "Teléfono")[:64],
            'employee_id': employee.id,
            'key_hash': hash_device_key(key),
            'user_agent': (user_agent or '')[:256],
        })
        return device, key

    def _field_find(self, employee, key):
        if not key:
            return self.browse()
        return self.sudo().search([
            ('employee_id', '=', employee.id),
            ('key_hash', '=', hash_device_key(key)),
        ], limit=1)
