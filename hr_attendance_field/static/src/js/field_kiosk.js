/* Asistencias en campo: kiosco del supervisor o marcaje personal del trabajador. */
(function () {
    "use strict";

    const { rpc, loadModels, startCamera, detect, snapshot, toast, distanceMeters } = window.FieldCommon;
    const app = document.getElementById("app");
    const token = app.dataset.token;
    const base = "/campo/" + encodeURIComponent(token);
    const storageKey = "field_attendance_device_" + token;
    const $ = (id) => document.getElementById(id);

    let deviceKey = null;
    let state = null;
    let position = null;
    let busy = false;
    let pending = null; // last punch request, reused when the server asks for a project or a PIN

    function readKey() {
        try {
            return window.localStorage.getItem(storageKey);
        } catch {
            return null;
        }
    }

    function writeKey(value) {
        try {
            window.localStorage.setItem(storageKey, value);
        } catch {
            toast("Este navegador no permite guardar la autorización. Usa Chrome o Safari normal (no privado).", "error", 8000);
        }
    }

    function show(screen) {
        for (const id of ["screen-register", "screen-pending", "screen-blocked", "screen-kiosk"]) {
            $(id).hidden = id !== screen;
        }
    }

    function openPanel(id) {
        $(id).hidden = false;
    }

    function closePanel(id) {
        $(id).hidden = true;
    }

    function renderActive() {
        const names = (state.active_projects || []).map((p) => p.name);
        if (names.length) {
            $("active-projects").textContent = "Obras de hoy: " + names.join(", ");
        } else if (state.role === "supervisor") {
            $("active-projects").textContent = "Sin obras activas: toca «Obras de hoy»";
        } else {
            $("active-projects").textContent = "Tu supervisor no ha activado obras hoy";
        }
        $("btn-projects").hidden = state.role !== "supervisor";
        $("btn-change").hidden = (state.active_projects || []).length < 2;
    }

    async function refresh() {
        deviceKey = readKey();
        const result = await rpc(base + "/state", { device_key: deviceKey });
        if (result.error) {
            $("active-projects").textContent = result.error;
            return;
        }
        if (result.device === "unregistered") {
            show("screen-register");
            $("active-projects").textContent = "";
            return;
        }
        if (result.device === "pending") {
            show("screen-pending");
            $("active-projects").textContent = "";
            return;
        }
        if (result.device === "revoked") {
            show("screen-blocked");
            $("active-projects").textContent = "";
            return;
        }
        state = result;
        show("screen-kiosk");
        renderActive();
        startKiosk();
    }

    let kioskStarted = false;
    async function startKiosk() {
        if (kioskStarted) {
            return;
        }
        kioskStarted = true;
        watchGps();
        try {
            await startCamera($("video"));
            $("camera-hint").textContent = "Cargando reconocimiento facial…";
            await loadModels();
            $("camera-hint").textContent = "Ponte frente a la cámara y toca Marcar";
            $("btn-punch").disabled = false;
        } catch (error) {
            $("camera-hint").textContent = "No se pudo abrir la cámara: " + error.message;
        }
    }

    function watchGps() {
        if (!navigator.geolocation) {
            $("gps-status").textContent = "Este teléfono no tiene GPS disponible";
            return;
        }
        navigator.geolocation.watchPosition(
            (pos) => {
                position = pos.coords;
                $("gps-status").textContent = "Ubicación lista (±" + Math.round(pos.coords.accuracy) + " m)";
            },
            (error) => {
                position = null;
                $("gps-status").textContent = "Sin ubicación: activa el GPS y da permiso al navegador (" + error.message + ")";
            },
            { enableHighAccuracy: true, maximumAge: 30000, timeout: 20000 }
        );
    }

    function projectsByDistance() {
        const projects = (state.active_projects || []).slice();
        for (const p of projects) {
            p._distance = position && p.has_location
                ? distanceMeters(position.latitude, position.longitude, p.latitude, p.longitude)
                : null;
        }
        projects.sort((a, b) => (a._distance ?? Infinity) - (b._distance ?? Infinity));
        return projects;
    }

    function formatDistance(meters) {
        if (meters === null) {
            return "";
        }
        return meters < 1000 ? Math.round(meters) + " m" : (meters / 1000).toFixed(1) + " km";
    }

    async function send(extra) {
        const params = Object.assign({}, pending, extra || {});
        pending = params;
        const result = await rpc(base + "/punch", params);
        if (result.need_project) {
            chooseProject();
            return;
        }
        if (result.need_pin) {
            askPin();
            return;
        }
        pending = null;
        if (result.error) {
            toast(result.error, "error", 6000);
            return;
        }
        const verb = result.action === "check_in" ? "Entrada" : "Salida";
        let message = verb + " registrada: " + result.employee + (result.project ? " · " + result.project : "");
        if (result.state === "review") {
            message += " (en revisión: " + (result.reasons || []).join(", ") + ")";
        }
        toast(message, result.state === "review" ? "warning" : "success", 6000);
    }

    async function punch(changeProject) {
        if (busy) {
            return;
        }
        busy = true;
        $("btn-punch").disabled = true;
        $("camera-hint").textContent = "Reconociendo…";
        try {
            const descriptor = await detect($("video"), 4);
            const photo = snapshot($("video"), $("canvas"));
            pending = {
                device_key: deviceKey,
                descriptor: descriptor,
                photo: photo,
                latitude: position ? position.latitude : null,
                longitude: position ? position.longitude : null,
                change_project: !!changeProject,
            };
            if (!descriptor) {
                askPin();
                return;
            }
            await send();
        } catch (error) {
            toast(error.message, "error", 6000);
        } finally {
            busy = false;
            $("btn-punch").disabled = false;
            $("camera-hint").textContent = "Ponte frente a la cámara y toca Marcar";
        }
    }

    function chooseProject() {
        const list = $("choose-list");
        list.replaceChildren();
        projectsByDistance().forEach((project, index) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "fk-btn " + (index === 0 ? "fk-btn-primary" : "fk-btn-light");
            const distance = formatDistance(project._distance);
            button.textContent = project.name + (distance ? " · " + distance : "") + (index === 0 && distance ? " (más cercana)" : "");
            button.addEventListener("click", async () => {
                closePanel("panel-choose");
                try {
                    await send({ project_id: project.id });
                } catch (error) {
                    toast(error.message, "error", 6000);
                }
            });
            list.appendChild(button);
        });
        openPanel("panel-choose");
    }

    function askPin() {
        const select = $("pin-employee");
        select.replaceChildren();
        for (const person of state.crew || []) {
            const option = document.createElement("option");
            option.value = person.id;
            option.textContent = person.name;
            select.appendChild(option);
        }
        $("pin-code").value = "";
        openPanel("panel-pin");
    }

    function openProjects() {
        const list = $("assignable-list");
        list.replaceChildren();
        const active = new Set((state.active_projects || []).map((p) => p.id));
        if (!(state.assignable_projects || []).length) {
            list.textContent = "Operaciones no te ha asignado obras.";
        }
        for (const project of state.assignable_projects || []) {
            const label = document.createElement("label");
            label.className = "fk-check";
            const input = document.createElement("input");
            input.type = "checkbox";
            input.value = project.id;
            input.checked = active.has(project.id);
            const span = document.createElement("span");
            span.textContent = project.name;
            label.append(input, span);
            list.appendChild(label);
        }
        $("activate-pin").value = "";
        openPanel("panel-projects");
    }

    async function activate() {
        const ids = Array.from($("assignable-list").querySelectorAll("input:checked")).map((i) => Number(i.value));
        try {
            const result = await rpc(base + "/activate", {
                device_key: deviceKey,
                pin: $("activate-pin").value,
                project_ids: ids,
            });
            if (result.error) {
                toast(result.error, "error", 6000);
                return;
            }
            state.active_projects = result.active_projects;
            renderActive();
            closePanel("panel-projects");
            toast("Obras de hoy guardadas", "success");
        } catch (error) {
            toast(error.message, "error", 6000);
        }
    }

    async function register() {
        try {
            const result = await rpc(base + "/register", { name: $("device-name").value });
            if (result.error) {
                toast(result.error, "error", 6000);
                return;
            }
            writeKey(result.device_key);
            await refresh();
        } catch (error) {
            toast(error.message, "error", 6000);
        }
    }

    $("btn-register").addEventListener("click", register);
    $("btn-refresh").addEventListener("click", () => refresh().catch((e) => toast(e.message, "error")));
    $("btn-punch").addEventListener("click", () => punch(false));
    $("btn-change").addEventListener("click", () => punch(true));
    $("btn-projects").addEventListener("click", openProjects);
    $("btn-activate").addEventListener("click", activate);
    $("btn-pin").addEventListener("click", async () => {
        closePanel("panel-pin");
        try {
            await send({ employee_id: Number($("pin-employee").value), pin: $("pin-code").value });
        } catch (error) {
            toast(error.message, "error", 6000);
        }
    });
    for (const button of document.querySelectorAll("[data-close]")) {
        button.addEventListener("click", () => {
            closePanel(button.dataset.close);
            pending = null;
        });
    }

    refresh().catch((error) => ($("active-projects").textContent = error.message));
})();
