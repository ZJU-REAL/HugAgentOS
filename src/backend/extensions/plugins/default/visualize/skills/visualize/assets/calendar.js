// Modified for HugAgentOS standalone previews; see ../../../NOTICE.
// Bundled into the sandbox only when the fragment references viz-calendar.
// Register before parsing the fragment: connectedCallback builds the styled
// shadow tree synchronously, without a network request or hydration frame.
(() => {
  if (customElements.get("viz-calendar")) return;

  const style = `
    :host{display:block;min-width:0;padding:12px;font:400 14px/1.5 Inter,system-ui,sans-serif;color:var(--foreground);container-type:inline-size}
    *{box-sizing:border-box}
    .layout{display:grid;grid-template-columns:88px minmax(0,1fr);gap:24px}
    .date{display:flex;flex-direction:column;gap:8px;padding-top:8px;border-right:1px solid var(--border)}
    .weekday,.context{color:color-mix(in srgb,var(--foreground) 65%,var(--background))}
    .day{font-size:48px;line-height:1.1;letter-spacing:-.03em}
    .context{font-size:12px;line-height:16px;overflow-wrap:anywhere}
    .axis{--hour:max(48px,var(--minimum-hour));position:relative;height:calc(var(--hours)*var(--hour) + 16px);margin-top:8px;min-width:0}
    .axis.compact{--hour:max(32px,var(--minimum-hour))}
    .hour{position:absolute;top:calc(var(--at)*var(--hour));inset-inline:0;color:var(--muted-foreground)}
    .hour span{display:block;width:36px;text-align:right;transform:translateY(-50%);font-variant-numeric:tabular-nums}
    .hour::after{content:"";position:absolute;top:0;left:48px;right:0;border-top:1px solid var(--border)}
    .events{position:absolute;inset:0 0 16px 48px;margin:0;padding:0;list-style:none;overflow:hidden}
    .event{position:absolute;top:calc(var(--at)*var(--hour) + 2px);height:calc(var(--duration)*var(--hour) - 4px);left:calc(var(--lane)*100%/var(--lanes) + var(--lane)*8px/var(--lanes));width:calc((100% - (var(--lanes) - 1)*8px)/var(--lanes));border-radius:8px;background:linear-gradient(110deg,color-mix(in srgb,var(--tone) 24%,var(--background)),color-mix(in srgb,var(--tone) 32%,var(--background)));overflow:hidden;container-type:size}
    .event.past{background:color-mix(in srgb,var(--tone) 12%,var(--background));color:color-mix(in srgb,var(--foreground) 70%,var(--background))}
    .event-body{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;height:100%;padding:4px 12px;font:inherit;color:inherit;text-align:left}
    button.event-body{appearance:none;border:0;border-radius:inherit;background:transparent}
    .cursor-interaction{cursor:var(--cursor-interaction,pointer)}
    button.event-body:hover{background:color-mix(in srgb,var(--foreground) 2%,transparent)}
    button.event-body:focus-visible{outline:2px solid var(--ring);outline-offset:-2px}
    /* Optically center Inter's glyphs within the unchanged line boxes. */
    .event-content{min-width:0;transform:translateY(-1px)}
    .title{display:block;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .detail{display:block;font-size:12px;line-height:16px;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    @container(max-height:48px){.detail{display:none}.event-body{padding-block:0}}
    .video{width:16px;height:16px;flex:none}
    .now{position:absolute;top:calc(var(--at)*var(--hour));left:48px;right:0;border-top:2px solid var(--red);pointer-events:none}
    .now::before{content:"";position:absolute;left:-4px;top:-5px;width:8px;height:8px;border-radius:50%;background:var(--red)}
    .now span{position:absolute;right:calc(100% + 12px);transform:translateY(-50%);color:var(--red);font:400 12px/16px Inter,system-ui,sans-serif;font-variant-numeric:tabular-nums}
    .empty{position:absolute;top:24px;left:64px;color:var(--muted-foreground)}
    @media(pointer:coarse){:host .axis{--hour:max(48px,var(--minimum-hour),var(--minimum-target-hour))}}
    @container(max-width:520px){.layout{grid-template-columns:minmax(0,1fr)}.date{flex-direction:row;align-items:baseline;border:0;padding:0}.event-body{padding-inline:8px}}
  `;
  const escape = (value) =>
    String(value).replace(
      /[&<>"']/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[char],
    );
  const minutes = (value) => {
    if (
      typeof value !== "string" ||
      !/^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$/.test(value)
    ) {
      throw new Error("Use HH:MM times between 00:00 and 24:00.");
    }
    return Number(value.slice(0, 2)) * 60 + Number(value.slice(3));
  };

  class Calendar extends HTMLElement {
    #initialized = false;
    #events;
    static observedAttributes = [
      "date",
      "start",
      "end",
      "now",
      "events",
      "lang",
      "time-zone",
      "empty-label",
      "interactive",
    ];

    constructor() {
      super();
      // Replay data assigned before the custom element was registered.
      if (Object.hasOwn(this, "events")) {
        const events = this.events;
        delete this.events;
        this.events = events;
      }
    }

    get events() {
      return this.#events === undefined
        ? JSON.parse(this.getAttribute("events") ?? "[]")
        : this.#events;
    }

    set events(events) {
      this.#events = events;
      if (this.#initialized) this.render();
    }

    connectedCallback() {
      // Keep the shared widget surface outside Shadow DOM; only layout is private.
      this.classList.add("widget");
      // Preserve a server-rendered declarative shadow root when upgrading it.
      // https://web.dev/articles/declarative-shadow-dom
      if (!this.shadowRoot) {
        this.attachShadow({ mode: "open", serializable: true });
        this.render();
      } else if (!this.#initialized && this.#events !== undefined) {
        this.render();
      }
      if (!this.#initialized) {
        this.shadowRoot.addEventListener("click", (event) => {
          const button = event.target.closest?.("button[data-event-index]");
          if (!button) return;
          const index = Number(button.dataset.eventIndex);
          this.dispatchEvent(
            new CustomEvent("eventselect", {
              bubbles: true,
              composed: true,
              detail: { event: this.events[index], index },
            }),
          );
        });
      }
      this.#initialized = true;
    }

    attributeChangedCallback(name, before, after) {
      if (before !== after && this.#initialized && this.shadowRoot)
        this.render();
    }

    render() {
      const shadow = this.shadowRoot;
      try {
        const date = this.getAttribute("date") ?? "";
        const calendarDate = new Date(`${date}T12:00:00Z`);
        if (
          !/^\d{4}-\d{2}-\d{2}$/.test(date) ||
          !Number.isFinite(calendarDate.getTime()) ||
          calendarDate.toISOString().slice(0, 10) !== date
        ) {
          throw new Error("Supply a valid calendar date as YYYY-MM-DD.");
        }
        const start = minutes(this.getAttribute("start") ?? "09:00");
        const end = minutes(this.getAttribute("end") ?? "17:00");
        if (end <= start)
          throw new Error("The visible end must be later than the start.");
        const now = this.hasAttribute("now")
          ? minutes(this.getAttribute("now"))
          : null;
        const input = this.events;
        if (!Array.isArray(input) || input.length > 200)
          throw new Error("Supply an array of at most 200 events.");
        const tones = new Set([
          "blue",
          "green",
          "red",
          "orange",
          "purple",
          "yellow",
        ]);
        const events = input
          .map((event, index) => {
            if (
              event == null ||
              typeof event !== "object" ||
              typeof event.title !== "string"
            ) {
              throw new Error("Each event needs a title, start and end.");
            }
            const from = minutes(event.start),
              to = minutes(event.end);
            if (to <= from)
              throw new Error("An event must end after it starts.");
            if (event.detail != null && typeof event.detail !== "string")
              throw new Error("Event detail must be text.");
            return {
              ...event,
              index,
              from,
              to,
              tone: tones.has(event.tone) ? event.tone : "blue",
              lane: 0,
              lanes: 1,
            };
          })
          .filter((event) => event.from < end && event.to > start)
          .sort((a, b) => a.from - b.from || b.to - a.to);

        // Allocate interval lanes within each connected overlap group. Adjacent
        // events share a lane; a chain of overlaps shares one column count.
        let group = [],
          laneEnds = [],
          groupEnd = -1;
        const finishGroup = () => {
          for (const event of group) event.lanes = laneEnds.length;
        };
        for (const event of events) {
          if (event.from >= groupEnd) {
            finishGroup();
            group = [];
            laneEnds = [];
          }
          let lane = laneEnds.findIndex((until) => until <= event.from);
          if (lane < 0) lane = laneEnds.length;
          event.lane = lane;
          laneEnds[lane] = event.to;
          group.push(event);
          groupEnd = Math.max(event.to, groupEnd);
        }
        finishGroup();

        const locale = this.lang || document.documentElement.lang || "en";
        const weekday = new Intl.DateTimeFormat(locale, {
          weekday: "short",
          timeZone: "UTC",
        }).format(calendarDate);
        const fullDate = new Intl.DateTimeFormat(locale, {
          dateStyle: "full",
          timeZone: "UTC",
        }).format(calendarDate);
        const timeZone = this.getAttribute("time-zone");
        const ticks = [start];
        for (
          let tick = (Math.floor(start / 60) + 1) * 60;
          tick < end;
          tick += 60
        )
          ticks.push(tick);
        ticks.push(end);
        const labelTime = (time) =>
          `${String(Math.floor(time / 60)).padStart(2, "0")}:${String(time % 60).padStart(2, "0")}`;
        const compact = end - start > 240;
        // Size from full event durations; the window clips partial events
        // without letting a one-minute sliver stretch the entire time axis.
        const minimumHour = Math.max(
          0,
          ...events.map((event) => (28 * 60) / (event.to - event.from)),
        );
        const minimumTargetHour = Math.max(
          0,
          ...events.map((event) => (48 * 60) / (event.to - event.from)),
        );
        const hourMarkup = ticks
          .map(
            (tick) =>
              `<div class="hour" style="--at:${(tick - start) / 60}">${now != null && now >= start && now <= end && Math.abs(tick - now) < 15 ? "" : `<span>${tick % 60 ? labelTime(tick) : tick / 60}</span>`}</div>`,
          )
          .join("");
        const interactive = this.hasAttribute("interactive");
        const eventMarkup = events
          .map((event) => {
            const label = escape(
              `${event.title}, ${event.start}-${event.end}${event.detail ? `, ${event.detail}` : ""}`,
            );
            const body = `button type="button"${interactive ? ` data-event-index="${event.index}"` : ""} aria-label="${label}" data-tooltip="${label}"`;
            return `<li class="event${now != null && event.to <= now ? " past" : ""}" style="--at:${(event.from - start) / 60};--duration:${(event.to - event.from) / 60};--lane:${event.lane};--lanes:${event.lanes};--tone:var(--${event.tone})"><${body} class="event-body cursor-interaction"><span class="event-content"><span class="title">${escape(event.title)}</span>${event.detail ? `<span class="detail">${escape(event.detail)}</span>` : ""}</span>${event.video ? '<svg class="video" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5"/><rect x="2" y="6" width="14" height="12" rx="2"/></svg>' : ""}</button></li>`;
          })
          .join("");
        const nowMarkup =
          now != null && now >= start && now <= end
            ? `<div class="now" style="--at:${(now - start) / 60}" role="img" aria-label="${escape(labelTime(now))}"><span>${labelTime(now)}</span></div>`
            : "";
        shadow.innerHTML = `<style>${style}</style><section class="calendar" aria-label="${escape(fullDate)}"><div class="layout"><div class="date"><span class="weekday">${escape(weekday)}</span><span class="day">${Number(date.slice(-2))}</span>${timeZone ? `<span class="context">${escape(timeZone)}</span>` : ""}</div><div class="axis${compact ? " compact" : ""}" style="--hours:${(end - start) / 60};--minimum-hour:${minimumHour}px;--minimum-target-hour:${minimumTargetHour}px">${hourMarkup}<ol class="events">${eventMarkup}</ol>${nowMarkup}${events.length ? "" : `<span class="empty">${escape(this.getAttribute("empty-label") ?? "No events")}</span>`}</div></div></section>`;
      } catch (error) {
        // Invalid model-authored input remains visible and recoverable through
        // the same attributes; never silently invent dates or drop events.
        shadow.innerHTML = `<style>${style}</style><div class="calendar" role="alert">${escape(error instanceof Error ? error.message : "Invalid calendar data.")}</div>`;
      }
    }
  }
  customElements.define("viz-calendar", Calendar);
})();
