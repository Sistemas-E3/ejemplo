from datetime import date

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.hr_field_roster import clean_line, normalize


@tagged('post_install', '-at_install')
class TestFieldRoster(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.monday = date(2026, 9, 28)
        cls.project = cls.env['project.project'].create({'name': 'Torre Norte', 'field_code': '1234'})
        cls.project_2 = cls.env['project.project'].create({'name': 'Plaza Sur', 'field_code': '5678'})
        cls.supervisor = cls.env['hr.employee'].create({
            'name': 'Juan Supervisor', 'field_role': 'supervisor',
            'field_project_ids': [(6, 0, cls.project.ids)],
        })
        cls.other_supervisor = cls.env['hr.employee'].create({'name': 'Luis Supervisor', 'field_role': 'supervisor'})
        Employee = cls.env['hr.employee']
        cls.pedro = Employee.create({'name': 'Pedro López García', 'field_role': 'worker', 'field_supervisor_id': cls.supervisor.id})
        cls.maria = Employee.create({'name': 'María Hernández', 'field_role': 'worker', 'field_supervisor_id': cls.supervisor.id})
        cls.jose = Employee.create({'name': 'José Ramírez', 'field_role': 'worker', 'field_supervisor_id': cls.other_supervisor.id})

    def _wizard(self, message, day=None):
        return self.env['hr.field.roster.import'].create({'message': message, 'date': day or self.monday})

    def test_helpers(self):
        self.assertEqual(normalize('  María  Hernández!! '), 'maria hernandez')
        self.assertEqual(clean_line('[28/09/26, 8:15:02] Juan Supervisor: 1. Pedro López'), 'Pedro López')
        self.assertEqual(clean_line('28/09/26, 8:15 - Juan Supervisor: - María'), 'María')
        self.assertEqual(clean_line('3) José'), 'José')

    def test_parse_and_register(self):
        wizard = self._wizard("1234 Torre Norte\nLista de hoy:\n1. Pedro Lopez\n2. maria hernandez\n3. Jose Ramires\n4. Fulano Desconocido")
        wizard.action_read_message()
        self.assertEqual(wizard.project_id, self.project)
        self.assertEqual(wizard.supervisor_id, self.supervisor, "Only supervisor of the project is proposed")
        by_raw = {l.raw_name: l for l in wizard.line_ids}
        self.assertEqual(set(by_raw), {'Pedro Lopez', 'maria hernandez', 'Jose Ramires', 'Fulano Desconocido'})
        self.assertEqual(by_raw['Pedro Lopez'].employee_id, self.pedro)
        self.assertEqual(by_raw['Pedro Lopez'].match, 'exact')
        self.assertEqual(by_raw['maria hernandez'].employee_id, self.maria)
        self.assertEqual(by_raw['Jose Ramires'].employee_id, self.jose, "Borrowed people from other crews are found")
        self.assertEqual(by_raw['Jose Ramires'].match, 'fuzzy')
        self.assertEqual(by_raw['Fulano Desconocido'].match, 'none')

        with self.assertRaises(UserError):
            wizard.action_confirm()
        by_raw['Fulano Desconocido'].include = False
        wizard.action_confirm()

        attendances = self.env['hr.attendance'].search([('field_project_id', '=', self.project.id)])
        self.assertEqual(attendances.employee_id, self.pedro | self.maria | self.jose)
        for attendance in attendances:
            self.assertEqual(attendance.date, self.monday)
            self.assertEqual(attendance.field_state, 'valid')
            self.assertEqual(attendance.field_supervisor_id, self.supervisor)
            self.assertTrue(attendance.check_out)
            self.assertAlmostEqual(attendance.worked_hours, 8, delta=0.01)
            self.assertEqual(attendance.field_timesheet_id.project_id, self.project)
            self.assertAlmostEqual(attendance.field_timesheet_id.unit_amount, attendance.worked_hours)
        alias = self.env['hr.field.alias'].search([('employee_id', '=', self.jose.id)])
        self.assertEqual(alias.name, 'jose ramires', "Confirmed fuzzy names are remembered")

    def test_alias_is_reused(self):
        self.env['hr.field.alias'].create({'name': 'El Güero', 'employee_id': self.pedro.id})
        wizard = self._wizard("Obra 5678\nel guero")
        wizard.action_read_message()
        self.assertEqual(wizard.project_id, self.project_2)
        self.assertEqual(wizard.line_ids.employee_id, self.pedro)
        self.assertEqual(wizard.line_ids.match, 'alias')

    def test_corrected_name_is_learned(self):
        wizard = self._wizard("1234\nPeter")
        wizard.action_read_message()
        self.assertFalse(wizard.line_ids.employee_id)
        wizard.line_ids.employee_id = self.pedro
        wizard.action_confirm()
        wizard = self._wizard("1234\npeter", day=date(2026, 9, 29))
        wizard.action_read_message()
        self.assertEqual(wizard.line_ids.employee_id, self.pedro)

    def test_person_in_two_lists_is_not_double_counted(self):
        first = self._wizard("1234 Torre Norte\nJosé Ramírez")
        first.action_read_message()
        first.action_confirm()
        second = self._wizard("5678 Plaza Sur\nJosé Ramírez")
        second.action_read_message()
        self.assertTrue(second.line_ids.conflict)
        self.assertIn('Torre Norte', second.line_ids.conflict)
        second.action_confirm()
        attendances = self.env['hr.attendance'].search([('employee_id', '=', self.jose.id)])
        self.assertEqual(len(attendances), 1)
        self.assertEqual(attendances.field_project_id, self.project)

    def test_duplicate_names_in_message(self):
        wizard = self._wizard("1234\nPedro Lopez\nPedro López")
        wizard.action_read_message()
        self.assertEqual(wizard.line_ids.filtered('include').employee_id, self.pedro)
        self.assertEqual(len(wizard.line_ids.filtered('include')), 1)

    def test_day_off_goes_to_review(self):
        sunday = date(2026, 9, 27)
        wizard = self._wizard("1234\nPedro Lopez", day=sunday)
        wizard.action_read_message()
        wizard.action_confirm()
        attendance = self.env['hr.attendance'].search([('employee_id', '=', self.pedro.id)])
        self.assertEqual(attendance.field_state, 'review')
        self.assertFalse(attendance.field_timesheet_id)

    def test_whatsapp_export_format(self):
        wizard = self._wizard(
            "[28/09/26, 8:15:02] Juan Supervisor: 1234 Torre Norte\n"
            "[28/09/26, 8:15:40] Juan Supervisor: Pedro López\n"
            "María Hernández")
        wizard.action_read_message()
        self.assertEqual(wizard.project_id, self.project)
        self.assertEqual(wizard.line_ids.employee_id, self.pedro | self.maria)
