import difflib
import re
import unicodedata
from odoo import _, api, fields, models
from odoo.exceptions import UserError

# "[29/09/26, 8:15] Juan: texto" (iOS) o "29/09/26, 8:15 - Juan: texto" (Android)
WHATSAPP_PREFIX = re.compile(
    r'^\s*(\[[^\]]*\]\s*[^:]{1,60}:\s*|\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}[^-]*-\s*[^:]{1,60}:\s*)')
BULLET = re.compile(r'^\s*([-*•·>]+|\d{1,3}\s*[.)\-]+|\d{1,3}\s+(?=\D))\s*')
KEY_LINE = re.compile(
    r'^\s*(presupuesto|proyecto|obra|actividad|ubicacion|personal)\s*[:.\-]?\s*(.*)$', re.IGNORECASE)
FRACTION = re.compile(
    r'(?<![\d/])(\d)\s*/\s*(\d)(?![\d/])\s*(?:de\s+)?(?:d[ií]as?|jornadas?)?'
    r'|½\s*(?:d[ií]a|jornada)?'
    r'|\bmedi[oa]\s+(?:d[ií]a|jornada)\b',
    re.IGNORECASE)
HOURS = re.compile(r'\b(\d{1,2}(?:[.,]\d+)?)\s*(?:h|hr|hrs|horas?)\b', re.IGNORECASE)
HEADER_WORDS = {
    'lista', 'asistencia', 'personal', 'gente', 'trabajadores', 'hoy', 'buenos', 'dias', 'buen', 'dia',
    'de', 'del', 'la', 'el', 'en', 'y', 'que', 'ingreso', 'ingresaron', 'entraron', 'entrada',
}


def normalize(text):
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r'[^a-z0-9ñ ]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def clean_line(line):
    line = WHATSAPP_PREFIX.sub('', line)
    line = BULLET.sub('', line)
    return line.strip(' \t-–—:;,.*_')


def split_amount(text):
    """Return (text without the amount, fraction or None, hours or None)."""
    fraction = hours = None
    match = FRACTION.search(text)
    if match:
        if match.group(1):
            denominator = int(match.group(2))
            fraction = int(match.group(1)) / denominator if denominator else None
        else:
            fraction = 0.5
        text = text[:match.start()] + text[match.end():]
    match = HOURS.search(text)
    if match:
        hours = float(match.group(1).replace(',', '.'))
        text = text[:match.start()] + text[match.end():]
    return text.strip(' \t-–—:;,.()*_'), fraction, hours


def is_header(text):
    words = set(normalize(text).split())
    return not words or words <= HEADER_WORDS


def parse_message(message):
    """Split a supervisor message into blocks of people.

    Returns a list of dicts: ``code`` (presupuesto), ``title``, ``activity``, ``location``,
    ``fraction`` (default for the block), ``tentative`` (title may be a person) and
    ``people`` as a list of (name, fraction, hours)."""
    groups, current = [], []
    for raw in (message or '').splitlines():
        line = clean_line(raw)
        if line:
            current.append(line)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    def new_block(**kwargs):
        block = {'code': '', 'title': '', 'activity': '', 'location': '', 'fraction': None,
                 'tentative': False, 'people': []}
        block.update(kwargs)
        blocks.append(block)
        return block

    def add_person(block, text):
        name, fraction, hours = split_amount(text)
        if name and not is_header(name) and re.search(r'[A-Za-zÁÉÍÓÚÑáéíóúñ]', name):
            block['people'].append((name, fraction, hours))

    blocks, block = [], None
    for index, group in enumerate(groups):
        keys = [KEY_LINE.match(normalize_keyless(line)) for line in group]
        if any(k and k.group(1).lower() in ('presupuesto', 'proyecto', 'obra') and k.group(2) for k in keys):
            block = new_block()
            in_people = False
            for line, key in zip(group, keys):
                if key:
                    field, value = key.group(1).lower(), original_value(line)
                    if field in ('presupuesto', 'proyecto', 'obra'):
                        block['code'] = value
                    elif field == 'actividad':
                        block['activity'] = value
                    elif field == 'ubicacion':
                        block['location'] = value
                    elif field == 'personal':
                        in_people = True
                        if value:
                            add_person(block, value)
                elif in_people:
                    add_person(block, line)
            continue
        next_group = groups[index + 1] if index + 1 < len(groups) else None
        next_is_project = next_group and any(
            (k := KEY_LINE.match(normalize_keyless(l))) and k.group(1).lower() in ('presupuesto', 'proyecto', 'obra')
            for l in next_group)
        if len(group) == 1 and next_group and not next_is_project and (block is None or block['people']):
            title, fraction, _hours = split_amount(group[0])
            block = new_block(title=title, fraction=fraction)
            continue
        if block is None:
            title, fraction, _hours = split_amount(group[0])
            block = new_block(title=title, fraction=fraction, tentative=True)
            group = group[1:]
        for line in group:
            add_person(block, line)
    return [b for b in blocks if b['people'] or b['tentative']]


