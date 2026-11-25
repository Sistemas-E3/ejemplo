import base64
import csv
import io
import unicodedata
from datetime import date, datetime, time

from odoo import _, fields, models
from odoo.exceptions import UserError

IMPORT_SERIAL = 'ARCHIVO'
# Column names seen in ZKTeco exports (USB, ZKBio Time, ZKTime.Net), compared without accents or case.
KEY_HEADERS = {'no', 'no.', 'id', 'ac-no', 'ac-no.', 'pin', 'clave', 'numero', 'no. empleado', 'no empleado',
               'numero de empleado', 'id de usuario', 'id usuario', 'user id', 'userid', 'emp no', 'emp no.',
               'id de empleado', 'id empleado', 'employee id', 'personnel id', 'enno', 'numero de usuario'}
DATETIME_HEADERS = {'fecha/hora', 'fecha hora', 'fecha y hora', 'date/time', 'date time', 'datetime', 'checktime',
                    'check time', 'tiempo', 'hora de marcaje', 'marcaje', 'registro', 'punch time', 'time'}
DATE_HEADERS = {'fecha', 'date', 'dia'}
TIME_HEADERS = {'hora', 'time', 'hora de registro'}
DATETIME_FORMATS = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M',
                    '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d-%m-%Y %H:%M:%S', '%d-%m-%Y %H:%M',
                    '%Y-%m-%dT%H:%M:%S']
DATE_FORMATS = ['%Y-%m-%d', '%Y/%m/%d', '%d/%m/%Y', '%d-%m-%Y']
TIME_FORMATS = ['%H:%M:%S', '%H:%M']


def normalize(value):
    text = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode()
    return ' '.join(text.lower().split())


def parse_datetime(value):
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    text = str(value or '').strip()
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or '').strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_time(value):
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, time):
        return value
    text = str(value or '').strip()
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def clean_key(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value or '').strip()


class HrFieldClockImport(models.TransientModel):
    """Load the punches of a time clock from a file: the one saved on a USB drive (attlog .dat) or an
    Excel/CSV export. They go through the same path as the punches the clock sends by itself."""
    _name = 'hr.field.clock.import'
    _description = "Importar marcajes del checador"

    file = fields.Binary("Archivo", required=True)
    filename = fields.Char("Nombre del archivo")
    clock_id = fields.Many2one(
        'hr.field.clock', "Checador", domain=[('state', '=', 'approved')],
        help="Vacío = un checador \"Importado de archivo\". Elige el checador real si ya está conectado, "
             "para que un marcaje que llegue por los dos caminos se guarde una sola vez.")

    def _rows(self):
        data = base64.b64decode(self.file)
        name = (self.filename or '').lower()
        if name.endswith('.xlsx') or data[:2] == b'PK':
            import openpyxl
            book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            return [list(row) for sheet in book.worksheets for row in sheet.iter_rows(values_only=True)]
        if name.endswith('.xls') or data[:4] == b'\xd0\xcf\x11\xe0':
            import xlrd
            book = xlrd.open_workbook(file_contents=data)
            rows = []
            for sheet in book.sheets():
                for r in range(sheet.nrows):
                    row = []
                    for c in range(sheet.ncols):
                        cell = sheet.cell(r, c)
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            row.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
                        else:
                            row.append(cell.value)
                    rows.append(row)
            return rows
        for encoding in ('utf-8-sig', 'utf-16', 'latin-1'):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if '\t' in text:
            return [line.split('\t') for line in text.splitlines()]
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;') if text.strip() else csv.excel
        return list(csv.reader(io.StringIO(text), dialect))

    @staticmethod
    def _columns(rows):
        """(header row index, key col, datetime col, date col, time col); without header, the first two columns."""
        for index, row in enumerate(rows[:15]):
            names = [normalize(cell) for cell in row]
            key = next((i for i, n in enumerate(names) if n in KEY_HEADERS), None)
            moment = next((i for i, n in enumerate(names) if n in DATETIME_HEADERS and n != 'time'), None)
            day = next((i for i, n in enumerate(names) if n in DATE_HEADERS), None)
            hour = next((i for i, n in enumerate(names) if n in TIME_HEADERS), None)
            if moment is None and day is None:
                moment = next((i for i, n in enumerate(names) if n == 'time'), None)
            if key is not None and (moment is not None or (day is not None and hour is not None)):
                return index, key, moment, day, hour
        return -1, 0, 1, None, None

    def _punches(self):
        """[(employee key, local datetime)] and the number of rows that could not be read."""
        rows = [row for row in self._rows() if any(clean_key(cell) for cell in row)]
        header, key_col, moment_col, day_col, hour_col = self._columns(rows)
        punches, skipped = [], 0
        for row in rows[header + 1:]:
            def cell(col):
                return row[col] if col is not None and col < len(row) else None
            key = clean_key(cell(key_col))
            if moment_col is not None:
                moment = parse_datetime(cell(moment_col))
            else:
                day, hour = parse_date(cell(day_col)), parse_time(cell(hour_col))
                moment = datetime.combine(day, hour) if day and hour else None
            if key and moment:
                punches.append((key, moment))
            else:
                skipped += 1
        return punches, skipped

    def _import_clock(self):
        if self.clock_id:
            return self.clock_id
        Clock = self.env['hr.field.clock'].sudo()
        clock = Clock.search([('serial', '=', IMPORT_SERIAL)], limit=1)
        if not clock:
            clock = Clock.create({'name': _("Importado de archivo"), 'serial': IMPORT_SERIAL, 'state': 'approved'})
        return clock

    def action_import(self):
        self.ensure_one()
        try:
            punches, skipped = self._punches()
        except Exception as e:  # noqa: BLE001 - any unreadable file gets the same message
            raise UserError(_("No pude leer el archivo. Usa el archivo de marcajes del USB (.dat), Excel o CSV.\n%s", e))
        if not punches:
            raise UserError(_("No encontré marcajes en el archivo. Debe tener el número de empleado y la fecha y hora."))
        clock = self._import_clock()
        before = set(clock.log_ids.ids)
        body = "\n".join(f"{key}\t{moment:%Y-%m-%d %H:%M:%S}" for key, moment in sorted(punches, key=lambda p: p[1]))
        clock._clock_receive(body)
        new_ids = sorted(set(clock.log_ids.ids) - before)
        if not new_ids:
            raise UserError(_("Los %s marcajes del archivo ya estaban importados.", len(punches)))
        action = self.env['ir.actions.act_window']._for_xml_id('Remote_Attendance.hr_field_clock_log_action')
        action.update({
            'name': _("Marcajes importados (%(new)s nuevos, %(skipped)s renglones sin leer)",
                      new=len(new_ids), skipped=skipped),
            'domain': [('id', 'in', new_ids)],
            'context': {},
            'target': 'current',
        })
        return action
