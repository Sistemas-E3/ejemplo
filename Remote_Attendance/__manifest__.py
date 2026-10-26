{
    'name': 'Asistencias en campo',
    'version': '18.0.1.5.0',
    'category': 'Human Resources/Attendances',
    'summary': 'Marcaje con reconocimiento facial y GPS desde el teléfono, ligado al proyecto y a sus horas',
    'description': """
Asistencias en campo
====================

* Kiosco personal por supervisor (y marcaje personal por trabajador) sin usuario de Odoo:
  enlace con token, teléfono aprobado por Operaciones y PIN.
* Reconocimiento facial en el navegador (face-api) con comparación en el servidor
  contra la cuadrilla del supervisor.
* Proyectos activos del día elegidos por el supervisor entre las que Operaciones le asignó.
* Geocerca por proyecto: fuera del radio el marcaje queda en revisión.
* Checador ZKTeco (ADMS): los marcajes llegan solos a Odoo.
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
        'views/hr_field_clock_views.xml',
        'views/field_menus.xml',
        'data/field_cron.xml',
        'data/import_mapping.xml',
    ],
    'installable': True,
    'application': False,
}
