import base64
import binascii
import math

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .hr_field_face import descriptor_distance, parse_descriptor

DEFAULT_FACE_THRESHOLD = 0.5
MAX_PHOTO_BYTES = 600 * 1024


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
            'crew': [{'id': e.id, 'name': e.name} for e in owner._field_candidates()],
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
    def _identify(self, owner, descriptor=None, employee_id=None, pin=None):
        """Return (employee, face_distance, review_reasons) or raise UserError."""
        candidates = owner._field_candidates()
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
    def _geo(self, latitude, longitude, device):
        from odoo.addons.hr_attendance.controllers.main import HrAttendance as AttendanceController
        from odoo.http import request
        geo = {'mode': 'kiosk'}
        if request:
            geo = AttendanceController._get_geoip_response(
                'kiosk', latitude=latitude, longitude=longitude,
                device_tracking_enabled=device.employee_id.company_id.attendance_device_tracking)
        if latitude and longitude:
            geo.update(latitude=latitude, longitude=longitude)
        return geo

    @api.model
    def _punch(self, owner, device, descriptor=None, employee_id=None, pin=None,
               project_id=None, latitude=None, longitude=None, photo=None, change_project=False):
        """Check in or out the recognised employee.

        Returns a dict for the kiosk: ``error`` or ``employee``, ``action``,
        ``project``, ``state`` and ``reasons``."""
        employee, face_distance, reasons = self._identify(owner, descriptor, employee_id, pin)
        if not employee:
            if reasons:
                return {'error': reasons[0]}
            return {'error': _("No te reconocí. Intenta de nuevo o marca con tu PIN."), 'need_pin': True}

        has_gps = latitude is not None and longitude is not None
        latitude = float(latitude) if has_gps else False
        longitude = float(longitude) if has_gps else False
        photo = clean_photo(photo)
        geo = self._geo(latitude, longitude, device)
        attendance = self.env['hr.attendance'].search(
            [('employee_id', '=', employee.id), ('check_out', '=', False)], limit=1)

        if attendance and not change_project:
            return self._check_out(attendance, device, geo, face_distance, photo, reasons, has_gps)

        leader = employee._field_leader()
        projects = leader._field_active_projects() if leader else self.env['project.project']
        if not projects:
            return {'error': _("Tu supervisor no tiene obras activas hoy.")}
        if project_id:
            project = projects.filtered(lambda p: p.id == int(project_id))
            if not project:
                return {'error': _("Esa obra no está activa para tu supervisor.")}
        elif len(projects) == 1:
            project = projects
        else:
            return {'error': _("Elige la obra."), 'need_project': True}

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
            'action': 'check_in',
            'project': project.display_name,
            'state': vals['field_state'],
            'reasons': in_reasons,
        }

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
    def _check_out(self, attendance, device, geo, face_distance, photo, reasons, has_gps, check_geofence=True):
        gps_distance = 0
        if attendance.field_project_id and check_geofence:
            gps_distance = self._gps_check(
                attendance.field_project_id, geo.get('latitude'), geo.get('longitude'), has_gps, reasons)
        vals = {
            'check_out': fields.Datetime.now(),
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
            'action': 'check_out',
            'project': attendance.field_project_id.display_name or '',
            'state': attendance.field_state,
            'reasons': reasons,
        }
