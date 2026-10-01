from datetime import date
from pathlib import Path

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.hr_field_roster import clean_line, normalize, parse_message, split_amount

EXAMPLE = (Path(__file__).parent / 'mensaje_ejemplo.txt').read_text(encoding='utf-8')


@tagged('post_install', '-at_install')
class TestFieldRoster(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.monday = date(2026, 9, 28)
        Project = cls.env['project.project']
        cls.p2316 = Project.create({'name': 'Cuarto Shirrock Danfoss', 'field_code': '2316-26'})
        cls.p2498 = Project.create({'name': '2498-26 Piso Terrepower'})
        cls.course = Project.create({'name': 'Capacitación interna'})
        cls.workshop = Project.create({'name': 'Taller'})
        cls.supervisor = cls.env['hr.employee'].create({
            'name': 'Juan Supervisor', 'field_role': 'supervisor',
            'field_project_ids': [(6, 0, (cls.p2316 | cls.p2498).ids)],
        })
        cls.other_supervisor = cls.env['hr.employee'].create({'name': 'Luis Supervisor', 'field_role': 'supervisor'})
        names = ['Efraín Salazar', 'Jhonatan Peña', 'Carlos Villanueva', 'Antonio Martínez', 'Pablo Reséndiz',
                 'Jassiel Gómez', 'Lorenzo Alvarado', 'Brayan Rivera', 'Ricardo Sánchez']
        cls.people = {
            name.split()[0]: cls.env['hr.employee'].create({'name': name, 'field_role': 'worker'})
            for name in names
        }

    def _wizard(self, message, day=None):
        wizard = self.env['hr.field.roster.import'].create({'message': message, 'date': day or self.monday})
        wizard.action_read_message()
        return wizard

    def _attendance(self, key):
        return self.env['hr.attendance'].search([('employee_id', '=', self.people[key].id)])

    def test_helpers(self):
        self.assertEqual(normalize('  María  Hernández!! '), 'maria hernandez')
        self.assertEqual(clean_line('[28/09/26, 8:15:02] Juan Supervisor: 1. Pedro López'), 'Pedro López')
        self.assertEqual(clean_line('28/09/26, 8:15 - Juan Supervisor: - María'), 'María')
        self.assertEqual(split_amount('Jhonatan peña 1/2 día'), ('Jhonatan peña', 0.5, None))
        self.assertEqual(split_amount('Carlos medio dia'), ('Carlos', 0.5, None))
        self.assertEqual(split_amount('Pedro 4 hrs'), ('Pedro', None, 4.0))
        self.assertEqual(split_amount('Efraín Salazar'), ('Efraín Salazar', None, None))

    def test_parse_real_example(self):
        blocks = parse_message(EXAMPLE)
        self.assertEqual([b['code'] or b['title'] for b in blocks], ['2316-26 _ES', '2498-26 CM', 'Curso hidro', 'taller'])
        self.assertEqual(blocks[0]['location'], 'Danfoss')
        self.assertEqual(blocks[0]['people'][1], ('Jhonatan peña', 0.5, None))
        self.assertEqual(blocks[2]['fraction'], 0.5)
        self.assertEqual(len(blocks[2]['people']), 6)

    def test_load_real_example(self):
        wizard = self._wizard(EXAMPLE)
        blocks = wizard.block_ids
        self.assertEqual(blocks[0].project_id, self.p2316, "Found by project number")
        self.assertEqual(blocks[1].project_id, self.p2498, "Found by the number in the project name")
        self.assertFalse(blocks[2].project_id, "Internal activities need a project the first time")
        self.assertEqual(blocks[3].project_id, self.workshop, "Found by project name")
        self.assertTrue(all(l.employee_id for l in wizard.line_ids), wizard.line_ids.mapped('raw_name'))

        with self.assertRaises(UserError):
            wizard.action_confirm()
        blocks[2].project_id = self.course
        wizard.action_confirm()

        efrain = self._attendance('Efraín')
        self.assertEqual(efrain.field_state, 'valid')
        self.assertAlmostEqual(efrain.worked_hours, 8, delta=0.01)
        self.assertEqual(efrain.field_timesheet_ids.project_id, self.p2316)
        self.assertAlmostEqual(efrain.field_timesheet_ids.unit_amount, 8, delta=0.01)

        jhonatan = self._attendance('Jhonatan')
        self.assertEqual(len(jhonatan), 1, "One attendance per person and day")
        self.assertEqual(jhonatan.field_state, 'valid')
        self.assertAlmostEqual(jhonatan.worked_hours, 8, delta=0.01)
        hours = {l.project_id: l.unit_amount for l in jhonatan.field_timesheet_ids}
        self.assertEqual(set(hours), {self.p2316, self.course})
        self.assertAlmostEqual(hours[self.p2316], 4, delta=0.01)
        self.assertAlmostEqual(hours[self.course], 4, delta=0.01)

        brayan = self._attendance('Brayan')
        self.assertEqual(set(brayan.field_timesheet_ids.project_id), {self.course, self.workshop})

        alias = self.env['hr.field.project.alias'].search([('project_id', '=', self.course.id)])
        self.assertEqual(alias.name, 'curso hidro', "The chosen project is remembered")
        wizard = self._wizard("Curso hidro\n\nEfraín Salazar", day=date(2026, 9, 29))
        self.assertEqual(wizard.block_ids.project_id, self.course)

    def test_borrowed_person_in_two_messages(self):
        self._wizard("PRESUPUESTO: 2316-26 _ES\nPERSONAL:\n\nPablo Reséndiz 1/2 día").action_confirm()
        second = self._wizard("PRESUPUESTO: 2498-26 CM\nPERSONAL:\n\nPablo Reséndiz 1/2 día")
        self.assertIn('se sumará', second.line_ids.warning)
        second.action_confirm()
        pablo = self._attendance('Pablo')
        self.assertEqual(len(pablo), 1)
        self.assertEqual(pablo.field_state, 'valid')
        self.assertEqual(set(pablo.field_timesheet_ids.project_id), {self.p2316, self.p2498})
        self.assertAlmostEqual(pablo.worked_hours, 8, delta=0.01)

    def test_more_than_one_day_goes_to_review(self):
        self._wizard("PRESUPUESTO: 2316-26\nPERSONAL:\n\nJassiel Gómez").action_confirm()
        second = self._wizard("PRESUPUESTO: 2498-26\nPERSONAL:\n\nJassiel Gómez")
        self.assertIn('revisión', second.line_ids.warning)
        second.action_confirm()
        jassiel = self._attendance('Jassiel')
        self.assertEqual(jassiel.field_state, 'review')
        self.assertFalse(jassiel.field_timesheet_ids, "Nothing is booked until Operations approves")
        jassiel.action_field_approve()
        self.assertEqual(len(jassiel.field_timesheet_ids), 2)

    def test_simple_list_and_learned_names(self):
        wizard = self._wizard("2316-26\nLista de hoy:\n1. Efrain Salasar\n2. El Güero")
        self.assertEqual(wizard.block_ids.project_id, self.p2316)
        by_raw = {l.raw_name: l for l in wizard.line_ids}
        self.assertEqual(by_raw['Efrain Salasar'].employee_id, self.people['Efraín'])
        self.assertEqual(by_raw['Efrain Salasar'].match, 'fuzzy')
        self.assertEqual(by_raw['El Güero'].match, 'none')
        by_raw['El Güero'].employee_id = self.people['Ricardo']
        wizard.action_confirm()
        wizard = self._wizard("2316-26\nel guero\nefrain salasar", day=date(2026, 9, 29))
        self.assertEqual(wizard.line_ids.mapped('match'), ['alias', 'alias'])
        self.assertEqual(wizard.line_ids.employee_id, self.people['Ricardo'] | self.people['Efraín'])

    def test_list_without_project_line(self):
        wizard = self._wizard("Efraín Salazar\nPablo Reséndiz")
        self.assertEqual(len(wizard.line_ids), 2, "The first line is a person when it is not a project")
        self.assertFalse(wizard.block_ids.project_id)

    def test_kiosk_attendance_is_not_duplicated(self):
        self.env['hr.attendance'].create({
            'employee_id': self.people['Efraín'].id,
            'check_in': '2026-09-28 14:00:00', 'check_out': '2026-09-28 20:00:00',
            'field_project_id': self.p2316.id, 'field_state': 'valid',
        })
        wizard = self._wizard("PRESUPUESTO: 2498-26\nPERSONAL:\n\nEfraín Salazar")
        self.assertIn('teléfono', wizard.line_ids.warning)
        wizard.action_confirm()
        self.assertEqual(len(self._attendance('Efraín')), 1)

    def test_day_off_goes_to_review(self):
        wizard = self._wizard("PRESUPUESTO: 2316-26\nPERSONAL:\n\nEfraín Salazar", day=date(2026, 9, 27))
        wizard.action_confirm()
        attendance = self._attendance('Efraín')
        self.assertEqual(attendance.field_state, 'review')
        self.assertFalse(attendance.field_timesheet_ids)

    def test_whatsapp_export_format(self):
        wizard = self._wizard(
            "[28/09/26, 8:15:02] Juan Supervisor: PRESUPUESTO: 2316-26 _ES\n"
            "[28/09/26, 8:15:02] Juan Supervisor: PERSONAL:\n"
            "Efraín Salazar\n"
            "Pablo resendiz 1/2 día")
        self.assertEqual(wizard.block_ids.project_id, self.p2316)
        self.assertEqual(wizard.line_ids.employee_id, self.people['Efraín'] | self.people['Pablo'])
        self.assertEqual(wizard.line_ids.mapped('fraction'), [1.0, 0.5])
