from odoo import _, api, fields, models

FIELD_STATES = [
    ('valid', "Válida"),
    ('review', "En revisión"),
    ('approved', "Aprobada"),
    ('rejected', "Rechazada"),
]
SYNC_FIELDS = {'check_in', 'check_out', 'field_project_id', 'field_state', 'employee_id'}


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    field_project_id = fields.Many2one(
        'project.project', string="Obra", index='btree_not_null', tracking=True)
    field_supervisor_id = fields.Many2one('hr.employee', string="Supervisor", readonly=True)
    field_state = fields.Selection(FIELD_STATES, string="Estado en campo", tracking=True, index=True)
    field_review_reason = fields.Text("Motivo de revisión", readonly=True)
    in_field_device_id = fields.Many2one('hr.field.device', string="Teléfono de entrada", readonly=True)
    out_field_device_id = fields.Many2one('hr.field.device', string="Teléfono de salida", readonly=True)
    in_field_photo = fields.Image("Foto de entrada", max_width=640, max_height=640, attachment=True, readonly=True)
    out_field_photo = fields.Image("Foto de salida", max_width=640, max_height=640, attachment=True, readonly=True)
    in_face_distance = fields.Float("Distancia facial entrada", digits=(4, 3), readonly=True,
                                    help="Menor es más parecido; vacío si marcó con PIN.")
    out_face_distance = fields.Float("Distancia facial salida", digits=(4, 3), readonly=True)
    in_gps_distance = fields.Integer("Distancia a la obra entrada (m)", readonly=True)
    out_gps_distance = fields.Integer("Distancia a la obra salida (m)", readonly=True)
    field_timesheet_id = fields.Many2one(
        'account.analytic.line', string="Línea de horas", readonly=True, copy=False)

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
        attendances.filtered('field_project_id')._field_sync_timesheet()
        return attendances

    def write(self, vals):
        res = super().write(vals)
        if SYNC_FIELDS & set(vals):
            self.filtered(lambda a: a.field_project_id or a.field_timesheet_id)._field_sync_timesheet()
        return res

    def unlink(self):
        timesheets = self.sudo().field_timesheet_id
        res = super().unlink()
        timesheets.exists().unlink()
        return res

    def _field_timesheet_vals(self):
        self.ensure_one()
        return {
            'name': _("Asistencia en obra"),
            'project_id': self.field_project_id.id,
            'employee_id': self.employee_id.id,
            'date': self.date,
            'unit_amount': self.worked_hours,
            'field_attendance_id': self.id,
        }

    def _field_sync_timesheet(self):
        """Keep one timesheet line per closed, accepted field attendance."""
        Line = self.env['account.analytic.line'].sudo()
        for attendance in self.sudo():
            line = attendance.field_timesheet_id
            wanted = (
                attendance.check_out
                and attendance.field_project_id
                and attendance.field_state in ('valid', 'approved')
            )
            if not wanted:
                if line:
                    attendance.field_timesheet_id = False
                    line.unlink()
                continue
            vals = attendance._field_timesheet_vals()
            if line:
                line.write(vals)
            else:
                attendance.field_timesheet_id = Line.create(vals)
