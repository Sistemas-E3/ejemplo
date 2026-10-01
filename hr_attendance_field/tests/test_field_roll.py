from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

MONDAY = date(2026, 9, 28)


class RollMixin:

    @classmethod
    def _setup_roll(cls):
        cls.service = cls.env['hr.field.service'].sudo()
        Project = cls.env['project.project']
        cls.site_a = Project.create({'name': 'Obra A', 'field_code': '2316-26'})
        cls.site_b = Project.create({'name': 'Obra B', 'field_code': '2498-26'})
        cls.foreign = Project.create({'name': 'Obra ajena'})
        cls.supervisor = cls.env['hr.employee'].create({
            'name': 'Juan Supervisor', 'field_role': 'supervisor', 'pin': '1234',
            'field_project_ids': [(6, 0, (cls.site_a | cls.site_b).ids)],
        })
        cls.other_supervisor = cls.env['hr.employee'].create({
            'name': 'Luis Supervisor', 'field_role': 'supervisor', 'pin': '9999',
            'field_project_ids': [(6, 0, cls.site_b.ids)],
        })
        Employee = cls.env['hr.employee']
        cls.pedro = Employee.create({'name': 'Pedro López', 'field_role': 'worker',
                                     'field_supervisor_id': cls.supervisor.id})
        cls.maria = Employee.create({'name': 'María Hernández', 'field_role': 'worker',
                                     'field_supervisor_id': cls.supervisor.id})
        cls.borrowed = Employee.create({'name': 'Brayan Rivera', 'field_role': 'worker',
                                        'field_supervisor_id': cls.other_supervisor.id})
        cls.office = Employee.create({'name': 'Ana Oficina'})

    def _today_patch(self):
        return patch.object(type(self.env['hr.employee']), '_field_today', lambda employee: MONDAY)


