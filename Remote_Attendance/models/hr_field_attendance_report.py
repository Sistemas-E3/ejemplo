import io
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta

import pytz
import xlsxwriter

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

MAX_DAYS = 366
# Minutes after the start of the day that still count as on time (Remote_Attendance.late_minutes).
DEFAULT_LATE_MINUTES = 10
WEEKDAYS = ['Lu', 'Ma', 'Mi', 'Ju', 'Vi', 'Sá', 'Do']
CODES = {
    'A': "Asistencia",
    'R': "Retardo",
    'F': "Falta",
    'V': "Vacaciones",
    'P': "Permiso",
    'I': "Incapacidad",
}


def plain(text):
    return unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode().lower()


class HrFieldAttendanceReport(models.AbstractModel):
    """Daily attendance grid (person × day) with absences, lateness and hours, for up to a year."""
    _name = 'hr.field.attendance.report'
    _description = "Reporte de asistencia"

    def _check_access(self):
        user = self.env.user
        if not (user.has_group('Remote_Attendance.group_field_operations')
                or user.has_group('hr_attendance.group_hr_attendance_officer')):
            raise AccessError(_("Solo Operaciones y RH pueden ver el reporte de asistencia."))

    @api.model
    def _dates(self, date_from, date_to):
        start = fields.Date.to_date(date_from) or fields.Date.context_today(self)
        end = fields.Date.to_date(date_to) or start
        if end < start:
            start, end = end, start
        if (end - start).days + 1 > MAX_DAYS:
            raise UserError(_("El reporte abarca como máximo un año (%s días).", MAX_DAYS))
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]

    @api.model
    def _employees(self, filters):
        companies = self.env.user.company_ids
        domain = [('field_role', '!=', 'office'), ('company_id', 'in', companies.ids)]
        if filters.get('company_id'):
            domain.append(('company_id', '=', int(filters['company_id'])))
        if filters.get('department_id'):
            domain.append(('department_id', 'child_of', int(filters['department_id'])))
        scope = filters.get('scope')
        if scope == 'field':
            domain.append(('field_role', 'in', ('supervisor', 'worker')))
        elif scope == 'office':
            domain.append(('field_role', 'in', (False, 'staff')))
        if filters.get('supervisor_id'):
            supervisor = int(filters['supervisor_id'])
            domain += ['|', ('field_supervisor_id', '=', supervisor), ('id', '=', supervisor)]
        search = (filters.get('search') or '').strip()
        if search:
            names = ['name', 'barcode', 'job_title', 'department_id.name']
            if 'registration_number' in self.env['hr.employee']._fields:
                names.append('registration_number')
            domain += ['|'] * (len(names) - 1) + [(name, 'ilike', search) for name in names]
        return self.env['hr.employee'].sudo().search(domain, order='name')

    @api.model
    def _workdays(self, employee):
        """Weekdays (0 = Monday) the employee works, from their working schedule; Monday to Saturday without one."""
        calendar = employee.resource_calendar_id
        days = {int(a.dayofweek) for a in calendar.attendance_ids if not a.display_type} if calendar else set()
        return days or {0, 1, 2, 3, 4, 5}

    @api.model
    def _start_hour(self, employee, weekday, schedule):
        calendar = employee.resource_calendar_id
        hours = [a.hour_from for a in calendar.attendance_ids
                 if not a.display_type and int(a.dayofweek) == weekday] if calendar else []
        return min(hours) if hours else schedule['day_start']

    @api.model
    def _leaves(self, employees, days):
        """{(employee id, day): code} from validated time off, when the Time Off app is installed."""
        result = {}
        if 'hr.leave' not in self.env or not employees:
            return result
        start = datetime.combine(days[0], datetime.min.time())
        end = datetime.combine(days[-1], datetime.max.time())
        leaves = self.env['hr.leave'].sudo().search([
            ('employee_id', 'in', employees.ids), ('state', '=', 'validate'),
            ('date_from', '<=', end), ('date_to', '>=', start),
        ])
        for leave in leaves:
            name = plain(leave.holiday_status_id.name)
            code = 'V' if 'vacac' in name else 'I' if 'incap' in name else 'P'
            tz = pytz.timezone(leave.employee_id._get_tz() or 'UTC')
            first = pytz.utc.localize(leave.date_from).astimezone(tz).date()
            last = pytz.utc.localize(leave.date_to).astimezone(tz).date()
            day = max(first, days[0])
            while day <= min(last, days[-1]):
                result[(leave.employee_id.id, day)] = code
                day += timedelta(days=1)
        return result

    @api.model
    def _compute(self, filters):
        self._check_access()
        days = self._dates(filters.get('date_from'), filters.get('date_to'))
        employees = self._employees(filters)
        Attendance = self.env['hr.attendance'].sudo()
        params = self.env['ir.config_parameter'].sudo()
        try:
            late_minutes = int(params.get_param('Remote_Attendance.late_minutes') or DEFAULT_LATE_MINUTES)
        except ValueError:
            late_minutes = DEFAULT_LATE_MINUTES
        schedule = Attendance._field_schedule()
        attendances = Attendance.search([
            ('employee_id', 'in', employees.ids),
            ('field_date', '>=', days[0]), ('field_date', '<=', days[-1]),
            ('field_state', '!=', 'rejected'),
        ], order='check_in')
        by_day = defaultdict(lambda: Attendance.browse())
        for attendance in attendances:
            by_day[(attendance.employee_id.id, attendance.field_date)] |= attendance
        leaves = self._leaves(employees, days)
        today = fields.Date.context_today(self)
        rows = []
        kpis = dict.fromkeys(('A', 'R', 'F', 'V', 'P', 'I', 'flags'), 0)
        for employee in employees:
            tz = pytz.timezone(employee._get_tz() or 'UTC')
            workdays = self._workdays(employee)
            crew = employee.field_role in ('supervisor', 'worker')
            # No absences before the person joined.
            hired_on = (employee.first_contract_date if 'first_contract_date' in employee._fields else False) \
                or employee.create_date.date()
            cells, totals = [], dict.fromkeys(('A', 'R', 'F', 'V', 'P', 'I'), 0)
            totals.update(hours=0.0, overtime=0.0)
            for day in days:
                marks = by_day.get((employee.id, day))
                cell = {'code': '', 'flag': False, 'hours': 0.0, 'in': '', 'out': ''}
                if marks:
                    first_in = pytz.utc.localize(marks[0].check_in).astimezone(tz)
                    start = self._start_hour(employee, day.weekday(), schedule)
                    limit = datetime.combine(day, datetime.min.time()) + timedelta(hours=start, minutes=late_minutes)
                    cell['code'] = 'R' if first_in.replace(tzinfo=None) > limit else 'A'
                    last = marks[-1]
                    cell['in'] = first_in.strftime('%H:%M')
                    if last.check_out:
                        cell['out'] = pytz.utc.localize(last.check_out).astimezone(tz).strftime('%H:%M')
                    regular = sum(marks.mapped('field_regular_hours'))
                    overtime = sum(a.field_overtime_hours for a in marks
                                   if a.field_overtime_state == 'approved' or not crew)
                    cell['hours'] = round(regular + overtime, 2)
                    # Orange dot: something to check (in review, no exit, field day without project).
                    cell['flag'] = any(
                        a.field_state == 'review'
                        or (not a.check_out and day < today)
                        or (a.check_out and crew and not (a.field_project_id or a.field_allocation_ids))
                        for a in marks)
                    totals['A'] += 1
                    totals['R'] += cell['code'] == 'R'
                    totals['hours'] += regular
                    totals['overtime'] += overtime
                elif (employee.id, day) in leaves:
                    cell['code'] = leaves[(employee.id, day)]
                    totals[cell['code']] += 1
                elif day.weekday() in workdays and day < today and day >= hired_on:
                    cell['code'] = 'F'
                    totals['F'] += 1
                cells.append(cell)
            for key in ('A', 'R', 'F', 'V', 'P', 'I'):
                kpis[key] += totals[key]
            kpis['flags'] += sum(1 for c in cells if c['flag'])
            totals['hours'] = round(totals['hours'], 2)
            totals['overtime'] = round(totals['overtime'], 2)
            rows.append({
                'id': employee.id,
                'name': employee.name,
                'key': employee.field_key or '',
                'job': employee.job_title or employee.job_id.name or '',
                'department': employee.department_id.name or '',
                'company': employee.company_id.name or '',
                'cells': cells,
                'totals': totals,
            })
        return days, rows, kpis

    @api.model
    def _day_header(self, days):
        return [{'date': fields.Date.to_string(d), 'label': d.strftime('%d/%m'), 'weekday': WEEKDAYS[d.weekday()],
                 'weekend': d.weekday() >= 5} for d in days]

    @api.model
    def get_report(self, filters=None, offset=0, limit=50):
        filters = filters or {}
        days, rows, kpis = self._compute(filters)
        grand = {key: sum(r['totals'][key] for r in rows) for key in ('A', 'R', 'F', 'V', 'P', 'I')}
        grand['hours'] = round(sum(r['totals']['hours'] for r in rows), 2)
        grand['overtime'] = round(sum(r['totals']['overtime'] for r in rows), 2)
        day_totals = [{
            'present': sum(1 for r in rows if r['cells'][i]['code'] in ('A', 'R')),
            'absent': sum(1 for r in rows if r['cells'][i]['code'] == 'F'),
        } for i in range(len(days))]
        return {
            'days': self._day_header(days),
            'rows': rows[offset:offset + limit],
            'count': len(rows),
            'kpis': dict(kpis, employees=len(rows)),
            'totals': grand,
            'day_totals': day_totals,
            'codes': CODES,
            'companies': [{'id': c.id, 'name': c.name} for c in self.env.user.company_ids],
            'departments': [{'id': d.id, 'name': d.complete_name} for d in self.env['hr.department'].sudo().search(
                [('company_id', 'in', self.env.user.company_ids.ids + [False])], order='complete_name')],
            'supervisors': [{'id': e.id, 'name': e.name} for e in self.env['hr.employee'].sudo().search(
                [('field_role', '=', 'supervisor'), ('company_id', 'in', self.env.user.company_ids.ids)], order='name')],
        }

    @api.model
    def _hours_text(self, value):
        minutes = int(round((value or 0) * 60))
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    @api.model
    def excel_file(self, filters):
        days, rows, kpis = self._compute(filters)
        output = io.BytesIO()
        book = xlsxwriter.Workbook(output, {'in_memory': True})
        bold = book.add_format({'bold': True})
        head = book.add_format({'bold': True, 'bg_color': '#EEF2F6', 'border': 1, 'align': 'center', 'valign': 'vcenter',
                                'text_wrap': True})
        center = book.add_format({'align': 'center', 'border': 1})
        colors = {'A': '#E5F4E4', 'R': '#F9EEDA', 'F': '#FBEBEB', 'V': '#E7F1FB', 'P': '#EFEDFF', 'I': '#F9ECE7'}
        code_formats = {code: book.add_format({'align': 'center', 'border': 1, 'bg_color': color})
                        for code, color in colors.items()}
        sheet = book.add_worksheet("Asistencia")
        sheet.write(0, 0, _("Reporte de asistencia del %s al %s", days[0].strftime('%d/%m/%Y'),
                            days[-1].strftime('%d/%m/%Y')), bold)
        sheet.write(1, 0, " · ".join(f"{code} = {name}" for code, name in CODES.items()))
        header = ["No. Empleado", "Nombre", "Puesto", "Departamento", "Empresa"]
        header += [f"{WEEKDAYS[d.weekday()]}\n{d.strftime('%d/%m')}" for d in days]
        header += ["Asistencias", "Retardos", "Faltas", "Vacaciones", "Permisos", "Incapacidades", "Horas", "Horas extra"]
        sheet.write_row(3, 0, header, head)
        sheet.set_row(3, 30)
        for r, row in enumerate(rows, start=4):
            sheet.write_row(r, 0, [row['key'], row['name'], row['job'], row['department'], row['company']])
            for c, cell in enumerate(row['cells'], start=5):
                sheet.write(r, c, cell['code'], code_formats.get(cell['code'], center))
            t = row['totals']
            col = 5 + len(days)
            sheet.write_row(r, col, [t['A'], t['R'], t['F'], t['V'], t['P'], t['I']], center)
            sheet.write(r, col + 6, self._hours_text(t['hours']), center)
            sheet.write(r, col + 7, self._hours_text(t['overtime']), center)
        sheet.set_column(0, 0, 11)
        sheet.set_column(1, 1, 32)
        sheet.set_column(2, 4, 20)
        sheet.set_column(5, 4 + len(days), 6)
        sheet.set_column(5 + len(days), 12 + len(days), 11)
        sheet.freeze_panes(4, 2)

        detail = book.add_worksheet("Entradas y salidas")
        detail.write_row(0, 0, ["Fecha", "No. Empleado", "Nombre", "Código", "Entrada", "Salida", "Horas"], head)
        line = 1
        for row in rows:
            for d, cell in zip(days, row['cells']):
                if cell['code']:
                    detail.write_row(line, 0, [d.strftime('%d/%m/%Y'), row['key'], row['name'], cell['code'],
                                               cell['in'], cell['out'],
                                               self._hours_text(cell['hours']) if cell['hours'] else ''])
                    line += 1
        detail.set_column(0, 1, 12)
        detail.set_column(2, 2, 32)
        detail.set_column(3, 6, 10)
        book.close()
        return output.getvalue(), f"Asistencia {days[0]:%Y-%m-%d} al {days[-1]:%Y-%m-%d}.xlsx"


