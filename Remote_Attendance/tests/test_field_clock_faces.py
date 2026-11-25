import base64
import io
import os
from datetime import datetime, timedelta

from PIL import Image

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


def photo(color=(180, 140, 120), size=(900, 1200)):
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue())


def vector(base):
    return str([base] + [0.0] * 127)


@tagged('post_install', '-at_install')
class TestFieldClockFaces(TransactionCase):
    """Faces registered with the phone go to the visible light clocks (MB10-VL) as a photo."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ops = cls.env['hr.department'].create({'name': 'Operaciones'})
        cls.office = cls.env['hr.department'].create({'name': 'Oficina'})
        cls.pedro = cls.env['hr.employee'].create({'name': 'Pedro López', 'barcode': '1042', 'department_id': cls.ops.id})
        cls.rosa = cls.env['hr.employee'].create({'name': 'Rosa Pérez', 'barcode': '3001', 'department_id': cls.office.id})
        Clock = cls.env['hr.field.clock']
        cls.shop = Clock.create({'name': 'Taller', 'serial': 'MB10-1', 'state': 'approved',
                                 'department_ids': [(6, 0, cls.ops.ids)]})
        cls.desk = Clock.create({'name': 'Oficina', 'serial': 'MB10-2', 'state': 'approved',
                                 'department_ids': [(6, 0, cls.office.ids)]})
        cls.new = Clock.create({'name': 'Nuevo', 'serial': 'MB10-3'})

    def _commands(self, clock):
        return clock.command_ids.sorted('id')

    def _photo_bytes(self, command):
        fields = dict(part.split('=', 1) for part in command.split('\t') if '=' in part)
        content = fields['Content']
        self.assertEqual(int(fields['Size']), len(content))
        return base64.b64decode(content)

    def test_face_registered_with_phone_goes_to_its_clocks(self):
        self.env['hr.field.face'].create([
            {'employee_id': self.pedro.id, 'descriptor': vector(1.0), 'image': photo()},
            {'employee_id': self.pedro.id, 'descriptor': vector(1.1), 'image': photo()},
        ])
        user, face = self._commands(self.shop)
        self.assertTrue(user.command.startswith('DATA UPDATE USERINFO PIN=1042\t'))
        self.assertTrue(face.command.startswith('DATA UPDATE BIOPHOTO PIN=1042\tType=9\t'))
        self.assertEqual(face.summary, "Rostro desde foto, usuario 1042")
        jpeg = self._photo_bytes(face.command)
        self.assertEqual(jpeg[:2], b'\xff\xd8', "JPEG photo")
        self.assertLessEqual(len(jpeg), 40 * 1024)
        width, height = Image.open(io.BytesIO(jpeg)).size
        self.assertLessEqual(max(width, height), 480)
        self.assertFalse(self.desk.command_ids, "Pedro does not punch at the office clock")
        self.assertFalse(self.new.command_ids, "Clocks not approved get nothing")

    def test_face_without_photo_is_not_sent(self):
        self.env['hr.field.face'].create({'employee_id': self.pedro.id, 'descriptor': vector(1.0)})
        self.assertFalse(self.shop.command_ids)

    def test_send_faces_uses_phone_photo_or_employee_photo(self):
        self.rosa.image_1920 = photo((90, 90, 200))
        self.desk.department_ids = False  # everybody punches here
        result = self.desk.action_send_faces()
        commands = self.desk.command_ids.mapped('command')
        self.assertTrue(any(c.startswith('DATA UPDATE BIOPHOTO PIN=3001\t') for c in commands),
                        "Rosa is sent with her employee photo")
        self.assertFalse(any('PIN=1042\t' in c for c in commands if 'BIOPHOTO' in c))
        self.assertIn('Pedro López', result['params']['message'], "People without photo are listed")
        with self.assertRaises(UserError):
            self.new.action_send_faces()

    def test_photos_are_sent_in_small_batches(self):
        noise = io.BytesIO()
        Image.frombytes('RGB', (480, 480), os.urandom(480 * 480 * 3)).save(noise, format='PNG')
        self.pedro.image_1920 = base64.b64encode(noise.getvalue())
        command = self.shop._clock_face_command(self.pedro)
        self.shop._clock_queue([command] * 12)
        first = self.shop._clock_next_commands()
        sent = self.shop.command_ids.filtered(lambda c: c.state == 'sent')
        self.assertTrue(0 < len(sent) < 12, "Big orders are split across connections")
        self.assertLessEqual(len(first), 150 * 1024 + len(command) + 64)
        self.shop._clock_next_commands()
        self.assertGreater(len(self.shop.command_ids.filtered(lambda c: c.state == 'sent')), len(sent))

    def test_allowed_ips(self):
        Clock = self.env['hr.field.clock']
        self.assertEqual(Clock._clock_seen('MB10-1', '8.8.8.8'), self.shop, "No IP set: any IP")
        self.shop.allowed_ips = '201.150.10.20, 189.0.0.0/16'
        self.assertEqual(Clock._clock_seen('MB10-1', '201.150.10.20'), self.shop)
        self.assertEqual(Clock._clock_seen('MB10-1', '189.0.44.7'), self.shop)
        self.assertFalse(Clock._clock_seen('MB10-1', '8.8.8.8'), "Someone else with the serial number is refused")
        self.assertFalse(Clock._clock_seen('MB10-1', None))
        with self.assertRaises(ValidationError):
            self.shop.allowed_ips = 'oficina'
        self.shop.allowed_ips = False
        self.shop.last_ip = '201.150.10.20'
        self.shop.action_use_last_ip()
        self.assertEqual(self.shop.allowed_ips, '201.150.10.20')

    def test_photo_removed_once_the_clock_answers(self):
        self.pedro.image_1920 = photo()
        self.shop._clock_queue([self.shop._clock_face_command(self.pedro)])
        command = self.shop.command_ids
        self.shop._clock_next_commands()
        self.shop._clock_command_results(f"ID={command.id}&Return=0&CMD=DATA")
        self.assertEqual(command.state, 'done')
        self.assertNotIn('Content=', command.command, "No copy of the face photo is kept")
        self.assertEqual(command.summary, "Rostro desde foto, usuario 1042")

    def test_punches_in_the_future_and_huge_requests_are_ignored(self):
        future = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        self.shop._clock_receive(f"1042\t{future}\t0\t15\n")
        self.assertFalse(self.env['hr.field.clock.log'].search([('clock_id', '=', self.shop.id)]))
        body = "".join(f"1042\t2026-09-28 07:{m % 60:02d}:{m // 60:02d}\t0\t15\n" for m in range(1500))
        self.shop._clock_receive(body)
        self.assertLessEqual(self.env['hr.field.clock.log'].search_count([('clock_id', '=', self.shop.id)]), 1000)
