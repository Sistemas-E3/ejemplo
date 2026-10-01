/* Asistencias en campo: utilidades compartidas por el kiosco y el registro de rostros. */
(function () {
    "use strict";

    const MODEL_URL = "/Remote_Attendance/static/lib/face-api/model";
    let modelsPromise = null;
    let toastTimer = null;

    async function rpc(url, params) {
        const response = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify({ jsonrpc: "2.0", method: "call", id: Date.now(), params: params || {} }),
        });
        const payload = await response.json();
        if (payload.error) {
            const data = payload.error.data || {};
            throw new Error(data.message || payload.error.message || "Error del servidor");
        }
        return payload.result;
    }

    function loadModels() {
        if (!modelsPromise) {
            modelsPromise = Promise.all([
                faceapi.nets.tinyFaceDetector.loadFromUri(MODEL_URL),
                faceapi.nets.faceLandmark68Net.loadFromUri(MODEL_URL),
                faceapi.nets.faceRecognitionNet.loadFromUri(MODEL_URL),
            ]);
        }
        return modelsPromise;
    }

    async function startCamera(video) {
        const stream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
            audio: false,
        });
        video.srcObject = stream;
        await new Promise((resolve) => {
            if (video.readyState >= 2) {
                resolve();
            } else {
                video.onloadeddata = () => resolve();
            }
        });
        return stream;
    }

    /** Detect one face and return its 128-number descriptor, or null. */
    async function detect(video, tries) {
        const options = new faceapi.TinyFaceDetectorOptions({ inputSize: 320, scoreThreshold: 0.5 });
        for (let i = 0; i < (tries || 3); i++) {
            const result = await faceapi
                .detectSingleFace(video, options)
                .withFaceLandmarks()
                .withFaceDescriptor();
            if (result) {
                return Array.from(result.descriptor);
            }
            await new Promise((r) => setTimeout(r, 250));
        }
        return null;
    }

    function snapshot(video, canvas) {
        const width = 480;
        const height = Math.round((video.videoHeight / video.videoWidth) * width) || 360;
        canvas.width = width;
        canvas.height = height;
        canvas.getContext("2d").drawImage(video, 0, 0, width, height);
        return canvas.toDataURL("image/jpeg", 0.7);
    }

    function toast(message, kind, ms) {
        const el = document.getElementById("toast");
        el.textContent = message;
        el.className = "fk-toast fk-toast-" + (kind || "info");
        el.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => (el.hidden = true), ms || 4000);
    }

    function distanceMeters(lat1, lon1, lat2, lon2) {
        const r = 6371000;
        const rad = (d) => (d * Math.PI) / 180;
        const dphi = rad(lat2 - lat1);
        const dl = rad(lon2 - lon1);
        const a = Math.sin(dphi / 2) ** 2 + Math.cos(rad(lat1)) * Math.cos(rad(lat2)) * Math.sin(dl / 2) ** 2;
        return 2 * r * Math.asin(Math.sqrt(a));
    }

    window.FieldCommon = { rpc, loadModels, startCamera, detect, snapshot, toast, distanceMeters };
})();
