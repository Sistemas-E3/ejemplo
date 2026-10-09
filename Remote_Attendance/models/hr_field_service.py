import base64
import binascii
import json
import math
from datetime import timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .hr_field_face import descriptor_distance, parse_descriptor

DEFAULT_FACE_THRESHOLD = 0.5
MAX_PHOTO_BYTES = 600 * 1024
PUNCH_KINDS = ('in', 'lunch_out', 'lunch_in', 'out')
# On the office tablet a second scan within this time is ignored (people stay in front of the camera).
REPEAT_MINUTES = 30


def gps_distance_m(lat1, lon1, lat2, lon2):
    """Haversine distance in metres."""
    radius = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def clean_photo(data_url):
    """Return base64 JPEG/PNG content from a data URL, or False when invalid."""
    if not data_url or not isinstance(data_url, str):
        return False
    payload = data_url.split(',', 1)[1] if data_url.startswith('data:') else data_url
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return False
    if len(raw) > MAX_PHOTO_BYTES or not (raw[:3] == b'\xff\xd8\xff' or raw[:8] == b'\x89PNG\r\n\x1a\n'):
        return False
    return payload


class HrFieldService(models.AbstractModel):
    """Business logic of the field kiosk. Every method expects a sudo environment:
    the caller (public controller) is responsible for validating the link token."""
    _name = 'hr.field.service'
    _description = "Servicio de asistencias en campo"

    @api.model
    def _face_threshold(self):
        value = self.env['ir.config_parameter'].sudo().get_param('Remote_Attendance.face_threshold')
        try:
            return float(value) if value else DEFAULT_FACE_THRESHOLD
        except ValueError:
            return DEFAULT_FACE_THRESHOLD

    @api.model
    def _manual_enabled(self):
        """Whether supervisor and worker links offer the manual options (marking with PIN from a list,
        roll call and lunch registered later). Off by default: in the field attendance is only by camera.
        Turn it on with the system parameter ``Remote_Attendance.supervisor_manual`` = 1."""
        value = self.env['ir.config_parameter'].sudo().get_param('Remote_Attendance.supervisor_manual')
        return (value or '').strip().lower() in ('1', 'true', 'yes', 'si', 'sí')

    @api.model
    def _owner_from_token(self, token):
        if not token or len(token) < 20:
            return self.env['hr.employee']
        return self.env['hr.employee'].sudo().search([
            ('field_token', '=', token), ('field_role', '!=', False),
        ], limit=1)

    @api.model
    def _match_face(self, descriptor, candidates):
        """Return (employee, distance) of the closest reference face, or (empty, None)."""
        vector = parse_descriptor(descriptor)
        best, best_distance = self.env['hr.employee'], None
        for face in candidates.sudo().field_face_ids:
            distance = descriptor_distance(vector, face._get_vector())
            if best_distance is None or distance < best_distance:
                best, best_distance = face.employee_id, distance
        if best_distance is None or best_distance > self._face_threshold():
            return self.env['hr.employee'], best_distance
        return best, best_distance

    @api.model
    def _project_payload(self, projects):
        return [{
            'id': p.id,
            'name': p.display_name,
            'latitude': p.field_latitude,
            'longitude': p.field_longitude,
            'radius': p.field_radius,
            'has_location': p._field_has_location(),
        } for p in projects]

    @api.model
    def _state(self, owner, device):
        leader = owner._field_leader()
        active = leader._field_active_projects() if leader else self.env['project.project']
        return {
            'owner': owner.name,
            'role': owner.field_role,
            'device': device.state,
            'leader': leader.name if leader else False,
            'active_projects': self._project_payload(active),
            'assignable_projects': self._project_payload(owner.field_project_ids)
            if owner.field_role == 'supervisor' else [],
            'crew': [{'id': e.id, 'name': e.name, 'key': e.field_key or '', 'face': bool(e.field_face_ids)}
                     for e in owner._field_candidates().sorted('name')],
            'can_enroll': owner.field_role in ('supervisor', 'office'),
            'manual': owner.field_role == 'office' or self._manual_enabled(),
            'days': [day.isoformat() for day in self._roll_days(owner)]
            if owner.field_role == 'supervisor' else [],
        }

    @api.model
    def _activate_projects(self, owner, pin, project_ids):
        if owner.field_role != 'supervisor':
            raise UserError(_("Solo un supervisor puede activar obras."))
        if not owner._field_check_pin(pin):
            return {'error': _("PIN incorrecto.")}
        allowed = owner.field_project_ids
        projects = allowed.filtered(lambda p: p.id in set(map(int, project_ids or [])))
        owner.write({
            'field_active_project_ids': [fields.Command.set(projects.ids)],
            'field_active_date': owner._field_today(),
        })
        return {'active_projects': self._project_payload(projects)}

    @api.model
    def _identify(self, owner, descriptor=None, employee_id=None, pin=None, badge=None):
        """Return (employee, face_distance, review_reasons) or raise UserError."""
        candidates = owner._field_candidates()
        if badge:
            employee = self.env['hr.employee']._field_by_key(badge) & candidates
            if not employee:
                return self.env['hr.employee'], None, [_("Tarjeta no registrada: %s", badge)]
            return employee, None, []
        if descriptor:
            employee, distance = self._match_face(descriptor, candidates)
            if employee:
                return employee, distance, []
        if employee_id and pin:
            employee = candidates.filtered(lambda e: e.id == int(employee_id))
            if not employee:
                raise UserError(_("Ese empleado no pertenece a este enlace."))
            if not employee._field_check_pin(pin):
                return self.env['hr.employee'], None, [_("PIN incorrecto")]
            return employee, None, [_("Marcó con PIN: no se reconoció el rostro")]
        return self.env['hr.employee'], None, []

    @api.model
    def _local_time(self, employee, value):
        tz = pytz.timezone(employee._get_tz() or 'UTC')
        return pytz.utc.localize(value).astimezone(tz).strftime('%H:%M')

    @api.model
    def _open_attendance(self, employee):
        """Open attendance of today. One left open on a previous day is closed for HR to fix."""
        attendance = self.env['hr.attendance'].search(
            [('employee_id', '=', employee.id), ('check_out', '=', False)], limit=1)
        if attendance and attendance.field_date and attendance.field_date < employee._field_today():
            attendance.write({'check_out': attendance.check_in, 'field_state': 'review'})
            attendance._field_add_review_reason(_("Sin salida registrada: captura la hora de salida"))
            return self.env['hr.attendance']
        return attendance

    @api.model
    def _pick_project(self, leader, project_id):
        """Return (project, error dict) among the leader's active projects."""
        projects = leader._field_active_projects() if leader else self.env['project.project']
        if not projects:
            return projects, {'error': _("Tu supervisor no tiene obras activas hoy.")}
        if project_id:
            project = projects.filtered(lambda p: p.id == int(project_id))
            if not project:
                return project, {'error': _("Esa obra no está activa para tu supervisor.")}
            return project, None
        if len(projects) == 1:
            return projects, None
        return projects.browse(), {'error': _("Elige la obra."), 'need_project': True}

    @api.model
    def _geo(self, latitude, longitude, device):
        from odoo.addons.hr_attendance.controllers.main import HrAttendance as AttendanceController
        from odoo.http import request
        geo = {'mode': 'kiosk'}
        if request:
            geo = AttendanceController._get_geoip_response('kiosk', latitude=latitude, longitude=longitude)
        if latitude and longitude:
            geo.update(latitude=latitude, longitude=longitude)
        return geo

    @api.model
    def _punch(self, owner, device, descriptor=None, employee_id=None, pin=None,
               project_id=None, latitude=None, longitude=None, photo=None, change_project=False,
               kind=None, badge=None, overtime=None):
        """Mark the recognised employee.

        ``kind`` is the button the supervisor pressed: ``in`` (entrada en obra), ``lunch_out``,
        ``lunch_in`` or ``out``; without it, it toggles entrance/exit.
        ``overtime`` answers "¿Es tiempo extra?", asked when leaving late (``need_overtime``).
        Returns a dict for the kiosk: ``error`` or ``employee``, ``action``,
        ``project``, ``state`` and ``reasons``."""
        if kind and kind not in PUNCH_KINDS:
            raise UserError(_("Tipo de marcaje inválido."))
        manual = owner.field_role == 'office' or self._manual_enabled()
        if not manual:
            employee_id = pin = None
        employee, face_distance, reasons = self._identify(owner, descriptor, employee_id, pin, badge)
        if not employee:
            if reasons:
                return {'error': reasons[0]}
            if not manual:
                return {'error': _("No te reconocí. Acércate a la cámara con buena luz e intenta de nuevo.")}
            return {'error': _("No te reconocí. Intenta de nuevo o marca con tu PIN."), 'need_pin': True}
        photo = clean_photo(photo)
        if owner.field_role == 'office':
            return self._punch_office(employee, device, face_distance, photo, reasons)

        has_gps = latitude is not None and longitude is not None
        latitude = float(latitude) if has_gps else False
        longitude = float(longitude) if has_gps else False
        geo = self._geo(latitude, longitude, device)
        attendance = self._open_attendance(employee)
        # Hours go to the site of whoever marks: on a supervisor's phone, his active projects.
        leader = owner if owner.field_role == 'supervisor' else employee._field_leader()

        if kind in ('in', 'lunch_in') and attendance:
            return {'error': _("%(name)s ya tiene entrada desde las %(time)s. Marca primero su salida.",
                               name=employee.name, time=self._local_time(employee, attendance.check_in))}
        if kind in ('lunch_out', 'out') and not attendance:
            return {'error': _("%s no tiene entrada abierta hoy.", employee.name)}
        if attendance and not change_project:
            if not attendance.field_project_id:
                # Entered at the office: the hours go to the site where they mark the exit.
                project, error = self._pick_project(leader, project_id)
                if error:
                    return error
                attendance.write({'field_project_id': project.id, 'field_supervisor_id': leader.id})
            out_kind = 'lunch' if kind == 'lunch_out' else 'day'
            overtime_state = False
            if out_kind == 'day' and attendance._field_needs_overtime_question(fields.Datetime.now()):
                if overtime is None:
                    return {'need_overtime': True, 'employee': employee.name}
                overtime_state = 'pending' if overtime else False
            return self._check_out(attendance, device, geo, face_distance, photo, reasons, has_gps,
                                   out_kind=out_kind, overtime_state=overtime_state)

        project, error = self._pick_project(leader, project_id)
        if error:
            return error
        if attendance and not attendance.field_project_id:
            attendance.write({'field_project_id': project.id, 'field_supervisor_id': leader.id})
            return {'employee': employee.name, 'action': 'project', 'project': project.display_name,
                    'state': attendance.field_state, 'reasons': []}
        if attendance:
            if attendance.field_project_id == project:
                return {'error': _("Ya estás registrado en esa obra.")}
            # The worker is already at the new site: its geofence is checked on the new check-in.
            self._check_out(attendance, device, geo, face_distance, photo, list(reasons), has_gps,
                            check_geofence=False)

        in_reasons = list(reasons)
        gps_distance = self._gps_check(project, latitude, longitude, has_gps, in_reasons)
        vals = {
            'employee_id': employee.id,
            'check_in': fields.Datetime.now(),
            'field_project_id': project.id,
            'field_supervisor_id': leader.id,
            'field_in_kind': 'lunch' if kind == 'lunch_in' else 'site',
            'field_state': 'review' if in_reasons else 'valid',
            'field_review_reason': '\n'.join(in_reasons) or False,
            'in_field_device_id': device.id,
            'in_field_photo': photo,
            'in_face_distance': face_distance or 0.0,
            'in_gps_distance': gps_distance,
            **{f'in_{key}': value for key, value in geo.items()},
        }
        self.env['hr.attendance'].create(vals)
        return {
            'employee': employee.name,
            'action': 'lunch_in' if kind == 'lunch_in' else 'check_in',
            'project': project.display_name,
            'state': vals['field_state'],
            'reasons': in_reasons,
        }

    @api.model
    def _punch_office(self, employee, device, face_distance, photo, reasons):
        """Office tablet: entrance without project (the site is set when they mark at the site)."""
        geo = self._geo(False, False, device)
        attendance = self._open_attendance(employee)
        now = fields.Datetime.now()
        if attendance:
            if now - attendance.check_in < timedelta(minutes=REPEAT_MINUTES):
                return {
                    'employee': employee.name,
                    'action': 'repeat',
                    'time': self._local_time(employee, attendance.check_in),
                    'project': '',
                    'state': attendance.field_state,
                    'reasons': [],
                }
            # Nobody answers on the office tablet: a late exit goes to HR as overtime to validate.
            late = attendance._field_needs_overtime_question(now)
            return self._check_out(attendance, device, geo, face_distance, photo, reasons, True,
                                   check_geofence=False, out_kind='day', overtime_state='pending' if late else False)
        vals = {
            'employee_id': employee.id,
            'check_in': now,
            'field_supervisor_id': employee._field_leader().id,
            'field_in_kind': 'office',
            'field_state': 'review' if reasons else 'valid',
            'field_review_reason': '\n'.join(reasons) or False,
            'in_field_device_id': device.id,
            'in_field_photo': photo,
            'in_face_distance': face_distance or 0.0,
            **{f'in_{key}': value for key, value in geo.items()},
        }
        self.env['hr.attendance'].create(vals)
        return {
            'employee': employee.name,
            'action': 'check_in',
            'time': self._local_time(employee, now),
            'project': '',
            'state': vals['field_state'],
            'reasons': reasons,
        }

    @api.model
    def _enroll(self, owner, device, pin, employee_id, descriptors, photo=None, consent=False):
        """Register the reference face of an employee from a supervisor's phone or the office tablet.
        Only employees without a face: replacing one is done by Operaciones in Odoo."""
        if owner.field_role not in ('supervisor', 'office'):
            raise UserError(_("Este enlace no puede registrar rostros."))
        if not owner._field_check_pin(pin):
            return {'error': _("PIN incorrecto.")}
        employee = owner._field_candidates().filtered(lambda e: e.id == int(employee_id or 0))
        if not employee:
            raise UserError(_("Esa persona no es personal de campo."))
        if employee.field_face_ids:
            return {'error': _("%s ya tiene rostro registrado. Para cambiarlo, Operaciones debe borrar sus rostros en Odoo.",
                               employee.name)}
        if not consent and not employee.field_face_consent_date:
            return {'error': _("Falta el consentimiento firmado del empleado.")}
        if not descriptors or len(descriptors) > 10:
            return {'error': _("Toma entre 1 y 10 fotos.")}
        vectors = [parse_descriptor(descriptor) for descriptor in descriptors]
        if not employee.field_face_consent_date:
            employee.field_face_consent_date = employee._field_today()
        image = clean_photo(photo)
        self.env['hr.field.face'].create([{
            'employee_id': employee.id,
            'descriptor': json.dumps(vector),
            'image': image if index == 0 else False,
            'enrolled_by_id': owner.id,
            'device_id': device.id,
        } for index, vector in enumerate(vectors)])
        return {'employee': employee.name, 'count': len(employee.field_face_ids)}

    @api.model
    def _gps_check(self, project, latitude, longitude, has_gps, reasons):
        if not has_gps:
            reasons.append(_("Sin ubicación GPS"))
            return 0
        if not project._field_has_location():
            return 0
        distance = int(gps_distance_m(latitude, longitude, project.field_latitude, project.field_longitude))
        if distance > (project.field_radius or 0):
            reasons.append(_("Fuera de obra: a %s m", distance))
        return distance

    @api.model
    def _check_out(self, attendance, device, geo, face_distance, photo, reasons, has_gps, check_geofence=True,
                   out_kind='day', overtime_state=False):
        gps_distance = 0
        if attendance.field_project_id and check_geofence:
            gps_distance = self._gps_check(
                attendance.field_project_id, geo.get('latitude'), geo.get('longitude'), has_gps, reasons)
        vals = {
            'check_out': fields.Datetime.now(),
            'field_out_kind': out_kind,
            'field_overtime_state': overtime_state,
            'out_field_device_id': device.id,
            'out_field_photo': photo,
            'out_face_distance': face_distance or 0.0,
            'out_gps_distance': gps_distance,
            **{f'out_{key}': value for key, value in geo.items()},
        }
        if reasons and attendance.field_state in ('valid', False):
            vals['field_state'] = 'review'
        attendance.write(vals)
        if reasons:
            for reason in reasons:
                attendance._field_add_review_reason(reason)
        return {
            'employee': attendance.employee_id.name,
            'action': 'lunch_out' if out_kind == 'lunch' else 'check_out',
            'project': attendance.field_project_id.display_name or '',
            'state': attendance.field_state,
            'reasons': reasons,
            'overtime': bool(overtime_state),
        }
