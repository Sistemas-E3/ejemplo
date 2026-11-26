from . import models
from . import controllers


def post_init_hook(env):
    """Asigna un código de gafete a los empleados que ya existían."""
    employees = env["hr.employee"].with_context(active_test=False).search(
        [("badge_token", "=", False)]
    )
    for employee in employees:
        employee.badge_token = employee._new_badge_token()
