import json
from datetime import datetime, time, timedelta

import pytz
from freezegun import freeze_time

from odoo.tests import HttpCase, TransactionCase, tagged

# Monday 28/09/2026 in Mexico City (UTC-6).
DAY = datetime(2026, 9, 28).date()
TZ = 'America/Mexico_City'


def vector(base):
    return [base] + [0.0] * 127


def utc(hour, minute=0):
    local = pytz.timezone(TZ).localize(datetime.combine(DAY, time(hour, minute)))
    return local.astimezone(pytz.utc).replace(tzinfo=None)


def iso(moment):
    return moment.isoformat() + 'Z'


@tagged('post_install', '-at_install')
class TestFieldOffline(TransactionCase):
    """Punches taken on a phone without signal and sent when the signal is back."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env['hr.field.service'].sudo()
        cls.project_a = cls.env['project.project'].create({
            'name': 'Proyecto A', 'field_latitude': 19.4326, 'field_longitude': -99.1332, 'field_radius': 200,
        })
        cls.project_b = cls.env['project.project'].create({
            'name': 'Proyecto B', 'field_latitude': 19.5, 'field_longitude': -99.2, 'field_radius': 200,
        })
        cls.supervisor = cls.env['hr.employee'].create({
            'name': 'Juan Supervisor', 'field_role': 'supervisor', 'tz': TZ, 'pin': '1234',
            'field_project_ids': [(6, 0, (cls.project_a | cls.project_b).ids)],
        })
        cls.worker = cls.env['hr.employee'].create({
            'name': 'Pedro Trabajador', 'field_role': 'worker', 'tz': TZ,
            'field_supervisor_id': cls.supervisor.id,
        })
        cls.env['hr.field.face'].create({'employee_id': cls.worker.id, 'descriptor': str(vector(1.0))})
        cls.supervisor.action_field_regenerate_token()
        cls.device, _key = cls.env['hr.field.device']._field_register(cls.supervisor, 'Tel', 'test')
        cls.device.action_approve()
        with freeze_time(utc(6, 30)):
            cls.service._activate_projects(cls.supervisor, '1234', (cls.project_a | cls.project_b).ids)

    def _sync(self, punches, phone_now=None):
        return self.service._sync(self.supervisor, self.device, punches, phone_now=phone_now)['results']

    def _punch(self, uuid, moment, **kwargs):
        values = {'uuid': uuid, 'time': iso(moment), 'descriptor': vector(1.0),
                  'latitude': 19.4326, 'longitude': -99.1332}
        values.update(kwargs)
        return values

    def _attendances(self):
        return self.env['hr.attendance'].search([('employee_id', '=', self.worker.id)], order='check_in')

    def test_day_marked_without_signal(self):
        punches = [
            # Sent out of order: they are replayed by the time they were taken.
            self._punch('d', utc(17, 0), kind='out'),
            self._punch('a', utc(7, 0), kind='in', project_id=self.project_a.id),
            self._punch('c', utc(13, 0), kind='lunch_in', project_id=self.project_a.id),
            self._punch('b', utc(12, 0), kind='lunch_out'),
        ]
        with freeze_time(utc(18, 0)):
            results = self._sync(punches, phone_now=iso(utc(18, 0)))
        self.assertEqual([r['uuid'] for r in results], ['a', 'b', 'c', 'd'])
        self.assertEqual([r['state'] for r in results], ['done'] * 4)
        self.assertEqual(results[0]['employee'], 'Pedro Trabajador')
        morning, afternoon = self._attendances()
        self.assertEqual((morning.check_in, morning.check_out), (utc(7, 0), utc(12, 0)))
        self.assertEqual((afternoon.check_in, afternoon.check_out), (utc(13, 0), utc(17, 0)))
        self.assertEqual(afternoon.field_project_id, self.project_a)
        self.assertEqual(sum((morning | afternoon).field_timesheet_ids.mapped('unit_amount')), 9)
        log = self.env['hr.field.offline.punch'].search([('uuid', '=', 'a')])
        self.assertEqual((log.attendance_id, log.employee_id), (morning, self.worker))

        # Out of place (an exit with nothing open): reported, not lost.
        with freeze_time(utc(18, 30)):
            result = self._sync([self._punch('e', utc(18, 10), kind='out')])[0]
        self.assertEqual(result['state'], 'error')
        self.assertIn('no tiene entrada abierta', result['detail'])

    def test_resend_is_stored_once(self):
        punch = self._punch('same', utc(7, 0), kind='in', project_id=self.project_a.id)
        with freeze_time(utc(9, 0)):
            first = self._sync([punch])
            second = self._sync([punch])
        self.assertEqual(first, second)
        self.assertEqual(len(self._attendances()), 1)
        self.assertEqual(self.env['hr.field.offline.punch'].search_count([('uuid', '=', 'same')]), 1)

    def test_wrong_phone_clock_is_corrected(self):
        # The phone runs one hour behind: it says 06:00 at 07:00 and 16:00 when it is 17:00.
        punches = [
            self._punch('in', utc(6, 0), kind='in', project_id=self.project_a.id),
        ]
        with freeze_time(utc(17, 0)):
            self._sync(punches, phone_now=iso(utc(16, 0)))
        self.assertEqual(self._attendances().check_in, utc(7, 0))
        log = self.env['hr.field.offline.punch'].search([('uuid', '=', 'in')])
        self.assertEqual(log.phone_time, utc(6, 0))
        self.assertEqual(log.punch_time, utc(7, 0))

    def test_late_exit_goes_to_validation(self):
        punches = [
            self._punch('in', utc(7, 0), kind='in', project_id=self.project_b.id),
            self._punch('out', utc(19, 0), kind='out'),
        ]
        with freeze_time(utc(20, 0)):
            results = self._sync(punches)
        self.assertEqual([r['state'] for r in results], ['done', 'done'])
        attendance = self._attendances()
        self.assertEqual(attendance.field_overtime_state, 'pending', "Nobody could answer: HR validates it")
        self.assertEqual(attendance.field_state, 'review', "Project B is far from where the phone was")

    def test_errors_are_kept(self):
        punches = [
            self._punch('stranger', utc(7, 0), kind='in', descriptor=vector(9.0), project_id=self.project_a.id),
            self._punch('no-project', utc(7, 5), kind='in'),
            self._punch('old', utc(7, 0) - timedelta(days=10), kind='in', project_id=self.project_a.id),
        ]
        with freeze_time(utc(8, 0)):
            results = {r['uuid']: r for r in self._sync(punches)}
        self.assertEqual({r['state'] for r in results.values()}, {'error'})
        self.assertIn('proyecto', results['no-project']['detail'])
        self.assertIn('días', results['old']['detail'])
        self.assertFalse(self._attendances())

    def test_project_no_longer_active(self):
        # Marked on Monday with the project active then; sent on Tuesday, when the supervisor activated
        # nothing yet: it still counts.
        punch = self._punch('monday', utc(7, 0), kind='in', project_id=self.project_b.id)
        with freeze_time(utc(7, 0) + timedelta(days=1)):
            self.assertEqual(self._sync([punch])[0]['state'], 'done')
        self.assertEqual(self._attendances().field_project_id, self.project_b)
        self.assertEqual(self._attendances().field_date, DAY)

    def test_office_staff_offline(self):
        staff = self.env['hr.employee'].create({'name': 'Laura Oficina', 'field_role': 'staff', 'tz': TZ})
        self.env['hr.field.face'].create({'employee_id': staff.id, 'descriptor': str(vector(3.0))})
        staff.action_field_regenerate_token()
        device, _key = self.env['hr.field.device']._field_register(staff, 'Tel', 'test')
        device.action_approve()
        punches = [{'uuid': 'in', 'time': iso(utc(9, 0)), 'descriptor': vector(3.0)},
                   {'uuid': 'out', 'time': iso(utc(18, 0)), 'descriptor': vector(3.0)}]
        with freeze_time(utc(19, 0)):
            results = self.service._sync(staff, device, punches)['results']
        self.assertEqual([r['state'] for r in results], ['done', 'done'])
        attendance = self.env['hr.attendance'].search([('employee_id', '=', staff.id)])
        self.assertEqual((attendance.check_in, attendance.check_out), (utc(9, 0), utc(18, 0)))


@tagged('post_install', '-at_install')
class TestFieldOfflineRoutes(HttpCase):

    def test_app_and_sync_routes(self):
        supervisor = self.env['hr.employee'].create({'name': 'Juan', 'field_role': 'supervisor'})
        supervisor.action_field_regenerate_token()
        base = '/campo/' + supervisor.field_token
        page = self.url_open(base)
        self.assertIn('app.webmanifest', page.text)
        self.assertIn('field_offline.js', page.text)
        manifest = json.loads(self.url_open(base + '/app.webmanifest').text)
        self.assertEqual(manifest['start_url'], base)
        worker = self.url_open('/campo/sw.js')
        self.assertEqual(worker.status_code, 200)
        self.assertIn('caches', worker.text)
        self.assertEqual(worker.headers['Service-Worker-Allowed'], '/campo/')
        self.assertEqual(self.url_open('/campo/nope/app.webmanifest').status_code, 404)

        key = self.make_jsonrpc_request(base + '/register', {'name': 'Tel'})['device_key']
        result = self.make_jsonrpc_request(base + '/sincronizar', {'device_key': key, 'punches': []})
        self.assertEqual(result['error'], 'Teléfono no autorizado.', "A phone not approved cannot send")
        supervisor.field_device_ids.action_approve()
        result = self.make_jsonrpc_request(base + '/sincronizar', {
            'device_key': key, 'punches': [{'uuid': 'x', 'time': 'nonsense', 'descriptor': vector(5.0)}]})
        self.assertEqual(result['results'][0]['state'], 'error')
