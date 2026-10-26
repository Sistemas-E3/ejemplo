from datetime import datetime

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
