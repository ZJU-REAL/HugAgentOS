// Modified for HugAgentOS standalone previews; see ../../../NOTICE.
(() => {
  const frame = document.getElementById("hugagent-visualization");
  if (!(frame instanceof HTMLIFrameElement)) {
    return;
  }
  let viewportHeight = globalThis.innerHeight;
  const storageKey = `hugagent:visualization-widget-state-v2:${JSON.stringify([
    globalThis.location.pathname,
    globalThis.location.search,
  ])}`;
  const parseState = (serializedState) => {
    if (
      typeof serializedState !== "string" ||
      new TextEncoder().encode(serializedState).length > 16 * 1024
    ) {
      throw new TypeError("Widget state must be a JSON object of at most 16 KiB");
    }
    /** @type {unknown} */
    const state = JSON.parse(serializedState);
    if (
      state == null ||
      typeof state !== "object" ||
      Array.isArray(state) ||
      ("imageIds" in state && (!Array.isArray(state.imageIds) || state.imageIds.length > 0))
    ) {
      throw new TypeError("Widget state must be a JSON object");
    }
    const storedState = { modelContent: null, privateContent: null, ...state };
    const canonicalState = JSON.stringify(storedState, (_key, /** @type {unknown} */ value) => {
      if (typeof value === "number" && !Number.isFinite(value)) {
        throw new TypeError("Widget state must contain finite numbers");
      }
      return value;
    });
    if (new TextEncoder().encode(canonicalState).length > 16 * 1024) {
      throw new TypeError("Widget state must be a JSON object of at most 16 KiB");
    }
    return storedState;
  };
  let storage = null;
  let state = null;
  try {
    const localStorage = globalThis.localStorage;
    const probeKey = `${storageKey}:probe`;
    localStorage.setItem(probeKey, "1");
    localStorage.removeItem(probeKey);
    storage = localStorage;
    const savedState = storage.getItem(storageKey);
    if (savedState != null) {
      try {
        state = parseState(savedState);
      } catch {
        // Invalid saved state does not disable future writes.
      }
    }
  } catch {
    storage = null;
  }
  const initialGlobals = {
    widgetState: state,
    statePersistence: storage == null ? "none" : "local",
  };

  const channel = new MessageChannel();
  channel.port1.addEventListener("message", (event) => {
    const data = event.data;
    if (typeof data !== "object" || data == null) {
      return;
    }
    if (data.type === "widget-state-write") {
      if (!Number.isSafeInteger(data.id) || data.id <= 0) {
        return;
      }
      let ok = false;
      try {
        if (storage != null) {
          const state = parseState(data.state);
          storage.setItem(storageKey, JSON.stringify(state));
          ok = true;
        }
      } catch {
        // Storage denial, quota limits, and invalid state return a failed receipt.
      }
      channel.port1.postMessage({
        type: "widget-state-result",
        id: data.id,
        ok,
      });
      return;
    }
    if (data.type === "height") {
      const height = data.height;
      if (
        typeof height === "number" &&
        Number.isFinite(height) &&
        height >= 0 &&
        height <= 10_000
      ) {
        frame.style.height = Math.ceil(height) + "px";
      }
      return;
    }
    if (data.type === "scroll-to") {
      const top = data.top;
      if (
        typeof top === "number" &&
        Number.isFinite(top) &&
        top >= 0 &&
        globalThis.navigator.userActivation?.isActive === true
      ) {
        globalThis.scrollTo({
          top: Math.ceil(globalThis.scrollY + frame.getBoundingClientRect().top + top),
        });
      }
      return;
    }
    if (data.type === "open-external") {
      const rawHref = data.href;
      if (typeof rawHref !== "string") {
        return;
      }
      let href;
      try {
        const url = new URL(rawHref, document.baseURI);
        if (url.protocol !== "https:") {
          return;
        }
        href = url.href;
      } catch {
        return;
      }
      if (globalThis.navigator.userActivation?.isActive === true) {
        const anchor = document.createElement("a");
        anchor.href = href;
        anchor.rel = "noopener noreferrer";
        anchor.target = "_blank";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      }
      return;
    }

  });
  channel.port1.start();
  const srcdoc = frame.dataset.srcdoc;
  if (srcdoc == null) {
    return;
  }
  globalThis.addEventListener("resize", () => {
    const height = Number.parseFloat(frame.style.height);
    if (!Number.isFinite(height) || height <= viewportHeight) {
      frame.style.removeProperty("height");
    }
    viewportHeight = globalThis.innerHeight;
    channel.port1.postMessage({ type: "measure" });
  });
  frame.addEventListener(
    "load",
    () => {
      frame.contentWindow?.postMessage({ type: "hugagent-visualization-initialize" }, "*", [
        channel.port2,
      ]);
    },
    { once: true },
  );
  // Match the trusted bootstrap element, not the same text in an authored title.
  frame.srcdoc = srcdoc.replace(
    '<script type="application/json" id="hugagent-visualization-widget-state">__HUGAGENT_VISUALIZATION_WIDGET_STATE__</script>',
    () =>
      '<script type="application/json" id="hugagent-visualization-widget-state">' +
      JSON.stringify(initialGlobals).replaceAll("<", "\\u003c") +
      "</script>",
  );
  delete frame.dataset.srcdoc;
})();
