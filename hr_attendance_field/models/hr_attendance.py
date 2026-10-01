import pytz

from odoo import _, api, fields, models

FIELD_STATES = [
    ('valid', "Válida"),
    ('review', "En revisión"),
    ('approved', "Aprobada"),
    ('rejected', "Rechazada"),
]
SYNC_FIELDS = {'check_in', 'check_out', 'field_project_id', 'field_state', 'employee_id', 'field_allocation_ids'}


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    field_project_id = fields.Many2one(
        'project.project', string="Obra", index='btree_not_null', tracking=True)
    field_supervisor_id = fields.Many2one('hr.employee', string="Supervisor", readonly=True)
    field_state = fields.Selection(FIELD_STATES, string="Estado en campo", tracking=True, index=True)
    field_review_reason = fields.Text("Motivo de revisión", readonly=True)
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
            return {self.field_project_id: self.worked_hours}
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
