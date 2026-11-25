import base64
import ipaddress
import logging
from datetime import datetime, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.addons.base.models.res_partner import _tz_get
from odoo.tools.image import image_process

from .hr_field_service import REPEAT_MINUTES

_logger = logging.getLogger(__name__)

MAX_PENDING_CLOCKS = 5
# A clock sends a few punches at a time; a bigger request is not a real clock.
CLOCK_MAX_LINES = 1000
# Punches ahead of the server clock (wrong date on the clock or forged) are not taken.
CLOCK_FUTURE_TOLERANCE = timedelta(minutes=10)
# Face photo for visible light clocks (ZKTeco BIOPHOTO, type 9): small JPEG, sent as base64.
CLOCK_PHOTO_SIZE = (480, 480)
CLOCK_PHOTO_MAX_BYTES = 40 * 1024
# The clock downloads its orders in one answer: keep it small when it carries photos.
CLOCK_BATCH_CHARS = 150 * 1024


class HrFieldClock(models.Model):
    """A ZKTeco time clock that sends its punches to Odoo by itself (ADMS / PUSH protocol)."""
    _name = 'hr.field.clock'
    _description = "Checador (asistencias en campo)"
    _inherit = ['mail.thread']
    _order = 'state desc, name'

    name = fields.Char("Checador", required=True, default="Checador", tracking=True)
    serial = fields.Char("Número de serie", required=True, copy=False, index=True, tracking=True,
                         help="El S/N que aparece en el checador (Menú › Información del sistema).")
    state = fields.Selection([
        ('pending', "Pendiente de aprobar"),
        ('approved', "Aprobado"),
        ('revoked', "Revocado"),
    ], default='pending', required=True, tracking=True)
    tz = fields.Selection(_tz_get, string="Zona horaria", required=True, default='America/Mexico_City',
                          help="La hora que muestra el checador se interpreta en esta zona.")
    department_ids = fields.Many2many(
        'hr.department', 'hr_field_clock_department_rel', 'clock_id', 'department_id',
        string="Departamentos que checan aquí", tracking=True,
        help="Vacío = todos. Con departamentos, solo su personal (y el de sus subdepartamentos) se manda a "
             "este checador y sus marcajes cuentan; los demás quedan como ignorados.")
    allowed_ips = fields.Char(
        "IPs permitidas", tracking=True,
        help="IP pública desde la que se conecta este checador (la de la oficina), o varias separadas por coma; "
             "también rangos como 201.150.10.0/24. Vacío = cualquier IP: el checador solo se identifica con su "
             "número de serie, así que con esto nadie más puede mandar marcajes ni recibir sus órdenes.")
    last_seen = fields.Datetime("Última conexión", readonly=True)
    last_ip = fields.Char("Última IP", readonly=True)
    att_stamp = fields.Char(readonly=True, copy=False)
    log_ids = fields.One2many('hr.field.clock.log', 'clock_id', string="Registros")
    command_ids = fields.One2many('hr.field.clock.command', 'clock_id', string="Órdenes")
    pending_command_count = fields.Integer("Órdenes por enviar", compute='_compute_log_count')
    log_count = fields.Integer("Número de registros", compute='_compute_log_count')
    error_count = fields.Integer("Con error", compute='_compute_log_count')

    _serial_unique = models.Constraint('unique(serial)', "Ya hay un checador con ese número de serie.")

    def _compute_log_count(self):
        Log = self.env['hr.field.clock.log']
        for clock in self:
            clock.log_count = Log.search_count([('clock_id', '=', clock.id)])
            clock.error_count = Log.search_count([('clock_id', '=', clock.id), ('state', '=', 'error')])
            clock.pending_command_count = len(clock.command_ids.filtered(lambda c: c.state in ('pending', 'sent')))

    @api.constrains('allowed_ips')
    def _check_allowed_ips(self):
        for clock in self:
            for item in clock._clock_ip_list():
                try:
                    ipaddress.ip_network(item, strict=False)
                except ValueError:
                    raise ValidationError(_("«%s» no es una IP ni un rango válido.", item))

    def _clock_ip_list(self):
        self.ensure_one()
        return [item.strip() for item in (self.allowed_ips or '').replace(';', ',').replace(' ', ',').split(',')
                if item.strip()]

    def _clock_ip_allowed(self, ip):
        """True when the request comes from one of the allowed IPs (none set = any IP)."""
        self.ensure_one()
        networks = self._clock_ip_list()
        if not networks:
            return True
        try:
            address = ipaddress.ip_address((ip or '').strip())
        except ValueError:
            return False
        return any(address in ipaddress.ip_network(net, strict=False) for net in networks)

    def action_use_last_ip(self):
        for clock in self.filtered('last_ip'):
            clock.allowed_ips = clock.last_ip

    def action_approve(self):
        self.write({'state': 'approved'})

    def action_revoke(self):
        self.write({'state': 'revoked'})

    def action_view_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('Remote_Attendance.hr_field_clock_log_action')
        action['domain'] = [('clock_id', '=', self.id)]
        return action

    def _clock_users(self):
        """Employees that should be on the clocks: active, with a registration number or badge."""
        domain = ['|', ('field_role', '=', False), ('field_role', '!=', 'office')]
        if len(self) == 1 and self.department_ids:
            domain.append(('department_id', 'child_of', self.department_ids.ids))
        employees = self.env['hr.employee'].sudo().search(domain)
        return employees.filtered(lambda e: self._clock_pin(e))

    def _clock_allows(self, employee):
        """True when ``employee`` may punch at this clock (by its departments; none set = everybody)."""
        self.ensure_one()
        if not self.department_ids:
            return True
        allowed = self.env['hr.department'].sudo().search([('id', 'child_of', self.department_ids.ids)])
        return employee.department_id in allowed

    def action_send_employees(self):
        """Create or update every active employee on the clock (number and name). Faces and fingerprints
        are still registered on the clock itself."""
        total = 0
        for clock in self:
            employees = clock._clock_users()
            total += len(employees)
            clock._clock_queue([clock._clock_user_command(e) for e in employees])
        return self._clock_notify(_("Se mandarán %s empleados al checador en su siguiente conexión.", total))

    def action_send_faces(self):
        """Register the faces on visible light clocks (MB10-VL...) from a photo: the photo taken with the
        phone when the face was registered in Odoo, or the employee's photo. The clock builds its own face
        template from it, so nobody has to enroll on the clock."""
        total, missing = 0, self.env['hr.employee']
        for clock in self:
            if clock.state != 'approved':
                raise UserError(_("Aprueba el checador %s antes de mandarle rostros.", clock.name))
            commands = []
            for employee in clock._clock_users():
                command = clock._clock_face_command(employee)
                if command:
                    commands += [clock._clock_user_command(employee), command]
                    total += 1
                else:
                    missing |= employee
            clock._clock_queue(commands)
        message = _("Se mandarán %s rostros en la siguiente conexión del checador.", total)
        if missing:
            message += " " + _("Sin foto (%(count)s): %(names)s.", count=len(missing),
                               names=", ".join(missing[:10].mapped('name')) + ("…" if len(missing) > 10 else ""))
        return self._clock_notify(message)

    @api.model
    def _clock_face_photo(self, employee):
        """Best photo for the clock: the latest one taken when registering the face, else the employee's
        photo. JPEG, small enough for the clock. None when there is no photo."""
        face = self.env['hr.field.face'].sudo().search(
            [('employee_id', '=', employee.id), ('image', '!=', False)], order='id desc', limit=1)
        source = face.image or employee.sudo().image_1920
        if not source:
            return None
        raw = base64.b64decode(source)
        if raw.lstrip()[:1] == b'<':
            # Odoo's generated avatar (initials in an SVG): not a face.
            return None
        photo = None
        for quality in (85, 75, 65, 55):
            try:
                photo = image_process(raw, size=CLOCK_PHOTO_SIZE, quality=quality, output_format='JPEG')
            except (UserError, ValueError):
                return None
            if len(photo) <= CLOCK_PHOTO_MAX_BYTES:
                break
        return photo if photo and photo[:2] == b'\xff\xd8' else None

    def _clock_face_command(self, employee):
        """ADMS order that registers ``employee``'s face on the clock from a photo (None without photo)."""
        pin = self._clock_pin(employee)
        photo = pin and self._clock_face_photo(employee)
        if not photo:
            return None
        content = base64.b64encode(photo).decode()
        return (f"DATA UPDATE BIOPHOTO PIN={pin}\tType=9\tSize={len(content)}\tContent={content}"
                f"\tFormat=0\tUrl=\tPostBackTmpFlag=0")

    def _clock_send_face(self, employee):
        """Send a newly registered face to every approved clock where the employee punches."""
        clocks = self.sudo().search([('state', '=', 'approved')]).filtered(lambda c: c._clock_allows(employee))
        for clock in clocks:
            command = clock._clock_face_command(employee)
            if command:
                clock._clock_queue([clock._clock_user_command(employee), command])

    def action_remove_archived(self):
        """Delete from the clock the employees archived in Odoo."""
        archived = self.env['hr.employee'].sudo().with_context(active_test=False).search(
            [('active', '=', False)]).filtered(lambda e: self._clock_pin(e))
        for clock in self:
            clock._clock_queue([f"DATA DELETE USERINFO PIN={self._clock_pin(e)}" for e in archived])
        return self._clock_notify(_("Se quitarán %s empleados dados de baja del checador.", len(archived)))

    @api.model
    def _clock_pin(self, employee):
        return ''.join(ch for ch in (employee.field_key or '') if ch.isalnum())[:24]

    @api.model
    def _clock_user_command(self, employee):
        name = ' '.join((employee.name or '').split())[:24]
        return f"DATA UPDATE USERINFO PIN={self._clock_pin(employee)}\tName={name}\tPri=0\tPasswd=\tCard=\tGrp=1\tTZ=0000000100000000\tVerify=0"

    def _clock_queue(self, commands):
        self.ensure_one()
        self.env['hr.field.clock.command'].sudo().create(
            [{'clock_id': self.id, 'command': command} for command in commands])

    def _clock_notify(self, message):
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'message': message, 'type': 'success'}}

    def _clock_next_commands(self, limit=50):
        """Commands for the clock's /iclock/getrequest, marked as sent."""
        self.ensure_one()
        pending = self.env['hr.field.clock.command'].sudo().search(
            [('clock_id', '=', self.id), ('state', '=', 'pending')], order='id', limit=limit)
        commands, size = pending.browse(), 0
        for command in pending:
            if commands and size + len(command.command) > CLOCK_BATCH_CHARS:
                break
            commands |= command
            size += len(command.command)
        commands.write({'state': 'sent', 'sent_on': fields.Datetime.now()})
        return "\n".join(f"C:{c.id}:{c.command}" for c in commands)

    def _clock_command_results(self, body):
        """Lines like ``ID=12&Return=0&CMD=DATA`` sent by the clock to /iclock/devicecmd."""
        self.ensure_one()
        Command = self.env['hr.field.clock.command'].sudo()
        for line in (body or '').splitlines():
            values = dict(part.split('=', 1) for part in line.strip().split('&') if '=' in part)
            if not values.get('ID', '').isdigit():
                continue
            command = Command.search([('id', '=', int(values['ID'])), ('clock_id', '=', self.id)])
            if command:
                ok = values.get('Return', '').strip() == '0'
                command.write({'state': 'done' if ok else 'error', 'return_code': values.get('Return', '')[:16],
                               'command': command._clock_without_photo()})

    @api.model
    def _clock_seen(self, serial, ip=None):
        """The clock that is talking to Odoo; an unknown serial number is left pending for Operations."""
        serial = (serial or '').strip()[:64]
        if not serial:
            return self.browse()
        clock = self.sudo().search([('serial', '=', serial)], limit=1)
        if clock and not clock._clock_ip_allowed(ip):
            _logger.warning("Time clock %s refused: request from %s, not an allowed IP", serial, ip)
            return self.browse()
        if not clock:
            if self.sudo().search_count([('state', '=', 'pending')]) >= MAX_PENDING_CLOCKS:
                return self.browse()
            clock = self.sudo().create({'name': _("Checador %s", serial), 'serial': serial})
        clock.write({'last_seen': fields.Datetime.now(), 'last_ip': (ip or '')[:64]})
        return clock

    def _clock_to_utc(self, text):
        self.ensure_one()
        local = datetime.strptime(text.strip(), '%Y-%m-%d %H:%M:%S')
        return pytz.timezone(self.tz).localize(local).astimezone(pytz.utc).replace(tzinfo=None)

    def _clock_receive(self, body, stamp=None):
        """Save the ATTLOG lines sent by the clock and turn them into attendances. Return the lines read."""
        self.ensure_one()
        Log = self.env['hr.field.clock.log'].sudo()
        logs = Log.browse()
        lines = [line for line in (body or '').splitlines() if line.strip()][:CLOCK_MAX_LINES]
        latest = fields.Datetime.now() + CLOCK_FUTURE_TOLERANCE
        for line in lines:
            parts = line.split('\t')
            if len(parts) < 2:
                continue
            key = parts[0].strip()[:32]
            try:
                when = self._clock_to_utc(parts[1])
            except ValueError:
                continue
            if when > latest:
                _logger.warning("Time clock %s sent a punch in the future (%s): ignored", self.serial, when)
                continue
            # The clock resends what it is not sure we got: the same punch is saved once.
            if Log.search_count([('clock_id', '=', self.id), ('employee_key', '=', key), ('punch_time', '=', when)]):
                continue
            logs |= Log.create({
                'clock_id': self.id,
                'employee_key': key,
                'punch_time': when,
                'device_status': parts[2].strip()[:8] if len(parts) > 2 else False,
                'verify': parts[3].strip()[:8] if len(parts) > 3 else False,
            })
        if stamp:
            self.att_stamp = stamp[:32]
        logs._clock_process()
        return len(lines)


