/* Asistencias en campo: registro de rostros de referencia (Operaciones). */
(function () {
    "use strict";

    const { rpc, loadModels, startCamera, detect, snapshot, toast } = window.FieldCommon;
    const app = document.getElementById("app");
    const employeeId = Number(app.dataset.employee);
    const hasConsent = app.dataset.consent === "1";
    const $ = (id) => document.getElementById(id);
    const descriptors = [];
    let firstPhoto = null;

    function update() {
        $("shots").textContent = "Fotos: " + descriptors.length;
        $("btn-save").disabled = descriptors.length === 0;
    }

    async function shot() {
        $("btn-shot").disabled = true;
        try {
            const descriptor = await detect($("video"), 4);
            if (!descriptor) {
                toast("No se detectó una cara. Acércate y busca buena luz.", "error");
                return;
            }
            if (!firstPhoto) {
                firstPhoto = snapshot($("video"), $("canvas"));
            }
            descriptors.push(descriptor);
            update();
        } finally {
            $("btn-shot").disabled = false;
        }
    }

    async function save() {
        const consent = hasConsent || ($("consent") && $("consent").checked);
        if (!consent) {
            toast("Marca que el empleado firmó el consentimiento.", "error");
            return;
        }
        try {
            const result = await rpc("/campo/enrolar/" + employeeId + "/guardar", {
                descriptors: descriptors,
                photo: firstPhoto,
                consent: consent,
            });
            if (result.error) {
                toast(result.error, "error", 6000);
                return;
            }
            descriptors.length = 0;
            firstPhoto = null;
            update();
            toast("Guardado. Rostros registrados: " + result.count, "success", 6000);
        } catch (error) {
            toast(error.message, "error", 6000);
        }
    }

    $("btn-shot").addEventListener("click", shot);
    $("btn-save").addEventListener("click", save);

    (async () => {
        try {
            await startCamera($("video"));
            $("camera-hint").textContent = "Cargando reconocimiento facial…";
            await loadModels();
            $("camera-hint").textContent = "Lista";
            $("btn-shot").disabled = false;
        } catch (error) {
            $("camera-hint").textContent = "No se pudo abrir la cámara: " + error.message;
        }
    })();
})();
