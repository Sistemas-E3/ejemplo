import base64
import io
from datetime import datetime

from freezegun import freeze_time

from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

TZ = 'America/Mexico_City'


@tagged('post_install', '-at_install')
class TestFieldClock(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Clock = cls.env['hr.field.clock']
        cls.clock = cls.Clock._clock_seen('CQZ7231560001')
        cls.clock.action_approve()
        cls.supervisor = cls.env['hr.employee'].create({'name': 'Juan Supervisor', 'field_role': 'supervisor', 'tz': TZ})
        cls.worker = cls.env['hr.employee'].create({
            'name': 'Pedro Trabajador', 'field_role': 'worker', 'tz': TZ, 'barcode': '1042',
            'field_supervisor_id': cls.supervisor.id,
        })

    def _attendances(self):
        return self.env['hr.attendance'].search([('employee_id', '=', self.worker.id)], order='check_in')

    def test_entrance_and_exit(self):
        # Mexico City is UTC-6: 06:55 local is 12:55 UTC.
        self.clock._clock_receive("1042\t2026-09-28 06:55:00\t0\t15\t0\t0\n1042\t2026-09-28 06:58:10\t0\t15\t0\t0\n")
        attendance = self._attendances()
        self.assertEqual(len(attendance), 1)
        self.assertEqual(attendance.check_in, datetime(2026, 9, 28, 12, 55))
        self.assertEqual(attendance.field_in_kind, 'clock')
        self.assertEqual(attendance.field_supervisor_id, self.supervisor)
        self.assertFalse(attendance.field_project_id)
        self.assertEqual(attendance.in_field_clock_id, self.clock)
        logs = self.clock.log_ids.sorted('punch_time')
        self.assertEqual(logs.mapped('state'), ['entry', 'ignored'])

        self.clock._clock_receive("1042\t2026-09-28 17:05:00\t1\t15\t0\t0\n")
        self.assertEqual(attendance.check_out, datetime(2026, 9, 28, 23, 5))
        self.assertFalse(attendance.field_overtime_state)
        self.assertEqual(attendance.out_field_clock_id, self.clock)

    def test_late_exit_is_overtime_to_validate(self):
        self.clock._clock_receive("1042\t2026-09-28 07:00:00\t0\t1\n1042\t2026-09-28 19:00:00\t1\t1\n")
        self.assertEqual(self._attendances().field_overtime_state, 'pending')

    def test_resent_punches_are_saved_once(self):
        body = "1042\t2026-09-28 07:00:00\t0\t1\n"
        self.clock._clock_receive(body)
        self.clock._clock_receive(body)
        self.assertEqual(len(self.clock.log_ids), 1)
        self.assertEqual(len(self._attendances()), 1)

    def test_forgotten_exit_goes_to_review(self):
        self.clock._clock_receive("1042\t2026-09-28 07:00:00\t0\t1\n1042\t2026-09-29 07:02:00\t0\t1\n")
        yesterday, today = self._attendances()
        self.assertEqual(yesterday.field_state, 'review')
        self.assertEqual(yesterday.check_out, yesterday.check_in)
        self.assertFalse(today.check_out)

    def test_unknown_key_can_be_retried(self):
        self.clock._clock_receive("2077\t2026-09-28 07:00:00\t0\t1\n")
        log = self.clock.log_ids
        self.assertEqual(log.state, 'error')
        self.worker.barcode = '2077'
        log.action_reprocess()
        self.assertEqual(log.state, 'entry')
        self.assertEqual(log.employee_id, self.worker)

    def test_office_staff_get_a_plain_attendance(self):
        office = self.env['hr.employee'].create({'name': 'Laura Contabilidad', 'tz': TZ, 'barcode': '3001'})
        self.clock._clock_receive("3001\t2026-09-28 08:58:00\t0\t15\n3001\t2026-09-28 19:10:00\t1\t15\n")
        attendance = self.env['hr.attendance'].search([('employee_id', '=', office.id)])
        self.assertEqual(len(attendance), 1)
        self.assertEqual(attendance.check_in, datetime(2026, 9, 28, 14, 58))
        self.assertEqual(attendance.check_out, datetime(2026, 9, 29, 1, 10))
        self.assertFalse(attendance.field_state, "Office attendances stay out of the field review")
        self.assertFalse(attendance.field_in_kind)
        self.assertFalse(attendance.field_overtime_state)
        self.assertEqual(attendance.in_field_clock_id, self.clock)
        self.assertEqual(self.clock.log_ids.mapped('state'), ['exit', 'entry'])

    def test_send_and_remove_employees(self):
        office = self.env['hr.employee'].create({'name': 'Laura   Contabilidad', 'barcode': '3001'})
        self.env['hr.employee'].create({'name': 'Sin clave'})
        self.clock.action_send_employees()
        commands = self.clock.command_ids
        texts = commands.mapped('command')
        self.assertIn("DATA UPDATE USERINFO PIN=1042\tName=Pedro Trabajador\tPri=0\tPasswd=\tCard=\tGrp=1"
                      "\tTZ=0000000100000000\tVerify=0", texts)
        self.assertIn("DATA UPDATE USERINFO PIN=3001\tName=Laura Contabilidad\tPri=0\tPasswd=\tCard=\tGrp=1"
                      "\tTZ=0000000100000000\tVerify=0", texts)
        self.assertFalse([t for t in texts if 'Sin clave' in t])

        sent = self.clock._clock_next_commands()
        self.assertEqual(len(sent.splitlines()), len(commands))
        self.assertTrue(sent.startswith('C:'))
        self.assertEqual(set(commands.mapped('state')), {'sent'})
        first, second = commands.sorted('id')[:2]
        self.clock._clock_command_results(f"ID={first.id}&Return=0&CMD=DATA\nID={second.id}&Return=-1&CMD=DATA\n")
        self.assertEqual(first.state, 'done')
        self.assertEqual(second.state, 'error')
        self.assertEqual(self.clock._clock_next_commands(), '')

        office.action_archive()
        self.clock.action_remove_archived()
        self.assertIn("DATA DELETE USERINFO PIN=3001", self.clock.command_ids.mapped('command'))

    def _import(self, content, filename, clock=None):
        wizard = self.env['hr.field.clock.import'].create({
            'file': base64.b64encode(content), 'filename': filename, 'clock_id': clock and clock.id,
        })
        return wizard.action_import()

    def test_import_usb_attlog(self):
        content = (b"     1042\t2026-09-28 06:55:00\t1\t0\t1\t0\r\n"
                   b"     1042\t2026-09-28 17:02:00\t1\t1\t1\t0\r\n"
                   b"     9999\t2026-09-28 07:10:00\t1\t0\t1\t0\r\n")
        action = self._import(content, '1_attlog.dat')
        attendance = self._attendances()
        self.assertEqual(attendance.check_in, datetime(2026, 9, 28, 12, 55))
        self.assertEqual(attendance.check_out, datetime(2026, 9, 28, 23, 2))
        clock = attendance.in_field_clock_id
        self.assertEqual(clock.serial, 'ARCHIVO')
        self.assertEqual(sorted(clock.log_ids.mapped('state')), ['entry', 'error', 'exit'])
        self.assertEqual(len(action['domain'][0][2]), 3)
        with self.assertRaises(UserError):
            self._import(content, '1_attlog.dat')  # already imported

    def test_import_excel_with_date_and_time_columns(self):
        import openpyxl
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.append(['Reporte de marcajes'])
        sheet.append(['No.', 'Nombre', 'Fecha', 'Hora'])
        sheet.append([1042, 'Pedro', '28/09/2026', '07:00'])
        sheet.append([1042, 'Pedro', '28/09/2026', '19:00'])
        buffer = io.BytesIO()
        book.save(buffer)
        self._import(buffer.getvalue(), 'marcajes.xlsx', self.clock)
        attendance = self._attendances()
        self.assertEqual(attendance.check_in, datetime(2026, 9, 28, 13, 0))
        self.assertEqual(attendance.field_overtime_state, 'pending')
        self.assertEqual(attendance.in_field_clock_id, self.clock)

    def test_import_csv_with_datetime_column(self):
        content = "Número de empleado;Fecha/Hora\n1042;2026-09-28 07:00:00\n".encode()
        self._import(content, 'marcajes.csv')
        self.assertEqual(self._attendances().check_in, datetime(2026, 9, 28, 13, 0))

    def _phone_attendance(self, check_in, check_out, project, out_kind='day', in_kind='site'):
        return self.env['hr.attendance'].create({
            'employee_id': self.worker.id, 'check_in': check_in, 'check_out': check_out,
            'field_project_id': project.id, 'field_supervisor_id': self.supervisor.id,
            'field_in_kind': in_kind, 'field_out_kind': out_kind, 'field_state': 'valid',
        })

    def test_clock_import_merges_with_phone_day(self):
        project = self.env['project.project'].create({'name': 'Proyecto Norte'})
        # Phone: 08:00 to 16:00 local at the site.
        phone = self._phone_attendance(datetime(2026, 9, 28, 14), datetime(2026, 9, 28, 22), project)
        # Clock at the workshop, imported later: 07:00 entrance and 17:05 exit.
        self.clock._clock_receive("1042\t2026-09-28 07:00:00\n1042\t2026-09-28 17:05:00\n")
        self.assertEqual(self._attendances(), phone, "No duplicate attendance")
        self.assertEqual(phone.check_in, datetime(2026, 9, 28, 13))
        self.assertEqual(phone.check_out, datetime(2026, 9, 28, 23, 5))
        self.assertEqual(phone.field_project_id, project)
        self.assertEqual(phone.field_in_kind, 'clock')
        self.assertEqual(self.clock.log_ids.mapped('state'), ['exit', 'entry'])

    def test_clock_import_with_phone_lunch(self):
        project = self.env['project.project'].create({'name': 'Proyecto Norte'})
        morning = self._phone_attendance(datetime(2026, 9, 28, 14), datetime(2026, 9, 28, 19), project, out_kind='lunch')
        afternoon = self._phone_attendance(datetime(2026, 9, 28, 20), datetime(2026, 9, 28, 23), project, in_kind='lunch')
        self.clock._clock_receive("1042\t2026-09-28 07:30:00\n1042\t2026-09-28 13:30:00\n"
                                  "1042\t2026-09-28 15:00:00\n1042\t2026-09-28 17:20:00\n")
        self.assertEqual(self._attendances(), morning | afternoon)
        self.assertEqual(morning.check_in, datetime(2026, 9, 28, 13, 30))
        self.assertEqual(afternoon.check_out, datetime(2026, 9, 28, 23, 20))
        states = self.clock.log_ids.sorted('punch_time').mapped('state')
        self.assertEqual(states, ['entry', 'ignored', 'ignored', 'exit'])

    def test_clock_lunch_return_keeps_project(self):
        project = self.env['project.project'].create({'name': 'Proyecto Norte'})
        morning = self._phone_attendance(datetime(2026, 9, 28, 13), datetime(2026, 9, 28, 19), project, out_kind='lunch')
        self.clock._clock_receive("1042\t2026-09-28 14:00:00\n")
        afternoon = self._attendances() - morning
        self.assertEqual(afternoon.field_in_kind, 'lunch')
        self.assertEqual(afternoon.field_project_id, project)

    @freeze_time('2026-09-28 15:00:00')
    def test_phone_entrance_sets_project_of_clock_attendance(self):
        project = self.env['project.project'].create({'name': 'Proyecto Norte'})
        self.supervisor.write({'field_project_ids': [(6, 0, project.ids)], 'pin': '1234'})
        self.env['hr.field.face'].create({'employee_id': self.worker.id, 'descriptor': str([1.0] + [0.0] * 127)})
        self.supervisor.action_field_regenerate_token()
        device, _key = self.env['hr.field.device']._field_register(self.supervisor, 'Tel', 'test')
        device.action_approve()
        self.clock._clock_receive("1042\t2026-09-28 08:30:00\n")
        result = self.env['hr.field.service'].sudo()._punch(
            self.supervisor, device, descriptor=[1.0] + [0.0] * 127, kind='in', latitude=None, longitude=None)
        self.assertEqual(result.get('action'), 'project', result)
        attendance = self._attendances()
        self.assertEqual(len(attendance), 1)
        self.assertEqual(attendance.field_project_id, project)

    def test_exit_on_the_phone_assigns_the_project(self):
        # Entrance at the clock leaves the day without project, like the office tablet.
        self.clock._clock_receive("1042\t2026-09-28 07:00:00\t0\t1\n")
        attendance = self._attendances()
        project = self.env['project.project'].create({'name': 'Proyecto Norte'})
        attendance.field_project_id = project
        self.assertEqual(attendance.field_project_id, project)


@tagged('post_install', '-at_install')
class TestFieldClockHttp(HttpCase):

    def test_adms_protocol(self):
        serial = 'CQZ7231560099'
        response = self.url_open(f'/iclock/cdata?SN={serial}&options=all&pushver=2.4.1')
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'GET OPTION FROM: {serial}', response.text)
        self.assertIn('TimeZone=-6', response.text)
        clock = self.env['hr.field.clock'].search([('serial', '=', serial)])
        self.assertEqual(clock.state, 'pending')

        body = "1042\t2026-09-28 07:00:00\t0\t1\t0\t0\n"
        url = f'/iclock/cdata?SN={serial}&table=ATTLOG&Stamp=9999'
        response = self.url_open(url, data=body)
        self.assertEqual(response.status_code, 403, "A clock that is not approved keeps its punches")

        clock.action_approve()
        self.env['hr.employee'].create({'name': 'Pedro', 'field_role': 'worker', 'tz': 'America/Mexico_City',
                                        'barcode': '1042'})
        response = self.url_open(url, data=body)
        self.assertEqual(response.text, 'OK: 1')
        self.assertEqual(clock.log_ids.state, 'entry')
        self.assertEqual(clock.att_stamp, '9999')
        self.assertEqual(self.url_open(f'/iclock/getrequest?SN={serial}').text, 'OK')

        clock.action_send_employees()
        command = clock.command_ids
        response = self.url_open(f'/iclock/getrequest?SN={serial}')
        self.assertEqual(response.text, f"C:{command.id}:{command.command}\n")
        self.url_open(f'/iclock/devicecmd?SN={serial}', data=f"ID={command.id}&Return=0&CMD=DATA\n")
        self.assertEqual(command.state, 'done')
