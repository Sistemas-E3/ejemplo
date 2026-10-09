/* Asistencias en campo: registrar rostros con la foto de la ficha del empleado (Operaciones). */
(function () {
    "use strict";

    const { rpc, loadModels, toast } = window.FieldCommon;
    const $ = (id) => document.getElementById(id);

    async function descriptorFromImage(img) {
        if (!img.complete) {
            await img.decode();
        }
        // Las fotos de ficha suelen tener la cara chica: se busca con más detalle que en la cámara.
        for (const inputSize of [416, 608]) {
            const result = await faceapi
                .detectSingleFace(img, new faceapi.TinyFaceDetectorOptions({ inputSize: inputSize, scoreThreshold: 0.4 }))
                .withFaceLandmarks()
                .withFaceDescriptor();
            if (result) {
                return Array.from(result.descriptor);
            }
        }
        return null;
    }

    async function run() {
        if (!$("consent").checked) {
            toast("Marca que tienes el consentimiento firmado.", "error");
            return;
        }
        $("btn-start").disabled = true;
        let done = 0;
        let failed = 0;
        for (const row of document.querySelectorAll(".fk-person[data-id]")) {
            const status = row.querySelector(".fk-status");
            if (status.classList.contains("ok")) {
                continue;
            }
            status.textContent = "Procesando…";
            try {
                const descriptor = await descriptorFromImage(row.querySelector("img"));
                if (!descriptor) {
                    status.textContent = "No se encontró la cara en la foto";
                    status.className = "fk-status bad";
                    failed++;
                    continue;
                }
                const result = await rpc("/campo/enrolar/" + row.dataset.id + "/guardar", {
                    descriptors: [descriptor],
                    consent: true,
                });
                if (result.error) {
                    status.textContent = result.error;
                    status.className = "fk-status bad";
                    failed++;
                } else {
                    status.textContent = "Registrado";
                    status.className = "fk-status ok";
                    done++;
                }
            } catch (error) {
                status.textContent = error.message;
                status.className = "fk-status bad";
                failed++;
            }
        }
        $("summary").textContent = "Registrados: " + done + ". Sin registrar: " + failed + ".";
        $("btn-start").textContent = "Reintentar los que faltan";
        $("btn-start").disabled = false;
    }

    $("btn-start").addEventListener("click", run);
    loadModels()
        .then(() => {
            const pending = document.querySelectorAll(".fk-person[data-id]").length;
            $("btn-start").textContent = pending ? "Registrar " + pending + " rostros" : "Nada pendiente";
            $("btn-start").disabled = !pending;
        })
        .catch((error) => ($("btn-start").textContent = "No se pudo cargar: " + error.message));
})();
