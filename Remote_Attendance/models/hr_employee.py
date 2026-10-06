import re
import secrets
from datetime import datetime, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError

OPS_GROUP = 'Remote_Attendance.group_field_operations'
PIN_MAX_FAILURES = 5
PIN_LOCK_MINUTES = 15


def compact_key(text):
    """Employee key without spaces, dashes or case: "E-0042" -> "e0042"."""
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    field_role = fields.Selection([
        ('supervisor', "Supervisor de campo"),
        ('worker', "Trabajador de campo"),
        ('office', "Kiosco de oficina"),
    ], string="Rol en campo", groups=OPS_GROUP, tracking=True,
        help="Kiosco de oficina: el enlace de la tablet de la entrada; reconoce a todo el personal de campo "
             "y acepta la tarjeta (ID de credencial).")
    field_supervisor_id = fields.Many2one(
        'hr.employee', string="Supervisor de su cuadrilla", groups=OPS_GROUP, tracking=True,
        domain=[('field_role', '=', 'supervisor')], index='btree_not_null')
    field_crew_ids = fields.One2many(
        'hr.employee', 'field_supervisor_id', string="Cuadrilla", groups=OPS_GROUP)
    field_project_ids = fields.Many2many(
        'project.project', 'project_field_supervisor_rel', 'employee_id', 'project_id',
        string="Obras asignadas", groups=OPS_GROUP,
        help="Obras que este supervisor puede activar. Las asigna Operaciones.")
    field_active_project_ids = fields.Many2many(
        'project.project', 'hr_employee_field_active_project_rel', 'employee_id', 'project_id',
        string="Obras activas", groups=OPS_GROUP, readonly=True)
    field_active_date = fields.Date("Obras activas del día", groups=OPS_GROUP, readonly=True)
    field_token = fields.Char("Clave del enlace", groups=OPS_GROUP, copy=False, index='btree_not_null')
    field_kiosk_url = fields.Char("Enlace de marcaje", compute='_compute_field_kiosk_url', groups=OPS_GROUP)
    field_device_ids = fields.One2many('hr.field.device', 'employee_id', string="Teléfonos", groups=OPS_GROUP)
    field_face_ids = fields.One2many('hr.field.face', 'employee_id', string="Rostros", groups=OPS_GROUP)
    field_face_count = fields.Integer("Rostros registrados", compute='_compute_field_face_count', groups=OPS_GROUP)
    field_face_consent_date = fields.Date(
        "Consentimiento biométrico", groups=OPS_GROUP, tracking=True,
        help="Fecha en que el empleado firmó el consentimiento para el uso de su rostro.")
    field_pin_failures = fields.Integer(groups=OPS_GROUP, copy=False)
    field_pin_locked_until = fields.Datetime("PIN bloqueado hasta", groups=OPS_GROUP, copy=False)

    _sql_constraints = [('field_token_unique', 'UNIQUE(field_token)', "La clave del enlace debe ser única.")]

    @api.depends('field_token')
    def _compute_field_kiosk_url(self):
        base_url = self.get_base_url()
        for employee in self:
            employee.field_kiosk_url = employee.field_token and f"{base_url}/campo/{employee.field_token}"

    @api.depends('field_face_ids')
    def _compute_field_face_count(self):
        for employee in self:
            employee.field_face_count = len(employee.field_face_ids)

    # ------------------------------------------------------------------
    # Actions (Operaciones)
    # ------------------------------------------------------------------

    def action_field_regenerate_token(self):
        """Create a new secret link; the previous link and its phones stop working."""
        for employee in self:
            if not employee.field_role:
                raise UserError(_("Asigna primero el rol en campo de %s.", employee.name))
            employee.sudo().field_device_ids.filtered(lambda d: d.state != 'revoked').action_revoke()
            employee.field_token = secrets.token_urlsafe(24)

    def action_field_unlock_pin(self):
        self.write({'field_pin_failures': 0, 'field_pin_locked_until': False})

    def action_field_enroll_face(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/campo/enrolar/{self.id}',
            'target': 'new',
        }

    def action_field_clear_faces(self):
        self.sudo().field_face_ids.unlink()

    # ------------------------------------------------------------------
    # Helpers used by the kiosk (called with sudo)
    # ------------------------------------------------------------------

    def _field_today(self):
        self.ensure_one()
        return datetime.now(pytz.timezone(self._get_tz() or 'UTC')).date()

    def _field_leader(self):
        """The supervisor whose active projects apply to this employee."""
        self.ensure_one()
        return self if self.field_role == 'supervisor' else self.field_supervisor_id

    def _field_active_projects(self):
        """Today's active projects of this supervisor, limited to the ones assigned to him."""
        self.ensure_one()
        if self.field_active_date != self._field_today():
            return self.env['project.project']
        return self.field_active_project_ids & self.field_project_ids

    @api.model
    def _field_staff(self):
        """Everybody who marks attendance in the field (supervisors and workers)."""
        return self.search([('field_role', 'in', ('supervisor', 'worker'))])

    def _field_candidates(self):
        """Employees that can be recognised on this owner's link.

        A supervisor's phone and the office kiosk recognise all the field staff, because
        supervisors borrow people from other crews; a personal link only its owner."""
        self.ensure_one()
        if self.field_role == 'supervisor':
            return self | self.field_crew_ids.filtered('active') | self._field_staff()
        if self.field_role == 'office':
            return self._field_staff()
        return self

    def _field_keys(self):
        """{compact key: employee} for the employees of self that have an "ID de credencial"."""
        return {compact_key(e.barcode): e for e in self if compact_key(e.barcode)}

    def _field_by_key(self, key):
        """Field employee whose "ID de credencial" is ``key`` (card reader or typed)."""
        key = compact_key(key)
        if not key:
            return self.browse()
        return self._field_staff()._field_keys().get(key, self.browse())

    def _field_check_pin(self, pin):
        """Check the employee PIN with lockout. Return True/False, raise when locked."""
        self.ensure_one()
        now = fields.Datetime.now()
        if self.field_pin_locked_until and self.field_pin_locked_until > now:
            raise UserError(_("PIN bloqueado por intentos fallidos. Pide a Operaciones que lo desbloquee."))
        if self.pin and pin and str(pin) == self.pin:
            if self.field_pin_failures:
                self.field_pin_failures = 0
            return True
        failures = self.field_pin_failures + 1
        vals = {'field_pin_failures': failures}
        if failures >= PIN_MAX_FAILURES:
            vals.update(field_pin_failures=0, field_pin_locked_until=now + timedelta(minutes=PIN_LOCK_MINUTES))
        self.write(vals)
        return False
