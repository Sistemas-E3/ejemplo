from datetime import datetime, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.addons.base.models.res_partner import _tz_get

from .hr_field_service import REPEAT_MINUTES

MAX_PENDING_CLOCKS = 5


class HrFieldClock(models.Model):
    """A ZKTeco time clock that sends its punches to Odoo by itself (ADMS / PUSH protocol)."""
    _name = 'hr.field.clock'
    _description = "Checador (asistencias en campo)"
    _inherit = ['mail.thread']
    _order = 'state desc, name'

    name = fields.Char("Checador", required=True, default="Checador", tracking=True)
    serial = fields.Char("Número de serie", required=True, copy=False, index=True, tracking=True,
                         help="El S/N que aparece en el checador (Menú › Información del sistema).")
    state = fields.Selection([
        ('pending', "Pendiente de aprobar"),
        ('approved', "Aprobado"),
        ('revoked', "Revocado"),
    ], default='pending', required=True, tracking=True)
    tz = fields.Selection(_tz_get, string="Zona horaria", required=True, default='America/Mexico_City',
                          help="La hora que muestra el checador se interpreta en esta zona.")
    last_seen = fields.Datetime("Última conexión", readonly=True)
    last_ip = fields.Char("Última IP", readonly=True)
    att_stamp = fields.Char(readonly=True, copy=False)
    log_ids = fields.One2many('hr.field.clock.log', 'clock_id', string="Registros")
    log_count = fields.Integer("Número de registros", compute='_compute_log_count')
    error_count = fields.Integer("Con error", compute='_compute_log_count')

    _sql_constraints = [('serial_unique', 'unique(serial)', "Ya hay un checador con ese número de serie.")]

    def _compute_log_count(self):
        Log = self.env['hr.field.clock.log']
        for clock in self:
            clock.log_count = Log.search_count([('clock_id', '=', clock.id)])
            clock.error_count = Log.search_count([('clock_id', '=', clock.id), ('state', '=', 'error')])

    def action_approve(self):
        self.write({'state': 'approved'})

    def action_revoke(self):
        self.write({'state': 'revoked'})

    def action_view_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('Remote_Attendance.hr_field_clock_log_action')
        action['domain'] = [('clock_id', '=', self.id)]
        return action

    @api.model
    def _clock_seen(self, serial, ip=None):
        """The clock that is talking to Odoo; an unknown serial number is left pending for Operations."""
        serial = (serial or '').strip()[:64]
        if not serial:
            return self.browse()
        clock = self.sudo().search([('serial', '=', serial)], limit=1)
        if not clock:
            if self.sudo().search_count([('state', '=', 'pending')]) >= MAX_PENDING_CLOCKS:
                return self.browse()
            clock = self.sudo().create({'name': _("Checador %s", serial), 'serial': serial})
        clock.write({'last_seen': fields.Datetime.now(), 'last_ip': (ip or '')[:64]})
        return clock

    def _clock_to_utc(self, text):
        self.ensure_one()
        local = datetime.strptime(text.strip(), '%Y-%m-%d %H:%M:%S')
        return pytz.timezone(self.tz).localize(local).astimezone(pytz.utc).replace(tzinfo=None)

    def _clock_receive(self, body, stamp=None):
        """Save the ATTLOG lines sent by the clock and turn them into attendances. Return the lines read."""
        self.ensure_one()
        Log = self.env['hr.field.clock.log'].sudo()
        logs = Log.browse()
        lines = [line for line in (body or '').splitlines() if line.strip()]
        for line in lines:
            parts = line.split('\t')
            if len(parts) < 2:
                continue
            key = parts[0].strip()[:32]
            try:
                when = self._clock_to_utc(parts[1])
            except ValueError:
                continue
            # The clock resends what it is not sure we got: the same punch is saved once.
            if Log.search_count([('clock_id', '=', self.id), ('employee_key', '=', key), ('punch_time', '=', when)]):
                continue
            logs |= Log.create({
                'clock_id': self.id,
                'employee_key': key,
                'punch_time': when,
                'device_status': parts[2].strip()[:8] if len(parts) > 2 else False,
                'verify': parts[3].strip()[:8] if len(parts) > 3 else False,
            })
        if stamp:
            self.att_stamp = stamp[:32]
        logs._clock_process()
        return len(lines)


