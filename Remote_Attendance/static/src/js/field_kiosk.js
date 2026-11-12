/* Asistencias en campo: kiosco del supervisor o marcaje personal del trabajador. */
(function () {
    "use strict";

    const { rpc, loadModels, startCamera, detect, snapshot, toast, distanceMeters } = window.FieldCommon;
    const app = document.getElementById("app");
    const token = app.dataset.token;
    const base = "/campo/" + encodeURIComponent(token);
    const storageKey = "field_attendance_device_" + token;
    const $ = (id) => document.getElementById(id);
    const isOffice = ["office", "staff"].includes(app.dataset.role);
    const isStaff = app.dataset.role === "staff";
    const ACTIONS = {
        check_in: "Entrada",
        check_out: "Salida",
        lunch_out: "Salida a comer",
        lunch_in: "Regreso de comer",
        project: "Proyecto asignado",
    };

    let deviceKey = null;
    let state = null;
    let position = null;
    let busy = false;
    let pending = null; // last punch request, reused when the server asks for a project or a PIN
    const Offline = window.FieldOffline;
    const stateKey = "field_attendance_state_" + token;
    let offline = false; // working from the saved state, without signal
    let syncing = false;
    let recentQueued = []; // office tablet without signal: faces saved in the last minutes

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
        for (const id of ["screen-register", "screen-pending", "screen-blocked", "screen-kiosk", "screen-roll"]) {
            $(id).hidden = id !== screen;
        }
    }

    function openPanel(id) {
        $(id).hidden = false;
    }

    function closePanel(id) {
        $(id).hidden = true;
    }

    function personLabel(person) {
        return (person.key ? person.key + " · " : "") + person.name;
    }

    function renderActive() {
        if (isOffice) {
            $("active-projects").textContent = isStaff
                ? "Ponte frente a la cámara para marcar tu entrada o salida"
                : "Entrada en oficina: ponte frente a la cámara o pasa tu tarjeta";
            $("btn-projects").hidden = true;
            $("btn-change").hidden = true;
            return;
        }
        const names = (state.active_projects || []).map((p) => p.name);
        if (names.length) {
            $("active-projects").textContent = "Proyectos de hoy: " + names.join(", ");
        } else if (state.role === "supervisor") {
            $("active-projects").textContent = "Sin proyectos activos: toca «Proyectos de hoy»";
        } else {
            $("active-projects").textContent = "Tu supervisor no ha activado proyectos hoy";
        }
        $("btn-projects").hidden = state.role !== "supervisor";
        $("btn-change").hidden = (state.active_projects || []).length < 2;
        $("btn-lunch").hidden = state.role !== "supervisor" || !state.manual;
    }

    function saveState(value) {
        try {
            window.localStorage.setItem(stateKey, JSON.stringify(value));
        } catch {
            // without storage it simply needs signal to open
        }
    }

    function savedState() {
        try {
            return JSON.parse(window.localStorage.getItem(stateKey) || "null");
        } catch {
            return null;
        }
    }

    async function refresh() {
        deviceKey = readKey();
        let result;
        try {
            result = await rpc(base + "/state", { device_key: deviceKey });
            offline = false;
        } catch (error) {
            const saved = deviceKey && Offline.isNetworkError(error) ? savedState() : null;
            if (!saved) {
                throw error;
            }
            // No signal: keep marking with what the phone knew the last time it had signal.
            offline = true;
            result = saved;
        }
        updateOfflineStatus();
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
        if (!offline) {
            saveState(result);
            syncNow();
        }
        if (state.role === "supervisor") {
            $("btn-lunch").hidden = !state.manual;
            setMode(state.manual ? mode : "camera");
        } else {
            show("screen-kiosk");
            renderActive();
            startKiosk();
        }
    }

    // ------------------------------------------------------------------
    // Pase de lista (supervisor): palomear quién vino, sin cámara
    // ------------------------------------------------------------------

    // El enlace abre directo en la cámara. El pase de lista, marcar con PIN y registrar la comida
    // después solo aparecen si Operaciones los activa (parámetro Remote_Attendance.supervisor_manual).
    let mode = "camera";
    let roll = null;
    let people = [];

    function setMode(value) {
        mode = value;
        $("btn-mode").hidden = !state.manual;
        if (mode === "roll") {
            show("screen-roll");
            $("btn-mode").textContent = "Cámara";
            $("btn-projects").hidden = true;
            $("active-projects").textContent = "Palomea a la gente que vino";
            loadRoll().catch((error) => toast(error.message, "error", 6000));
        } else {
            show("screen-kiosk");
            $("btn-mode").textContent = "Pase de lista";
            renderActive();
            startKiosk();
        }
    }

    function plain(text) {
        return (text || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
    }

    function dayLabel(iso, index) {
        const [y, m, d] = iso.split("-");
        return (index === 0 ? "Hoy" : "Ayer") + " " + d + "/" + m;
    }

    async function loadRoll(day) {
        const result = await rpc(base + "/lista", { device_key: deviceKey, date: day || (roll && roll.date) });
        if (result.error) {
            toast(result.error, "error", 6000);
            return;
        }
        roll = result;
        renderDays();
        renderRollProjects();
        applyRegistered();
    }

    function renderDays() {
        const box = $("roll-days");
        box.replaceChildren();
        roll.days.forEach((day, index) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "fk-btn " + (day === roll.date ? "fk-btn-selected" : "fk-btn-light");
            button.textContent = dayLabel(day, index);
            button.addEventListener("click", () => loadRoll(day).catch((e) => toast(e.message, "error", 6000)));
            box.appendChild(button);
        });
    }

    function renderRollProjects() {
        const select = $("roll-project");
        const current = select.value;
        select.replaceChildren();
        for (const project of roll.projects) {
            const option = document.createElement("option");
            option.value = project.id;
            const count = (roll.registered[project.id] || []).length;
            option.textContent = project.name + (count ? " · enviada (" + count + ")" : "");
            select.appendChild(option);
        }
        if (!roll.projects.length) {
            const option = document.createElement("option");
            option.textContent = "Operaciones no te ha asignado proyectos";
            select.appendChild(option);
        }
        if (roll.projects.some((p) => String(p.id) === current)) {
            select.value = current;
        }
        select.disabled = !roll.projects.length;
        $("roll-send").disabled = !roll.projects.length;
    }

    function applyRegistered() {
        const registered = roll.registered[$("roll-project").value] || [];
        const byId = new Map(registered.map((r) => [r.employee_id, r.fraction]));
        people = roll.crew.map((e) => ({
            id: e.id, name: e.name, key: e.key, extra: false, checked: byId.has(e.id), fraction: byId.get(e.id) || 1,
        }));
        for (const r of registered) {
            const other = roll.others.find((e) => e.id === r.employee_id);
            if (other && !people.some((p) => p.id === other.id)) {
                people.push({ id: other.id, name: other.name, key: other.key, extra: true, checked: true, fraction: r.fraction });
            }
        }
        const saved = $("roll-saved");
        saved.hidden = !registered.length;
        saved.textContent = registered.length
            ? "Ya enviaste esta lista con " + registered.length + " personas. Si la cambias y la vuelves a enviar, se reemplaza."
            : "";
        $("roll-search").value = "";
        $("roll-results").replaceChildren();
        renderPeople();
    }

    function renderPeople() {
        const list = $("roll-list");
        list.replaceChildren();
        if (!people.length) {
            list.textContent = "No tienes cuadrilla asignada; busca a la gente abajo.";
        }
        for (const person of people) {
            const row = document.createElement("div");
            row.className = "fk-person";
            const label = document.createElement("label");
            const input = document.createElement("input");
            input.type = "checkbox";
            input.checked = person.checked;
            const name = document.createElement("span");
            name.textContent = personLabel(person);
            const notes = [];
            if (person.extra) {
                notes.push("otra cuadrilla");
            }
            const projectId = Number($("roll-project").value);
            for (const busy of roll.busy[person.id] || []) {
                if (!(busy.mine && busy.project_id === projectId)) {
                    notes.push("ya tiene " + busy.label);
                }
            }
            if (notes.length) {
                const note = document.createElement("small");
                note.textContent = " · " + notes.join(" · ");
                name.appendChild(note);
            }
            label.append(input, name);
            const half = document.createElement("button");
            half.type = "button";
            half.className = "fk-btn fk-btn-light fk-half";
            half.textContent = person.fraction === 1 ? "Día completo" : "½ día";
            half.hidden = !person.checked;
            half.addEventListener("click", () => {
                person.fraction = person.fraction === 1 ? 0.5 : 1;
                renderPeople();
            });
            input.addEventListener("change", () => {
                person.checked = input.checked;
                renderPeople();
            });
            row.append(label, half);
            list.appendChild(row);
        }
        const count = people.filter((p) => p.checked).length;
        $("roll-count").textContent = count + (count === 1 ? " persona" : " personas");
    }

    function searchOthers() {
        const query = plain($("roll-search").value.trim());
        const box = $("roll-results");
        box.replaceChildren();
        if (query.length < 2 && !/^\d+$/.test(query)) {
            return;
        }
        const taken = new Set(people.map((p) => p.id));
        const words = query.split(/\s+/);
        const matches = roll.others
            .filter((e) => !taken.has(e.id) && words.every((w) => plain(personLabel(e)).includes(w)))
            .slice(0, 6);
        if (!matches.length) {
            box.textContent = "No se encontró a nadie con ese nombre o clave.";
        }
        for (const employee of matches) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "fk-btn fk-result";
            button.textContent = "+ " + personLabel(employee);
            button.addEventListener("click", () => {
                people.push({ id: employee.id, name: employee.name, key: employee.key, extra: true, checked: true, fraction: 1 });
                $("roll-search").value = "";
                box.replaceChildren();
                renderPeople();
            });
            box.appendChild(button);
        }
    }

    async function sendRoll() {
        if (busy) {
            return;
        }
        const entries = people.filter((p) => p.checked).map((p) => ({ employee_id: p.id, fraction: p.fraction }));
        const projectId = Number($("roll-project").value);
        const already = (roll.registered[projectId] || []).length;
        if (!entries.length && !already) {
            toast("Palomea al menos a una persona", "error");
            return;
        }
        if (!entries.length && !window.confirm("¿Quitar la lista de este proyecto?")) {
            return;
        }
        if (!$("roll-pin").value) {
            toast("Escribe tu PIN", "error");
            return;
        }
        busy = true;
        $("roll-send").disabled = true;
        try {
            const result = await rpc(base + "/lista/guardar", {
                device_key: deviceKey,
                pin: $("roll-pin").value,
                date: roll.date,
                project_id: projectId,
                entries: entries,
            });
            if (result.error) {
                toast(result.error, "error", 6000);
                return;
            }
            $("roll-pin").value = "";
            const total = result.done.length + result.review.length;
            let message = "Lista enviada: " + total + (total === 1 ? " persona" : " personas") + " en " + result.project + ".";
            if (result.review.length) {
                message += " En revisión: " + result.review.join(", ") + ".";
            }
            if (result.skipped.length) {
                message += " Ya marcaron con cámara: " + result.skipped.join(", ") + ".";
            }
            toast(message, result.review.length || result.skipped.length ? "warning" : "success", 7000);
            await loadRoll(roll.date);
        } catch (error) {
            toast(error.message, "error", 6000);
        } finally {
            busy = false;
            $("roll-send").disabled = !(roll && roll.projects.length);
        }
    }

    let kioskStarted = false;
    async function startKiosk() {
        if (kioskStarted) {
            return;
        }
        kioskStarted = true;
        if (isStaff) {
            // Office staff on their own phone: automatic like the tablet, keeping where they marked.
            $("punch-kinds").hidden = true;
            watchGps();
        } else if (isOffice) {
            $("gps-status").hidden = true;
            $("punch-kinds").hidden = true;
            $("btn-office-pin").hidden = false;
            listenBadge();
        } else {
            watchGps();
        }
        try {
            await startCamera($("video"));
            $("camera-hint").textContent = "Cargando reconocimiento facial…";
            await loadModels();
            setPunchEnabled(true);
            $("btn-enroll").hidden = !state.can_enroll;
            if (isOffice) {
                startOfficeLoop();
            } else {
                $("camera-hint").textContent = "Ponte frente a la cámara y toca lo que vas a marcar";
            }
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

    function needsProjectOffline(params) {
        if (isOffice || params.project_id) {
            return false;
        }
        const projects = (state && state.active_projects) || [];
        return projects.length > 1 && (params.change_project || ["in", "lunch_in"].includes(params.kind));
    }

    function faceDistance(a, b) {
        let sum = 0;
        for (let i = 0; i < a.length; i++) {
            sum += (a[i] - b[i]) ** 2;
        }
        return Math.sqrt(sum);
    }

    /** No signal: keep the punch on the phone. Return true when it was saved. */
    async function queue(params) {
        if (!params.descriptor && !params.badge) {
            toast("Sin señal: marcar con PIN necesita internet. Usa la cámara.", "error", 6000);
            return false;
        }
        if (needsProjectOffline(params)) {
            chooseProject();
            return false;
        }
        if (isOffice && params.descriptor) {
            // The tablet sees the same face several times in a row: save it once.
            const now = Date.now();
            recentQueued = recentQueued.filter((item) => now - item.at < 120000);
            if (recentQueued.some((item) => faceDistance(item.descriptor, params.descriptor) < 0.45)) {
                showOfficeResult({ queued: true, repeat: true });
                return true;
            }
            recentQueued.push({ descriptor: params.descriptor, at: now });
        }
        await Offline.add(token, params);
        offline = true;
        await updateOfflineStatus();
        if (isOffice) {
            showOfficeResult({ queued: true });
        } else {
            toast("Sin señal: el marcaje quedó guardado en el teléfono y se enviará solo cuando vuelva la señal.",
                "warning", 7000);
        }
        return true;
    }

    async function send(extra) {
        const params = Object.assign({}, pending, extra || {});
        pending = params;
        let result;
        try {
            result = await rpc(base + "/punch", params);
        } catch (error) {
            if (!Offline.isNetworkError(error)) {
                throw error;
            }
            if (await queue(params)) {
                pending = null;
            }
            return;
        }
        if (offline) {
            offline = false;
            syncNow();
        }
        if (result.need_project) {
            chooseProject();
            return;
        }
        if (result.need_overtime) {
            $("overtime-text").textContent = result.employee
                + " sale más de una hora después del horario. Si es tiempo extra, se envía a validar.";
            openPanel("panel-overtime");
            return;
        }
        if (result.need_pin && !isOffice) {
            askPin();
            return;
        }
        pending = null;
        if (isOffice) {
            showOfficeResult(result);
            return;
        }
        if (result.error) {
            toast(result.error, "error", 6000);
            return;
        }
        const verb = ACTIONS[result.action] || "Marcaje";
        let message = verb + ": " + result.employee + (result.project ? " · " + result.project : "");
        if (result.state === "review") {
            message += " (en revisión: " + (result.reasons || []).join(", ") + ")";
        }
        if (result.overtime) {
            message += " · tiempo extra enviado a validar";
        }
        toast(message, result.state === "review" ? "warning" : "success", 6000);
    }

    function setPunchEnabled(enabled) {
        for (const button of document.querySelectorAll("#punch-kinds [data-kind]")) {
            button.disabled = !enabled;
        }
    }

    async function punch(kind, changeProject) {
        if (busy) {
            return;
        }
        busy = true;
        setPunchEnabled(false);
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
                kind: kind || null,
            };
            if (!descriptor) {
                if (state.manual) {
                    askPin();
                } else {
                    pending = null;
                    toast("No te reconocí. Acércate a la cámara con buena luz e intenta de nuevo.", "error", 6000);
                }
                return;
            }
            await send();
        } catch (error) {
            toast(error.message, "error", 6000);
        } finally {
            busy = false;
            setPunchEnabled(true);
            $("camera-hint").textContent = "Ponte frente a la cámara y toca lo que vas a marcar";
        }
    }

    // ------------------------------------------------------------------
    // Kiosco de oficina: reconoce solo, sin tocar la pantalla, y acepta tarjeta
    // ------------------------------------------------------------------

    let officePauseUntil = 0;
    let officeTimer = null;

    function showOfficeResult(result) {
        const box = $("office-result");
        box.replaceChildren();
        const title = document.createElement("div");
        const detail = document.createElement("small");
        if (result.queued) {
            box.className = "fk-office-result warn";
            title.textContent = result.repeat ? "Ya quedó guardado" : "Guardado sin señal";
            detail.textContent = "Se registra cuando vuelva el internet";
        } else if (result.error) {
            box.className = "fk-office-result bad";
            title.textContent = result.error;
            detail.textContent = result.need_pin ? "Acércate a la cámara o usa «Marcar con PIN»" : "";
        } else if (result.action === "repeat") {
            box.className = "fk-office-result ok";
            title.textContent = result.employee;
            detail.textContent = "Ya tenías entrada a las " + result.time;
        } else {
            box.className = "fk-office-result " + (result.state === "review" ? "warn" : "ok");
            title.textContent = result.employee;
            detail.textContent = (ACTIONS[result.action] || "Marcaje") + (result.time ? " " + result.time : "")
                + (result.state === "review" ? " · en revisión" : "");
        }
        box.append(title, detail);
        box.hidden = false;
        officePauseUntil = Date.now() + (result.error ? 2500 : 3000);
        clearTimeout(officeTimer);
        officeTimer = setTimeout(() => (box.hidden = true), 3500);
    }

    function startOfficeLoop() {
        $("camera-hint").textContent = "Mira a la cámara";
        const tick = async () => {
            if (!busy && !enrolling && Date.now() >= officePauseUntil && $("panel-pin").hidden) {
                busy = true;
                try {
                    const descriptor = await detect($("video"), 1);
                    if (descriptor) {
                        $("camera-hint").textContent = "Reconociendo…";
                        pending = { device_key: deviceKey, descriptor: descriptor, photo: snapshot($("video"), $("canvas")) };
                        if (isStaff && position) {
                            pending.latitude = position.latitude;
                            pending.longitude = position.longitude;
                        }
                        await send();
                    }
                } catch (error) {
                    showOfficeResult({ error: error.message });
                } finally {
                    busy = false;
                    $("camera-hint").textContent = "Mira a la cámara";
                }
            }
            setTimeout(tick, 400);
        };
        tick();
    }

    function listenBadge() {
        // A card reader types the number like a keyboard and ends with Enter.
        let buffer = "";
        let last = 0;
        document.addEventListener("keydown", async (event) => {
            if (event.target && ["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) {
                return;
            }
            const now = Date.now();
            if (now - last > 300) {
                buffer = "";
            }
            last = now;
            if (event.key === "Enter") {
                const badge = buffer.trim();
                buffer = "";
                if (badge.length >= 3) {
                    pending = { device_key: deviceKey, badge: badge };
                    try {
                        await send();
                    } catch (error) {
                        showOfficeResult({ error: error.message });
                    }
                }
            } else if (event.key.length === 1) {
                buffer += event.key;
            }
        });
    }

    // ------------------------------------------------------------------
    // Registrar rostro desde el enlace (supervisor o tablet de oficina)
    // ------------------------------------------------------------------

    let enrolling = false;
    let enrollShots = [];
    let enrollPhoto = null;

    function renderEnrollOptions() {
        const query = plain($("enroll-search").value.trim());
        const select = $("enroll-employee");
        select.replaceChildren();
        const people = (state.crew || []).filter((p) => !p.face && (!query || plain(personLabel(p)).includes(query)));
        for (const person of people) {
            const option = document.createElement("option");
            option.value = person.id;
            option.textContent = personLabel(person);
            select.appendChild(option);
        }
        if (!people.length) {
            const option = document.createElement("option");
            option.value = "";
            option.textContent = query ? "Nadie sin rostro con ese nombre" : "Todos ya tienen rostro registrado";
            select.appendChild(option);
        }
    }

    function updateEnroll() {
        $("enroll-count").textContent = "Fotos: " + enrollShots.length + (enrollShots.length < 3 ? " de 3" : "");
        $("enroll-save").disabled = !enrollShots.length;
    }

    function openEnroll() {
        enrolling = true;
        enrollShots = [];
        enrollPhoto = null;
        $("enroll-search").value = "";
        $("enroll-consent").checked = false;
        $("enroll-pin").value = "";
        renderEnrollOptions();
        updateEnroll();
        $("enroll-box").hidden = false;
        $("punch-kinds").hidden = true;
        $("btn-enroll").hidden = true;
        $("camera-hint").textContent = "Pon al empleado frente a la cámara y toca Tomar foto";
    }

    function closeEnroll() {
        enrolling = false;
        $("enroll-box").hidden = true;
        $("punch-kinds").hidden = isOffice;
        $("btn-enroll").hidden = false;
        $("camera-hint").textContent = isOffice ? "Mira a la cámara" : "Ponte frente a la cámara y toca lo que vas a marcar";
    }

    async function enrollShot() {
        $("enroll-shot").disabled = true;
        try {
            const descriptor = await detect($("video"), 4);
            if (!descriptor) {
                toast("No se detectó una cara. Acércate y busca buena luz.", "error");
                return;
            }
            if (!enrollPhoto) {
                enrollPhoto = snapshot($("video"), $("canvas"));
            }
            enrollShots.push(descriptor);
            updateEnroll();
        } catch (error) {
            toast(error.message, "error", 6000);
        } finally {
            $("enroll-shot").disabled = false;
        }
    }

    async function enrollSave() {
        const employeeId = Number($("enroll-employee").value);
        if (!employeeId) {
            toast("Elige al empleado", "error");
            return;
        }
        if (!$("enroll-consent").checked) {
            toast("Marca que el empleado firmó el consentimiento.", "error");
            return;
        }
        try {
            const result = await rpc(base + "/rostro", {
                device_key: deviceKey,
                pin: $("enroll-pin").value,
                employee_id: employeeId,
                descriptors: enrollShots,
                photo: enrollPhoto,
                consent: true,
            });
            if (result.error) {
                toast(result.error, "error", 7000);
                return;
            }
            const person = (state.crew || []).find((p) => p.id === employeeId);
            if (person) {
                person.face = true;
            }
            toast("Rostro de " + result.employee + " guardado", "success", 6000);
            enrollShots = [];
            enrollPhoto = null;
            $("enroll-consent").checked = false;
            renderEnrollOptions();
            updateEnroll();
        } catch (error) {
            toast(error.message, "error", 6000);
        }
    }

    // ------------------------------------------------------------------
    // Comida registrada después (supervisor en otro proyecto a la hora de comer)
    // ------------------------------------------------------------------

    let lunchDay = null;

    function openLunch() {
        const select = $("lunch-employee");
        select.replaceChildren();
        const crewIds = new Set();
        for (const person of state.crew || []) {
            crewIds.add(person.id);
            const option = document.createElement("option");
            option.value = person.id;
            option.textContent = personLabel(person);
            select.appendChild(option);
        }
        const days = $("lunch-days");
        days.replaceChildren();
        lunchDay = (state.days || [])[0];
        (state.days || []).forEach((day, index) => {
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = dayLabel(day, index);
            button.className = "fk-btn " + (index === 0 ? "fk-btn-selected" : "fk-btn-light");
            button.addEventListener("click", () => {
                lunchDay = day;
                for (const other of days.children) {
                    other.className = "fk-btn " + (other === button ? "fk-btn-selected" : "fk-btn-light");
                }
            });
            days.appendChild(button);
        });
        const projects = $("lunch-project");
        projects.replaceChildren();
        for (const project of state.active_projects || []) {
            const option = document.createElement("option");
            option.value = project.id;
            option.textContent = project.name;
            projects.appendChild(option);
        }
        projects.hidden = (state.active_projects || []).length < 2;
        $("lunch-pin").value = "";
        openPanel("panel-lunch");
    }

    async function saveLunch() {
        try {
            const result = await rpc(base + "/comida", {
                device_key: deviceKey,
                pin: $("lunch-pin").value,
                employee_id: Number($("lunch-employee").value),
                date: lunchDay,
                lunch_out: $("lunch-out").value,
                lunch_in: $("lunch-in").value,
                project_id: Number($("lunch-project").value) || null,
            });
            if (result.error) {
                toast(result.error, "error", 6000);
                return;
            }
            closePanel("panel-lunch");
            toast("Comida de " + result.employee + " de " + result.lunch_out + " a " + result.lunch_in
                + " guardada; RH la revisará.", "success", 7000);
        } catch (error) {
            toast(error.message, "error", 6000);
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
            option.textContent = personLabel(person);
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
            list.textContent = "Operaciones no te ha asignado proyectos.";
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
            toast("Proyectos de hoy guardados", "success");
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
    $("btn-mode").addEventListener("click", () => setMode(mode === "roll" ? "camera" : "roll"));
    $("roll-project").addEventListener("change", applyRegistered);
    $("roll-search").addEventListener("input", searchOthers);
    $("roll-send").addEventListener("click", sendRoll);
    $("roll-all").addEventListener("click", () => {
        const all = people.every((p) => p.checked);
        for (const person of people) {
            person.checked = !all;
        }
        renderPeople();
    });
    $("btn-refresh").addEventListener("click", () => refresh().catch((e) => toast(e.message, "error")));
    for (const button of document.querySelectorAll("#punch-kinds [data-kind]")) {
        button.addEventListener("click", () => punch(button.dataset.kind, false));
    }
    $("btn-change").addEventListener("click", () => punch(null, true));
    $("btn-lunch").addEventListener("click", openLunch);
    $("btn-enroll").addEventListener("click", openEnroll);
    $("enroll-close").addEventListener("click", closeEnroll);
    $("enroll-shot").addEventListener("click", enrollShot);
    $("enroll-save").addEventListener("click", enrollSave);
    $("enroll-search").addEventListener("input", renderEnrollOptions);
    for (const [id, answer] of [["btn-overtime-yes", true], ["btn-overtime-no", false]]) {
        $(id).addEventListener("click", async () => {
            closePanel("panel-overtime");
            try {
                await send({ overtime: answer });
            } catch (error) {
                toast(error.message, "error", 6000);
            }
        });
    }
    $("btn-lunch-save").addEventListener("click", saveLunch);
    $("btn-office-pin").addEventListener("click", () => {
        pending = { device_key: deviceKey };
        askPin();
    });
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

    // ------------------------------------------------------------------
    // Sin señal: enviar lo guardado cuando vuelve el internet
    // ------------------------------------------------------------------

    async function updateOfflineStatus() {
        let count = 0;
        try {
            count = (await Offline.list(token)).length;
        } catch {
            count = 0;
        }
        const box = $("offline-status");
        box.classList.toggle("fk-offline-sending", syncing);
        if (syncing) {
            box.textContent = "Enviando " + count + " marcajes guardados…";
        } else if (offline) {
            box.textContent = "Sin señal" + (count ? " · " + count + " marcajes guardados en el teléfono" : ": los marcajes se guardan en el teléfono");
        } else {
            box.textContent = count ? count + " marcajes por enviar" : "";
        }
        box.hidden = !box.textContent;
    }

    async function syncNow() {
        if (syncing || !deviceKey) {
            return;
        }
        let queued;
        try {
            queued = await Offline.list(token);
        } catch {
            return;
        }
        if (!queued.length) {
            await updateOfflineStatus();
            return;
        }
        syncing = true;
        await updateOfflineStatus();
        let done = 0;
        const errors = [];
        try {
            for (let i = 0; i < queued.length; i += 10) {
                const batch = queued.slice(i, i + 10).map((punch) => {
                    const copy = Object.assign({}, punch);
                    delete copy.token;
                    return copy;
                });
                const result = await rpc(base + "/sincronizar", {
                    device_key: deviceKey, punches: batch, phone_now: new Date().toISOString(),
                });
                if (result.error) {
                    throw new Error(result.error);
                }
                await Offline.remove(result.results.map((r) => r.uuid));
                for (const r of result.results) {
                    if (r.state === "error") {
                        errors.push((r.employee ? r.employee + ": " : "") + r.detail);
                    } else {
                        done += 1;
                    }
                }
            }
            offline = false;
        } catch (error) {
            if (!Offline.isNetworkError(error)) {
                toast("No se pudieron enviar los marcajes guardados: " + error.message, "error", 8000);
            }
        } finally {
            syncing = false;
            await updateOfflineStatus();
        }
        if (errors.length) {
            toast("Marcajes guardados sin señal: " + done + " registrados, " + errors.length
                + " no se registraron (" + errors.slice(0, 3).join("; ") + "). Revísalos con Operaciones.", "error", 12000);
        } else if (done) {
            toast(done + " marcajes guardados sin señal ya quedaron registrados.", "success", 6000);
        }
    }

    window.addEventListener("online", () => syncNow());
    window.addEventListener("offline", () => {
        offline = true;
        updateOfflineStatus();
    });
    setInterval(() => {
        if (navigator.onLine) {
            syncNow();
        }
    }, 60000);

    if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/campo/sw.js", { scope: "/campo/" }).catch(() => {
            // Without it the link still works, only not without signal.
        });
    }

    refresh().catch((error) => ($("active-projects").textContent = error.message));
})();
