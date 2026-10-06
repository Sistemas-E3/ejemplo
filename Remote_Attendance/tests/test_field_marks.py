from datetime import datetime, time, timedelta

import pytz

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.hr_field_roster import clean_line, parse_message


def vector(base):
    return [base] + [0.0] * 127


@tagged('post_install', '-at_install')
class TestFieldMarks(TransactionCase):
    """Office entrance, the four marks of the day and lunch registered later."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env['hr.field.service'].sudo()
        cls.Attendance = cls.env['hr.attendance']
        cls.project_a = cls.env['project.project'].create({
            'name': 'Obra A', 'field_latitude': 19.4326, 'field_longitude': -99.1332, 'field_radius': 200,
        })
        cls.project_b = cls.env['project.project'].create({
            'name': 'Obra B', 'field_latitude': 19.5, 'field_longitude': -99.2, 'field_radius': 200,
        })
        cls.supervisor = cls.env['hr.employee'].create({
            'name': 'Juan Supervisor', 'field_role': 'supervisor', 'pin': '1234',
            'field_project_ids': [(6, 0, cls.project_a.ids)],
        })
        cls.other_supervisor = cls.env['hr.employee'].create({
            'name': 'Luis Supervisor', 'field_role': 'supervisor', 'pin': '4321',
            'field_project_ids': [(6, 0, cls.project_b.ids)],
        })
        cls.worker = cls.env['hr.employee'].create({
            'name': 'Pedro Trabajador', 'field_role': 'worker', 'pin': '5678', 'barcode': '1042',
            'field_supervisor_id': cls.supervisor.id,
        })
        cls.borrowed = cls.env['hr.employee'].create({
            'name': 'Mario Prestado', 'field_role': 'worker', 'barcode': 'E-2001',
            'field_supervisor_id': cls.other_supervisor.id,
        })
        cls.office = cls.env['hr.employee'].create({'name': 'Tablet oficina', 'field_role': 'office', 'pin': '9999'})
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

    def test_four_marks(self):
        result = self._office(descriptor=vector(1.01))
        self.assertEqual((result['employee'], result['action']), (self.worker.name, 'check_in'))
        entrance = self._attendances(self.worker)
        self.assertFalse(entrance.field_project_id, "The office entrance has no site yet")
        self.assertEqual(entrance.field_in_kind, 'office')
        self.assertEqual(entrance.field_supervisor_id, self.supervisor)
        self.assertEqual(self._office(descriptor=vector(1.0))['action'], 'repeat',
                         "A second scan at the office is ignored")
        self.assertEqual(len(self._attendances(self.worker)), 1)

        self._back(entrance, 5)
        result = self._punch(self.supervisor, descriptor=vector(1.0), kind='in')
        self.assertIn('error', result, "The entrance is already open")
        result = self._punch(self.supervisor, descriptor=vector(1.0), kind='lunch_out')
        self.assertEqual(result['action'], 'lunch_out')
        self.assertEqual(entrance.field_project_id, self.project_a, "Hours go to the site where they mark")
        self.assertEqual(entrance.field_out_kind, 'lunch')
        self.assertEqual(entrance.field_timesheet_ids.project_id, self.project_a)
        # Lunch lasted an hour: move the morning back so the afternoon can start earlier.
        entrance.write({'check_in': entrance.check_in - timedelta(hours=4),
                        'check_out': entrance.check_out - timedelta(hours=4)})

        self.assertIn('error', self._punch(self.supervisor, descriptor=vector(1.0), kind='out'))
        result = self._punch(self.supervisor, descriptor=vector(1.0), kind='lunch_in')
        self.assertEqual(result['action'], 'lunch_in')
        after = self._attendances(self.worker)[-1]
        self.assertEqual((after.field_in_kind, after.field_project_id), ('lunch', self.project_a))
        self._back(after, 3)
        result = self._punch(self.supervisor, descriptor=vector(1.0), kind='out')
        self.assertEqual(result['action'], 'check_out')
        self.assertEqual(after.field_out_kind, 'day')
        lines = self._attendances(self.worker).field_timesheet_ids
        self.assertEqual(len(lines), 2)
        self.assertAlmostEqual(sum(lines.mapped('unit_amount')), 8, delta=0.05,
                               msg="Marked lunch: the schedule's lunch break is not deducted again")

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
        self.assertAlmostEqual(sum((before | after).field_timesheet_ids.mapped('unit_amount')), 9, places=2)

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
