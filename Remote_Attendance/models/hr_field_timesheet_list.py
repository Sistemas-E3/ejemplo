import base64
import io

import xlsxwriter

from datetime import timedelta

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError

# Same headers as the timesheet export of Odoo, so the file goes back in with
# Proyectos › Hojas de horas › Importar without mapping columns by hand.
IMPORT_HEADERS = ['Fecha', 'Empleado', 'Proyecto', 'Cantidad']
# Lists older than this are deleted every night (Remote_Attendance.list_keep_days, 0 = keep them).
DEFAULT_KEEP_DAYS = 365
DETAIL_HEADERS = ['Fecha', 'No. Empleado', 'Empleado', 'Supervisor', 'Proyecto', 'Entrada', 'Salida',
                  'Horas', 'Horas extra', 'Tiempo extra', 'Cargada a mano por']


class HrFieldTimesheetList(models.Model):
    """A saved list of the hours of the field staff for a period, ready to export to Excel."""
    _name = 'hr.field.timesheet.list'
    _description = "Lista de horas de campo"
    _inherit = ['mail.thread']
    _order = 'create_date desc, id desc'

    name = fields.Char("Nombre", compute='_compute_name', store=True, readonly=False)
    date_from = fields.Date("Desde", required=True, default=fields.Date.context_today, tracking=True)
    date_to = fields.Date("Hasta", required=True, default=fields.Date.context_today, tracking=True)
    supervisor_id = fields.Many2one('hr.employee', "Supervisor", domain=[('field_role', '=', 'supervisor')],
                                    tracking=True, help="Vacío = todos los supervisores.")
    project_id = fields.Many2one('project.project', "Obra", tracking=True, help="Vacío = todas las obras.")
    state = fields.Selection([
        ('draft', "Borrador"),
        ('exported', "Exportada"),
    ], string="Estado", default='draft', tracking=True)
    line_ids = fields.One2many('hr.field.timesheet.list.line', 'list_id', string="Renglones")
    line_count = fields.Integer("Renglones", compute='_compute_totals')
    total_hours = fields.Float("Horas en total", compute='_compute_totals')
    excel_file = fields.Binary("Excel", attachment=True, readonly=True, copy=False)
    excel_name = fields.Char(readonly=True, copy=False)
    exported_by_id = fields.Many2one('res.users', "Exportada por", readonly=True, copy=False)
    exported_on = fields.Datetime("Exportada el", readonly=True, copy=False)

    @api.depends('date_from', 'date_to', 'supervisor_id', 'project_id')
    def _compute_name(self):
        for record in self:
            if not (record.date_from and record.date_to):
                record.name = _("Lista de horas")
                continue
            period = record.date_from.strftime('%d/%m/%Y')
            if record.date_to != record.date_from:
                period += " al " + record.date_to.strftime('%d/%m/%Y')
            parts = [period] + [p for p in (record.supervisor_id.name, record.project_id.display_name) if p]
            record.name = " · ".join(parts)

    @api.depends('line_ids.hours')
    def _compute_totals(self):
        for record in self:
            record.line_count = len(record.line_ids)
            record.total_hours = sum(record.line_ids.mapped('hours'))

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        if any(r.date_to < r.date_from for r in self):
            raise UserError(_("La fecha final debe ser igual o posterior a la inicial."))

    def _attendances(self):
        self.ensure_one()
        domain = [
            ('field_state', 'in', ('valid', 'approved')),
            ('check_out', '!=', False),
            ('field_date', '>=', self.date_from),
            ('field_date', '<=', self.date_to),
        ]
        if self.supervisor_id:
            domain.append(('field_supervisor_id', '=', self.supervisor_id.id))
        return self.env['hr.attendance'].sudo().search(domain, order='field_date, field_supervisor_id, employee_id')

    def action_generate(self):
        """Fill the list from the accepted field attendances of the period (one row per person, day and project)."""
        for record in self:
            rows = {}
            for attendance in record._attendances():
                for project, hours in attendance._field_wanted_hours().items():
                    if record.project_id and project != record.project_id:
                        continue
                    key = (attendance.field_date, attendance.employee_id.id, project.id)
                    row = rows.setdefault(key, {
                        'date': attendance.field_date,
                        'employee_id': attendance.employee_id.id,
                        'project_id': project.id,
                        'supervisor_id': attendance.field_supervisor_id.id,
                        'hours': 0.0,
                        'overtime_hours': 0.0,
                        'attendance_ids': [],
                    })
                    row['hours'] += hours
                    if attendance.field_overtime_state == 'approved':
                        row['overtime_hours'] += attendance.field_overtime_hours
                    row['attendance_ids'].append(attendance.id)
            record.line_ids = [Command.clear()] + [
                Command.create(dict(row, attendance_ids=[Command.set(row['attendance_ids'])]))
                for row in rows.values() if row['hours'] > 0
            ]
            record.state = 'draft'
        return True

    def action_export_excel(self):
        self.ensure_one()
        if not self.line_ids:
            self.action_generate()
        if not self.line_ids:
            raise UserError(_("No hay horas aceptadas en ese periodo."))
        self.write({
            'excel_file': base64.b64encode(self._excel_bytes()),
            'excel_name': "Hojas de horas %s.xlsx" % self.name.replace('/', '-'),
            'state': 'exported',
            'exported_by_id': self.env.user.id,
            'exported_on': fields.Datetime.now(),
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/excel_file/{self.excel_name}?download=true',
            'target': 'self',
        }

    @api.model
    def _cron_delete_old(self):
        value = self.env['ir.config_parameter'].sudo().get_param('Remote_Attendance.list_keep_days')
        try:
            days = int(value) if value not in (None, False, '') else DEFAULT_KEEP_DAYS
        except ValueError:
            days = DEFAULT_KEEP_DAYS
        if days <= 0:
            return
        limit = fields.Datetime.now() - timedelta(days=days)
        self.sudo().search([('create_date', '<', limit)]).unlink()

    def action_back_to_draft(self):
        self.write({'state': 'draft'})

    def _excel_bytes(self):
        self.ensure_one()
        output = io.BytesIO()
        book = xlsxwriter.Workbook(output, {'in_memory': True})
        bold = book.add_format({'bold': True})
        date = book.add_format({'num_format': 'yyyy-mm-dd'})
        number = book.add_format({'num_format': '#,##0.00'})
        clock = book.add_format({'num_format': 'hh:mm'})

        sheet = book.add_worksheet("Hojas de horas")
        sheet.write_row(0, 0, IMPORT_HEADERS, bold)
        for row, line in enumerate(self.line_ids, start=1):
            sheet.write_datetime(row, 0, fields.Datetime.to_datetime(line.date), date)
            sheet.write_string(row, 1, line.employee_id.name or '')
            sheet.write_string(row, 2, line.project_id.display_name or '')
            sheet.write_number(row, 3, round(line.hours, 2), number)
        sheet.set_column(0, 0, 12)
        sheet.set_column(1, 2, 40)
        sheet.set_column(3, 3, 10)

        detail = book.add_worksheet("Detalle")
        detail.write_row(0, 0, DETAIL_HEADERS, bold)
        row = 1
        for line in self.line_ids:
            for attendance in line.attendance_ids.sorted('check_in'):
                tz_in = self.env['hr.field.service']._local_time(attendance.employee_id, attendance.check_in)
                tz_out = self.env['hr.field.service']._local_time(attendance.employee_id, attendance.check_out)
                detail.write_datetime(row, 0, fields.Datetime.to_datetime(line.date), date)
                detail.write_string(row, 1, attendance.employee_id.field_key or '')
                detail.write_string(row, 2, attendance.employee_id.name or '')
                detail.write_string(row, 3, attendance.field_supervisor_id.name or '')
                detail.write_string(row, 4, line.project_id.display_name or '')
                detail.write_string(row, 5, tz_in or '')
                detail.write_string(row, 6, tz_out or '')
                detail.write_number(row, 7, round(attendance.field_regular_hours, 2), number)
                detail.write_number(row, 8, round(attendance.field_overtime_hours, 2), number)
                detail.write_string(row, 9, dict(attendance._fields['field_overtime_state'].selection).get(
                    attendance.field_overtime_state, ''))
                detail.write_string(row, 10, attendance.field_loaded_by_id.name or '')
                row += 1
        detail.set_column(0, 1, 12)
        detail.set_column(2, 4, 32)
        detail.set_column(5, 10, 12)
        book.close()
        return output.getvalue()


class HrFieldTimesheetListLine(models.Model):
    _name = 'hr.field.timesheet.list.line'
    _description = "Renglón de la lista de horas de campo"
    _order = 'date, supervisor_id, employee_id, id'

    list_id = fields.Many2one('hr.field.timesheet.list', required=True, ondelete='cascade', index=True)
    date = fields.Date("Fecha", required=True)
    employee_id = fields.Many2one('hr.employee', "Empleado", required=True)
    employee_key = fields.Char("No. Empleado", related='employee_id.field_key')
    supervisor_id = fields.Many2one('hr.employee', "Supervisor")
    project_id = fields.Many2one('project.project', "Proyecto", required=True)
    hours = fields.Float("Horas")
    overtime_hours = fields.Float("De ellas, extra")
    attendance_ids = fields.Many2many('hr.attendance', string="Asistencias")
