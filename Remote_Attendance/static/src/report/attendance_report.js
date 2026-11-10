/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { download } from "@web/core/network/download";

const PAGE_SIZE = 50;
const { DateTime } = luxon;

export class FieldAttendanceReport extends Component {
    static template = "Remote_Attendance.AttendanceReport";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        const today = DateTime.now().toISODate();
        this.state = useState({
            filters: {
                date_from: today,
                date_to: today,
                company_id: "",
                department_id: "",
                supervisor_id: "",
                scope: "",
                search: "",
            },
            page: 0,
            data: null,
            loading: false,
            error: "",
        });
        onWillStart(() => this.load());
    }

    get pageCount() {
        return this.state.data ? Math.max(1, Math.ceil(this.state.data.count / PAGE_SIZE)) : 1;
    }

    async load() {
        this.state.loading = true;
        this.state.error = "";
        try {
            this.state.data = await this.orm.call("hr.field.attendance.report", "get_report", [], {
                filters: this.state.filters,
                offset: this.state.page * PAGE_SIZE,
                limit: PAGE_SIZE,
            });
        } catch (e) {
            this.state.error = e.data?.message || e.message || String(e);
        } finally {
            this.state.loading = false;
        }
    }

    reload() {
        this.state.page = 0;
        return this.load();
    }

    onFilter(field, ev) {
        this.state.filters[field] = ev.target.value;
        return this.reload();
    }

    onSearch(ev) {
        if (ev.key === "Enter") {
            this.state.filters.search = ev.target.value;
            return this.reload();
        }
    }

    preset(kind) {
        const now = DateTime.now();
        let from = now;
        let to = now;
        if (kind === "yesterday") {
            from = to = now.minus({ days: 1 });
        } else if (kind === "week") {
            from = now.startOf("week");
        } else if (kind === "month") {
            from = now.startOf("month");
        } else if (kind === "last_month") {
            from = now.minus({ months: 1 }).startOf("month");
            to = now.minus({ months: 1 }).endOf("month");
        } else if (kind === "year") {
            from = now.minus({ years: 1 }).plus({ days: 1 });
        }
        this.state.filters.date_from = from.toISODate();
        this.state.filters.date_to = to.toISODate();
        return this.reload();
    }

    goPage(page) {
        this.state.page = Math.min(Math.max(page, 0), this.pageCount - 1);
        return this.load();
    }

    hours(value) {
        const minutes = Math.round((value || 0) * 60);
        return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
    }

    cellTitle(row, cell, day) {
        const parts = [`${row.name} · ${day.weekday} ${day.label}`];
        if (cell.code) {
            parts.push(this.state.data.codes[cell.code]);
        }
        if (cell.in) {
            parts.push(`Entrada ${cell.in}` + (cell.out ? ` · Salida ${cell.out}` : " · sin salida"));
            parts.push(`Horas ${this.hours(cell.hours)}`);
        }
        if (cell.flag) {
            parts.push("Revisar: en revisión, sin salida o sin proyecto");
        }
        return parts.join("\n");
    }

    async exportExcel() {
        await download({
            url: "/campo/reporte-asistencia/excel",
            data: { filters: JSON.stringify(this.state.filters) },
        });
    }

    exportPdf() {
        return this.action.doAction({
            type: "ir.actions.report",
            report_type: "qweb-pdf",
            report_name: "Remote_Attendance.attendance_report_pdf",
            report_file: "Remote_Attendance.attendance_report_pdf",
            data: { filters: { ...this.state.filters } },
        });
    }
}

registry.category("actions").add("remote_attendance_report", FieldAttendanceReport);
