{
    'name': 'Asistencias en campo',
    'version': '18.0.1.4.0',
    'category': 'Human Resources/Attendances',
    'summary': 'Marcaje con reconocimiento facial y GPS desde el teléfono, ligado a la obra y a las horas del proyecto',
    'description': """
Asistencias en campo
====================

* Kiosco personal por supervisor (y marcaje personal por trabajador) sin usuario de Odoo:
  enlace con token, teléfono aprobado por Operaciones y PIN.
* Reconocimiento facial en el navegador (face-api) con comparación en el servidor
  contra la cuadrilla del supervisor.
* Obras activas del día elegidas por el supervisor entre las que Operaciones le asignó.
* Geocerca por obra: fuera del radio el marcaje queda en revisión.
* Cada asistencia cerrada genera su línea de horas en el proyecto.
""",
    'author': 'E3',
    'license': 'LGPL-3',
    'depends': ['hr_attendance', 'hr_timesheet', 'project', 'mail', 'base_import'],
    'data': [
        'security/field_security.xml',
        'security/ir.model.access.csv',
        'views/field_templates.xml',
        'views/hr_field_device_views.xml',
        'views/hr_employee_views.xml',
        'views/project_project_views.xml',
        'views/hr_attendance_views.xml',
        'views/hr_field_roster_views.xml',
        'views/hr_field_manual_views.xml',
        'views/hr_field_timesheet_list_views.xml',
        'views/field_menus.xml',
        'data/field_cron.xml',
        'data/import_mapping.xml',
    ],
    'installable': True,
    'application': False,
}
