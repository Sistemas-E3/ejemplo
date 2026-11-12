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
        return request.render('Remote_Attendance.field_kiosk_page', {
            'token': token,
            'owner_name': owner.name,
            'is_supervisor': owner.field_role == 'supervisor',
            'role': owner.field_role,
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
              project_id=None, latitude=None, longitude=None, photo=None, change_project=False,
              kind=None, badge=None, overtime=None):
        owner = self._owner(token)
        device = owner and self._approved_device(owner, device_key)
        if not device:
            return {'error': _("Teléfono no autorizado.")}
        return self._safe(
            self._service()._punch, owner, device,
            descriptor=descriptor, employee_id=employee_id, pin=pin, project_id=project_id,
            latitude=latitude, longitude=longitude, photo=photo, change_project=bool(change_project),
            kind=kind or None, badge=(str(badge).strip()[:32] or None) if badge else None,
            overtime=None if overtime is None else bool(overtime),
        )

    @http.route('/campo/<string:token>/rostro', type='jsonrpc', auth='public')
    def enroll_from_link(self, token, device_key=None, pin=None, employee_id=None, descriptors=None,
                         photo=None, consent=False):
        owner = self._owner(token)
        device = owner and self._approved_device(owner, device_key)
        if not device:
            return {'error': _("Teléfono no autorizado.")}
        return self._safe(self._service()._enroll, owner, device, pin, employee_id, descriptors,
                          photo=photo, consent=bool(consent))

    @http.route('/campo/<string:token>/comida', type='jsonrpc', auth='public')
    def late_lunch(self, token, device_key=None, pin=None, employee_id=None, date=None,
                   lunch_out=None, lunch_in=None, project_id=None):
        owner = self._owner(token)
        device = owner and self._approved_device(owner, device_key)
        if not device:
            return {'error': _("Teléfono no autorizado.")}
        return self._safe(self._service()._late_lunch, owner, pin, employee_id, self._parse_date(date),
                          lunch_out, lunch_in, project_id)

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

    @http.route('/campo/<string:token>/sincronizar', type='jsonrpc', auth='public')
    def sync(self, token, device_key=None, punches=None, phone_now=None):
        """Punches taken without signal, sent when the phone is back online."""
        owner = self._owner(token)
        device = owner and self._approved_device(owner, device_key)
        if not device:
            return {'error': _("Teléfono no autorizado.")}
        return self._safe(self._service()._sync, owner, device, punches if isinstance(punches, list) else [],
                          phone_now=phone_now)

    # ------------------------------------------------------------------
    # Installable app and offline (service worker)
    # ------------------------------------------------------------------

    @http.route('/campo/<string:token>/app.webmanifest', type='http', auth='public', sitemap=False)
    def web_manifest(self, token):
        owner = self._owner(token)
        if not owner:
            raise request.not_found()
        start = '/campo/' + token
        manifest = {
            'name': _("Asistencia · %s", owner.name),
            'short_name': _("Asistencia"),
            'start_url': start,
            'scope': start,
            'display': 'standalone',
            'orientation': 'any',
            'background_color': '#111827',
            'theme_color': '#1f2937',
            'icons': [{'src': '/Remote_Attendance/static/src/img/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
                      {'src': '/Remote_Attendance/static/src/img/icon-512.png', 'sizes': '512x512', 'type': 'image/png'}],
        }
        return request.make_response(json.dumps(manifest), headers=[
            ('Content-Type', 'application/manifest+json'), ('Cache-Control', 'no-cache')])

    @http.route('/campo/sw.js', type='http', auth='public', sitemap=False, save_session=False)
    def service_worker(self):
        """Served under /campo/ so it can keep the kiosk pages working without signal."""
        from odoo.tools import file_open
        with file_open('Remote_Attendance/static/src/js/field_sw.js', 'rb') as f:
            content = f.read()
        return request.make_response(content, headers=[
            ('Content-Type', 'text/javascript'), ('Cache-Control', 'no-cache'),
            ('Service-Worker-Allowed', '/campo/')])

    # ------------------------------------------------------------------
    # Face enrolment (Operaciones, logged in)
    # ------------------------------------------------------------------

    def _enroll_employee(self, employee_id):
        if not request.env.user.has_group('Remote_Attendance.group_field_operations'):
            raise AccessError(_("Solo Operaciones puede registrar rostros."))
        employee = request.env['hr.employee'].browse(int(employee_id)).exists()
        if not employee:
            raise AccessError(_("Empleado no encontrado."))
        return employee

    @http.route('/campo/enrolar/fotos', type='http', auth='user', sitemap=False)
    def enroll_photos_page(self):
        """Register the faces of all the field staff from the photo of their employee record."""
        if not request.env.user.has_group('Remote_Attendance.group_field_operations'):
            raise AccessError(_("Solo Operaciones puede registrar rostros."))
        staff = request.env['hr.employee']._field_people().sorted('name')
        # Employees without a photo get a generated SVG avatar: only real pictures are useful.
        photos = request.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'hr.employee'), ('res_field', '=', 'image_1920'),
            ('res_id', 'in', staff.ids), ('mimetype', '!=', 'image/svg+xml'),
        ])
        with_photo = set(photos.mapped('res_id'))
        return request.render('Remote_Attendance.field_enroll_photos_page', {
            'pending': staff.filtered(lambda e: e.id in with_photo and not e.field_face_ids),
            'without_photo': staff.filtered(lambda e: e.id not in with_photo and not e.field_face_ids),
            'done_count': len(staff.filtered('field_face_ids')),
        })

    @http.route('/campo/enrolar/<int:employee_id>', type='http', auth='user', sitemap=False)
    def enroll_page(self, employee_id):
        employee = self._enroll_employee(employee_id)
        return request.render('Remote_Attendance.field_enroll_page', {
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


class FieldAttendanceReportController(http.Controller):

    @http.route('/campo/reporte-asistencia/excel', type='http', auth='user', methods=['POST'])
    def attendance_report_excel(self, filters='{}', **kwargs):
        try:
            values = json.loads(filters)
        except ValueError:
            values = {}
        content, name = request.env['hr.field.attendance.report'].excel_file(values)
        return request.make_response(content, headers=[
            ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Disposition', http.content_disposition(name)),
        ])
