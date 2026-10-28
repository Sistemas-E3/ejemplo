{
    "name": "QR para vigencia de gafetes",
    "summary": "QR en el gafete que valida en línea si el empleado sigue activo",
    "version": "18.0.1.0.0",
    "category": "Human Resources",
    "author": "Constructora Etres",
    "license": "LGPL-3",
    "depends": ["hr"],
    "data": [
        "views/hr_employee_views.xml",
        "views/verify_templates.xml",
        "data/server_actions.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
}
