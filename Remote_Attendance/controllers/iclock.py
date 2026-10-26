import logging
from datetime import datetime

import pytz

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class FieldClock(http.Controller):
    """ZKTeco ADMS (PUSH protocol): the clock calls Odoo by itself with the punches.

    The clock only sends its serial number; Operations approves it in Asistencias › Campo › Checadores
    before its punches are accepted. While it is pending, the clock keeps its punches and resends them."""

    def _text(self, text, status=200):
        return request.make_response(text, headers=[('Content-Type', 'text/plain')], status=status)

    def _clock(self):
        serial = request.params.get('SN')
        return request.env['hr.field.clock'].sudo()._clock_seen(serial, request.httprequest.remote_addr)

    @staticmethod
    def _offset_hours(tz):
        offset = datetime.now(pytz.timezone(tz)).utcoffset().total_seconds() / 3600
        return int(offset) if offset == int(offset) else offset

    def _options(self, clock):
        return "\n".join([
            f"GET OPTION FROM: {clock.serial}",
            f"ATTLOGStamp={clock.att_stamp or 'None'}",
            "OPERLOGStamp=9999",
            "ATTPHOTOStamp=9999",
            "ErrorDelay=60",
            "Delay=30",
            "TransTimes=00:00;14:05",
            "TransInterval=1",
            "TransFlag=TransData AttLog",
            f"TimeZone={self._offset_hours(clock.tz)}",
            "Realtime=1",
            "Encrypt=None",
        ]) + "\n"

    @http.route(['/iclock/cdata', '/iclock/cdata.aspx'], type='http', auth='public', methods=['GET', 'POST'],
                csrf=False, save_session=False, sitemap=False)
    def cdata(self, **kwargs):
        clock = self._clock()
        if not clock:
            return self._text("ERROR", status=403)
        if request.httprequest.method == 'GET':
            return self._text(self._options(clock))
        table = (kwargs.get('table') or '').upper()
        if table != 'ATTLOG':
            # Operation log, photos, users...: nothing to do with them.
            return self._text("OK")
        if clock.state != 'approved':
            _logger.info("Time clock %s (%s) sent punches but is not approved", clock.serial, clock.state)
            return self._text("ERROR: checador no aprobado", status=403)
        body = request.httprequest.get_data(as_text=False).decode('utf-8', errors='replace')
        count = clock._clock_receive(body, kwargs.get('Stamp'))
        return self._text(f"OK: {count}")

    @http.route(['/iclock/getrequest', '/iclock/getrequest.aspx'], type='http', auth='public', methods=['GET'],
                csrf=False, save_session=False, sitemap=False)
    def getrequest(self, **kwargs):
        # No commands for the clock: users and faces are registered on the clock itself.
        self._clock()
        return self._text("OK")

    @http.route(['/iclock/devicecmd', '/iclock/devicecmd.aspx'], type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False, sitemap=False)
    def devicecmd(self, **kwargs):
        return self._text("OK")
