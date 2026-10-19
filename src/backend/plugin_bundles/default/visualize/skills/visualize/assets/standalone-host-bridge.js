// Modified for HugAgentOS standalone previews; see ../../../NOTICE.
(() => {
  const root = document.documentElement;
  const mediaQuery = globalThis.matchMedia("(prefers-color-scheme: dark)");
  const apply = Reflect.apply;
  const isArray = Array.isArray;
  const hasOwn = Object.hasOwn;
  const parentWindow = globalThis.parent;
  const stringify = JSON.stringify;
  const parse = JSON.parse;
  const encode = TextEncoder.prototype.encode.bind(new TextEncoder());
  const initialStateElement = document.getElementById("hugagent-visualization-widget-state");
  const initialGlobals = parse(initialStateElement?.textContent ?? "{}");
  initialStateElement?.remove();
  /** @type {Record<string, unknown> | null} */
  let state = initialGlobals.widgetState ?? null;
  const statePersistence = initialGlobals.statePersistence ?? "none";
  let nextStateRequestId = 0;
  /** @type {Map<number, { resolve: () => void; reject: (error: Error) => void; timeout: ReturnType<typeof globalThis.setTimeout>; serializedState: string }>} */
  const pendingStateWrites = new Map();
  const persistenceError = () =>
    Object.assign(new Error("The host could not persist the widget state update"), {
      code: -32000,
      data: { code: "widget_state_persistence_failed", retryable: false },
    });
  /**
   * @param {object} prototype
   * @param {string} property
   */
  const getPrototypeGetter = (prototype, property) => {
    // oxlint-disable-next-line typescript/unbound-method -- invoked through captured Reflect.apply
    const getter = Object.getOwnPropertyDescriptor(prototype, property)?.get;
    if (getter == null) {
      throw new Error(`Missing ${property} getter`);
    }
    return getter;
  };
  const getMessageData = getPrototypeGetter(MessageEvent.prototype, "data");
  const getMessagePorts = getPrototypeGetter(MessageEvent.prototype, "ports");
  const getMessageSource = getPrototypeGetter(MessageEvent.prototype, "source");
  // oxlint-disable-next-line typescript/unbound-method -- captured before the untrusted fragment runs
  const addHostMessageListener = MessagePort.prototype.addEventListener;
  // oxlint-disable-next-line typescript/unbound-method -- captured before the untrusted fragment runs
  const postHostMessage = MessagePort.prototype.postMessage;
  // oxlint-disable-next-line typescript/unbound-method -- captured before the untrusted fragment runs
  const startHostPort = MessagePort.prototype.start;
  // oxlint-disable-next-line typescript/unbound-method -- captured before the untrusted fragment runs
  const stopMessagePropagation = Event.prototype.stopImmediatePropagation;
  /** @type {MessagePort | null} */
  let hostPort = null;
  const postToHost = (type, payload) => {
    if (hostPort != null) {
      apply(postHostMessage, hostPort, [{ type, ...payload }]);
    }
  };
  const openExternal = ({ href }) => {
    if (globalThis.navigator.userActivation?.isActive === true) {
      postToHost("open-external", { href });
    }
  };
  /**
   * @param {Record<string, unknown> | ((previous: Record<string, unknown> | null) => Record<string, unknown>)} next
   * @returns {Promise<void>}
   */
  const setWidgetState = async (next) => {
    const nextState = typeof next === "function" ? next(state) : next;
    if (nextState == null || typeof nextState !== "object" || isArray(nextState)) {
      throw new TypeError("Widget state must be a JSON object");
    }
    const inputState = stringify(nextState, (_key, /** @type {unknown} */ value) => {
      if (
        typeof value === "undefined" ||
        typeof value === "function" ||
        typeof value === "symbol" ||
        (typeof value === "number" && !Number.isFinite(value))
      ) {
        throw new TypeError("Widget state must be a JSON object");
      }
      return value;
    });
    const parsedState = parse(inputState);
    if (parsedState == null || typeof parsedState !== "object" || isArray(parsedState)) {
      throw new TypeError("Widget state must be a JSON object");
    }
    const storedState = {
      modelContent: null,
      privateContent: null,
      ...parsedState,
    };
    const serializedState = stringify(storedState);
    if (encode(serializedState).length > 16 * 1024) {
      throw new TypeError("Widget state must be a JSON object of at most 16 KiB");
    }
    if (
      hasOwn(storedState, "imageIds") &&
      (!isArray(storedState.imageIds) ||
        storedState.imageIds.some((/** @type {unknown} */ id) => typeof id !== "string"))
    ) {
      throw new TypeError("Widget state imageIds must be an array of strings");
    }
    if (storedState.imageIds?.length > 0) {
      throw Object.assign(new TypeError("Visualization state does not support image attachments"), {
        code: -32602,
        data: { code: "images_unsupported" },
      });
    }
    state = storedState;
    globalThis.hugagent.widgetState = state;
    globalThis.dispatchEvent(
      new CustomEvent("hugagent:set_globals", {
        detail: { globals: { widgetState: state } },
      }),
    );
    if (statePersistence === "none") {
      throw persistenceError();
    }
    const id = ++nextStateRequestId;
    return new Promise((resolve, reject) => {
      const timeout = globalThis.setTimeout(() => {
        pendingStateWrites.delete(id);
        reject(persistenceError());
      }, 30_000);
      pendingStateWrites.set(id, { resolve, reject, timeout, serializedState });
      postToHost("widget-state-write", { id, state: serializedState });
    });
  };
  const syncTheme = () => {
    const theme = mediaQuery.matches ? "dark" : "light";
    root.dataset.theme = theme;
    globalThis.hugagent = {
      ...globalThis.hugagent,
      openExternal,
      setWidgetState,
      theme,
      visualizationStyleVariables: {},
      visualizationTheme: theme,
      widgetState: state,
      statePersistence,
      stateModelContext: "none",
    };
    globalThis.dispatchEvent(
      new CustomEvent("hugagent:set_globals", {
        detail: { globals: globalThis.hugagent },
      }),
    );
  };
  const sendHeight = () => {
    postToHost("height", {
      height: Math.ceil(
        Math.max(document.body.scrollHeight, document.body.getBoundingClientRect().height),
      ),
    });
  };

  globalThis.addEventListener(
    "message",
    (event) => {
      const data = apply(getMessageData, event, []);
      const ports = apply(getMessagePorts, event, []);
      const source = apply(getMessageSource, event, []);
      if (
        hostPort != null ||
        source !== parentWindow ||
        typeof data !== "object" ||
        data?.type !== "hugagent-visualization-initialize" ||
        !isArray(ports) ||
        ports.length !== 1
      ) {
        return;
      }
      apply(stopMessagePropagation, event, []);
      const nextHostPort = ports[0];
      try {
        apply(addHostMessageListener, nextHostPort, [
          "message",
          (event) => {
            const data = apply(getMessageData, event, []);
            if (typeof data === "object" && data?.type === "measure") {
              sendHeight();
            } else if (typeof data === "object" && data?.type === "widget-state-result") {
              /** @type {unknown} */
              const id = data.id;
              if (typeof id !== "number") {
                return;
              }
              const pending = pendingStateWrites.get(id);
              if (pending != null) {
                pendingStateWrites.delete(id);
                globalThis.clearTimeout(pending.timeout);
                if (data.ok === true) {
                  pending.resolve();
                } else {
                  pending.reject(persistenceError());
                }
              }
            }
          },
        ]);
        apply(startHostPort, nextHostPort, []);
      } catch {
        return;
      }
      hostPort = nextHostPort;
      for (const [id, { serializedState }] of pendingStateWrites) {
        postToHost("widget-state-write", { id, state: serializedState });
      }
      sendHeight();
    },
    { capture: true },
  );
  document.currentScript?.remove();
  mediaQuery.addEventListener("change", syncTheme);
  globalThis.addEventListener("click", (event) => {
    if (event.defaultPrevented) {
      return;
    }
    let element = null;
    if (event.target instanceof globalThis.Element) {
      element = event.target;
    } else if (event.target instanceof globalThis.Node) {
      element = event.target.parentElement;
    }
    const link = element?.closest("a[href]");
    const href = link?.getAttribute("href");
    if (href == null) {
      return;
    }
    if (href.startsWith("#")) {
      let id;
      try {
        id = decodeURIComponent(href.slice(1));
      } catch {
        return;
      }
      const target = id.length === 0 ? root : document.getElementById(id);
      if (target == null || globalThis.navigator.userActivation?.isActive !== true) {
        return;
      }
      event.preventDefault();
      postToHost("scroll-to", {
        top: target.getBoundingClientRect().top + globalThis.scrollY,
      });
      return;
    }
    event.preventDefault();
    openExternal({ href });
  });
  new ResizeObserver(sendHeight).observe(document.body);
  syncTheme();
})();
