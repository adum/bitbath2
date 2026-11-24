// Keep unacknowledged records on this device. Each turn has a stable key so a
// later snapshot can replace its earlier one, including after a reload or retry.
const GameRecording = (() => {
  const nameKey = "bitbath.playerName";
  const finalSeq = 1000000;
  const pending = new Map();
  const key = r => r.id + "/" + r.seq;
  let timer, sending = false, backoff = 1000, status = "";
  const hosted = location.protocol === "https:" || location.protocol === "http:";

  function report(message) {
    status = message;
    const label = document.getElementById("recording-status");
    if (label) label.textContent = message;
  }

  function savedName() {
    try { return localStorage.getItem(nameKey) || ""; } catch { return ""; }
  }
  function setName(name) {
    try { localStorage.setItem(nameKey, name); } catch { /* Name still applies to this match. */ }
  }

  const database = hosted ? new Promise(resolve => {
    try {
      const request = indexedDB.open("bitbath-game-outbox", 1);
      request.onupgradeneeded = () => request.result.createObjectStore("pending", { keyPath: ["id", "seq"] });
      request.onsuccess = () => resolve(request.result);
      request.onerror = request.onblocked = () => resolve(null);
    } catch { resolve(null); }
  }) : Promise.resolve(null);

  async function hydrate() {
    const db = await database;
    if (!db) return;
    const request = db.transaction("pending").objectStore("pending").getAll();
    request.onsuccess = () => {
      for (const r of request.result) {
        if ((pending.get(key(r))?.revision || 0) < r.revision) pending.set(key(r), r);
      }
      schedule();
    };
  }

  async function writeLocal(record, remove) {
    const db = await database;
    if (!db) return;
    const tx = db.transaction("pending", "readwrite");
    const store = tx.objectStore("pending");
    const read = store.get([record.id, record.seq]);
    read.onsuccess = () => {
      if (remove) {
        if (read.result?.revision === record.revision) store.delete([record.id, record.seq]);
      } else if (!read.result || read.result.revision < record.revision) {
        store.put(record);
      }
    };
    return new Promise((resolve, reject) => {
      tx.oncomplete = resolve;
      tx.onabort = tx.onerror = () => reject(tx.error);
    });
  }

  function queue(record) {
    if (!hosted) return;
    pending.set(key(record), record);
    writeLocal(record, false).catch(() => report("Local backup unavailable"));
    schedule();
  }

  function schedule(delay = 200) {
    if (!hosted || timer || sending || !pending.size) return;
    timer = setTimeout(() => { timer = null; flush(); }, delay);
  }

  // Ordered batches keep the header ahead of its turns and the result last.
  function batch() {
    const { id } = pending.values().next().value;
    return [...pending.values()].filter(r => r.id === id).sort((a, b) => a.seq - b.seq).slice(0, 12);
  }

  async function flush() {
    if (sending || !pending.size) return;
    sending = true;
    const records = batch();
    let delay = 0;
    try {
      const res = await fetch("/api/games/" + records[0].id, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(records), signal: AbortSignal.timeout(20000),
      });
      if (res.status !== 204) throw new Error("Save unconfirmed");
      // A turn may have changed while the request was in flight.
      for (const r of records) {
        if (pending.get(key(r))?.revision === r.revision) pending.delete(key(r));
      }
      // A failed local deletion can only replay an already-acknowledged revision.
      await Promise.all(records.map(r => writeLocal(r, true))).catch(() => {});
      backoff = 1000;
      if (status) report("");
      if (!pending.size) window.dispatchEvent(new Event("bitbath-games-saved"));
    } catch {
      report("Game save pending · retrying");
      delay = backoff;
      backoff = Math.min(60000, backoff * 2);
    } finally {
      sending = false;
      schedule(delay);
    }
  }

  function onExit() {
    if (!hosted || !pending.size || !navigator.sendBeacon) return;
    const records = batch();
    const blob = new Blob([JSON.stringify(records)], { type: "application/json" });
    if (blob.size <= 60000) navigator.sendBeacon("/api/games/" + records[0].id, blob);
  }
  window.addEventListener("pagehide", onExit);
  document.addEventListener("visibilitychange", () => { if (document.hidden) onExit(); });
  window.addEventListener("online", () => { clearTimeout(timer); timer = null; schedule(0); });
  hydrate().catch(() => {});

  function start(metadata) {
    if (!hosted) return null;
    // getRandomValues also works for a local server opened over plain HTTP.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
    const id = [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
    let revision = 0;
    const save = (seq, value) => queue({ id, seq, revision: ++revision, at: Date.now(), data: structuredClone(value) });
    save(0, metadata);
    const dirty = new Map();
    let scheduled = false;
    function checkpoint() {
      scheduled = false;
      for (const [seq, turn] of dirty) save(seq, turn);
      dirty.clear();
    }
    return {
      turn(seq, turn) {
        dirty.set(seq, turn);
        if (!scheduled) { scheduled = true; queueMicrotask(checkpoint); }
      },
      finish(result) { checkpoint(); save(finalSeq, result); },
    };
  }
  return { savedName, setName, start };
})();
