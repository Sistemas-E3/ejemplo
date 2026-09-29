import difflib
import re
import unicodedata
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# "[29/09/26, 8:15] Juan: texto" (iOS) o "29/09/26, 8:15 - Juan: texto" (Android)
WHATSAPP_PREFIX = re.compile(
    r'^\s*(\[[^\]]*\]\s*[^:]{1,60}:\s*|\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}[^-]*-\s*[^:]{1,60}:\s*)')
BULLET = re.compile(r'^\s*([-*•·>]+|\d{1,3}\s*[.)\-]+|\d{1,3}\s+(?=\D))\s*')
HEADER_WORDS = {
    'lista', 'asistencia', 'personal', 'gente', 'trabajadores', 'hoy', 'buenos', 'dias', 'buen', 'dia',
    'de', 'del', 'la', 'el', 'en', 'y', 'que', 'ingreso', 'ingresaron', 'entraron', 'entrada',
}
DEFAULT_START = time(8, 0)
DEFAULT_HOURS = 8.0


def normalize(text):
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r'[^a-z0-9ñ ]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def clean_line(line):
    line = WHATSAPP_PREFIX.sub('', line)
    line = BULLET.sub('', line)
    return line.strip(' \t-–—:;,.')


class HrFieldAlias(models.Model):
    _name = 'hr.field.alias'
    _description = "Nombre recordado (listas de WhatsApp)"
    _order = 'name'

    name = fields.Char("Como lo escriben", required=True, index=True)
    employee_id = fields.Many2one('hr.employee', "Empleado", required=True, ondelete='cascade')

    _name_unique = models.Constraint('UNIQUE(name)', "Ese nombre ya está asociado a un empleado.")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals['name'] = normalize(vals.get('name'))
        return super().create(vals_list)

    def write(self, vals):
        if 'name' in vals:
            vals['name'] = normalize(vals['name'])
        return super().write(vals)


