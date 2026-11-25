import json
import math

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

DESCRIPTOR_SIZE = 128
# face-api descriptors are small numbers (about -1 to 1); anything far outside is not a real face.
DESCRIPTOR_LIMIT = 10.0


def parse_descriptor(value):
    """Return a list of 128 floats from a JSON string or list, or raise ValueError."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, (list, tuple)) or len(value) != DESCRIPTOR_SIZE:
        raise ValueError("Invalid face descriptor")
    vector = [float(v) for v in value]
    # NaN or infinite values would make every distance comparison false and match anybody.
    if not all(math.isfinite(v) and abs(v) <= DESCRIPTOR_LIMIT for v in vector):
        raise ValueError("Invalid face descriptor")
    return vector


def descriptor_distance(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


class HrFieldFace(models.Model):
    _name = 'hr.field.face'
    _description = "Rostro de referencia (asistencias en campo)"
    _order = 'employee_id, id'

    employee_id = fields.Many2one('hr.employee', required=True, ondelete='cascade', index=True)
    descriptor = fields.Text(
        required=True, help="Huella facial: 128 números calculados por face-api en el navegador.")
    image = fields.Image("Foto de registro", max_width=512, max_height=512, attachment=True)
    enrolled_by_id = fields.Many2one(
        'hr.employee', "Registró", readonly=True,
        help="Supervisor o kiosco que registró el rostro desde su enlace; vacío si lo registró Operaciones en Odoo.")
    device_id = fields.Many2one('hr.field.device', "Teléfono", readonly=True)

    @api.constrains('descriptor')
    def _check_descriptor(self):
        for face in self:
            try:
                parse_descriptor(face.descriptor)
            except (ValueError, TypeError):
                raise ValidationError(_("La huella facial no es válida."))

    @api.model_create_multi
    def create(self, vals_list):
        faces = super().create(vals_list)
        # Keep the clocks in sync: a face registered with a photo goes to the clocks where the person punches.
        clock = self.env['hr.field.clock']
        for employee in faces.filtered('image').employee_id:
            clock._clock_send_face(employee)
        return faces

    def _get_vector(self):
        self.ensure_one()
        return parse_descriptor(self.descriptor)