class HrFieldClockCommand(models.Model):
    """An order for the clock (create or delete a user); the clock picks it up on its next connection."""
    _name = 'hr.field.clock.command'
    _description = "Orden para el checador"
    _order = 'id desc'
    _rec_name = 'summary'

    clock_id = fields.Many2one('hr.field.clock', "Checador", required=True, ondelete='cascade', index=True)
    command = fields.Char("Orden completa", required=True)
    summary = fields.Char("Orden", compute='_compute_summary', store=True)
    state = fields.Selection([
        ('pending', "Por enviar"),
        ('sent', "Enviada"),
        ('done', "Hecha"),
        ('error', "Con error"),
    ], string="Estado", default='pending', required=True, index=True)
    sent_on = fields.Datetime("Enviada el", readonly=True)
    return_code = fields.Char("Respuesta del checador", readonly=True)

    def _clock_without_photo(self):
        """The order without the face photo, kept once the clock answered (no biometric copies left)."""
        self.ensure_one()
        command = self.command or ''
        if not command.startswith('DATA UPDATE BIOPHOTO') or 'Content=' not in command:
            return command
        return '\t'.join(part for part in command.split('\t') if not part.startswith('Content='))

    @api.depends('command')
    def _compute_summary(self):
        for record in self:
            command = record.command or ''
            if command.startswith('DATA UPDATE BIOPHOTO'):
                pin = command.split('PIN=', 1)[1].split('\t', 1)[0]
                record.summary = _("Rostro desde foto, usuario %s", pin)
            else:
                record.summary = command[:120]