class HrFieldRosterImport(models.TransientModel):
    _name = 'hr.field.roster.import'
    _description = "Cargar lista de asistencia de WhatsApp"

    date = fields.Date("Fecha", required=True, default=fields.Date.context_today)
    message = fields.Text("Mensaje de WhatsApp", required=True,
                          help="Pega el mensaje tal cual: número y nombre del proyecto y la lista de gente.")
    project_id = fields.Many2one('project.project', "Obra", domain=[('is_template', '=', False)])
    supervisor_id = fields.Many2one('hr.employee', "Supervisor que envió la lista",
                                    domain=[('field_role', '=', 'supervisor')])
    line_ids = fields.One2many('hr.field.roster.line', 'wizard_id', "Personas")
    state = fields.Selection([('draft', "Pegar"), ('preview', "Revisar")], default='draft')
    unmatched_count = fields.Integer(compute='_compute_counts')
    conflict_count = fields.Integer(compute='_compute_counts')

    @api.depends('line_ids.employee_id', 'line_ids.include', 'line_ids.conflict')
    def _compute_counts(self):
        for wizard in self:
            lines = wizard.line_ids.filtered('include')
            wizard.unmatched_count = len(lines.filtered(lambda l: not l.employee_id))
            wizard.conflict_count = len(lines.filtered('conflict'))

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @api.model
    def _find_project(self, line):
        Project = self.env['project.project']
        domain = [('is_template', '=', False)]
        for number in re.findall(r'\d{2,}', line):
            project = Project.search(domain + ['|', '|',
                                               ('field_code', '=', number),
                                               ('account_id.code', '=', number),
                                               ('name', '=ilike', number + '%')], limit=2)
            if len(project) == 1:
                return project
        text = normalize(line)
        if len(text) < 4:
            return Project
        matches = Project.search(domain).filtered(lambda p: normalize(p.name) and normalize(p.name) in text)
        if len(matches) > 1:
            matches = matches.sorted(lambda p: len(p.name), reverse=True)[:1]
        return matches

    @api.model
    def _field_staff(self):
        return self.env['hr.employee'].search([('field_role', '!=', False)])

    @api.model
    def _match_employee(self, raw, staff, staff_names):
        """Return (employee, match kind)."""
        text = normalize(raw)
        alias = self.env['hr.field.alias'].search([('name', '=', text)], limit=1)
        if alias:
            return alias.employee_id, 'alias'
        exact = staff.filtered(lambda e: staff_names[e.id] == text)
        if len(exact) == 1:
            return exact, 'exact'
        tokens = set(text.split())
        if len(tokens) >= 2:
            partial = staff.filtered(lambda e: tokens <= set(staff_names[e.id].split()))
            if len(partial) == 1:
                return partial, 'exact'
        by_name = {name: emp_id for emp_id, name in staff_names.items()}
        close = difflib.get_close_matches(text, list(by_name), n=1, cutoff=0.75)
        if close:
            return self.env['hr.employee'].browse(by_name[close[0]]), 'fuzzy'
        return self.env['hr.employee'], 'none'

    def _parse(self):
        self.ensure_one()
        project = self.env['project.project']
        names = []
        for raw_line in (self.message or '').splitlines():
            line = clean_line(raw_line)
            if not line or not re.search(r'[A-Za-zÁÉÍÓÚÑáéíóúñ]', line):
                if not project and line:
                    project = self._find_project(line)
                continue
            if not project and not names:
                found = self._find_project(line)
                if found:
                    project = found
                    continue
            words = set(normalize(line).split())
            if not names and (not words or words <= HEADER_WORDS or re.match(r'^(obra|proyecto)\b', normalize(line))):
                continue
            names.append(line)
        return project, names

    def action_read_message(self):
        self.ensure_one()
        project, names = self._parse()
        if not names:
            raise UserError(_("No encontré nombres en el mensaje."))
        staff = self._field_staff()
        staff_names = {e.id: normalize(e.name) for e in staff}
        lines, seen = [], set()
        for raw in names:
            employee, match = self._match_employee(raw, staff, staff_names)
            duplicate = bool(employee) and employee.id in seen
            if employee:
                seen.add(employee.id)
            lines.append({
                'raw_name': raw,
                'employee_id': employee.id,
                'match': match,
                'include': not duplicate,
                'suggested_employee_id': employee.id,
            })
        vals = {'state': 'preview', 'line_ids': [fields.Command.clear()] + [fields.Command.create(l) for l in lines]}
        if project:
            vals['project_id'] = project.id
            if not self.supervisor_id and len(project.field_supervisor_ids) == 1:
                vals['supervisor_id'] = project.field_supervisor_ids.id
        self.write(vals)
        return self._reopen()

    def action_back(self):
        self.write({'state': 'draft', 'line_ids': [fields.Command.clear()]})
        return self._reopen()

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'name': _("Cargar lista de WhatsApp"),
        }

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------

    def _employee_day(self, employee):
        """Return (check_in, check_out) in naive UTC for the employee's schedule on ``self.date``,
        and whether it is a regular working day."""
        tz = pytz.timezone(employee._get_tz() or 'UTC')
        start = tz.localize(datetime.combine(self.date, time.min))
        end = tz.localize(datetime.combine(self.date, time.max))
        calendar = employee.resource_calendar_id
        if calendar and employee.resource_id:
            intervals = list(calendar._work_intervals_batch(start, end, employee.resource_id)[employee.resource_id.id])
            if intervals:
                first = min(i[0] for i in intervals)
                last = max(i[1] for i in intervals)
                return first.astimezone(pytz.utc).replace(tzinfo=None), last.astimezone(pytz.utc).replace(tzinfo=None), True
        hours = (calendar.hours_per_day if calendar else 0) or DEFAULT_HOURS
        check_in = tz.localize(datetime.combine(self.date, DEFAULT_START)).astimezone(pytz.utc).replace(tzinfo=None)
        return check_in, check_in + timedelta(hours=hours), False

    def action_confirm(self):
        self.ensure_one()
        if not self.project_id:
            raise UserError(_("Elige la obra."))
        lines = self.line_ids.filtered('include')
        if not lines:
            raise UserError(_("No hay personas para registrar."))
        if lines.filtered(lambda l: not l.employee_id):
            raise UserError(_("Hay %s nombres sin identificar: elige el empleado o quita la palomita.",
                              len(lines.filtered(lambda l: not l.employee_id))))
        Attendance = self.env['hr.attendance']
        Alias = self.env['hr.field.alias']
        created = Attendance
        skipped = []
        for line in lines:
            employee = line.employee_id
            if line.match != 'exact' or line.employee_id.id != line.suggested_employee_id.id:
                key = normalize(line.raw_name)
                alias = Alias.search([('name', '=', key)], limit=1)
                if alias:
                    alias.employee_id = employee
                elif normalize(employee.name) != key:
                    Alias.create({'name': key, 'employee_id': employee.id})
            if line.conflict:
                skipped.append(f"{employee.name}: {line.conflict}")
                continue
            check_in, check_out, regular = self._employee_day(employee)
            reasons = [] if regular else [_("Día no laborable según su horario")]
            created |= Attendance.create({
                'employee_id': employee.id,
                'check_in': check_in,
                'check_out': check_out,
                'in_mode': 'manual',
                'out_mode': 'manual',
                'field_project_id': self.project_id.id,
                'field_supervisor_id': self.supervisor_id.id,
                'field_state': 'review' if reasons else 'valid',
                'field_review_reason': '\n'.join(reasons) or False,
            })
        message = _("Se registraron %(count)s personas en %(project)s.",
                    count=len(created), project=self.project_id.display_name)
        if skipped:
            message += " " + _("Sin registrar por estar ya en otra lista: %s.", "; ".join(skipped))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Lista cargada"),
                'message': message,
                'type': 'warning' if skipped else 'success',
                'sticky': bool(skipped),
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }


class HrFieldRosterLine(models.TransientModel):
    _name = 'hr.field.roster.line'
    _description = "Persona de la lista de WhatsApp"

    wizard_id = fields.Many2one('hr.field.roster.import', required=True, ondelete='cascade')
    include = fields.Boolean("Registrar", default=True)
    raw_name = fields.Char("Como viene en el mensaje", readonly=True)
    employee_id = fields.Many2one('hr.employee', "Empleado")
    suggested_employee_id = fields.Many2one('hr.employee')
    match = fields.Selection([
        ('exact', "Coincide"),
        ('alias', "Recordado"),
        ('fuzzy', "Parecido, confirma"),
        ('none', "No encontrado"),
    ], string="Reconocimiento", readonly=True)
    conflict = fields.Char("Aviso", compute='_compute_conflict')

    @api.depends('employee_id', 'wizard_id.date', 'wizard_id.project_id')
    def _compute_conflict(self):
        for line in self:
            line.conflict = False
            if not line.employee_id or not line.wizard_id.date:
                continue
            existing = self.env['hr.attendance'].search([
                ('employee_id', '=', line.employee_id.id),
                ('date', '=', line.wizard_id.date),
            ], limit=1)
            if existing:
                where = existing.field_project_id.display_name or _("sin obra")
                who = existing.field_supervisor_id.name
                line.conflict = _("ya tiene asistencia ese día en %(where)s%(who)s",
                                  where=where, who=f" ({who})" if who else "")
