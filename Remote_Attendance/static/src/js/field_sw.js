/* Asistencias en campo: keeps the kiosk working without signal.
 *
 * The page of each link is saved every time it opens with signal; the face recognition files and the
 * scripts are saved once. The punches themselves are not handled here: the page keeps them and sends
 * them when the signal is back. */
"use strict";

const VERSION = "field-v1";
const STATIC = "/Remote_Attendance/static/";
const PRECACHE = [
    STATIC + "src/css/field_kiosk.css",
    STATIC + "src/js/field_common.js",
    STATIC + "src/js/field_kiosk.js",
    STATIC + "src/js/field_offline.js",
    STATIC + "lib/face-api/face-api.js",
    STATIC + "lib/face-api/model/tiny_face_detector_model-weights_manifest.json",
    STATIC + "lib/face-api/model/tiny_face_detector_model.bin",
    STATIC + "lib/face-api/model/face_landmark_68_model-weights_manifest.json",
    STATIC + "lib/face-api/model/face_landmark_68_model.bin",
    STATIC + "lib/face-api/model/face_recognition_model-weights_manifest.json",
    STATIC + "lib/face-api/model/face_recognition_model.bin",
    STATIC + "src/img/icon-192.png",
];

self.addEventListener("install", (event) => {
    event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
            .then(() => self.clients.claim())
    );
});

function isPage(url, request) {
    return request.mode === "navigate" && /^\/campo\/[^/]+$/.test(url.pathname) && !url.pathname.endsWith(".js");
}

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") {
        return; // punches and other calls always go to the server
    }
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) {
        return;
    }
    if (isPage(url, request) || url.pathname.endsWith("/app.webmanifest")) {
        // Fresh page when there is signal, the saved one when there is not.
        event.respondWith(
            fetch(request)
                .then((response) => {
                    if (response.ok) {
                        const copy = response.clone();
                        caches.open(VERSION).then((cache) => cache.put(url.pathname, copy));
                    }
                    return response;
                })
                .catch(() => caches.match(url.pathname).then((saved) => saved || Response.error()))
        );
        return;
    }
    if (url.pathname.startsWith(STATIC)) {
        // Scripts and the face recognition files: the network first (so updates arrive), else the copy.
        event.respondWith(
            fetch(request)
                .then((response) => {
                    if (response.ok) {
                        const copy = response.clone();
                        caches.open(VERSION).then((cache) => cache.put(url.pathname, copy));
                    }
                    return response;
                })
                .catch(() => caches.match(url.pathname).then((saved) => saved || Response.error()))
        );
    }
});