@tagged('post_install', '-at_install')
class TestFieldRoll(RollMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_roll()

    def setUp(self):
        super().setUp()
        patcher = self._today_patch()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _save(self, entries, project=None, pin='1234', day=MONDAY, owner=None):
        return self.service._roll_save(owner or self.supervisor, pin, day, (project or self.site_a).id, [
            {'employee_id': employee.id, 'fraction': fraction} for employee, fraction in entries])

    def _attendance(self, employee):
        return self.env['hr.attendance'].search([('employee_id', '=', employee.id), ('field_date', '=', MONDAY)])

    def test_state(self):
        state = self.service._roll_state(self.supervisor)
        self.assertEqual(state['date'], '2026-09-28')
        self.assertEqual(state['days'], ['2026-09-28', '2026-09-27'])
        self.assertEqual({p['id'] for p in state['projects']}, {self.site_a.id, self.site_b.id})
        crew = {p['id'] for p in state['crew']}
        self.assertEqual(crew, {self.supervisor.id, self.pedro.id, self.maria.id})
        others = {p['id'] for p in state['others']}
        self.assertIn(self.borrowed.id, others)
        self.assertNotIn(self.office.id, others, "Only field personnel can be added")
        self.assertNotIn(self.pedro.id, others)
        with self.assertRaises(UserError):
            self.service._roll_state(self.pedro)

    def test_full_and_half_day(self):
        result = self._save([(self.pedro, 1.0), (self.maria, 0.5)])
        self.assertEqual(result['done'], ['Pedro López', 'María Hernández'])
        pedro = self._attendance(self.pedro)
        self.assertEqual(pedro.field_state, 'valid')
        self.assertEqual(pedro.field_project_id, self.site_a)
        self.assertEqual(pedro.field_supervisor_id, self.supervisor)
        self.assertEqual(pedro.field_timesheet_ids.project_id, self.site_a)
        full_day = pedro.field_timesheet_ids.unit_amount
        self.assertGreater(full_day, 0)
        maria = self._attendance(self.maria)
        self.assertAlmostEqual(maria.field_timesheet_ids.unit_amount, full_day / 2)
        state = self.service._roll_state(self.supervisor)
        self.assertEqual(len(state['registered'][self.site_a.id]), 2)

    def test_resend_replaces_previous_list(self):
        self._save([(self.pedro, 1.0), (self.maria, 1.0)])
        result = self._save([(self.pedro, 0.5)])
        self.assertEqual(result['removed'], ['María Hernández'])
        self.assertFalse(self._attendance(self.maria))
        self.assertFalse(self.env['account.analytic.line'].search([('employee_id', '=', self.maria.id)]))
        pedro = self._attendance(self.pedro)
        self.assertEqual(len(pedro.field_allocation_ids), 1)
        self.assertEqual(pedro.field_allocation_ids.fraction, 0.5)
        self.assertEqual(pedro.field_state, 'valid', "A corrected list is not counted twice")
        self.assertEqual(len(pedro.field_timesheet_ids), 1)

    def test_borrowed_person_split_between_supervisors(self):
        self._save([(self.borrowed, 0.5)])
        self._save([(self.borrowed, 0.5)], project=self.site_b, pin='9999', owner=self.other_supervisor)
        attendance = self._attendance(self.borrowed)
        self.assertEqual(len(attendance), 1)
        self.assertEqual(attendance.field_state, 'valid')
        self.assertFalse(attendance.field_project_id)
        self.assertEqual(attendance.field_timesheet_ids.project_id, self.site_a | self.site_b)
        busy = self.service._roll_state(self.supervisor)['busy'][self.borrowed.id]
        self.assertIn('½ día en Obra B (Luis Supervisor)', [b['label'] for b in busy])
        self.assertIn(True, [b['mine'] for b in busy])
        # Luis corrects his list: only his part is replaced.
        self._save([], project=self.site_b, pin='9999', owner=self.other_supervisor)
        self.assertEqual(attendance.field_allocation_ids.project_id, self.site_a)
        self.assertEqual(attendance.field_project_id, self.site_a)
        self.assertEqual(attendance.field_timesheet_ids.project_id, self.site_a)

    def test_more_than_a_day_goes_to_review(self):
        self._save([(self.borrowed, 1.0)])
        result = self._save([(self.borrowed, 1.0)], project=self.site_b, pin='9999', owner=self.other_supervisor)
        self.assertEqual(result['review'], ['Brayan Rivera'])
        attendance = self._attendance(self.borrowed)
        self.assertEqual(attendance.field_state, 'review')
        self.assertIn('más de una jornada', attendance.field_review_reason)

    def test_validations(self):
        self.assertEqual(self._save([(self.pedro, 1.0)], pin='0000'), {'error': 'PIN incorrecto.'})
        self.assertEqual(self.supervisor.field_pin_failures, 1)
        with self.assertRaises(UserError):
            self._save([(self.pedro, 1.0)], project=self.foreign)
        with self.assertRaises(UserError):
            self._save([(self.office, 1.0)])
        with self.assertRaises(UserError):
            self._save([(self.pedro, 0.3)])
        with self.assertRaises(UserError):
            self._save([(self.pedro, 1.0)], day=date(2026, 9, 20))
        with self.assertRaises(UserError):
            self._save([(self.pedro, 1.0)], owner=self.pedro)
        self.assertFalse(self._attendance(self.pedro))

    def test_yesterday_allowed(self):
        self._save([(self.pedro, 1.0)], day=date(2026, 9, 27))
        attendance = self.env['hr.attendance'].search([('employee_id', '=', self.pedro.id)])
        self.assertEqual(attendance.field_date, date(2026, 9, 27))

    def test_camera_attendance_not_duplicated(self):
        self.env['hr.attendance'].create({
            'employee_id': self.pedro.id,
            'check_in': '2026-09-28 14:00:00',
            'check_out': '2026-09-28 20:00:00',
            'field_project_id': self.site_a.id,
            'field_state': 'valid',
        })
        result = self._save([(self.pedro, 1.0), (self.maria, 1.0)])
        self.assertEqual(result['skipped'], ['Pedro López'])
        self.assertEqual(len(self._attendance(self.pedro)), 1)
        self.assertFalse(self._attendance(self.pedro).field_allocation_ids)

    def test_whatsapp_and_roll_call_add_up(self):
        wizard = self.env['hr.field.roster.import'].create({
            'date': MONDAY, 'supervisor_id': self.other_supervisor.id,
            'message': 'PRESUPUESTO: 2498-26\nPERSONAL:\nBrayan Rivera 1/2 día',
        })
        wizard.action_read_message()
        wizard.action_confirm()
        self._save([(self.borrowed, 0.5)])
        attendance = self._attendance(self.borrowed)
        self.assertEqual(attendance.field_allocation_ids.project_id, self.site_a | self.site_b)
        self.assertEqual(attendance.field_state, 'valid')


@tagged('post_install', '-at_install')
class TestFieldRollRoutes(RollMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_roll()

    def test_roll_routes(self):
        self.supervisor.action_field_regenerate_token()
        base = '/campo/' + self.supervisor.field_token
        key = self.make_jsonrpc_request(base + '/register', {'name': 'Tel'})['device_key']
        self.assertIn('error', self.make_jsonrpc_request(base + '/lista', {'device_key': key}))
        self.supervisor.field_device_ids.action_approve()
        with self._today_patch():
            state = self.make_jsonrpc_request(base + '/lista', {'device_key': key, 'date': 'basura'})
            self.assertEqual(state['date'], '2026-09-28')
            result = self.make_jsonrpc_request(base + '/lista/guardar', {
                'device_key': key, 'pin': '1234', 'date': '2026-09-28', 'project_id': self.site_a.id,
                'entries': [{'employee_id': self.pedro.id, 'fraction': 1}],
            })
            self.assertEqual(result['done'], ['Pedro López'])
            refused = self.make_jsonrpc_request(base + '/lista/guardar', {
                'device_key': key, 'pin': '1234', 'date': '2026-09-28', 'project_id': self.foreign.id,
                'entries': [{'employee_id': self.pedro.id, 'fraction': 1}],
            })
            self.assertEqual(refused, {'error': 'Esa obra no está asignada a ti.'})
            state = self.make_jsonrpc_request(base + '/lista', {'device_key': key})
            self.assertEqual(state['registered'], {str(self.site_a.id): [{'employee_id': self.pedro.id, 'fraction': 1.0}]})
        page = self.url_open(base)
        self.assertIn('Pase de lista', page.text)
