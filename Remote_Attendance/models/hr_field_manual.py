from datetime import datetime

import pytz

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError

from .hr_attendance import LUNCH_MIN_HOURS, float_to_time


class HrFieldManualLoad(models.TransientModel):
    """Operations types a day of attendance by hand: who came, from what time to what time."""
    _name = 'hr.field.manual.load'
    _description = "Cargar asistencias a mano"

    date = fields.Date("Día", required=True, default=fields.Date.context_today)
    project_id = fields.Many2one('project.project', "Obra", required=True)
    supervisor_id = fields.Many2one(
        'hr.employee', "Supervisor", domain=[('field_role', '=', 'supervisor')],
        help="Al elegirlo se agrega su cuadrilla a la lista.")
    check_in = fields.Float("Entrada", default=lambda self: self.env['hr.attendance']._field_schedule()['day_start'])
    check_out = fields.Float("Salida", default=lambda self: self.env['hr.attendance']._field_schedule()['day_end'])
    line_ids = fields.One2many('hr.field.manual.line', 'wizard_id', string="Personas")
    present_count = fields.Integer("Asistieron", compute='_compute_totals')
    total_hours = fields.Float("Horas en total", compute='_compute_totals')

    @api.depends('line_ids.present', 'line_ids.hours', 'line_ids.overtime_hours')
    def _compute_totals(self):
        for wizard in self:
            present = wizard.line_ids.filtered('present')
            wizard.present_count = len(present)
            wizard.total_hours = sum(present.mapped('hours')) + sum(present.mapped('overtime_hours'))

    def _new_lines(self, employees):
        listed = self.line_ids.employee_id
        return [Command.create({
            'employee_id': employee.id,
            'check_in': self.check_in,
            'check_out': self.check_out,
        }) for employee in employees if employee not in listed]

    @api.onchange('supervisor_id')
    def _onchange_supervisor_id(self):
        if self.supervisor_id:
            crew = self.supervisor_id | self.supervisor_id.field_crew_ids.filtered('active')
            self.line_ids = self._new_lines(crew)

    @api.onchange('check_in', 'check_out')
    def _onchange_times(self):
        """Changing the default times of the day changes them for everybody on the list."""
        for line in self.line_ids:
            line.check_in = self.check_in
            line.check_out = self.check_out

    def action_add_field_staff(self):
        self.ensure_one()
        self.write({'line_ids': self._new_lines(self.env['hr.employee']._field_staff())})
        return self._reopen()

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'name': _("Cargar asistencias a mano"),
        }

    def _to_utc(self, employee, hour):
        tz = pytz.timezone(employee._get_tz() or 'UTC')
        local = tz.localize(datetime.combine(self.date, float_to_time(hour)))
        return local.astimezone(pytz.utc).replace(tzinfo=None)

    def action_confirm(self):
        self.ensure_one()
        lines = self.line_ids.filtered('present')
        if not lines:
            raise UserError(_("Marca al menos a una persona que asistió."))
        if self.date > fields.Date.context_today(self):
            raise UserError(_("No se pueden cargar asistencias de un día que aún no llega."))
        missing = lines.filtered(lambda l: not l.employee_id)
        if missing:
            raise UserError(_("Elige al empleado en todas las filas marcadas."))
        repeated = {e.name for e in lines.employee_id if len(lines.filtered(lambda l: l.employee_id == e)) > 1}
        if repeated:
            raise UserError(_("Hay personas repetidas en la lista: %s.", ", ".join(sorted(repeated))))
        wrong = lines.filtered(lambda l: l.check_out <= l.check_in or l.check_out > 24)
        if wrong:
            raise UserError(_("La salida debe ser después de la entrada: %s.",
                              ", ".join(wrong.employee_id.mapped('name'))))

        # Operations may not be the attendance manager of every employee; access is given by this wizard.
        Attendance = self.env['hr.attendance'].sudo()
        done, skipped = [], []
        for line in lines:
            employee = line.employee_id
            if Attendance.search_count([('employee_id', '=', employee.id), ('field_date', '=', self.date)]):
                skipped.append(employee.name)
                continue
            Attendance.create({
                'employee_id': employee.id,
                'check_in': self._to_utc(employee, line.check_in),
                'check_out': self._to_utc(employee, line.check_out),
                'in_mode': 'manual',
                'out_mode': 'manual',
                'field_project_id': (line.project_id or self.project_id).id,
                'field_supervisor_id': self.supervisor_id.id,
                'field_in_kind': 'site',
                'field_out_kind': 'day',
                'field_skip_lunch': not line.lunch,
                'field_state': 'valid',
                # Operations is who validates overtime, so what they type is already approved.
                'field_overtime_state': 'approved' if line.overtime_hours else False,
                'field_loaded_by_id': self.env.user.id,
            })
            done.append(employee.name)

        message = _("Se cargaron %s asistencias.", len(done))
        if skipped:
            message += " " + _("Sin cargar porque ya tienen asistencia ese día: %s.", ", ".join(skipped))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Asistencias cargadas"),
                'message': message,
                'type': 'warning' if skipped else 'success',
                'sticky': bool(skipped),
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }


class HrFieldManualLine(models.TransientModel):
    _name = 'hr.field.manual.line'
    _description = "Persona en la carga manual de asistencias"
    _order = 'id'

    wizard_id = fields.Many2one('hr.field.manual.load', required=True, ondelete='cascade')
    present = fields.Boolean("Asistió", default=True)
    employee_id = fields.Many2one('hr.employee', "Empleado")
    project_id = fields.Many2one('project.project', "Otra obra",
                                 help="Solo si esta persona trabajó en una obra distinta a la de arriba.")
    check_in = fields.Float("Entrada", default=lambda self: self.env['hr.attendance']._field_schedule()['day_start'])
    check_out = fields.Float("Salida", default=lambda self: self.env['hr.attendance']._field_schedule()['day_end'])
    lunch = fields.Boolean("Salió a comer", default=True,
                           help="Se descuenta la hora de comida cuando trabajó 6 horas o más.")
    hours = fields.Float("Horas trabajadas", compute='_compute_hours', store=True, readonly=False,
                         help="Horas dentro del horario, sin la comida. Si las escribes, se ajusta la salida.")
    overtime_hours = fields.Float("Horas extra", compute='_compute_overtime_hours', store=True)

    @api.depends('check_in', 'check_out', 'lunch')
    def _compute_hours(self):
        # Same rule as the attendance: only the working day counts and the lunch is deducted.
        schedule = self.env['hr.attendance']._field_schedule()
        for line in self:
            begin = max(line.check_in, schedule['day_start'])
            finish = min(line.check_out, schedule['day_end'])
            regular = max(0.0, finish - begin)
            if line.lunch and regular >= LUNCH_MIN_HOURS:
                regular -= schedule['lunch_hours']
            line.hours = regular

    @api.depends('check_in', 'check_out')
    def _compute_overtime_hours(self):
        day_end = self.env['hr.attendance']._field_schedule()['day_end']
        for line in self:
            line.overtime_hours = max(0.0, line.check_out - max(line.check_in, day_end))

    @api.onchange('hours')
    def _onchange_hours(self):
        """Typing the hours worked moves the exit, adding the lunch when it applies."""
        schedule = self.env['hr.attendance']._field_schedule()
        start = max(self.check_in, schedule['day_start'])
        lunch = schedule['lunch_hours'] if self.lunch and self.hours + schedule['lunch_hours'] >= LUNCH_MIN_HOURS else 0.0
        check_out = min(start + self.hours + lunch, 24.0)
        if abs(check_out - self.check_out) > 1 / 120:
            self.check_out = check_out
