from collections import OrderedDict
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError

DEFAULT_START = time(8, 0)
DEFAULT_HOURS = 8.0
ROLL_FRACTIONS = (0.5, 1.0)
# Exit data moved to the after-lunch attendance when a closed day is split.
LUNCH_COPY_FIELDS = (
    'check_out', 'field_out_kind', 'out_face_distance', 'out_gps_distance', 'out_latitude', 'out_longitude',
    'out_mode', 'out_ip_address', 'out_browser', 'out_city', 'out_country_name', 'field_overtime_state',
)


class HrFieldService(models.AbstractModel):
    """Day lists (WhatsApp import and supervisor roll call): one attendance per person and day,
    split between projects with hr.field.allocation."""
    _inherit = 'hr.field.service'

    @api.model
    def _employee_day(self, employee, day):
        """Return (work intervals as naive UTC pairs, hours of a full day, regular working day)."""
        tz = pytz.timezone(employee._get_tz() or 'UTC')
        start = tz.localize(datetime.combine(day, time.min))
        end = tz.localize(datetime.combine(day, time.max))
        calendar = employee.resource_calendar_id
        if calendar and employee.resource_id:
            intervals = sorted(
                (i[0].astimezone(pytz.utc).replace(tzinfo=None), i[1].astimezone(pytz.utc).replace(tzinfo=None))
                for i in calendar._work_intervals_batch(start, end, employee.resource_id)[employee.resource_id.id])
            if intervals:
                hours = sum((e - s).total_seconds() for s, e in intervals) / 3600
                return intervals, hours, True
        hours = (calendar.hours_per_day if calendar else 0) or DEFAULT_HOURS
        check_in = tz.localize(datetime.combine(day, DEFAULT_START)).astimezone(pytz.utc).replace(tzinfo=None)
        return [(check_in, check_in + timedelta(hours=hours))], hours, False

    @staticmethod
    def _end_after(intervals, hours):
        """Point in time after working ``hours`` along the schedule intervals."""
        remaining = timedelta(hours=hours)
        end = intervals[0][0]
        for start, stop in intervals:
            if remaining <= stop - start:
                return start + remaining
            remaining -= stop - start
            end = stop
        return end + remaining

    @api.model
    def _refresh_day_attendance(self, attendance, intervals, regular):
        """Recompute check-out, project and review state from the attendance allocations."""
        allocations = attendance.field_allocation_ids
        total_fraction = sum(allocations.mapped('fraction'))
        reasons = []
        if not regular:
            reasons.append(_("Día no laborable según su horario"))
        if total_fraction > 1.001:
            reasons.append(_("Reportado por más de una jornada (%s)", round(total_fraction, 2)))
        projects = allocations.project_id
        vals = {
            'check_out': self._end_after(intervals, sum(allocations.mapped('hours'))),
            'field_project_id': projects.id if len(projects) == 1 else False,
        }
        if reasons and attendance.field_state == 'valid':
            vals['field_state'] = 'review'
        attendance.write(vals)
        for reason in reasons:
            attendance._field_add_review_reason(reason)

    @api.model
    def _register_day(self, day, supervisor, entries, replace_project=None):
        """Book a list of people for one day.

        ``entries``: dicts with employee, project, name, fraction and optional hours.
        ``replace_project``: when set, what ``supervisor`` reported before for that project on
        that day is replaced instead of added (a corrected roll call).
        Return {'done': [...], 'review': [...], 'skipped': [...], 'removed': [...]} of employee names.
        """
        Attendance = self.env['hr.attendance']
        Allocation = self.env['hr.field.allocation']
        result = {'done': [], 'review': [], 'skipped': [], 'removed': []}

        touched = Attendance
        if replace_project:
            previous = Allocation.search([
                ('attendance_id.field_date', '=', day),
                ('supervisor_id', '=', supervisor.id),
                ('project_id', '=', replace_project.id),
            ])
            touched = previous.attendance_id
            previous.unlink()

        by_employee = OrderedDict()
        for entry in entries:
            by_employee.setdefault(entry['employee'], []).append(entry)

        processed = Attendance
        for employee, employee_entries in by_employee.items():
            intervals, day_hours, regular = self._employee_day(employee, day)
            existing = Attendance.search([('employee_id', '=', employee.id), ('field_date', '=', day)])
            if len(existing) > 1 or (existing and not existing.field_allocation_ids and existing not in touched):
                result['skipped'].append(employee.name)
                continue
            if existing:
                attendance = existing
            else:
                attendance = Attendance.create({
                    'employee_id': employee.id,
                    'check_in': intervals[0][0],
                    'check_out': intervals[0][0] + timedelta(minutes=1),
                    'in_mode': 'manual',
                    'out_mode': 'manual',
                    'field_supervisor_id': supervisor.id,
                    'field_state': 'valid',
                })
            Allocation.create([{
                'attendance_id': attendance.id,
                'project_id': entry['project'].id,
                'name': entry.get('name'),
                'fraction': entry['fraction'],
                'hours': entry.get('hours') or entry['fraction'] * day_hours,
                'supervisor_id': supervisor.id,
            } for entry in employee_entries])
            self._refresh_day_attendance(attendance, intervals, regular)
            processed |= attendance
            result['review' if attendance.field_state == 'review' else 'done'].append(employee.name)

        for attendance in touched - processed:
            if attendance.field_allocation_ids:
                intervals, __, regular = self._employee_day(attendance.employee_id, day)
                self._refresh_day_attendance(attendance, intervals, regular)
            else:
                result['removed'].append(attendance.employee_id.name)
                attendance.unlink()
        return result

    # ------------------------------------------------------------------
    # Supervisor roll call (public link, called with sudo)
    # ------------------------------------------------------------------

    @api.model
    def _roll_days(self, owner):
        today = owner._field_today()
        return [today, today - timedelta(days=1)]

    @api.model
    def _roll_people(self, owner):
        crew = owner | owner.field_crew_ids.filtered('active')
        others = self.env['hr.employee']._field_staff() - crew
        return crew, others

    @api.model
    def _roll_state(self, owner, day=None):
        if owner.field_role != 'supervisor':
            raise UserError(_("Solo un supervisor puede pasar lista."))
        if not self._manual_enabled():
            raise UserError(_("La asistencia en campo es solo con cámara."))
        days = self._roll_days(owner)
        day = day if day in days else days[0]
        crew, others = self._roll_people(owner)
        registered = {}
        allocations = self.env['hr.field.allocation'].search([
            ('attendance_id.field_date', '=', day),
            ('supervisor_id', '=', owner.id),
            ('project_id', 'in', owner.field_project_ids.ids),
        ])
        for allocation in allocations:
            registered.setdefault(allocation.project_id.id, []).append({
                'employee_id': allocation.attendance_id.employee_id.id,
                'fraction': allocation.fraction,
            })
        return {
            'busy': self._roll_busy(owner, day, crew | others),
            'date': day.isoformat(),
            'days': [d.isoformat() for d in days],
            'projects': [{'id': p.id, 'name': p.display_name} for p in owner.field_project_ids],
            'crew': [{'id': e.id, 'name': e.name, 'key': e.field_key or ''} for e in crew],
            'others': [{'id': e.id, 'name': e.name, 'key': e.field_key or ''} for e in others],
            'registered': registered,
        }

    @api.model
    def _roll_busy(self, owner, day, employees):
        """What else each person already has that day, so the supervisor sees it while ticking."""
        busy = {}
        for attendance in self.env['hr.attendance'].search([('employee_id', 'in', employees.ids), ('field_date', '=', day)]):
            notes = busy.setdefault(attendance.employee_id.id, [])
            if not attendance.field_allocation_ids:
                notes.append({'project_id': False, 'mine': False, 'label': _("marcaje con cámara")})
            for allocation in attendance.field_allocation_ids:
                amount = _("día") if allocation.fraction >= 1 else _("½ día") if allocation.fraction == 0.5 \
                    else _("%s día", round(allocation.fraction, 2))
                label = _("%(amount)s en %(project)s", amount=amount, project=allocation.project_id.display_name)
                if allocation.supervisor_id and allocation.supervisor_id != owner:
                    label += " (%s)" % allocation.supervisor_id.name
                notes.append({
                    'project_id': allocation.project_id.id,
                    'mine': allocation.supervisor_id == owner,
                    'label': label,
                })
        return busy

    @api.model
    def _roll_save(self, owner, pin, day, project_id, entries):
        if owner.field_role != 'supervisor':
            raise UserError(_("Solo un supervisor puede pasar lista."))
        if not self._manual_enabled():
            raise UserError(_("La asistencia en campo es solo con cámara."))
        if not owner._field_check_pin(pin):
            return {'error': _("PIN incorrecto.")}
        if day not in self._roll_days(owner):
            raise UserError(_("Solo se puede pasar lista de hoy o de ayer."))
        project = owner.field_project_ids.filtered(lambda p: p.id == int(project_id or 0))
        if not project:
            raise UserError(_("Esa obra no está asignada a ti."))
        crew, others = self._roll_people(owner)
        allowed = crew | others
        rows, seen = [], set()
        for entry in entries or []:
            employee_id = int(entry.get('employee_id') or 0)
            fraction = float(entry.get('fraction') or 1.0)
            if employee_id in seen:
                continue
            employee = allowed.filtered(lambda e: e.id == employee_id)
            if not employee:
                raise UserError(_("Una de las personas no es personal de campo."))
            if fraction not in ROLL_FRACTIONS:
                raise UserError(_("La jornada debe ser día completo o medio día."))
            seen.add(employee_id)
            rows.append({'employee': employee, 'project': project, 'name': _("Pase de lista"), 'fraction': fraction})
        result = self._register_day(day, owner, rows, replace_project=project)
        return dict(result, project=project.display_name)

    # ------------------------------------------------------------------
    # Lunch registered later (the supervisor was at another site)
    # ------------------------------------------------------------------

    @api.model
    def _parse_local_time(self, employee, day, value):
        """'13:30' on ``day`` in the employee's timezone -> naive UTC datetime."""
        try:
            hour, minute = (int(part) for part in str(value).split(':'))
            local = time(hour, minute)
        except (TypeError, ValueError):
            raise UserError(_("Hora inválida: %s", value))
        tz = pytz.timezone(employee._get_tz() or 'UTC')
        return tz.localize(datetime.combine(day, local)).astimezone(pytz.utc).replace(tzinfo=None)

    @api.model
    def _late_lunch(self, owner, pin, employee_id, day, lunch_out, lunch_in, project_id=None):
        """Split the attendance that covers the lunch into before and after lunch.
        Both parts go to HR review."""
        if owner.field_role != 'supervisor':
            raise UserError(_("Solo un supervisor puede registrar la comida."))
        if not self._manual_enabled():
            raise UserError(_("La asistencia en campo es solo con cámara."))
        if not owner._field_check_pin(pin):
            return {'error': _("PIN incorrecto.")}
        if day not in self._roll_days(owner):
            raise UserError(_("Solo se puede registrar la comida de hoy o de ayer."))
        employee = owner._field_candidates().filtered(lambda e: e.id == int(employee_id or 0))
        if not employee:
            raise UserError(_("Esa persona no es personal de campo."))
        start = self._parse_local_time(employee, day, lunch_out)
        end = self._parse_local_time(employee, day, lunch_in)
        if end <= start:
            raise UserError(_("El regreso de comer debe ser después de la salida a comer."))
        if end > fields.Datetime.now():
            raise UserError(_("El regreso de comer no puede ser una hora que aún no pasa."))
        Attendance = self.env['hr.attendance']
        attendance = Attendance.search([
            ('employee_id', '=', employee.id), ('check_in', '<', start),
            '|', ('check_out', '=', False), ('check_out', '>', end),
        ], limit=1)
        if not attendance:
            return {'error': _("%s no tiene una entrada que cubra ese horario.", employee.name)}
        if attendance.field_allocation_ids:
            return {'error': _("La asistencia de %s viene de un pase de lista; ahí no se separa la comida.",
                               employee.name)}
        project = attendance.field_project_id
        if not project:
            project, error = self._pick_project(owner, project_id)
            if error:
                return error
        reason = _("Comida registrada después por %s", owner.name)
        out_fields = [name for name in LUNCH_COPY_FIELDS if name in attendance._fields]
        after_vals = {
            'employee_id': employee.id,
            'check_in': end,
            'field_project_id': project.id,
            'field_supervisor_id': owner.id,
            'field_in_kind': 'lunch',
            'field_state': 'review',
            'field_review_reason': reason,
        }
        if attendance.check_out:
            after_vals.update({name: attendance[name] for name in out_fields})
            after_vals['out_field_device_id'] = attendance.out_field_device_id.id
            after_vals['out_field_photo'] = attendance.out_field_photo
        attendance.write({
            'check_out': start,
            'field_out_kind': 'lunch',
            'field_overtime_state': False,
            'field_project_id': project.id,
            'field_state': 'review' if attendance.field_state in ('valid', 'approved', False) else attendance.field_state,
            'out_field_device_id': False,
            'out_field_photo': False,
        })
        attendance._field_add_review_reason(reason)
        Attendance.create(after_vals)
        return {
            'employee': employee.name,
            'lunch_out': self._local_time(employee, start),
            'lunch_in': self._local_time(employee, end),
            'project': project.display_name,
        }
