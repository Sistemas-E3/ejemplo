from datetime import datetime, time, timedelta

import pytz

from odoo import fields
from odoo.exceptions import UserError
from freezegun import freeze_time

from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.hr_field_roster import clean_line, parse_message


# Monday 28/09/2026, 12:00 in Brussels (the timezone of the test employees).
DAY = datetime(2026, 9, 28).date()


TZ = 'Europe/Brussels'


def vector(base):
    return [base] + [0.0] * 127


@tagged('post_install', '-at_install')
@freeze_time('2026-09-28 10:00:00')
class TestFieldMarks(TransactionCase):
    """Office entrance, the four marks of the day and lunch registered later."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # These tests cover the manual options, hidden by default (camera only).
        cls.env['ir.config_parameter'].sudo().set_param('Remote_Attendance.supervisor_manual', '1')
        cls.service = cls.env['hr.field.service'].sudo()
        cls.Attendance = cls.env['hr.attendance']
        cls.project_a = cls.env['project.project'].create({
            'name': 'Obra A', 'field_latitude': 19.4326, 'field_longitude': -99.1332, 'field_radius': 200,
        })
        cls.project_b = cls.env['project.project'].create({
            'name': 'Obra B', 'field_latitude': 19.5, 'field_longitude': -99.2, 'field_radius': 200,
        })
        cls.supervisor = cls.env['hr.employee'].create({
            'name': 'Juan Supervisor', 'field_role': 'supervisor', 'tz': TZ, 'pin': '1234',
            'field_project_ids': [(6, 0, cls.project_a.ids)],
        })
        cls.other_supervisor = cls.env['hr.employee'].create({
            'name': 'Luis Supervisor', 'field_role': 'supervisor', 'tz': TZ, 'pin': '4321',
            'field_project_ids': [(6, 0, cls.project_b.ids)],
        })
        cls.worker = cls.env['hr.employee'].create({
            'name': 'Pedro Trabajador', 'field_role': 'worker', 'tz': TZ, 'pin': '5678', 'barcode': '1042',
            'field_supervisor_id': cls.supervisor.id,
        })
        cls.borrowed = cls.env['hr.employee'].create({
            'name': 'Mario Prestado', 'field_role': 'worker', 'tz': TZ, 'barcode': 'E-2001',
            'field_supervisor_id': cls.other_supervisor.id,
        })
        cls.office = cls.env['hr.employee'].create({'name': 'Tablet oficina', 'field_role': 'office', 'tz': TZ, 'pin': '9999'})
        cls.env['hr.field.face'].create([
            {'employee_id': cls.worker.id, 'descriptor': str(vector(1.0))},
            {'employee_id': cls.borrowed.id, 'descriptor': str(vector(2.0))},
        ])
        cls.devices = {}
        for owner in (cls.supervisor, cls.other_supervisor, cls.office):
            owner.action_field_regenerate_token()
            device, _key = cls.env['hr.field.device']._field_register(owner, 'Tel', 'test')
            device.action_approve()
            cls.devices[owner] = device
        cls.service._activate_projects(cls.supervisor, '1234', cls.project_a.ids)
        cls.service._activate_projects(cls.other_supervisor, '4321', cls.project_b.ids)

    def _punch(self, owner, **kwargs):
        kwargs.setdefault('latitude', 19.4326)
        kwargs.setdefault('longitude', -99.1332)
        return self.service._punch(owner, self.devices[owner], **kwargs)

    def _office(self, **kwargs):
        return self.service._punch(self.office, self.devices[self.office], **kwargs)

    def _attendances(self, employee):
        return self.Attendance.search([('employee_id', '=', employee.id)], order='check_in')

    def _back(self, attendance, hours):
        attendance.check_in = attendance.check_in - timedelta(hours=hours)

    def _local(self, employee, day, hour):
        tz = pytz.timezone(employee._get_tz() or 'UTC')
        return tz.localize(datetime.combine(day, time(hour))).astimezone(pytz.utc).replace(tzinfo=None)

    def _at(self, hour, minute=0):
        """Freeze the clock at ``hour`` local time of the test day."""
        tz = pytz.timezone(self.worker._get_tz() or 'UTC')
        local = tz.localize(datetime.combine(DAY, time(hour, minute)))
        return freeze_time(local.astimezone(pytz.utc).replace(tzinfo=None))

    def _hours(self, employee):
        return sum(self._attendances(employee).field_timesheet_ids.mapped('unit_amount'))

    def test_four_marks(self):
        with self._at(6, 50):
            result = self._office(descriptor=vector(1.01))
        self.assertEqual((result['employee'], result['action']), (self.worker.name, 'check_in'))
        entrance = self._attendances(self.worker)
        self.assertFalse(entrance.field_project_id, "The office entrance has no site yet")
        self.assertEqual(entrance.field_in_kind, 'office')
        self.assertEqual(entrance.field_supervisor_id, self.supervisor)
        with self._at(7, 0):
            self.assertEqual(self._office(descriptor=vector(1.0))['action'], 'repeat',
                             "A second scan at the office is ignored")
        self.assertEqual(len(self._attendances(self.worker)), 1)

        with self._at(12, 0):
            result = self._punch(self.supervisor, descriptor=vector(1.0), kind='in')
            self.assertIn('error', result, "The entrance is already open")
            result = self._punch(self.supervisor, descriptor=vector(1.0), kind='lunch_out')
        self.assertEqual(result['action'], 'lunch_out')
        self.assertEqual(entrance.field_project_id, self.project_a, "Hours go to the site where they mark")
        self.assertEqual(entrance.field_out_kind, 'lunch')
        self.assertAlmostEqual(entrance.field_timesheet_ids.unit_amount, 5, places=2,
                               msg="Counted from 7:00, not from the 6:50 check-in")

        with self._at(13, 0):
            self.assertIn('error', self._punch(self.supervisor, descriptor=vector(1.0), kind='out'))
            result = self._punch(self.supervisor, descriptor=vector(1.0), kind='lunch_in')
        self.assertEqual(result['action'], 'lunch_in')
        after = self._attendances(self.worker)[-1]
        self.assertEqual((after.field_in_kind, after.field_project_id), ('lunch', self.project_a))
        with self._at(17, 40):
            result = self._punch(self.supervisor, descriptor=vector(1.0), kind='out')
        self.assertEqual(result['action'], 'check_out', "Less than an hour late: no overtime question")
        self.assertEqual(after.field_out_kind, 'day')
        self.assertFalse(after.field_overtime_state)
        self.assertAlmostEqual(self._hours(self.worker), 9, places=2,
                               msg="7 to 12 and 13 to 17: the lunch and the time after 17:00 are not counted")

    def _day_until(self, hour, minute=0, **kwargs):
        with self._at(7, 0):
            self._punch(self.supervisor, descriptor=vector(1.0), kind='in')
        with self._at(hour, minute):
            return self._punch(self.supervisor, descriptor=vector(1.0), kind='out', **kwargs)

    def test_overtime_question_yes(self):
        result = self._day_until(18, 30)
        self.assertTrue(result.get('need_overtime'))
        attendance = self._attendances(self.worker)
        self.assertFalse(attendance.check_out, "Nothing is saved until the question is answered")
        with self._at(18, 30):
            result = self._punch(self.supervisor, descriptor=vector(1.0), kind='out', overtime=True)
        self.assertTrue(result['overtime'])
        self.assertEqual(attendance.field_overtime_state, 'pending')
        self.assertAlmostEqual(attendance.field_overtime_hours, 1.5, places=2)
        self.assertAlmostEqual(self._hours(self.worker), 9, places=2,
                               msg="7 to 17 without the lunch hour; the overtime waits for validation")
        attendance.action_field_overtime_approve()
        self.assertAlmostEqual(self._hours(self.worker), 10.5, places=2)
        attendance.action_field_overtime_reject()
        self.assertAlmostEqual(self._hours(self.worker), 9, places=2)

    def test_overtime_question_no(self):
        self.assertTrue(self._day_until(19, 0).get('need_overtime'))
        with self._at(19, 0):
            result = self._punch(self.supervisor, descriptor=vector(1.0), kind='out', overtime=False)
        self.assertFalse(result['overtime'])
        self.assertFalse(self._attendances(self.worker).field_overtime_state)
        self.assertAlmostEqual(self._hours(self.worker), 9, places=2)

    def test_office_late_exit_goes_to_validation(self):
        with self._at(7, 0):
            self._office(descriptor=vector(1.0))
        attendance = self._attendances(self.worker)
        attendance.field_project_id = self.project_a
        with self._at(18, 15):
            self._office(descriptor=vector(1.0))
        self.assertEqual(attendance.field_overtime_state, 'pending')

    def test_single_assigned_project_is_active_without_activation(self):
        self.other_supervisor.field_active_date = False
        self.assertEqual(self.other_supervisor._field_active_projects(), self.project_b)
        self.other_supervisor.field_project_ids |= self.project_a
        self.assertFalse(self.other_supervisor._field_active_projects(), "With several sites it must choose")

    def test_field_is_camera_only_by_default(self):
        self.env['ir.config_parameter'].sudo().set_param('Remote_Attendance.supervisor_manual', False)
        self.assertFalse(self.service._state(self.supervisor, self.devices[self.supervisor])['manual'])
        self.assertTrue(self.service._state(self.office, self.devices[self.office])['manual'])
        result = self._punch(self.supervisor, employee_id=self.worker.id, pin='5678', kind='in')
        self.assertIn('error', result)
        self.assertNotIn('need_pin', result)
        self.assertFalse(self.Attendance.search([('employee_id', '=', self.worker.id)]))
        self.assertEqual(self._punch(self.supervisor, descriptor=vector(1.0), kind='in')['employee'], self.worker.name)
        with self.assertRaises(UserError):
            self.service._roll_state(self.supervisor)
        with self.assertRaises(UserError):
            self.service._late_lunch(self.supervisor, '1234', self.worker.id, DAY, '13:00', '14:00')
        # The office tablet keeps its PIN for whoever is not recognised.
        result = self._office(employee_id=self.borrowed.id, pin='0000')
        self.assertIn('error', result)

    def test_enroll_from_link(self):
        device = self.devices[self.supervisor]
        newcomer = self.env['hr.employee'].create({'name': 'Nuevo', 'field_role': 'worker'})
        args = (self.supervisor, device)
        self.assertEqual(self.service._enroll(*args, '0000', newcomer.id, [vector(3.0)], consent=True),
                         {'error': 'PIN incorrecto.'})
        self.assertIn('error', self.service._enroll(*args, '1234', newcomer.id, [vector(3.0)]), "Consent is required")
        result = self.service._enroll(*args, '1234', newcomer.id, [vector(3.0), vector(3.01)], consent=True)
        self.assertEqual(result['count'], 2)
        self.assertEqual(newcomer.field_face_ids.enrolled_by_id, self.supervisor)
        self.assertTrue(newcomer.field_face_consent_date)
        self.assertIn('error', self.service._enroll(*args, '1234', newcomer.id, [vector(4.0)], consent=True),
                      "Replacing a face is done by Operaciones")
        with self.assertRaises(UserError):
            self.service._enroll(*args, '1234', self.office.id, [vector(4.0)], consent=True)
        other = self.env['hr.employee'].create({'name': 'Otro', 'field_role': 'worker'})
        result = self.service._enroll(self.office, self.devices[self.office], '9999', other.id,
                                      [vector(5.0)], consent=True)
        self.assertEqual(result['employee'], 'Otro', "The office tablet can register faces too")
        with self._at(7, 0):
            self.assertEqual(self._punch(self.supervisor, descriptor=vector(3.02), kind='in')['employee'], 'Nuevo')

    def test_invalid_kind_and_exit_without_entrance(self):
        with self.assertRaises(UserError):
            self._punch(self.supervisor, descriptor=vector(1.0), kind='nap')
        self.assertIn('error', self._punch(self.supervisor, descriptor=vector(1.0), kind='lunch_out'))

    def test_office_badge(self):
        result = self._office(badge='E2001')
        self.assertEqual((result['employee'], result['action']), (self.borrowed.name, 'check_in'))
        self.assertEqual(self._attendances(self.borrowed).field_state, 'valid')
        self.assertIn('error', self._office(badge='7777'))
        self.assertTrue(self._office(descriptor=vector(9.0)).get('need_pin'))

    def test_office_cannot_manage_sites(self):
        with self.assertRaises(UserError):
            self.service._activate_projects(self.office, '9999', self.project_a.ids)
        self.assertNotIn(self.office, self.office._field_candidates())
        with self.assertRaises(UserError):
            self.service._roll_state(self.office)

    def test_borrowed_person_goes_to_site_of_who_marks(self):
        result = self._punch(self.supervisor, descriptor=vector(2.0), kind='in')
        self.assertEqual(result['employee'], self.borrowed.name)
        attendance = self._attendances(self.borrowed)
        self.assertEqual(attendance.field_project_id, self.project_a)
        self.assertEqual(attendance.field_supervisor_id, self.supervisor)

    def test_forgotten_exit_is_closed_for_review(self):
        yesterday = self.worker._field_today() - timedelta(days=1)
        old = self.Attendance.create({
            'employee_id': self.worker.id, 'check_in': self._local(self.worker, yesterday, 8),
            'field_project_id': self.project_a.id, 'field_state': 'valid',
        })
        result = self._office(descriptor=vector(1.0))
        self.assertEqual(result['action'], 'check_in')
        self.assertEqual(old.check_out, old.check_in)
        self.assertEqual(old.field_state, 'review')
        self.assertIn('Sin salida', old.field_review_reason)

    def test_late_lunch_splits_closed_day(self):
        yesterday = self.supervisor._field_today() - timedelta(days=1)
        day = self.Attendance.create({
            'employee_id': self.worker.id,
            'check_in': self._local(self.worker, yesterday, 8),
            'check_out': self._local(self.worker, yesterday, 18),
            'field_project_id': self.project_a.id, 'field_state': 'valid',
            'field_in_kind': 'office', 'field_out_kind': 'day', 'out_gps_distance': 12,
        })
        self.assertEqual(len(day.field_timesheet_ids), 1)
        args = (self.supervisor, '1234', self.worker.id, yesterday)
        self.assertEqual(self.service._late_lunch(self.supervisor, '0000', self.worker.id, yesterday, '14:00', '15:00'),
                         {'error': 'PIN incorrecto.'})
        with self.assertRaises(UserError):
            self.service._late_lunch(*args, '15:00', '14:00')
        with self.assertRaises(UserError):
            self.service._late_lunch(*args, '25:00', '14:00')
        self.assertIn('error', self.service._late_lunch(*args, '19:00', '19:30'))

        result = self.service._late_lunch(*args, '14:00', '15:00')
        self.assertEqual((result['lunch_out'], result['lunch_in']), ('14:00', '15:00'))
        before, after = self._attendances(self.worker)
        self.assertEqual(before.check_out, self._local(self.worker, yesterday, 14))
        self.assertEqual(before.field_out_kind, 'lunch')
        self.assertEqual(after.check_in, self._local(self.worker, yesterday, 15))
        self.assertEqual(after.check_out, self._local(self.worker, yesterday, 18))
        self.assertEqual((after.field_in_kind, after.field_out_kind, after.out_gps_distance), ('lunch', 'day', 12))
        self.assertEqual((before.field_state, after.field_state), ('review', 'review'))
        self.assertIn('Comida registrada después', after.field_review_reason)
        self.assertFalse((before | after).field_timesheet_ids, "Hours wait for HR")
        (before | after).action_field_approve()
        self.assertAlmostEqual(sum((before | after).field_timesheet_ids.mapped('unit_amount')), 8, places=2,
                               msg="8 to 14 and 15 to 17: the hour after 17:00 is not overtime")

    def test_late_lunch_open_attendance(self):
        today = self.supervisor._field_today()
        self.Attendance.create({
            'employee_id': self.worker.id,
            'check_in': self._local(self.worker, today - timedelta(days=1), 8),
            'field_state': 'valid', 'field_in_kind': 'office',
        })
        result = self.service._late_lunch(
            self.supervisor, '1234', self.worker.id, today - timedelta(days=1), '13:00', '14:00')
        self.assertEqual(result['project'], self.project_a.display_name, "No site yet: the supervisor's site")
        before, after = self._attendances(self.worker)
        self.assertEqual(before.field_project_id, self.project_a)
        self.assertFalse(after.check_out)
        with self.assertRaises(UserError):
            self.service._late_lunch(self.supervisor, '1234', self.worker.id, today - timedelta(days=5),
                                     '13:00', '14:00')
        with self.assertRaises(UserError):
            self.service._late_lunch(self.supervisor, '1234', self.worker.id, today, '23:58', '23:59')

    def test_whatsapp_employee_key(self):
        self.assertEqual(clean_line('1042 Pedro'), '1042 Pedro', "Four digits are a key, not a bullet")
        self.assertEqual(clean_line('12 Pedro'), 'Pedro')
        self.assertEqual(clean_line('3. 1042'), '1042')
        blocks = parse_message("PRESUPUESTO: 2316-26\nPERSONAL:\n1. 1042 Pedrito\n2. E-2001\n3) Juan Supervisor")
        self.assertEqual([p[0] for p in blocks[0]['people']], ['1042 Pedrito', 'E-2001', 'Juan Supervisor'])
        self.project_a.field_code = '2316-26'
        wizard = self.env['hr.field.roster.import'].create({
            'message': "PRESUPUESTO: 2316-26\nPERSONAL:\n1. 1042 Pedrito\n2. E-2001\n3) Juan Supervisor",
            'date': self.supervisor._field_today(),
        })
        wizard.action_read_message()
        found = [(line.employee_id, line.match) for line in wizard.line_ids]
        self.assertEqual(found, [(self.worker, 'key'), (self.borrowed, 'key'), (self.supervisor, 'exact')])

    def test_payroll_registration_number_is_the_key(self):
        if 'registration_number' not in self.worker._fields:
            self.skipTest("Sin nómina instalada")
        self.worker.write({'registration_number': '1608', 'barcode': '9001'})
        self.assertEqual(self.worker.field_key, '1608')
        self.assertEqual(self.env['hr.employee']._field_by_key('1608'), self.worker)
        self.assertEqual(self.env['hr.employee']._field_by_key('9001'), self.worker, "The card still works")

    def test_state_has_keys(self):
        state = self.service._state(self.supervisor, self.devices[self.supervisor])
        keys = {person['id']: person['key'] for person in state['crew']}
        self.assertEqual(keys[self.worker.id], '1042')
        self.assertIn(self.borrowed.id, keys, "A supervisor can recognise people from other crews")
        self.assertEqual(len(state['days']), 2)
        roll = self.service._roll_state(self.supervisor)
        self.assertEqual({p['id'] for p in roll['crew']}, {self.supervisor.id, self.worker.id})


@tagged('post_install', '-at_install')
class TestFieldMarksRoutes(HttpCase):

    def test_office_page_and_routes(self):
        office = self.env['hr.employee'].create({'name': 'Tablet oficina', 'field_role': 'office'})
        worker = self.env['hr.employee'].create({'name': 'Pedro', 'field_role': 'worker', 'barcode': '5555'})
        office.action_field_regenerate_token()
        base = '/campo/' + office.field_token
        response = self.url_open(base)
        self.assertIn('fk-office', response.text)
        key = self.make_jsonrpc_request(base + '/register', {'name': 'Tablet'})['device_key']
        office.field_device_ids.action_approve()
        result = self.make_jsonrpc_request(base + '/punch', {'device_key': key, 'badge': '5555'})
        self.assertEqual((result['employee'], result['action']), ('Pedro', 'check_in'))
        self.assertTrue(self.env['hr.attendance'].search([('employee_id', '=', worker.id)]))
        result = self.make_jsonrpc_request(base + '/comida', {'device_key': key, 'pin': '1'})
        self.assertIn('error', result, "Only supervisors register lunch")
        result = self.make_jsonrpc_request(base + '/comida', {'device_key': 'nope'})
        self.assertEqual(result['error'], 'Teléfono no autorizado.')

    def test_enroll_photos_page(self):
        worker = self.env['hr.employee'].create({
            'name': 'Con foto', 'field_role': 'worker',
            'image_1920': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
        })
        self.env['hr.employee'].create({'name': 'Sin foto', 'field_role': 'worker'})
        self.authenticate('admin', 'admin')
        response = self.url_open('/campo/enrolar/fotos')
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'data-id="{worker.id}"', response.text)
        self.assertIn('Sin foto', response.text)
        self.make_jsonrpc_request(f'/campo/enrolar/{worker.id}/guardar', {'descriptors': [vector(6.0)], 'consent': True})
        self.assertEqual(worker.field_face_count, 1)
