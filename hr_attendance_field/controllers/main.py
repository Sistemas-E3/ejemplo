import json
import logging
from datetime import date

from odoo import _, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)


class FieldAttendance(http.Controller):

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _service(self):
        return request.env['hr.field.service'].sudo()

    def _owner(self, token):
        return self._service()._owner_from_token(token)

    def _approved_device(self, owner, device_key):
        device = request.env['hr.field.device'].sudo()._field_find(owner, device_key)
        if not device or device.state != 'approved':
            return None
        device.last_seen = http.request.env.cr.now()
        return device

    def _find_approved(self, owner, device_key):
        """Approved phone, without writing (for read-only routes)."""
        device = request.env['hr.field.device'].sudo()._field_find(owner, device_key)
        return device if device and device.state == 'approved' else None

    @staticmethod
    def _parse_date(value):
        try:
            return date.fromisoformat(value) if value else None
        except (TypeError, ValueError):
            return None

    def _safe(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (UserError, ValidationError) as e:
            request.env.cr.rollback()
            return {'error': e.args[0]}
        except (ValueError, TypeError):
            request.env.cr.rollback()
            return {'error': _("Datos inválidos.")}

    # ------------------------------------------------------------------
    # Kiosk (public, protected by token + approved phone + PIN)
    # ------------------------------------------------------------------

    @http.route('/campo/<string:token>', type='http', auth='public', sitemap=False)
    def kiosk_page(self, token):
        owner = self._owner(token)
        if not owner:
            raise request.not_found()
        return request.render('hr_attendance_field.field_kiosk_page', {
            'token': token,
            'owner_name': owner.name,
            'is_supervisor': owner.field_role == 'supervisor',
        })

    @http.route('/campo/<string:token>/register', type='jsonrpc', auth='public')
    def register_device(self, token, name=None):
        owner = self._owner(token)
        if not owner:
            return {'error': _("Enlace inválido.")}
        user_agent = request.httprequest.user_agent.string
        try:
            device, key = request.env['hr.field.device']._field_register(owner, name, user_agent)
        except UserError as e:
            return {'error': e.args[0]}
        _logger.info("Field attendance: phone %s registered for employee %s (pending)", device.id, owner.id)
        return {'device_key': key, 'device': device.state}

    @http.route('/campo/<string:token>/state', type='jsonrpc', auth='public', readonly=True)
    def state(self, token, device_key=None):
        owner = self._owner(token)
        if not owner:
            return {'error': _("Enlace inválido.")}
        device = request.env['hr.field.device'].sudo()._field_find(owner, device_key)
        if not device:
            return {'device': 'unregistered', 'owner': owner.name}
        if device.state != 'approved':
            return {'device': device.state, 'owner': owner.name}
        return self._service()._state(owner, device)

    @http.route('/campo/<string:token>/activate', type='jsonrpc', auth='public')
    def activate(self, token, device_key=None, pin=None, project_ids=None):
        owner = self._owner(token)
        device = owner and self._approved_device(owner, device_key)
        if not device:
            return {'error': _("Teléfono no autorizado.")}
        return self._safe(self._service()._activate_projects, owner, pin, project_ids or [])

    @http.route('/campo/<string:token>/punch', type='jsonrpc', auth='public')
    def punch(self, token, device_key=None, descriptor=None, employee_id=None, pin=None,
              project_id=None, latitude=None, longitude=None, photo=None, change_project=False):
        owner = self._owner(token)
        device = owner and self._approved_device(owner, device_key)
        if not device:
            return {'error': _("Teléfono no autorizado.")}
        return self._safe(
            self._service()._punch, owner, device,
            descriptor=descriptor, employee_id=employee_id, pin=pin, project_id=project_id,
            latitude=latitude, longitude=longitude, photo=photo, change_project=bool(change_project),
        )

    @http.route('/campo/<string:token>/lista', type='jsonrpc', auth='public', readonly=True)
    def roll_state(self, token, device_key=None, date=None):
        owner = self._owner(token)
        device = owner and self._find_approved(owner, device_key)
        if not device:
            return {'error': _("Teléfono no autorizado.")}
        return self._safe(self._service()._roll_state, owner, self._parse_date(date))

    @http.route('/campo/<string:token>/lista/guardar', type='jsonrpc', auth='public')
    def roll_save(self, token, device_key=None, pin=None, date=None, project_id=None, entries=None):
        owner = self._owner(token)
        device = owner and self._approved_device(owner, device_key)
        if not device:
            return {'error': _("Teléfono no autorizado.")}
        return self._safe(self._service()._roll_save, owner, pin, self._parse_date(date), project_id, entries)

    # ------------------------------------------------------------------
    # Face enrolment (Operaciones, logged in)
    # ------------------------------------------------------------------

    def _enroll_employee(self, employee_id):
        if not request.env.user.has_group('hr_attendance_field.group_field_operations'):
            raise AccessError(_("Solo Operaciones puede registrar rostros."))
        employee = request.env['hr.employee'].browse(int(employee_id)).exists()
        if not employee:
            raise AccessError(_("Empleado no encontrado."))
        return employee

    @http.route('/campo/enrolar/<int:employee_id>', type='http', auth='user', sitemap=False)
    def enroll_page(self, employee_id):
        employee = self._enroll_employee(employee_id)
        return request.render('hr_attendance_field.field_enroll_page', {
            'employee': employee,
            'has_consent': bool(employee.field_face_consent_date),
        })

    @http.route('/campo/enrolar/<int:employee_id>/guardar', type='jsonrpc', auth='user')
    def enroll_save(self, employee_id, descriptors=None, photo=None, consent=False):
        employee = self._enroll_employee(employee_id)
        if not consent and not employee.field_face_consent_date:
            return {'error': _("Falta el consentimiento firmado del empleado.")}
        if not descriptors or len(descriptors) > 10:
            return {'error': _("Toma entre 1 y 10 fotos.")}
        from ..models.hr_field_service import clean_photo
        image = clean_photo(photo)

        def _save():
            if consent and not employee.field_face_consent_date:
                employee.field_face_consent_date = employee._field_today()
            request.env['hr.field.face'].create([{
                'employee_id': employee.id,
                'descriptor': json.dumps(descriptor),
                'image': image if index == 0 else False,
            } for index, descriptor in enumerate(descriptors)])
            return {'count': len(employee.field_face_ids)}
        return self._safe(_save)
