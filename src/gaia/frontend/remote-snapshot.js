"use strict";

(() => {
  const nativeFetch = window.fetch.bind(window);
  const LOCAL_PATH = "/assets/last-known-inventory.json";
  const REMOTE_URL = "https://raw.githubusercontent.com/catears124/GAIA/snapshot-data/src/gaia/frontend/last-known-inventory.json";

  function requestUrl(input) {
    try { return new URL(input instanceof Request ? input.url : input, location.href); }
    catch { return null; }
  }

  function isSnapshotRequest(input, init = {}) {
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const url = requestUrl(input);
    return method === "GET" && url?.origin === location.origin && url.pathname === LOCAL_PATH;
  }

  async function remoteSnapshot(init) {
    const version = Math.floor(Date.now() / 60000);
    const headers = new Headers(init?.headers || {});
    headers.set("Accept", "application/json");
    const response = await nativeFetch(`${REMOTE_URL}?v=${version}`, {
      ...init,
      method: "GET",
      headers,
      cache: "no-store",
      credentials: "omit",
      mode: "cors",
    });
    if (!response.ok) throw new Error(`remote snapshot HTTP ${response.status}`);
    return response;
  }

  window.fetch = async function snapshotTransport(input, init = {}) {
    if (!isSnapshotRequest(input, init)) return nativeFetch(input, init);
    try {
      return await remoteSnapshot(init);
    } catch {
      return nativeFetch(input, init);
    }
  };
})();
