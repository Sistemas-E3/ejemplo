from datetime import datetime, time

import pytz
from freezegun import freeze_time

from odoo.tests import TransactionCase, tagged

# Monday 28/09/2026 in Mexico City (UTC-6). Default schedule: 7:00 to 17:00, lunch exit 12:00-15:00,
# one hour of lunch, 10 minutes of tolerance.
DAY = datetime(2026, 9, 28).date()
TZ = 'America/Mexico_City'


def vector(base):
    return [base] + [0.0] * 127


def utc(hour, minute=0):
    local = pytz.timezone(TZ).localize(datetime.combine(DAY, time(hour, minute)))
    return local.astimezone(pytz.utc).replace(tzinfo=None)


@tagged('post_install', '-at_install')
class TestFieldMixed(TransactionCase):
    """The four marks of the day, each from the clock or the phone, in one day without duplicates."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env['hr.field.service'].sudo()
        cls.project = cls.env['project.project'].create({
            'name': 'Proyecto A', 'field_latitude': 19.4326, 'field_longitude': -99.1332, 'field_radius': 200,
        })
        cls.supervisor = cls.env['hr.employee'].create({
            'name': 'Juan Supervisor', 'field_role': 'supervisor', 'tz': TZ,
            'field_project_ids': [(6, 0, cls.project.ids)],
        })
        cls.worker = cls.env['hr.employee'].create({
            'name': 'Pedro Trabajador', 'field_role': 'worker', 'tz': TZ, 'barcode': '1042',
            'field_supervisor_id': cls.supervisor.id,
        })
        cls.env['hr.field.face'].create({'employee_id': cls.worker.id, 'descriptor': str(vector(1.0))})
        cls.supervisor.action_field_regenerate_token()
        cls.device, _key = cls.env['hr.field.device']._field_register(cls.supervisor, 'Tel', 'test')
        cls.device.action_approve()
        cls.clock = cls.env['hr.field.clock'].sudo()._clock_seen('CQZ-MIXTO')
        cls.clock.action_approve()

    def _phone(self, hour, minute, kind, **kwargs):
        with freeze_time(utc(hour, minute)):
            return self.service._punch(self.supervisor, self.device, descriptor=vector(1.0), kind=kind,
                                       latitude=19.4326, longitude=-99.1332, **kwargs)

    def _clock(self, hour, minute):
        with freeze_time(utc(hour, minute)):
            self.clock._clock_receive(f"1042\t2026-09-28 {hour:02d}:{minute:02d}:00\t0\t15\t0\t0\n")
        return self.clock.log_ids.sorted('punch_time')[-1]

    def _attendances(self):
        return self.env['hr.attendance'].search([('employee_id', '=', self.worker.id)], order='check_in')

    def _hours(self):
        return round(sum(self._attendances().field_timesheet_ids.mapped('unit_amount')), 2)

    def test_clock_entrance_phone_lunch_clock_return_phone_exit(self):
        self._clock(7, 0)
        self.assertEqual(self._phone(12, 30, 'lunch_out')['action'], 'lunch_out')
        self.assertEqual(self._clock(13, 25).state, 'entry', "Back from lunch at the clock")
        self.assertEqual(self._phone(17, 0, 'out')['action'], 'check_out')
        morning, afternoon = self._attendances()
        self.assertEqual((morning.field_in_kind, morning.field_out_kind), ('clock', 'lunch'))
        self.assertEqual(morning.field_project_id, self.project, "The phone gave the day its project")
        self.assertEqual((afternoon.field_in_kind, afternoon.field_out_kind), ('lunch', 'day'))
        self.assertEqual(afternoon.field_project_id, self.project)
        self.assertEqual((morning.field_state, afternoon.field_state), ('valid', 'valid'))
        self.assertEqual(self._hours(), 9.08, "7:00-12:30 and 13:25-17:00")

    def test_phone_entrance_clock_lunch_and_exit_with_repeats(self):
        self._phone(7, 0, 'in')
        self.assertEqual(self._clock(12, 15).result, 'Salida a comer')
        self.assertEqual(self._phone(12, 20, 'lunch_out')['action'], 'repeat',
                         "The clock already took the lunch exit")
        self._clock(13, 10)
        self.assertEqual(self._phone(13, 15, 'lunch_in')['action'], 'repeat',
                         "The clock already took the return")
        self.assertEqual(self._clock(17, 5).state, 'exit')
        morning, afternoon = self._attendances()
        self.assertEqual((morning.check_out, morning.field_out_kind), (utc(12, 15), 'lunch'))
        self.assertEqual((afternoon.check_in, afternoon.check_out), (utc(13, 10), utc(17, 5)))
        self.assertEqual(afternoon.field_project_id, self.project)
        self.assertFalse(afternoon.field_overtime_state, "Five minutes late is not overtime")
        self.assertEqual(self._hours(), 9.08, "7:00-12:15 and 13:10-17:00; after 17:00 only approved overtime")

    def test_lunch_exit_range(self):
        self._phone(7, 0, 'in')
        result = self._phone(11, 0, 'lunch_out')
        self.assertEqual(result['error'], 'La salida a comer se marca de 12:00 a 15:00.')
        self.assertFalse(self._attendances().check_out)
        self.assertEqual(self._phone(12, 0, 'lunch_out')['action'], 'lunch_out')
        # Before the range, the clock exit is a normal exit (and an early one).
        self._phone(13, 0, 'lunch_in')
        log = self._clock(14, 0)
        self.assertEqual(log.result, 'Salida', "Only the first exit of the day can be the lunch")

    def test_late_lunch_return_goes_to_review(self):
        self._phone(7, 0, 'in')
        self._phone(12, 0, 'lunch_out')
        result = self._phone(13, 30, 'lunch_in')
        self.assertEqual(result['state'], 'review')
        self.assertIn('Regresó de comer tarde: 1:30', result['reasons'][0])

    def test_lunch_return_within_tolerance(self):
        self._phone(7, 0, 'in')
        self._phone(12, 0, 'lunch_out')
        self.assertEqual(self._phone(13, 10, 'lunch_in')['state'], 'valid', "1:10 is within the tolerance")

    def test_early_exit(self):
        self._phone(7, 0, 'in')
        self._phone(12, 0, 'lunch_out')
        self._phone(13, 0, 'lunch_in')
        self.assertEqual(self._phone(16, 55, 'out')['state'], 'valid', "Within the 10 minutes of tolerance")

    def test_early_exit_at_the_clock(self):
        self._phone(7, 0, 'in')
        self._phone(12, 0, 'lunch_out')
        self._clock(13, 0)
        self._clock(16, 0)
        afternoon = self._attendances()[-1]
        self.assertEqual(afternoon.field_state, 'review')
        self.assertIn('Salió antes de las 17:00 (16:00)', afternoon.field_review_reason)

    def test_clock_exit_relabelled_as_lunch_by_phone_return(self):
        # Clock exit outside the lunch range (an early exit), then the supervisor marks "Regreso de comer".
        self._phone(7, 0, 'in')
        self._clock(15, 30)
        morning = self._attendances()
        self.assertEqual(morning.field_out_kind, 'day')
        self.assertEqual(self._phone(16, 0, 'lunch_in')['action'], 'lunch_in')
        self.assertEqual(morning.field_out_kind, 'lunch', "The return shows the exit was the lunch")

    def test_settings(self):
        settings = self.env['res.config.settings'].create({
            'field_lunch_from': 13.0, 'field_lunch_to': 14.0, 'field_late_minutes': 5, 'field_early_minutes': 0,
        })
        settings.execute()
        params = self.env['ir.config_parameter'].sudo()
        self.assertEqual(float(params.get_param('Remote_Attendance.lunch_from')), 13.0)
        self._phone(7, 0, 'in')
        self.assertEqual(self._phone(12, 30, 'lunch_out')['error'], 'La salida a comer se marca de 13:00 a 14:00.')
        self._phone(13, 0, 'lunch_out')
        self.assertEqual(self._phone(14, 10, 'lunch_in')['state'], 'review', "Only 5 minutes of tolerance now")

    def test_supervisor_marks_anybody_registered(self):
        other_supervisor = self.env['hr.employee'].create({'name': 'Luis Supervisor', 'field_role': 'supervisor', 'tz': TZ})
        borrowed = self.env['hr.employee'].create({'name': 'Mario de otra cuadrilla', 'field_role': 'worker', 'tz': TZ,
                                                   'field_supervisor_id': other_supervisor.id})
        office = self.env['hr.employee'].create({'name': 'Laura Oficina', 'field_role': 'staff', 'tz': TZ})
        self.env['hr.field.face'].create([
            {'employee_id': borrowed.id, 'descriptor': str(vector(2.0))},
            {'employee_id': office.id, 'descriptor': str(vector(3.0))},
        ])
        candidates = self.supervisor._field_candidates()
        self.assertIn(borrowed, candidates)
        self.assertIn(office, candidates)
        with freeze_time(utc(7, 0)):
            result = self.service._punch(self.supervisor, self.device, descriptor=vector(2.0), kind='in')
        self.assertEqual(result['project'], 'Proyecto A', "Borrowed people work on the supervisor's project")
        with freeze_time(utc(8, 0)):
            result = self.service._punch(self.supervisor, self.device, descriptor=vector(3.0), kind='in')
        self.assertEqual((result['employee'], result['action'], result['project']), ('Laura Oficina', 'check_in', ''))
        attendance = self.env['hr.attendance'].search([('employee_id', '=', office.id)])
        self.assertFalse(attendance.field_state, "Office staff keep a plain attendance")
        self.assertFalse(attendance.field_project_id)

    def test_without_projects(self):
        self.env['ir.config_parameter'].sudo().set_param('Remote_Attendance.without_projects', 'True')
        self.assertFalse(self.service._state(self.supervisor, self.device)['use_projects'])
        self._clock(7, 0)
        self.assertEqual(self._phone(7, 10, 'in')['action'], 'repeat', "The clock already took the entrance")
        self.assertEqual(self._phone(12, 0, 'lunch_out')['action'], 'lunch_out')
        result = self._phone(13, 0, 'lunch_in')
        self.assertEqual((result['action'], result['project']), ('lunch_in', ''))
        self.assertEqual(self._phone(17, 0, 'out')['action'], 'check_out')
        morning, afternoon = self._attendances()
        self.assertFalse((morning | afternoon).field_project_id)
        self.assertEqual((morning.field_state, afternoon.field_state), ('valid', 'valid'))
        self.assertFalse((morning | afternoon).field_timesheet_ids, "No hours in projects")
        self.assertEqual(morning.field_in_source, 'Checador: ' + self.clock.name)
        self.assertEqual(morning.field_out_source, 'Teléfono de Juan Supervisor (Tel)')
        report = self.env['hr.field.attendance.report'].get_report(
            {'date_from': '2026-09-28', 'date_to': '2026-09-28', 'search': 'Pedro'})
        self.assertFalse(report['rows'][0]['cells'][0]['flag'], "A day without project is fine now")
        self.assertIn('error', self._phone(17, 30, None, change_project=True))

    def test_report_four_marks_and_excel(self):
        import io
        import openpyxl
        self._clock(6, 55)
        self._phone(12, 5, 'lunch_out')
        self._clock(13, 20)  # 1:15 of lunch: more than the hour
        self._phone(17, 0, 'out')
        Report = self.env['hr.field.attendance.report']
        filters = {'date_from': '2026-09-28', 'date_to': '2026-09-28', 'search': 'Pedro'}
        cell = Report.get_report(dict(filters))['rows'][0]['cells'][0]
        self.assertEqual((cell['in'], cell['lunch_out'], cell['lunch_in'], cell['out'], cell['lunch']),
                         ('06:55', '12:05', '13:20', '17:00', 'TT'))
        content, _name = Report.excel_file(dict(filters))
        sheet = openpyxl.load_workbook(io.BytesIO(content))['Entradas y salidas']
        self.assertEqual([c.value for c in sheet[2]][4:10],
                         ['06:55 a. m.', '12:05 p. m.', '01:20 p. m.', '05:00 p. m.', 'A', 'TT'])

    def test_report_lunch_in_progress(self):
        self._phone(7, 0, 'in')
        self._phone(12, 0, 'lunch_out')
        cell = self.env['hr.field.attendance.report'].get_report(
            {'date_from': '2026-09-28', 'date_to': '2026-09-28', 'search': 'Pedro'})['rows'][0]['cells'][0]
        self.assertEqual((cell['lunch_out'], cell['lunch_in'], cell['out'], cell['lunch']), ('12:00', '', '', 'IA'))
