from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    field_day_start = fields.Float("Hora de entrada", config_parameter='Remote_Attendance.day_start', default=7.0)
    field_day_end = fields.Float("Hora de salida", config_parameter='Remote_Attendance.day_end', default=17.0)
    field_late_minutes = fields.Integer(
        "Tolerancia de entrada (min)", config_parameter='Remote_Attendance.late_minutes', default=10,
        help="Entrar después de la hora de entrada más estos minutos es retardo. "
             "También es la tolerancia para regresar de comer.")
    field_early_minutes = fields.Integer(
        "Tolerancia de salida (min)", config_parameter='Remote_Attendance.early_minutes', default=10,
        help="Salir antes de la hora de salida menos estos minutos manda el día a revisión.")
    field_overtime_after = fields.Float(
        "Preguntar tiempo extra después de", config_parameter='Remote_Attendance.overtime_after', default=1.0,
        help="Salir esta cantidad de tiempo después de la hora de salida pregunta si es tiempo extra.")
    field_lunch_from = fields.Float("Salida a comer desde", config_parameter='Remote_Attendance.lunch_from', default=12.0)
    field_lunch_to = fields.Float("Salida a comer hasta", config_parameter='Remote_Attendance.lunch_to', default=15.0)
    field_lunch_hours = fields.Float(
        "Tiempo de comida", config_parameter='Remote_Attendance.lunch_hours', default=1.0,
        help="Se descuenta si no marcaron la comida. Regresar después de este tiempo más la tolerancia "
             "de entrada manda el regreso a revisión.")