class HrFieldClockLog(models.Model):
    """Every punch the clock sent, as it came, and what Odoo did with it."""
    _name = 'hr.field.clock.log'
    _description = "Registro del checador"
    _order = 'punch_time desc, id desc'
    _rec_name = 'employee_key'

    clock_id = fields.Many2one('hr.field.clock', "Checador", required=True, ondelete='cascade', index=True)
    employee_key = fields.Char("Clave en el checador", required=True, index=True)
    employee_id = fields.Many2one('hr.employee', "Empleado", readonly=True)
    punch_time = fields.Datetime("Hora", required=True, index=True)
    device_status = fields.Char("Estado en el checador", readonly=True)
    verify = fields.Char("Verificación", readonly=True, help="1 huella, 15 rostro, 4 tarjeta, 0 contraseña.")
    state = fields.Selection([
        ('new', "Por procesar"),
        ('entry', "Entrada"),
        ('exit', "Salida"),
        ('ignored', "Ignorado"),
        ('error', "Con error"),
    ], string="Resultado", default='new', required=True, index=True)
    result = fields.Char("Detalle", readonly=True)
    attendance_id = fields.Many2one('hr.attendance', "Asistencia", readonly=True, ondelete='set null')

    def _clock_process(self):
        Employee = self.env['hr.employee'].sudo()
        service = self.env['hr.field.service'].sudo()
        for log in self.sorted(lambda l: (l.punch_time, l.id)):
            employee = Employee._field_by_key(log.employee_key)
            if not employee:
                log.write({'state': 'error', 'employee_id': False, 'result': _(
                    "La clave %s no es el número de registro de nadie del personal de campo.", log.employee_key)})
                continue
            try:
                with self.env.cr.savepoint():
                    state, result, attendance = service._punch_clock(employee, log.clock_id, log.punch_time)
            except ValidationError as e:
                state, result, attendance = 'error', e.args[0], self.env['hr.attendance']
            log.write({'employee_id': employee.id, 'state': state, 'result': result,
                       'attendance_id': attendance.id})

    def action_reprocess(self):
        """For punches that failed, e.g. after giving the employee their registration number."""
        self.filtered(lambda l: l.state in ('error', 'new'))._clock_process()


class HrFieldServiceClock(models.AbstractModel):
    _inherit = 'hr.field.service'

    @api.model
    def _punch_clock(self, employee, clock, when):
        """Entrance or exit of ``employee`` at ``when`` (UTC) from the time clock, as the office tablet does:
        the first punch opens the day without project, the next one closes it.
        Return (log state, detail, attendance)."""
        Attendance = self.env['hr.attendance']
        repeat = timedelta(minutes=REPEAT_MINUTES)
        last = Attendance.search([('employee_id', '=', employee.id), ('check_in', '<=', when)],
                                 order='check_in desc', limit=1)
        if last and not last.check_out:
            if when - last.check_in < repeat:
                return 'ignored', _("Repetido: ya tenía entrada a las %s.", self._local_time(employee, last.check_in)), last
            tz = pytz.timezone(employee._get_tz() or 'UTC')
            day = pytz.utc.localize(when).astimezone(tz).date()
            if last.field_date and last.field_date < day:
                # Forgot to punch out yesterday: closed without hours for HR to fix, and today starts anew.
                last.write({'check_out': last.check_in, 'field_state': 'review'})
                last._field_add_review_reason(_("Sin salida registrada: captura la hora de salida"))
            else:
                late = last._field_needs_overtime_question(when)
                last.write({
                    'check_out': when,
                    'field_out_kind': 'day',
                    'field_overtime_state': 'pending' if late else False,
                    'out_field_clock_id': clock.id,
                })
                return 'exit', _("Salida"), last
        elif last and last.check_out > when:
            return 'ignored', _("Ya hay una asistencia que cubre esa hora."), last
        elif last and when - last.check_out < repeat:
            return 'ignored', _("Repetido: ya tenía salida a las %s.", self._local_time(employee, last.check_out)), last
        attendance = Attendance.create({
            'employee_id': employee.id,
            'check_in': when,
            'field_supervisor_id': employee._field_leader().id,
            'field_in_kind': 'clock',
            'field_state': 'valid',
            'in_field_clock_id': clock.id,
            'in_mode': 'technical',
        })
        return 'entry', _("Entrada"), attendance