def normalize_keyless(line):
    """Lowercase and strip accents but keep punctuation, to read 'KEY: value' lines."""
    text = unicodedata.normalize('NFKD', line)
    return ''.join(c for c in text if not unicodedata.combining(c))


def original_value(line):
    match = re.match(r'^\s*\S+\s*[:.\-]?\s*(.*)$', line)
    return (match.group(1) if match else '').strip(' \t:;,.')


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


class HrFieldProjectAlias(models.Model):
    _name = 'hr.field.project.alias'
    _description = "Obra o actividad recordada (listas de WhatsApp)"
    _order = 'name'

    name = fields.Char("Como lo escriben", required=True, index=True)
    project_id = fields.Many2one('project.project', "Proyecto", required=True, ondelete='cascade')

    _name_unique = models.Constraint('UNIQUE(name)', "Ese texto ya está asociado a un proyecto.")

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
                          help="Pega el mensaje tal cual: presupuesto u obra y la lista de gente.")
    supervisor_id = fields.Many2one('hr.employee', "Supervisor que envió la lista",
                                    domain=[('field_role', '=', 'supervisor')])
    block_ids = fields.One2many('hr.field.roster.block', 'wizard_id', "Obras y actividades")
    line_ids = fields.One2many('hr.field.roster.line', 'wizard_id', "Personas")
    state = fields.Selection([('draft', "Pegar"), ('preview', "Revisar")], default='draft')
    unmatched_count = fields.Integer(compute='_compute_counts')
    unassigned_block_count = fields.Integer(compute='_compute_counts')

    @api.depends('line_ids.employee_id', 'line_ids.include', 'block_ids.project_id')
    def _compute_counts(self):
        for wizard in self:
            lines = wizard.line_ids.filtered('include')
            wizard.unmatched_count = len(lines.filtered(lambda l: not l.employee_id))
            wizard.unassigned_block_count = len(wizard.block_ids.filtered(lambda b: not b.project_id))

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    @api.model
    def _project_by_code(self, code):
        Project = self.env['project.project'].with_context(active_test=True)
        domain = [('is_template', '=', False)]
        variants = {code.strip(), re.sub(r'\s+', '', code)}
        core = re.match(r'\s*(\d[\d\-/]*\d|\d)', code)
        if core:
            variants.add(core.group(1))
        variants.discard('')
        for value in sorted(variants, key=len, reverse=True):
            project = Project.search(domain + ['|', ('field_code', '=ilike', value), ('account_id.code', '=ilike', value)], limit=2)
            if len(project) == 1:
                return project
        if core:
            project = Project.search(domain + [('name', 'ilike', core.group(1))], limit=2)
            if len(project) == 1:
                return project
        return Project.browse()

    @api.model
    def _project_by_text(self, text):
        Project = self.env['project.project']
        key = normalize(text)
        if len(key) < 3:
            return Project
        alias = self.env['hr.field.project.alias'].search([('name', '=', key)], limit=1)
        if alias:
            return alias.project_id
        for number in re.findall(r'\d{2,}(?:-\d+)?', text):
            project = self._project_by_code(number)
            if project:
                return project
        matches = Project.search([('is_template', '=', False)]).filtered(
            lambda p: normalize(p.name) and (normalize(p.name) == key or normalize(p.name) in key))
        return matches.sorted(lambda p: len(p.name), reverse=True)[:1]

    @api.model
    def _resolve_block(self, block):
        """Return (project, match, alias key, display title)."""
        Project = self.env['project.project']
        parts = [p for p in (block['code'], block['activity'], block['location']) if p]
        title = " · ".join(parts) if parts else block['title']
        if block['code']:
            key = block['code']
            alias = self.env['hr.field.project.alias'].search([('name', '=', normalize(key))], limit=1)
            if alias:
                return alias.project_id, 'alias', key, title
            project = self._project_by_code(block['code'])
            return project, 'exact' if project else 'none', key, title
        if block['title']:
            project = self._project_by_text(block['title'])
            return project, 'exact' if project else 'none', block['title'], title
        return Project, 'none', '', title or _("Sin obra")

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

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def action_read_message(self):
        self.ensure_one()
        blocks = parse_message(self.message)
        staff = self._field_staff()
        staff_names = {e.id: normalize(e.name) for e in staff}
        block_cmds, total_people = [], 0
        for sequence, block in enumerate(blocks):
            project, match, key, title = self._resolve_block(block)
            people = list(block['people'])
            if block['tentative']:
                if project:
                    title = block['title']
                else:
                    # The first line was a person, not a project.
                    name, fraction, hours = split_amount(block['title'])
                    if name and not is_header(name):
                        people.insert(0, (name, fraction, hours))
                    project, match, key, title = self.env['project.project'], 'none', '', _("Sin obra")
            seen, line_cmds = set(), []
            for raw, fraction, hours in people:
                employee, emp_match = self._match_employee(raw, staff, staff_names)
                duplicate = bool(employee) and employee.id in seen
                if employee:
                    seen.add(employee.id)
                line_cmds.append({
                    'wizard_id': self.id,
                    'raw_name': raw,
                    'employee_id': employee.id,
                    'suggested_employee_id': employee.id,
                    'match': emp_match,
                    'include': not duplicate,
                    'fraction': fraction or block['fraction'] or 1.0,
                    'hours': hours or 0.0,
                })
            total_people += len(line_cmds)
            block_cmds.append({
                'sequence': sequence,
                'title': title,
                'alias_key': key,
                'project_id': project.id,
                'suggested_project_id': project.id,
                'match': match,
                'line_ids': [fields.Command.create(vals) for vals in line_cmds],
            })
        if not total_people:
            raise UserError(_("No encontré nombres en el mensaje."))
        self.block_ids.unlink()
        self.line_ids.unlink()
        self.write({'state': 'preview', 'block_ids': [fields.Command.create(vals) for vals in block_cmds]})
        if not self.supervisor_id:
            supervisors = self.block_ids.project_id.field_supervisor_ids
            if len(supervisors) == 1:
                self.supervisor_id = supervisors
        return self._reopen()

    def action_back(self):
        self.block_ids.unlink()
        self.line_ids.unlink()
        self.state = 'draft'
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

    def _learn_aliases(self, lines):
        Alias = self.env['hr.field.alias']
        for line in lines:
            if line.match == 'exact' and line.employee_id == line.suggested_employee_id:
                continue
            key = normalize(line.raw_name)
            if not key or normalize(line.employee_id.name) == key:
                continue
            alias = Alias.search([('name', '=', key)], limit=1)
            if alias:
                alias.employee_id = line.employee_id
            else:
                Alias.create({'name': key, 'employee_id': line.employee_id.id})
        ProjectAlias = self.env['hr.field.project.alias']
        for block in self.block_ids.filtered(lambda b: b.project_id and b.alias_key):
            if block.match in ('exact', 'alias') and block.project_id == block.suggested_project_id:
                continue
            key = normalize(block.alias_key)
            alias = ProjectAlias.search([('name', '=', key)], limit=1)
            if alias:
                alias.project_id = block.project_id
            else:
                ProjectAlias.create({'name': key, 'project_id': block.project_id.id})

    def action_confirm(self):
        self.ensure_one()
        lines = self.line_ids.filtered('include')
        if not lines:
            raise UserError(_("No hay personas para registrar."))
        missing_blocks = lines.block_id.filtered(lambda b: not b.project_id)
        if missing_blocks:
            raise UserError(_("Elige el proyecto de: %s", ", ".join(missing_blocks.mapped('title'))))
        missing = lines.filtered(lambda l: not l.employee_id)
        if missing:
            raise UserError(_("Hay %s nombres sin identificar: elige el empleado o quita la palomita.", len(missing)))
        self._learn_aliases(lines)

        result = self.env['hr.field.service']._register_day(self.date, self.supervisor_id, [{
            'employee': line.employee_id,
            'project': line.block_id.project_id,
            'name': line.block_id.title,
            'fraction': line.fraction,
            'hours': line.hours,
        } for line in lines])
        done, review, skipped = result['done'], result['review'], result['skipped']
        message = _("Se registraron %s personas.", len(done) + len(review))
        if review:
            message += " " + _("En revisión: %s.", ", ".join(review))
        if skipped:
            message += " " + _("Sin registrar porque ya marcaron con el teléfono ese día: %s.", ", ".join(skipped))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Lista cargada"),
                'message': message,
                'type': 'warning' if (skipped or review) else 'success',
                'sticky': bool(skipped or review),
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }


