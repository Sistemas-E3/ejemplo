from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models

FIELD_STATES = [
    ('valid', "Válida"),
    ('review', "En revisión"),
    ('approved', "Aprobada"),
    ('rejected', "Rechazada"),
]
SYNC_FIELDS = {'check_in', 'check_out', 'field_project_id', 'field_state', 'employee_id', 'field_allocation_ids',
               'field_in_kind', 'field_out_kind', 'field_overtime_state', 'field_skip_lunch'}
# Working day of the field staff (hours, local time), editable in Ajustes › Técnico › Parámetros del sistema.
SCHEDULE_DEFAULTS = {
    'day_start': 7.0,       # Remote_Attendance.day_start: hours before this are not counted
    'day_end': 17.0,        # Remote_Attendance.day_end: hours after this count only as approved overtime
    'lunch_hours': 1.0,     # Remote_Attendance.lunch_hours: deducted when the lunch was not marked
    'overtime_after': 1.0,  # Remote_Attendance.overtime_after: exits this long after day_end ask for overtime
}
# Without lunch marks, the lunch is deducted only from attendances at least this long.
LUNCH_MIN_HOURS = 6.0


def float_to_time(value):
    hours = int(value)
    return time(hours, int(round((value - hours) * 60)) % 60)


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    field_project_id = fields.Many2one(
        'project.project', string="Obra", index='btree_not_null', tracking=True)
    field_supervisor_id = fields.Many2one('hr.employee', string="Supervisor", readonly=True)
    field_state = fields.Selection(FIELD_STATES, string="Estado en campo", tracking=True, index=True)
    field_review_reason = fields.Text("Motivo de revisión", readonly=True)
    field_in_kind = fields.Selection([
        ('office', "Entrada en oficina"),
        ('site', "Entrada en obra"),
        ('lunch', "Regreso de comer"),
    ], string="Tipo de entrada", readonly=True)
    field_out_kind = fields.Selection([
        ('lunch', "Salida a comer"),
        ('day', "Salida"),
    ], string="Tipo de salida", readonly=True)
    field_date = fields.Date("Día", compute='_compute_field_date', store=True, index=True,
                             help="Día de la entrada en la zona horaria del empleado.")
    in_field_device_id = fields.Many2one('hr.field.device', string="Teléfono de entrada", readonly=True)
    out_field_device_id = fields.Many2one('hr.field.device', string="Teléfono de salida", readonly=True)
    in_field_photo = fields.Image("Foto de entrada", max_width=640, max_height=640, attachment=True, readonly=True)
    out_field_photo = fields.Image("Foto de salida", max_width=640, max_height=640, attachment=True, readonly=True)
    in_face_distance = fields.Float("Distancia facial entrada", digits=(4, 3), readonly=True,
                                    help="Menor es más parecido; vacío si marcó con PIN.")
    out_face_distance = fields.Float("Distancia facial salida", digits=(4, 3), readonly=True)
    in_gps_distance = fields.Integer("Distancia a la obra entrada (m)", readonly=True)
    out_gps_distance = fields.Integer("Distancia a la obra salida (m)", readonly=True)
    field_overtime_state = fields.Selection([
        ('pending', "Por validar"),
        ('approved', "Aprobado"),
        ('rejected', "Rechazado"),
    ], string="Tiempo extra", tracking=True, index=True, copy=False,
        help="Al salir más tarde del horario, el supervisor indicó que es tiempo extra. "
             "Esas horas se cargan al proyecto solo cuando se aprueban.")
    field_skip_lunch = fields.Boolean(
        "Sin hora de comida", help="Cargada a mano indicando que no salió a comer: no se descuenta la comida.")
    field_loaded_by_id = fields.Many2one('res.users', string="Cargada a mano por", readonly=True)
    field_regular_hours = fields.Float("Horas en horario", compute='_compute_field_hours')
    field_overtime_hours = fields.Float("Horas extra", compute='_compute_field_hours')
    field_allocation_ids = fields.One2many(
        'hr.field.allocation', 'attendance_id', string="Reparto por proyecto",
        help="Cuando la asistencia viene de una lista de WhatsApp, cómo se reparte el día entre proyectos.")
    field_timesheet_ids = fields.One2many(
        'account.analytic.line', 'field_attendance_id', string="Líneas de horas", readonly=True)

    @api.depends('check_in', 'employee_id')
    def _compute_field_date(self):
        for attendance in self:
            if not attendance.check_in:
                attendance.field_date = False
                continue
            tz = pytz.timezone(attendance.employee_id._get_tz() or 'UTC')
            attendance.field_date = pytz.utc.localize(attendance.check_in).astimezone(tz).date()

    @api.model
    def _field_schedule(self):
        params = self.env['ir.config_parameter'].sudo()
        schedule = {}
        for key, default in SCHEDULE_DEFAULTS.items():
            try:
                schedule[key] = float(params.get_param(f'Remote_Attendance.{key}') or default)
            except ValueError:
                schedule[key] = default
        return schedule

    def _field_day_bounds(self, schedule=None):
        """Start and end of the working day of the check-in, as naive UTC datetimes."""
        self.ensure_one()
        schedule = schedule or self._field_schedule()
        tz = pytz.timezone(self.employee_id._get_tz() or 'UTC')
        day = pytz.utc.localize(self.check_in).astimezone(tz).date()

        def to_utc(hour):
            return tz.localize(datetime.combine(day, float_to_time(hour))).astimezone(pytz.utc).replace(tzinfo=None)
        return to_utc(schedule['day_start']), to_utc(schedule['day_end'])

    def _field_needs_overtime_question(self, when):
        """True when leaving at ``when`` is late enough to ask whether it is overtime."""
        self.ensure_one()
        schedule = self._field_schedule()
        _start, end = self._field_day_bounds(schedule)
        return when >= end + timedelta(hours=schedule['overtime_after'])

    @api.depends('check_in', 'check_out', 'field_in_kind', 'field_out_kind', 'field_skip_lunch', 'employee_id')
    def _compute_field_hours(self):
        schedule = self._field_schedule()
        for attendance in self:
            if not (attendance.check_in and attendance.check_out):
                attendance.field_regular_hours = attendance.field_overtime_hours = 0.0
                continue
            start, end = attendance._field_day_bounds(schedule)
            begin = max(attendance.check_in, start)
            finish = min(attendance.check_out, end)
            regular = max(0.0, (finish - begin).total_seconds() / 3600)
            lunch_marked = attendance.field_in_kind == 'lunch' or attendance.field_out_kind == 'lunch'
            if not (lunch_marked or attendance.field_skip_lunch) and regular >= LUNCH_MIN_HOURS:
                regular -= schedule['lunch_hours']
            overtime = (attendance.check_out - max(attendance.check_in, end)).total_seconds() / 3600
            attendance.field_regular_hours = regular
            attendance.field_overtime_hours = max(0.0, overtime)

    def action_field_overtime_approve(self):
        self.filtered('field_overtime_state').write({'field_overtime_state': 'approved'})

    def action_field_overtime_reject(self):
        self.filtered('field_overtime_state').write({'field_overtime_state': 'rejected'})

    def _field_add_review_reason(self, reason):
        for attendance in self:
            reasons = [r for r in (attendance.field_review_reason or '').split('\n') if r]
            if reason not in reasons:
                reasons.append(reason)
            attendance.field_review_reason = '\n'.join(reasons)

    def action_field_approve(self):
        self.filtered(lambda a: a.field_state in ('review', 'rejected')).write({'field_state': 'approved'})

    def action_field_reject(self):
        self.filtered(lambda a: a.field_state in ('review', 'valid', 'approved')).write({'field_state': 'rejected'})

    # ------------------------------------------------------------------
    # Timesheet synchronisation
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        attendances = super().create(vals_list)
        attendances.filtered(lambda a: a.field_project_id or a.field_allocation_ids)._field_sync_timesheet()
        return attendances

    def write(self, vals):
        res = super().write(vals)
        if SYNC_FIELDS & set(vals):
            self.filtered(
                lambda a: a.field_project_id or a.field_allocation_ids or a.field_timesheet_ids
            )._field_sync_timesheet()
        return res

    def unlink(self):
        timesheets = self.sudo().field_timesheet_ids
        res = super().unlink()
        timesheets.exists().unlink()
        return res

    def _field_wanted_hours(self):
        """Return {project: hours} this attendance should book."""
        self.ensure_one()
        if not (self.check_out and self.field_state in ('valid', 'approved')):
            return {}
        if self.field_allocation_ids:
            wanted = {}
            for allocation in self.field_allocation_ids:
                wanted[allocation.project_id] = wanted.get(allocation.project_id, 0.0) + allocation.hours
            return wanted
        if self.field_project_id:
            # Only the working day (7 to 5 by default) counts, without lunch; later hours only as approved overtime.
            hours = self.field_regular_hours
            if self.field_overtime_state == 'approved':
                hours += self.field_overtime_hours
            return {self.field_project_id: hours}
        return {}

    def _field_sync_timesheet(self):
        """Keep one timesheet line per project for each closed, accepted field attendance."""
        Line = self.env['account.analytic.line'].sudo()
        for attendance in self.sudo():
            wanted = attendance._field_wanted_hours()
            lines = attendance.field_timesheet_ids
            stale = lines.filtered(lambda l: l.project_id not in wanted)
            stale.unlink()
            lines -= stale
            for project, hours in wanted.items():
                vals = {
                    'name': _("Asistencia en obra"),
                    'project_id': project.id,
                    'employee_id': attendance.employee_id.id,
                    'date': attendance.field_date,
                    'unit_amount': hours,
                    'field_attendance_id': attendance.id,
                }
                line = lines.filtered(lambda l: l.project_id == project)[:1]
                if line:
                    line.write(vals)
                else:
                    Line.create(vals)


class HrFieldAllocation(models.Model):
    _name = 'hr.field.allocation'
    _description = "Reparto de una asistencia por proyecto"
    _order = 'attendance_id, id'

    attendance_id = fields.Many2one('hr.attendance', required=True, ondelete='cascade', index=True)
    project_id = fields.Many2one('project.project', "Proyecto", required=True)
    name = fields.Char("Actividad")
    fraction = fields.Float("Jornada", digits=(4, 2), help="1 = día completo, 0.5 = medio día.")
    hours = fields.Float("Horas")
    supervisor_id = fields.Many2one('hr.employee', "Lo reportó")

    @api.model_create_multi
    def create(self, vals_list):
        allocations = super().create(vals_list)
        allocations.attendance_id._field_sync_timesheet()
        return allocations

    def write(self, vals):
        res = super().write(vals)
        self.attendance_id._field_sync_timesheet()
        return res

    def unlink(self):
        attendances = self.attendance_id
        res = super().unlink()
        attendances.exists()._field_sync_timesheet()
        return res
