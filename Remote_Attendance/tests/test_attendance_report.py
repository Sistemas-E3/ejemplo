import io
from datetime import datetime

from freezegun import freeze_time

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

TZ = 'America/Mexico_City'


@tagged('post_install', '-at_install')
@freeze_time('2026-10-01 18:00:00')  # Thursday, noon in Mexico City
class TestAttendanceReport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Report = cls.env['hr.field.attendance.report']
        # Only the employees of the test, whatever the database already has.
        cls.env['hr.employee'].search([]).write({'active': False})
        cls.company_b = cls.env['res.company'].create({'name': 'Empresa B'})
        cls.env.user.company_ids |= cls.company_b
        cls.dept = cls.env['hr.department'].create({'name': 'Obra civil'})
        cls.ana = cls.env['hr.employee'].create({'name': 'Ana Puntual', 'tz': TZ, 'barcode': '0101',
                                                 'department_id': cls.dept.id, 'resource_calendar_id': False})
        cls.beto = cls.env['hr.employee'].create({'name': 'Beto Tarde', 'tz': TZ, 'barcode': '0102',
                                                  'field_role': 'worker', 'resource_calendar_id': False})
        cls.carla = cls.env['hr.employee'].create({'name': 'Carla Otra Empresa', 'tz': TZ, 'barcode': '0103',
                                                   'company_id': cls.company_b.id, 'resource_calendar_id': False})
        employees = cls.ana | cls.beto | cls.carla
        cls.env.cr.execute("UPDATE hr_employee SET create_date = '2026-01-01' WHERE id IN %s", [tuple(employees.ids)])
        employees.invalidate_recordset(['create_date'])
        Attendance = cls.env['hr.attendance']
        # Monday 28: Ana 07:05-17:00, Beto 07:40-17:00 (late). Tuesday 29: only Ana. Wednesday 30: nobody.
        Attendance.create([
            {'employee_id': cls.ana.id, 'check_in': datetime(2026, 9, 28, 13, 5), 'check_out': datetime(2026, 9, 28, 23)},
            {'employee_id': cls.beto.id, 'check_in': datetime(2026, 9, 28, 13, 40), 'check_out': datetime(2026, 9, 28, 23),
             'field_state': 'valid'},
            {'employee_id': cls.ana.id, 'check_in': datetime(2026, 9, 29, 13), 'check_out': datetime(2026, 9, 29, 23)},
        ])
        cls.filters = {'date_from': '2026-09-28', 'date_to': '2026-09-30'}

    def _row(self, data, employee):
        return next(r for r in data['rows'] if r['id'] == employee.id)

    def test_codes_and_totals(self):
        data = self.Report.get_report(dict(self.filters))
        ana, beto = self._row(data, self.ana), self._row(data, self.beto)
        self.assertEqual([c['code'] for c in ana['cells']], ['A', 'A', 'F'])
        self.assertEqual([c['code'] for c in beto['cells']], ['R', 'F', 'F'])
        self.assertEqual(ana['cells'][0]['in'], '07:05')
        self.assertEqual(ana['totals']['hours'], 17.92, "8:55 + 9:00, without the lunch hour")
        self.assertEqual((beto['totals']['A'], beto['totals']['R'], beto['totals']['F']), (1, 1, 2))
        self.assertTrue(beto['cells'][0]['flag'], "A field day without project is to be checked")
        self.assertEqual(data['day_totals'][0], {'present': 3 - 1, 'absent': 1})

    def test_future_days_are_not_absences(self):
        data = self.Report.get_report({'date_from': '2026-10-01', 'date_to': '2026-10-03'})
        self.assertEqual({c['code'] for r in data['rows'] for c in r['cells']}, {''})

    def test_filters_company_department_scope_search(self):
        ids = lambda f: {r['id'] for r in self.Report.get_report(dict(self.filters, **f))['rows']}
        self.assertEqual(ids({'company_id': self.company_b.id}), {self.carla.id})
        self.assertEqual(ids({'department_id': self.dept.id}), {self.ana.id})
        self.assertEqual(ids({'scope': 'field'}), {self.beto.id})
        self.assertNotIn(self.beto.id, ids({'scope': 'office'}))
        self.assertEqual(ids({'search': '0102'}), {self.beto.id})

    def test_one_year_maximum(self):
        self.Report.get_report({'date_from': '2025-10-02', 'date_to': '2026-10-01', 'search': 'Ana'})
        with self.assertRaises(UserError):
            self.Report.get_report({'date_from': '2025-01-01', 'date_to': '2026-10-01'})

    def test_excel_and_pdf(self):
        import openpyxl
        content, name = self.Report.excel_file(dict(self.filters, search='Ana'))
        self.assertTrue(name.endswith('.xlsx'))
        sheet = openpyxl.load_workbook(io.BytesIO(content)).worksheets[0]
        self.assertEqual(sheet.cell(5, 2).value, 'Ana Puntual')
        self.assertEqual([sheet.cell(5, c).value for c in (6, 7, 8)], ['A', 'A', 'F'])
        html, _type = self.env['ir.actions.report']._render_qweb_html(
            'Remote_Attendance.attendance_report_pdf', [], data={'filters': dict(self.filters, search='Ana')})
        self.assertIn(b'Ana Puntual', html)
        self.assertIn(b'Reporte de asistencia del 28/09/2026', html)
