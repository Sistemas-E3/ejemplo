from odoo import fields, models


class ProjectProject(models.Model):
    _inherit = 'project.project'

    field_code = fields.Char(
        "Número de proyecto", index='btree_not_null', copy=False,
        help="Número con el que los supervisores identifican el proyecto en sus mensajes.")
    field_latitude = fields.Float("Latitud del proyecto", digits=(10, 7))
    field_longitude = fields.Float("Longitud del proyecto", digits=(10, 7))
    field_radius = fields.Integer(
        "Radio de geocerca (m)", default=200,
        help="Los marcajes a mayor distancia del proyecto quedan en revisión.")
    field_supervisor_ids = fields.Many2many(
        'hr.employee', 'project_field_supervisor_rel', 'project_id', 'employee_id',
        string="Supervisores de campo", domain=[('field_role', '=', 'supervisor')],
        help="Supervisores que pueden activar este proyecto en su kiosco.")

    def _field_has_location(self):
        self.ensure_one()
        return bool(self.field_latitude or self.field_longitude)
