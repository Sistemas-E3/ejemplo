from datetime import timedelta

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.hr_field_service import gps_distance_m


TZ = 'Europe/Brussels'


def vector(base):
    return [base] + [0.0] * 127


@tagged('post_install', '-at_install')
@freeze_time('2026-09-28 10:00:00')  # Monday 12:00 in Brussels, inside the working day
class TestFieldAttendance(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env['hr.field.service'].sudo()
        cls.project_a = cls.env['project.project'].create({
            'name': 'Obra A', 'field_latitude': 19.4326, 'field_longitude': -99.1332, 'field_radius': 200,
        })
        cls.project_b = cls.env['project.project'].create({
            'name': 'Obra B', 'field_latitude': 19.5000, 'field_longitude': -99.2000, 'field_radius': 200,
        })
        cls.project_other = cls.env['project.project'].create({'name': 'Obra ajena'})
        cls.supervisor = cls.env['hr.employee'].create({
            'name': 'Juan Supervisor', 'field_role': 'supervisor', 'tz': TZ, 'pin': '1234',
            'field_project_ids': [(6, 0, (cls.project_a | cls.project_b).ids)],
        })
        cls.worker = cls.env['hr.employee'].create({
            'name': 'Pedro Trabajador', 'field_role': 'worker', 'tz': TZ, 'pin': '5678',
            'field_supervisor_id': cls.supervisor.id,
        })
        cls.env['hr.field.face'].create([
            {'employee_id': cls.supervisor.id, 'descriptor': str(vector(0.0))},
            {'employee_id': cls.worker.id, 'descriptor': str(vector(1.0))},
        ])
        cls.supervisor.action_field_regenerate_token()
        cls.worker.action_field_regenerate_token()
        cls.sup_device, _key = cls.env['hr.field.device']._field_register(cls.supervisor, 'Tel Juan', 'test')
        cls.sup_device.action_approve()
        cls.worker_device, _key = cls.env['hr.field.device']._field_register(cls.worker, 'Tel Pedro', 'test')
        cls.worker_device.action_approve()

    def _activate(self, projects, pin='1234'):
        return self.service._activate_projects(self.supervisor, pin, projects.ids)

    def _punch(self, device, **kwargs):
        owner = device.employee_id
        kwargs.setdefault('latitude', 19.4326)
        kwargs.setdefault('longitude', -99.1332)
        return self.service._punch(owner, device, **kwargs)

    def test_gps_distance(self):
        self.assertAlmostEqual(gps_distance_m(19.4326, -99.1332, 19.4326, -99.1332), 0)
        self.assertAlmostEqual(gps_distance_m(0, 0, 0, 1), 111195, delta=5)

    def test_token_lookup(self):
        self.assertEqual(self.service._owner_from_token(self.supervisor.field_token), self.supervisor)
        self.assertFalse(self.service._owner_from_token('x' * 30))
        old = self.supervisor.field_token
        self.supervisor.action_field_regenerate_token()
        self.assertFalse(self.service._owner_from_token(old))
        self.assertEqual(self.sup_device.state, 'revoked', "Regenerating the link revokes its phones")

    def test_activate_only_assigned_projects(self):
        result = self._activate(self.project_a | self.project_other)
        self.assertEqual([p['id'] for p in result['active_projects']], [self.project_a.id])
        self.assertEqual(self.supervisor._field_active_projects(), self.project_a)

    def test_activate_wrong_pin_locks(self):
        for _i in range(4):
            self.assertIn('error', self._activate(self.project_a, pin='0000'))
        self.assertIn('error', self._activate(self.project_a, pin='0000'))
        self.assertTrue(self.supervisor.field_pin_locked_until)
        with self.assertRaises(Exception):
            self._activate(self.project_a)

    def test_active_projects_expire_next_day(self):
        self._activate(self.project_a)
        self.supervisor.field_active_date = fields.Date.today() - timedelta(days=1)
        self.assertFalse(self.supervisor._field_active_projects())

    def test_check_in_and_out_creates_timesheet(self):
        self._activate(self.project_a)
        result = self._punch(self.sup_device, descriptor=vector(1.02))
        self.assertEqual(result['employee'], self.worker.name)
        self.assertEqual(result['action'], 'check_in')
        self.assertEqual(result['state'], 'valid')
        attendance = self.env['hr.attendance'].search([('employee_id', '=', self.worker.id)])
        self.assertEqual(attendance.field_project_id, self.project_a)
        self.assertEqual(attendance.field_supervisor_id, self.supervisor)
        self.assertEqual(attendance.in_mode, 'kiosk')
        attendance.check_in = fields.Datetime.now() - timedelta(hours=8)

        result = self._punch(self.sup_device, descriptor=vector(0.98))
        self.assertEqual(result['action'], 'check_out')
        line = attendance.field_timesheet_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.project_id, self.project_a)
        self.assertEqual(line.employee_id, self.worker)
        self.assertAlmostEqual(line.unit_amount, 5, places=2, msg="From 7:00 (not 4:00) to 12:00")
        self.assertEqual(line.field_attendance_id, attendance)

    def test_unknown_face_asks_pin(self):
        self._activate(self.project_a)
        result = self._punch(self.sup_device, descriptor=vector(5.0))
        self.assertTrue(result.get('need_pin'))
        result = self._punch(self.sup_device, descriptor=vector(5.0), employee_id=self.worker.id, pin='5678')
        self.assertEqual(result['state'], 'review')
        attendance = self.env['hr.attendance'].search([('employee_id', '=', self.worker.id)])
        attendance.check_in = fields.Datetime.now() - timedelta(hours=2)
        self._punch(self.sup_device, employee_id=self.worker.id, pin='5678')
        self.assertFalse(attendance.field_timesheet_ids, "Attendances in review do not create hours")
        attendance.action_field_approve()
        self.assertTrue(attendance.field_timesheet_ids)
        attendance.action_field_reject()
        self.assertFalse(attendance.field_timesheet_ids)

    def test_worker_cannot_use_pin_of_someone_outside_link(self):
        self._activate(self.project_a)
        with self.assertRaises(UserError):
            self._punch(self.worker_device, employee_id=self.supervisor.id, pin='1234')

    def test_personal_link_only_recognises_owner(self):
        self._activate(self.project_a)
        result = self._punch(self.worker_device, descriptor=vector(0.0))
        self.assertTrue(result.get('need_pin'), "The supervisor's face is not accepted on the worker's link")
        result = self._punch(self.worker_device, descriptor=vector(1.0))
        self.assertEqual(result['employee'], self.worker.name)

    def test_outside_geofence_goes_to_review(self):
        self._activate(self.project_a)
        result = self._punch(self.sup_device, descriptor=vector(1.0), latitude=19.44, longitude=-99.1332)
        self.assertEqual(result['state'], 'review')
        self.assertTrue(any('Fuera de obra' in r for r in result['reasons']))

    def test_no_gps_goes_to_review(self):
        self._activate(self.project_a)
        result = self._punch(self.sup_device, descriptor=vector(1.0), latitude=None, longitude=None)
        self.assertEqual(result['state'], 'review')

    def test_several_projects_need_choice_and_change_project(self):
        self._activate(self.project_a | self.project_b)
        result = self._punch(self.sup_device, descriptor=vector(1.0))
        self.assertTrue(result.get('need_project'))
        result = self._punch(self.sup_device, descriptor=vector(1.0), project_id=self.project_other.id)
        self.assertIn('error', result)
        result = self._punch(self.sup_device, descriptor=vector(1.0), project_id=self.project_a.id)
        self.assertEqual(result['action'], 'check_in')
        first = self.env['hr.attendance'].search([('employee_id', '=', self.worker.id)])
        first.check_in = fields.Datetime.now() - timedelta(hours=3)

        result = self._punch(self.sup_device, descriptor=vector(1.0), project_id=self.project_b.id,
                             latitude=19.5, longitude=-99.2, change_project=True)
        self.assertEqual(result['action'], 'check_in')
        self.assertEqual(result['project'], self.project_b.display_name)
        attendances = self.env['hr.attendance'].search([('employee_id', '=', self.worker.id)], order='check_in')
        self.assertEqual(len(attendances), 2)
        self.assertTrue(first.check_out)
        self.assertEqual(first.field_timesheet_ids.project_id, self.project_a)
        self.assertEqual(attendances[-1].field_project_id, self.project_b)

    def test_no_active_projects(self):
        result = self._punch(self.sup_device, descriptor=vector(1.0))
        self.assertIn('error', result)

    def test_device_pending_limit(self):
        for _i in range(3):
            self.env['hr.field.device']._field_register(self.worker, 'x', 'x')
        with self.assertRaises(Exception):
            self.env['hr.field.device']._field_register(self.worker, 'x', 'x')


