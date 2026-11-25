from datetime import datetime, time

import pytz
from freezegun import freeze_time

from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged

# Monday 28/09/2026 in Mexico City (the timezone of the test employees).
DAY = datetime(2026, 9, 28).date()
TZ = 'America/Mexico_City'


def vector(base):
    return [base] + [0.0] * 127


@tagged('post_install', '-at_install')
class TestFieldUnified(TransactionCase):
    """Default mode: no projects, the same four marks for everybody, combining phone, tablet and clock."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env['hr.field.service'].sudo()
        cls.Attendance = cls.env['hr.attendance']
        cls.ops = cls.env['hr.department'].create({'name': 'Operaciones'})
        cls.admin_dept = cls.env['hr.department'].create({'name': 'Administración'})
        cls.supervisor = cls.env['hr.employee'].create({
            'name': 'Juan Supervisor', 'field_role': 'supervisor', 'tz': TZ, 'pin': '1234',
            'department_id': cls.ops.id,
        })
        # No role at all: before, this was "office staff" with a plain attendance.
        cls.rosa = cls.env['hr.employee'].create({
            'name': 'Rosa Contabilidad', 'tz': TZ, 'barcode': '3001', 'department_id': cls.admin_dept.id,
            'field_supervisor_id': cls.supervisor.id,
        })
        cls.pedro = cls.env['hr.employee'].create({
            'name': 'Pedro Cuadrilla', 'tz': TZ, 'barcode': '1042', 'department_id': cls.ops.id,
            'field_supervisor_id': cls.supervisor.id,
        })
        cls.tablet = cls.env['hr.employee'].create({'name': 'Tablet recepción', 'field_role': 'office', 'tz': TZ})
        cls.env['hr.field.face'].create([
            {'employee_id': cls.rosa.id, 'descriptor': str(vector(3.0))},
            {'employee_id': cls.pedro.id, 'descriptor': str(vector(1.0))},
        ])
        cls.devices = {}
        for owner in (cls.supervisor, cls.tablet):
            owner.action_field_regenerate_token()
            device, _key = cls.env['hr.field.device']._field_register(owner, 'Tel', 'test')
            device.action_approve()
            cls.devices[owner] = device
        cls.clock = cls.env['hr.field.clock'].create({'name': 'Checador taller', 'serial': 'UNI-1'})

    def _utc(self, hour, minute=0):
        local = pytz.timezone(TZ).localize(datetime.combine(DAY, time(hour, minute)))
        return local.astimezone(pytz.utc).replace(tzinfo=None)

    def _at(self, hour, minute=0):
        return freeze_time(self._utc(hour, minute))

    def _tablet(self, descriptor):
        return self.service._punch(self.tablet, self.devices[self.tablet], descriptor=descriptor)

    def _phone(self, descriptor, kind):
        return self.service._punch(self.supervisor, self.devices[self.supervisor], descriptor=descriptor,
                                   kind=kind, latitude=19.43, longitude=-99.13)

    def _attendances(self, employee):
        return self.Attendance.search([('employee_id', '=', employee.id)], order='check_in')

    def test_installs_without_projects(self):
        self.assertFalse(self.service._use_projects(), "The module works without projects by default")
        self.assertTrue(self.rosa._field_is_crew(), "No difference between field and office staff")
        self.assertFalse(self.tablet._field_is_crew(), "The tablet link is not a person")
        self.assertIn(self.rosa, self.env['hr.employee']._field_staff())

    def test_four_marks_on_the_tablet(self):
        with self._at(8, 0):
            self.assertEqual(self._tablet(vector(3.0))['action'], 'check_in')
        with self._at(8, 10):
            self.assertEqual(self._tablet(vector(3.0))['action'], 'repeat', "A second scan is the same mark")
        with self._at(13, 0):
            self.assertEqual(self._tablet(vector(3.0))['action'], 'lunch_out')
        with self._at(13, 5):
            self.assertEqual(self._tablet(vector(3.0))['action'], 'repeat', "Not a return from lunch")
        with self._at(14, 0):
            self.assertEqual(self._tablet(vector(3.0))['action'], 'lunch_in')
        with self._at(17, 0):
            self.assertEqual(self._tablet(vector(3.0))['action'], 'check_out')
        morning, afternoon = self._attendances(self.rosa)
        self.assertEqual((morning.check_in, morning.check_out), (self._utc(8), self._utc(13)))
        self.assertEqual(morning.field_out_kind, 'lunch')
        self.assertEqual(afternoon.field_in_kind, 'lunch')
        self.assertEqual((afternoon.check_in, afternoon.check_out), (self._utc(14), self._utc(17)))
        self.assertEqual(morning.in_field_device_id, self.devices[self.tablet])
        self.assertEqual(morning.in_mode, 'kiosk')
        self.assertFalse((morning | afternoon).field_project_id)
        self.assertFalse((morning | afternoon).field_timesheet_ids, "No hours in projects")

    def test_phone_tablet_and_clock_combined(self):
        # Entrance at the clock, lunch out on the supervisor's phone, back at the tablet, exit at the clock.
        self.service._punch_clock(self.pedro, self.clock, self._utc(7, 55))
        with self._at(13, 0):
            self.assertEqual(self._phone(vector(1.0), 'lunch_out')['action'], 'lunch_out')
        with self._at(14, 0):
            self.assertEqual(self._tablet(vector(1.0))['action'], 'lunch_in')
        self.service._punch_clock(self.pedro, self.clock, self._utc(17, 2))
        morning, afternoon = self._attendances(self.pedro)
        self.assertEqual((morning.check_in, morning.check_out), (self._utc(7, 55), self._utc(13)))
        self.assertEqual(morning.in_field_clock_id, self.clock)
        self.assertEqual(morning.out_field_device_id, self.devices[self.supervisor])
        self.assertEqual(afternoon.in_field_device_id, self.devices[self.tablet])
        self.assertEqual((afternoon.check_in, afternoon.check_out), (self._utc(14), self._utc(17, 2)))
        self.assertEqual(afternoon.out_field_clock_id, self.clock)
        self.assertEqual((morning | afternoon).mapped('field_state'), ['valid', 'valid'])

    def test_phone_marks_anybody_without_project(self):
        with self._at(8, 0):
            result = self._phone(vector(3.0), 'in')
        self.assertEqual((result['action'], result['employee'], result['project']),
                         ('check_in', 'Rosa Contabilidad', ''))
        with self._at(17, 0):
            self.assertEqual(self._phone(vector(3.0), 'out')['action'], 'check_out')
        attendance = self._attendances(self.rosa)
        self.assertEqual(len(attendance), 1)
        self.assertEqual(attendance.field_state, 'valid')
        self.assertFalse(attendance.field_project_id)

    def test_report_and_marks_by_department(self):
        with self._at(8, 0):
            self._tablet(vector(3.0))
            self._tablet(vector(1.0))
        self.assertEqual(self._attendances(self.rosa).field_department_id, self.admin_dept)
        Report = self.env['hr.field.attendance.report']
        rows = Report.get_report({'date_from': '2026-09-28', 'date_to': '2026-09-28',
                                  'department_id': self.admin_dept.id})['rows']
        self.assertEqual([r['id'] for r in rows], [self.rosa.id])
        groups = self.Attendance._read_group([('field_date', '=', DAY)], ['field_department_id'])
        self.assertEqual({department.id for department, in groups}, {self.ops.id, self.admin_dept.id})

    def test_link_type_list(self):
        self.supervisor.field_link = 'supervisor_auto'
        self.assertEqual((self.supervisor.field_role, self.supervisor.field_auto_mark), ('supervisor', True))
        self.supervisor.field_link = 'supervisor'
        self.assertEqual((self.supervisor.field_role, self.supervisor.field_auto_mark), ('supervisor', False))
        self.rosa.field_link = 'staff'
        self.assertEqual((self.rosa.field_role, self.rosa.field_auto_mark), ('staff', False))
        self.rosa.field_link = False
        self.assertFalse(self.rosa.field_role)
        self.tablet.invalidate_recordset()
        self.assertEqual(self.tablet.field_link, 'office')

    def test_supervisor_with_automatic_marking(self):
        self.supervisor.field_auto_mark = True
        self.assertTrue(self.supervisor._field_marks_automatically())
        phone = lambda: self.service._punch(self.supervisor, self.devices[self.supervisor], descriptor=vector(1.0),
                                            latitude=19.43, longitude=-99.13)
        actions = []
        for hour, minute in ((7, 58), (8, 5), (13, 10), (14, 0), (17, 0)):
            with self._at(hour, minute):
                actions.append(phone()['action'])
        self.assertEqual(actions, ['check_in', 'repeat', 'lunch_out', 'lunch_in', 'check_out'])
        morning, afternoon = self._attendances(self.pedro)
        self.assertEqual(morning.in_field_device_id, self.devices[self.supervisor])
        self.assertAlmostEqual(morning.in_latitude, 19.43)
        self.assertEqual((morning.field_out_kind, afternoon.field_in_kind), ('lunch', 'lunch'))
        # Without the option the supervisor chooses the mark.
        self.supervisor.field_auto_mark = False
        self.assertFalse(self.supervisor._field_marks_automatically())


@tagged('post_install', '-at_install')
class TestFieldUnifiedPage(HttpCase):

    def test_link_page_tells_the_mode(self):
        supervisor = self.env['hr.employee'].create({'name': 'Sup', 'field_role': 'supervisor', 'field_auto_mark': True})
        supervisor.action_field_regenerate_token()
        page = self.url_open(f'/campo/{supervisor.field_token}').text
        self.assertIn('data-auto="1"', page)
        supervisor.field_auto_mark = False
        page = self.url_open(f'/campo/{supervisor.field_token}').text
        self.assertIn('data-auto="0"', page)


@tagged('post_install', '-at_install')
class TestFieldAudit(TransactionCase):
    """Hardening found in the security review."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env['hr.field.service'].sudo()
        cls.supervisor = cls.env['hr.employee'].create({'name': 'Sup', 'field_role': 'supervisor', 'tz': TZ, 'pin': '1234'})
        cls.pedro = cls.env['hr.employee'].create({'name': 'Pedro', 'tz': TZ, 'barcode': '1042'})
        cls.env['hr.field.face'].create({'employee_id': cls.pedro.id, 'descriptor': str(vector(1.0))})
        cls.supervisor.action_field_regenerate_token()
        cls.device, _key = cls.env['hr.field.device']._field_register(cls.supervisor, 'Tel', 'test')
        cls.device.action_approve()

    def test_nan_descriptor_does_not_match_anybody(self):
        for bad in ([float('nan')] * 128, [float('inf')] * 128, [1e6] * 128):
            with self.assertRaises(ValueError, msg="Rejected (the link answers 'Datos inválidos')"):
                self.service._match_face(bad, self.pedro)
            with self.assertRaises(ValueError):
                self.service._punch(self.supervisor, self.device, descriptor=bad, kind='in')
        self.assertFalse(self.env['hr.attendance'].search([('employee_id', '=', self.pedro.id)]))
        with self.assertRaises(ValidationError):
            self.env['hr.field.face'].create({'employee_id': self.pedro.id, 'descriptor': str([float('nan')] * 128)})

    def test_card_only_on_the_office_tablet(self):
        result = self.service._punch(self.supervisor, self.device, badge='1042', kind='in')
        self.assertIn('error', result, "A supervisor's phone cannot mark someone by typing their number")
        self.assertFalse(self.env['hr.attendance'].search([('employee_id', '=', self.pedro.id)]))
        tablet = self.env['hr.employee'].create({'name': 'Tablet', 'field_role': 'office', 'tz': TZ})
        tablet.action_field_regenerate_token()
        device, _key = self.env['hr.field.device']._field_register(tablet, 'Tablet', 'test')
        device.action_approve()
        result = self.service._punch(tablet, device, badge='1042')
        self.assertEqual((result['employee'], result['action']), ('Pedro', 'check_in'))
