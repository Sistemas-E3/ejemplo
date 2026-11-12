from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

MAX_DAYS = 7
MAX_BATCH = 50


class HrFieldOfflinePunch(models.Model):
    """A punch taken on a phone without signal and sent when it came back.

    Its ``uuid`` is made by the phone, so a punch resent after a lost answer is stored once."""
    _name = 'hr.field.offline.punch'
    _description = "Marcaje sin señal"
    _order = 'punch_time desc, id desc'
    _rec_name = 'uuid'

    uuid = fields.Char(required=True, index=True, readonly=True)
    device_id = fields.Many2one('hr.field.device', "Teléfono", required=True, ondelete='cascade', readonly=True)
    owner_id = fields.Many2one(related='device_id.employee_id', string="Enlace de", store=True)
    punch_time = fields.Datetime("Hora del marcaje", required=True, readonly=True,
                                 help="Hora del teléfono al marcar, corregida con la diferencia de su reloj al enviar.")
    phone_time = fields.Datetime("Hora según el teléfono", readonly=True)
    received_at = fields.Datetime("Recibido", default=fields.Datetime.now, readonly=True)
    employee_id = fields.Many2one('hr.employee', "Empleado", readonly=True)
    attendance_id = fields.Many2one('hr.attendance', "Asistencia", readonly=True, ondelete='set null')
    state = fields.Selection([('done', "Registrado"), ('error', "No se registró")], required=True, readonly=True)
    detail = fields.Char("Resultado", readonly=True)

    _sql_constraints = [('uuid_unique', 'unique(device_id, uuid)', "Ese marcaje ya se recibió.")]

    def _result(self):
        self.ensure_one()
        return {'uuid': self.uuid, 'state': self.state, 'detail': self.detail or '',
                'employee': self.employee_id.name or ''}


class HrFieldServiceOffline(models.AbstractModel):
    _inherit = 'hr.field.service'

    @api.model
    def _offline_time(self, value, skew):
        """UTC naive datetime of an ISO timestamp from the phone, corrected by its clock skew."""
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if moment.tzinfo:
            moment = (moment - moment.utcoffset()).replace(tzinfo=None)
        return (moment + skew).replace(microsecond=0)

    @api.model
    def _sync(self, owner, device, punches, phone_now=None):
        """Replay the punches a phone took without signal, oldest first, each at the moment it was taken.

        Return one result per punch: ``done`` or ``error`` (both final, the phone forgets them)."""
        Log = self.env['hr.field.offline.punch']
        now = fields.Datetime.now()
        skew = timedelta(0)
        if phone_now:
            try:
                skew = now - self._offline_time(phone_now, timedelta(0))
            except (TypeError, ValueError):
                skew = timedelta(0)
            if abs(skew) < timedelta(minutes=2):
                skew = timedelta(0)  # network delay, not a wrong clock
        items = []
        for punch in (punches or [])[:MAX_BATCH]:
            if not isinstance(punch, dict) or not punch.get('uuid'):
                continue
            try:
                when = self._offline_time(punch.get('time'), skew)
            except (TypeError, ValueError):
                when = None
            items.append((when or now, punch))
        results = []
        for when, punch in sorted(items, key=lambda item: item[0]):
            uuid = str(punch['uuid'])[:64]
            done = Log.search([('device_id', '=', device.id), ('uuid', '=', uuid)], limit=1)
            if done:
                results.append(done._result())
                continue
            log_vals = {'uuid': uuid, 'device_id': device.id, 'punch_time': min(when, now)}
            try:
                log_vals['phone_time'] = self._offline_time(punch.get('time'), timedelta(0))
            except (TypeError, ValueError):
                pass
            result = self._sync_one(owner, device, punch, min(when, now), now)
            employee = self.env['hr.employee'].browse(result.pop('employee_id', False) or [])
            attendance = self.env['hr.attendance'].browse(result.pop('attendance_id', False) or [])
            log_vals.update(
                employee_id=employee.id,
                attendance_id=attendance.id,
                state='error' if result.get('error') else 'done',
                detail=result.get('error') or self._sync_detail(result),
            )
            results.append(Log.create(log_vals)._result())
        return {'results': results}

    @api.model
    def _sync_detail(self, result):
        labels = {'check_in': _("Entrada"), 'check_out': _("Salida"), 'lunch_out': _("Salida a comer"),
                  'lunch_in': _("Regreso de comer"), 'project': _("Proyecto asignado"), 'repeat': _("Repetido")}
        text = labels.get(result.get('action'), _("Marcaje"))
        if result.get('project'):
            text += " · " + result['project']
        if result.get('state') == 'review':
            text += " · " + _("en revisión")
        return text

    @api.model
    def _sync_one(self, owner, device, punch, when, now):
        if when < now - timedelta(days=MAX_DAYS):
            return {'error': _("Tiene más de %s días: captúralo a mano.", MAX_DAYS)}
        kind = punch.get('kind') or None
        service = self.with_context(field_now=when, field_offline=True)
        employee = service._identify(owner, punch.get('descriptor'), badge=punch.get('badge') or None)[0]
        try:
            with self.env.cr.savepoint():
                result = service._punch(
                    owner, device,
                    descriptor=punch.get('descriptor'), badge=punch.get('badge') or None,
                    project_id=punch.get('project_id') or None,
                    latitude=punch.get('latitude'), longitude=punch.get('longitude'),
                    photo=punch.get('photo'), kind=kind, change_project=bool(punch.get('change_project')),
                    overtime=None if punch.get('overtime') is None else bool(punch.get('overtime')),
                )
                if result.get('need_project'):
                    raise UserError(_("Faltó elegir el proyecto."))
                if result.get('need_pin'):
                    raise UserError(_("No se reconoció el rostro."))
        except (UserError, ValidationError) as e:
            return {'error': e.args[0]}
        except (TypeError, ValueError):
            return {'error': _("Datos inválidos.")}
        if result.get('error'):
            return result
        attendance = self.env['hr.attendance'].search(
            [('employee_id', '=', employee.id), ('check_in', '<=', when)], order='check_in desc', limit=1) \
            if employee else self.env['hr.attendance']
        result.update(employee_id=employee.id, attendance_id=attendance.id)
        return result