class HrFieldClockLog(models.Model):
    """Every punch the clock sent, as it came, and what Odoo did with it."""
    _name = 'hr.field.clock.log'
    _description = "Registro del checador"
    _order = 'punch_time desc, id desc'
    _rec_name = 'employee_key'

    clock_id = fields.Many2one('hr.field.clock', "Checador", required=True, ondelete='cascade', index=True)
    employee_key = fields.Char("Clave en el checador", required=True, index=True)
    employee_id = fields.Many2one('hr.employee', "Empleado", readonly=True)
    punch_time = fields.Datetime("Hora", required=True, index=True)
    device_status = fields.Char("Estado en el checador", readonly=True)
    verify = fields.Char("Verificación", readonly=True, help="1 huella, 15 rostro, 4 tarjeta, 0 contraseña.")
    state = fields.Selection([
        ('new', "Por procesar"),
        ('entry', "Entrada"),
        ('exit', "Salida"),
        ('ignored', "Ignorado"),
        ('error', "Con error"),
    ], string="Resultado", default='new', required=True, index=True)
    result = fields.Char("Detalle", readonly=True)
    attendance_id = fields.Many2one('hr.attendance', "Asistencia", readonly=True, ondelete='set null')

    def _clock_process(self):
        Employee = self.env['hr.employee'].sudo()
        service = self.env['hr.field.service'].sudo()
        for log in self.sorted(lambda l: (l.punch_time, l.id)):
            employee = Employee._field_clock_employee(log.employee_key)
            if not employee:
                log.write({'state': 'error', 'employee_id': False, 'result': _(
                    "La clave %s no es el número de registro de ningún empleado.", log.employee_key)})
                continue
            if not log.clock_id._clock_allows(employee):
                log.write({'state': 'ignored', 'employee_id': employee.id, 'attendance_id': False, 'result': _(
                    "Su departamento (%s) no checa en este checador.", employee.department_id.name or _("sin departamento"))})
                continue
            try:
                with self.env.cr.savepoint():
                    state, result, attendance = service._punch_clock(employee, log.clock_id, log.punch_time)
            except ValidationError as e:
                state, result, attendance = 'error', e.args[0], self.env['hr.attendance']
            log.write({'employee_id': employee.id, 'state': state, 'result': result,
                       'attendance_id': attendance.id})

    def action_reprocess(self):
        """For punches that failed, e.g. after giving the employee their registration number."""
        self.filtered(lambda l: l.state in ('error', 'new'))._clock_process()