class IrActionsReport(models.Model):
    _inherit = 'ir.actions.report'

    def _get_rendering_context_model(self, report):
        # Model names must be lowercase, so 'report.Remote_Attendance...' cannot exist.
        if report.report_name == 'Remote_Attendance.attendance_report_pdf':
            return self.env['report.remote_attendance.attendance_report_pdf']
        return super()._get_rendering_context_model(report)


class ReportFieldAttendance(models.AbstractModel):
    _name = 'report.remote_attendance.attendance_report_pdf'
    _description = "Reporte de asistencia (PDF)"

    @api.model
    def _get_report_values(self, docids, data=None):
        filters = (data or {}).get('filters') or {}
        Report = self.env['hr.field.attendance.report']
        days, rows, kpis = Report._compute(filters)
        # A whole month fits on a landscape page; longer periods print the totals only.
        detailed = len(days) <= 31
        return {
            'days': Report._day_header(days) if detailed else [],
            'rows': rows,
            'kpis': dict(kpis, employees=len(rows)),
            'detailed': detailed,
            'date_from': days[0].strftime('%d/%m/%Y'),
            'date_to': days[-1].strftime('%d/%m/%Y'),
            'codes': CODES,
            'hours_text': Report._hours_text,
            'company': self.env.company,
        }
