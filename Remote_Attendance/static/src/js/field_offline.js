/* Asistencias en campo: punches kept on the phone while there is no signal, sent when it comes back. */
(function () {
    "use strict";

    const DB_NAME = "field_attendance";
    const STORE = "punches";
    let dbPromise = null;

    function open() {
        if (!dbPromise) {
            dbPromise = new Promise((resolve, reject) => {
                const request = indexedDB.open(DB_NAME, 1);
                request.onupgradeneeded = () => {
                    const store = request.result.createObjectStore(STORE, { keyPath: "uuid" });
                    store.createIndex("token", "token");
                };
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
            });
            dbPromise.catch(() => (dbPromise = null));
        }
        return dbPromise;
    }

    async function run(mode, action) {
        const db = await open();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(STORE, mode);
            const result = action(tx.objectStore(STORE));
            tx.oncomplete = () => resolve(result && "result" in result ? result.result : undefined);
            tx.onerror = () => reject(tx.error);
            tx.onabort = () => reject(tx.error || new Error("No se pudo guardar en el teléfono"));
        });
    }

    function newId() {
        if (window.crypto && crypto.randomUUID) {
            return crypto.randomUUID();
        }
        return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
    }

    /** Save a punch for later. ``params`` are the ones the server would have received. */
    async function add(token, params) {
        const punch = Object.assign({}, params, { uuid: newId(), token: token, time: new Date().toISOString() });
        delete punch.device_key;
        await run("readwrite", (store) => store.put(punch));
        return punch;
    }

    async function list(token) {
        const all = await run("readonly", (store) => store.index("token").getAll(token));
        return (all || []).sort((a, b) => (a.time < b.time ? -1 : 1));
    }

    async function remove(uuids) {
        if (!uuids.length) {
            return;
        }
        await run("readwrite", (store) => {
            for (const uuid of uuids) {
                store.delete(uuid);
            }
        });
    }

    /** True when the call failed for lack of signal (not because the server answered an error). */
    function isNetworkError(error) {
        return !navigator.onLine || error instanceof TypeError || (error && error.offline);
    }

    window.FieldOffline = { add, list, remove, isNetworkError };
})();