class HrFieldServiceClock(models.AbstractModel):
    _inherit = 'hr.field.service'

    @api.model
    def _punch_clock(self, employee, clock, when):
        """Entrance or exit of ``employee`` at ``when`` (UTC) from the time clock.

        There is one day per person, whichever way it was marked: a clock punch on a day that already has
        attendances (from the phone or imported late) is merged into them instead of creating another one.
        Before the first entrance it becomes the entrance, after the exit of the day it becomes the exit,
        and inside a marked period it is already covered. Without attendances that day it opens one without
        project, as the office tablet does. Office staff (without field role) get a plain Odoo attendance.
        Return (log state, detail, attendance)."""
        Attendance = self.env['hr.attendance']
        field = employee._field_is_crew()
        repeat = timedelta(minutes=REPEAT_MINUTES)
        tz = pytz.timezone(employee._field_tz() or 'UTC')
        day = pytz.utc.localize(when).astimezone(tz).date()

        # Forgot to punch out on a previous day: closed without hours for HR to fix.
        for stale in Attendance.search([('employee_id', '=', employee.id), ('check_out', '=', False),
                                        ('field_date', '<', day)]):
            if field:
                stale.write({'check_out': stale.check_in, 'field_state': 'review'})
                stale._field_add_review_reason(_("Sin salida registrada: captura la hora de salida"))
            else:
                stale.write({'check_out': stale.check_in})

        today = Attendance.search([('employee_id', '=', employee.id), ('field_date', '=', day),
                                   ('field_state', '!=', 'rejected')], order='check_in')
        if not today:
            return 'entry', _("Entrada"), self._clock_new_attendance(employee, clock, when)
        first, last = today[0], today[-1]
        if when < first.check_in:
            vals = {'check_in': when, 'in_field_clock_id': clock.id}
            if field and first.field_in_kind != 'lunch':
                vals['field_in_kind'] = 'clock'
            first.write(vals)
            return 'entry', _("Entrada (unida a la asistencia del día)"), first
        if not last.check_out and when >= last.check_in:
            if when - last.check_in < repeat:
                return 'ignored', _("Repetido: ya tenía entrada a las %s.", self._local_time(employee, last.check_in)), last
            vals = {'check_out': when, 'out_field_clock_id': clock.id}
            early = False
            if field:
                schedule = last._field_schedule()
                lunched = today.filtered(lambda a: a.field_out_kind == 'lunch' or a.field_in_kind == 'lunch')
                if last == first and not lunched and last._field_in_lunch_window(when, schedule):
                    # First exit of the day inside the lunch range: the lunch, the return opens the afternoon.
                    last.write(dict(vals, field_out_kind='lunch', field_overtime_state=False))
                    return 'exit', _("Salida a comer"), last
                late = last._field_needs_overtime_question(when)
                vals.update(field_out_kind='day', field_overtime_state='pending' if late else False)
                early = last._field_early_exit_reason(when, schedule)
            last.write(vals)
            if early:
                self._clock_review(last, early)
            return 'exit', _("Salida"), last
        covering = today.filtered(lambda a: a.check_in <= when <= (a.check_out or when))
        if covering:
            return 'ignored', _("Ya está cubierto por la asistencia de %s a %s.",
                                self._local_time(employee, covering[0].check_in),
                                self._local_time(employee, covering[0].check_out) if covering[0].check_out else '…'), covering[0]
        if not last.check_out or when < last.check_out:
            return 'ignored', _("Cae entre dos asistencias del día (comida)."), last
        if last.out_field_clock_id and when - last.check_out < repeat:
            return 'ignored', _("Repetido: ya tenía salida a las %s.", self._local_time(employee, last.check_out)), last
        if field and last.field_out_kind != 'lunch':
            # Left the site with the supervisor and punched out later at the clock: the day ends here.
            vals = {'check_out': when, 'out_field_clock_id': clock.id}
            if last._field_needs_overtime_question(when) and not last.field_overtime_state:
                vals['field_overtime_state'] = 'pending'
            last.write(vals)
            return 'exit', _("Salida (unida a la asistencia del día)"), last
        attendance = self._clock_new_attendance(employee, clock, when, after=last)
        if field:
            late = last._field_late_lunch_reason(when)
            if late:
                self._clock_review(attendance, late)
        return 'entry', _("Regreso de comer") if field else _("Entrada"), attendance

    @api.model
    def _clock_review(self, attendance, reason):
        if attendance.field_state in ('valid', False):
            attendance.field_state = 'review'
        attendance._field_add_review_reason(reason)

    @api.model
    def _clock_new_attendance(self, employee, clock, when, after=None):
        vals = {
            'employee_id': employee.id,
            'check_in': when,
            'in_field_clock_id': clock.id,
            'in_mode': 'technical',
        }
        if employee._field_is_crew():
            vals.update(field_supervisor_id=employee._field_leader().id, field_in_kind='clock', field_state='valid')
            if after:
                # Back from lunch: same project and supervisor as the morning.
                vals.update(field_in_kind='lunch', field_project_id=after.field_project_id.id,
                            field_supervisor_id=after.field_supervisor_id.id or vals['field_supervisor_id'])
        return self.env['hr.attendance'].create(vals)