class HrFieldRosterBlock(models.TransientModel):
    _name = 'hr.field.roster.block'
    _description = "Obra o actividad de la lista de WhatsApp"
    _order = 'sequence, id'
    _rec_name = 'title'

    wizard_id = fields.Many2one('hr.field.roster.import', required=True, ondelete='cascade')
    sequence = fields.Integer()
    title = fields.Char("Como viene en el mensaje", readonly=True)
    alias_key = fields.Char()
    project_id = fields.Many2one('project.project', "Proyecto", domain=[('is_template', '=', False)])
    suggested_project_id = fields.Many2one('project.project')
    match = fields.Selection([
        ('exact', "Coincide"),
        ('alias', "Recordado"),
        ('none', "Elige el proyecto"),
    ], string="Reconocimiento", readonly=True)
    line_ids = fields.One2many('hr.field.roster.line', 'block_id')
    people_count = fields.Integer("Personas", compute='_compute_people_count')

    @api.depends('line_ids')
    def _compute_people_count(self):
        for block in self:
            block.people_count = len(block.line_ids)


class HrFieldRosterLine(models.TransientModel):
    _name = 'hr.field.roster.line'
    _description = "Persona de la lista de WhatsApp"
    _order = 'block_sequence, id'

    wizard_id = fields.Many2one('hr.field.roster.import', ondelete='cascade')
    block_id = fields.Many2one('hr.field.roster.block', "Obra", ondelete='cascade', readonly=True)
    block_sequence = fields.Integer(related='block_id.sequence', store=True)
    include = fields.Boolean("Registrar", default=True)
    raw_name = fields.Char("Como viene en el mensaje", readonly=True)
    employee_id = fields.Many2one('hr.employee', "Empleado")
    suggested_employee_id = fields.Many2one('hr.employee')
    fraction = fields.Float("Jornada", digits=(4, 2), default=1.0, help="1 = día completo, 0.5 = medio día.")
    hours = fields.Float("Horas", help="Solo si el mensaje dice horas exactas; si no, se calculan con la jornada.")
    match = fields.Selection([
        ('exact', "Coincide"),
        ('alias', "Recordado"),
        ('fuzzy', "Parecido, confirma"),
        ('none', "No encontrado"),
    ], string="Reconocimiento", readonly=True)
    warning = fields.Char("Aviso", compute='_compute_warning')

    @api.depends('employee_id', 'wizard_id.date', 'fraction', 'include')
    def _compute_warning(self):
        for line in self:
            line.warning = False
            if not line.employee_id or not line.wizard_id.date or not line.include:
                continue
            existing = self.env['hr.attendance'].search([
                ('employee_id', '=', line.employee_id.id),
                ('date', '=', line.wizard_id.date),
            ], limit=1)
            others = line.wizard_id.line_ids.filtered(
                lambda l: l.include and l.employee_id == line.employee_id)
            total = sum(others.mapped('fraction'))
            if existing and not existing.field_allocation_ids:
                line.warning = _("ya marcó con el teléfono ese día; no se registrará")
                continue
            if existing:
                total += sum(existing.field_allocation_ids.mapped('fraction'))
                where = ", ".join(existing.field_allocation_ids.project_id.mapped('display_name'))
                line.warning = _("ya tiene %(f)s jornada en %(where)s; se sumará",
                                 f=round(sum(existing.field_allocation_ids.mapped('fraction')), 2), where=where)
            if total > 1.001:
                line.warning = (line.warning + "; " if line.warning else "") + _(
                    "suma %s jornadas: quedará en revisión", round(total, 2))
