from odoo import fields, models


class ProjectProject(models.Model):
    _inherit = 'project.project'

    field_latitude = fields.Float("Latitud de la obra", digits=(10, 7))
    field_longitude = fields.Float("Longitud de la obra", digits=(10, 7))
    field_radius = fields.Integer(
        "Radio de geocerca (m)", default=200,
        help="Los marcajes a mayor distancia de la obra quedan en revisión.")
    field_supervisor_ids = fields.Many2many(
        'hr.employee', 'project_field_supervisor_rel', 'project_id', 'employee_id',
        string="Supervisores de campo", domain=[('field_role', '=', 'supervisor')],
        help="Supervisores que pueden activar esta obra en su kiosco.")

    def _field_has_location(self):
        self.ensure_one()
        return bool(self.field_latitude or self.field_longitude)