@tagged('post_install', '-at_install')
class TestFieldAttendanceHttp(HttpCase):

    def test_routes(self):
        supervisor = self.env['hr.employee'].create({'name': 'Sup', 'field_role': 'supervisor', 'pin': '1111'})
        supervisor.action_field_regenerate_token()
        token = supervisor.field_token
        self.assertEqual(self.url_open('/campo/' + 'x' * 30).status_code, 404)
        response = self.url_open('/campo/' + token)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Sup', response.text)

        base = '/campo/' + token
        state = self.make_jsonrpc_request(base + '/state', {'device_key': None})
        self.assertEqual(state['device'], 'unregistered')
        registered = self.make_jsonrpc_request(base + '/register', {'name': 'Tel'})
        key = registered['device_key']
        state = self.make_jsonrpc_request(base + '/state', {'device_key': key})
        self.assertEqual(state['device'], 'pending')
        punch = self.make_jsonrpc_request(base + '/punch', {'device_key': key, 'descriptor': vector(0)})
        self.assertIn('error', punch, "A pending phone cannot punch")
        supervisor.field_device_ids.action_approve()
        state = self.make_jsonrpc_request(base + '/state', {'device_key': key})
        self.assertEqual(state['device'], 'approved')
        self.assertEqual(state['role'], 'supervisor')

    def test_enroll_face(self):
        employee = self.env['hr.employee'].create({'name': 'Nuevo', 'field_role': 'worker'})
        self.authenticate('admin', 'admin')
        self.assertEqual(self.url_open(f'/campo/enrolar/{employee.id}').status_code, 200)
        route = f'/campo/enrolar/{employee.id}/guardar'
        result = self.make_jsonrpc_request(route, {'descriptors': [vector(0.3)]})
        self.assertIn('error', result, "Consent is required")
        result = self.make_jsonrpc_request(route, {'descriptors': [vector(0.3), vector(0.31)], 'consent': True})
        self.assertEqual(result['count'], 2)
        self.assertTrue(employee.field_face_consent_date)

    def test_enroll_requires_operations_group(self):
        employee = self.env['hr.employee'].create({'name': 'Nuevo', 'field_role': 'worker'})
        self.env['res.users'].create({
            'name': 'Interno', 'login': 'interno', 'password': 'interno12345',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.authenticate('interno', 'interno12345')
        response = self.url_open(f'/campo/enrolar/{employee.id}')
        self.assertEqual(response.status_code, 403)
