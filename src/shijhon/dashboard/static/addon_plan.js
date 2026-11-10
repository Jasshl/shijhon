// The Add-ons page's plan: how a song starts, drawn on a timeline. Each add-on has a bar
// that begins when its step does and ends with the step's time; bars that begin together
// race. Pull a bar into another step's column, or onto the line between two for a new one;
// pull a step's edge to change its time (or use the arrow keys on either). Steps that begin
// after apps stop waiting get no column: their add-ons wait in a list under the board. A
// change is saved in the background, and "Watch it" plays a start through with the
// add-ons' recent speeds.
(() => {
  const root = document.getElementById("plan");
  if (!root) return;
  const data = JSON.parse(root.dataset.plan);
  const card = root.querySelector(".plan-card");
  const board = root.querySelector(".plan-board");
  const says = root.querySelector(".plan-says");
  const guess = root.querySelector(".plan-guess");
  const status = root.querySelector(".plan-status");
  const announce = root.querySelector(".plan-announce");
  const watch = root.querySelector(".plan-show");
  const presetHelp = root.querySelector(".plan-preset-help");
  const fold = root.querySelector(".plan-fold");
  const foldWords = root.querySelector(".plan-fold-words");
  const foldButton = root.querySelector(".plan-fold-button");
  const still = window.matchMedia("(prefers-reduced-motion: reduce)");

  const MIN_SECONDS = 1;
  const MAX_SECONDS = 600; // as the server takes (a worker that prepares whole files)
  const NAME_WIDTH = 228; // px: the names' column, beside the bars on a wide screen
  const EDGE = 16; // px of room before the first step, for a new first step
  const HEAD = 40; // px: the steps' headings
  const FOOT = 26; // px: the time axis
  const ROW = { wide: 54, narrow: 64 };
  const SHELF_ROW = 36; // px: an add-on that is never reached
  const SHELF_HEAD = 46; // px: the line and words above those
  const STUB = 60; // px: what such an add-on is pulled by
  const GAP = 3; // px between a bar and its step's edges, on both sides
  const SLIM = 24; // px: a narrower step shows its edge only when it is opened

  const plan = {
    steps: data.steps.map((step) => ({ seconds: step.seconds, addons: [...step.addons] })),
    checks: data.checks,
    handover: data.handover,
  };
  const everyone = () => plan.steps.flatMap((step) => step.addons);
  const number = (value) => String(Number(value.toFixed(1)));
  const seconds = (value) => `${number(value)} s`;
  const listed = (names) =>
    names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
  const live = (addon) => !addon.off;
  const sum = (steps) => steps.reduce((total, step) => total + step.seconds, 0);
  const stepOf = (addon) => plan.steps.findIndex((step) => step.addons.includes(addon));
  // When each step begins.
  const starts = () => {
    let at = 0;
    return plan.steps.map((step) => {
      const begins = at;
      at += step.seconds;
      return begins;
    });
  };

  // --- the board's elements ------------------------------------------------------------

  const make = (tag, name, parent) => {
    const element = document.createElement(tag);
    if (name) element.className = name;
    if (parent) parent.append(element);
    return element;
  };
  const inner = make("div", "plan-inner", board);
  const lit = make("div", "plan-open", inner);
  const band = make("div", "plan-band", inner);
  const zone = make("div", "plan-zone", inner);
  const insert = make("div", "plan-insert", inner);
  const limit = make("div", "plan-limit", inner);
  const limitText = make("span", "plan-limit-text", limit);
  limitText.textContent = `Apps stop waiting at ${seconds(data.wait)}`;
  const shelfLine = make("div", "plan-shelf-line", inner);
  const shelf = make("div", "plan-shelf", inner);
  const shelfText = make("span", "plan-shelf-text", shelf);
  const now = make("div", "plan-now", inner);
  const nowTime = make("span", "plan-now-time", now);
  const tip = make("div", "plan-tip", inner);
  for (const element of [lit, band, zone, insert, limit, shelfLine, now, tip]) {
    element.setAttribute("aria-hidden", "true");
  }

  const rows = new Map();
  for (const addon of everyone()) {
    const row = make("div", "plan-row", inner);
    const name = make("div", "plan-name", row);
    const light = make("span", `led${addon.light ? ` led-${addon.light}` : ""}`, name);
    light.setAttribute("aria-hidden", "true");
    const words = make("span", "plan-name-words", name);
    make("span", "plan-name-title", words).textContent = addon.name;
    const line = make("span", "plan-name-line", words);
    if (addon.off) {
      line.textContent = "Off: passed over";
    } else if (!addon.tried) {
      line.textContent = "No plays yet";
    } else {
      if (addon.usual !== null) make("span", "", line).textContent = `usually ${seconds(addon.usual)}`;
      const share = addon.played / addon.tried;
      const tone = share >= 0.9 ? " tag-meets" : share < 0.5 ? " tag-misses" : "";
      const tag = make("span", `tag${tone}`, line);
      tag.textContent = `played ${addon.played} of ${addon.tried}`;
      tag.title = `Sent audio in ${addon.played} of its last ${addon.tried} starts`;
    }
    // After its step, an add-on goes on trying until a song plays or apps stop waiting.
    const trail = make("span", "plan-trail", row);
    trail.setAttribute("aria-hidden", "true");
    const trailFill = make("span", "plan-trail-fill", trail);
    let later = null;
    if (addon.usual !== null && !addon.off) {
      later = make("span", "plan-usual plan-usual-later", trail);
      later.dataset.time = `usually ${seconds(addon.usual)}`;
    }
    const bar = make("button", "plan-bar", row);
    bar.type = "button";
    bar.disabled = Boolean(data.readonly);
    bar.classList.toggle("is-off", addon.off);
    const fill = make("span", "plan-fill", bar);
    const result = make("span", "plan-result", bar);
    const num = make("span", "plan-num", bar); // (a never-reached add-on's step)
    let usual = null;
    if (addon.usual !== null && !addon.off) {
      usual = make("span", "plan-usual", bar);
      usual.dataset.time = `usually ${seconds(addon.usual)}`;
    }
    rows.set(addon.id, {
      addon, row, bar, fill, result, num, usual, trail, trailFill, later, step: 0, shelved: false,
      trailX: 0, trailW: 0,
    });
  }
  const itemOf = (element) => [...rows.values()].find((item) => item.bar === element);

  const heads = [];
  const edges = [];
  const ticks = [];
  const pooled = (pool, index, build) => {
    if (!pool[index]) pool[index] = build(index);
    return pool[index];
  };
  const buildHead = (index) => {
    const box = make("div", "plan-head", inner);
    const title = make("span", "plan-head-title", box);
    const time = make("span", "plan-head-time", box);
    box.addEventListener("click", () => {
      open = open === index ? null : index;
      layout();
    });
    return { box, title, time };
  };
  const buildEdge = (index) => {
    const edge = make("button", "plan-edge", inner);
    edge.type = "button";
    edge.setAttribute("role", "slider");
    edge.setAttribute("aria-valuemin", String(MIN_SECONDS));
    edge.setAttribute("aria-valuemax", String(MAX_SECONDS));
    edge.dataset.step = String(index);
    edge.disabled = Boolean(data.readonly);
    // (Reached with the keyboard, a narrow step opens, so its time shows.)
    edge.addEventListener("focus", () => {
      if (edge.matches(":focus-visible") && edge.classList.contains("is-slim") && open !== index) {
        open = index;
        layout();
      }
    });
    return edge;
  };
  const buildTick = () => make("div", "plan-tick", inner);
  const setTime = (step, value) => {
    step.seconds = Math.min(MAX_SECONDS, Math.max(MIN_SECONDS, Math.round(value * 2) / 2));
  };

  // --- where everything goes -----------------------------------------------------------

  let shape = null;
  let open = null; // the step whose time is at hand: its edge shows even when it is narrow
  function measure(rate) {
    const width = board.clientWidth;
    const stacked = width < 560;
    const names = stacked ? 0 : NAME_WIDTH;
    const room = Math.max(120, width - names - EDGE - 24);
    const begins = starts();
    // Only the steps apps reach are on the time axis.
    const count = begins.filter((at) => at < data.wait).length;
    // The last step runs on to where apps stop waiting: its own time counts for nothing in a
    // play, so it is drawn to the end.
    const lasting = (step, index) => (index === plan.steps.length - 1 && begins[index] < data.wait
      ? Math.max(step.seconds, data.wait - begins[index]) : step.seconds);
    const reached = plan.steps.slice(0, count).map((step, index) => ({ seconds: lasting(step, index) }));
    const all = plan.steps.reduce((total, step, index) => total + lasting(step, index), 0);
    const shown = sum(reached);
    // The board runs to where apps stop waiting, so a step can be pulled out to it.
    const span = count < plan.steps.length ? shown : Math.max(all, data.wait);
    const least = stacked ? 12 : 16; // px: a step is never thinner than this
    let pps = rate;
    if (!pps) {
      // Seconds to pixels: a short step keeps its least width, the rest share what is left.
      pps = room / span;
      for (let pass = 0; pass < 4; pass++) {
        const short = reached.filter((step) => step.seconds * pps < least);
        const timed = span - sum(short);
        if (timed > 0) pps = Math.max(1, (room - short.length * least) / timed);
      }
    }
    const left = names + EDGE;
    let x = left;
    const columns = reached.map((step, index) => {
      const w = Math.max(least, step.seconds * pps);
      const column = { x, w, from: begins[index], to: begins[index] + step.seconds, last: index === plan.steps.length - 1 };
      x += w;
      return column;
    });
    const end = x;
    // Where a time falls on the board.
    const place = (time) => {
      const inside = columns.find((column) => time <= column.to);
      if (!inside) return end + (time - shown) * pps;
      return inside.x + ((time - inside.from) / (inside.to - inside.from)) * inside.w;
    };
    const reach = data.wait <= Math.max(span, all);
    return {
      stacked, names, left, end, pps, columns, place, shown, reach,
      limit: place(data.wait),
      row: stacked ? ROW.narrow : ROW.wide,
      full: Math.max(width, (reach ? Math.max(end, place(data.wait)) : end) + 24),
    };
  }

  function layout(rate) {
    shape = measure(rate);
    const { columns, stacked } = shape;
    const steps = plan.steps.length;
    inner.classList.toggle("is-stacked", stacked);
    inner.style.width = `${shape.full}px`;
    root.style.setProperty("--plan-names", `${shape.names}px`);
    if (open !== null && !columns[open]) open = null;
    const reachedRows = plan.steps.slice(0, columns.length).reduce((n, step) => n + step.addons.length, 0);
    const axis = HEAD + reachedRows * shape.row; // where the time axis is
    const late = steps - columns.length;
    const bands = [];
    let y = HEAD;
    plan.steps.forEach((step, index) => {
      const column = columns[index];
      if (index === columns.length) y = axis + FOOT + SHELF_HEAD;
      const top = y;
      if (column) {
        const isOpen = open === index;
        const only = steps === 1;
        // The last step's time counts for nothing in a play: its add-ons keep trying until a
        // song plays or apps stop waiting. It has no time to show and no edge to pull.
        const last = index === steps - 1;
        const { box, title, time } = pooled(heads, index, buildHead);
        box.hidden = false;
        box.classList.toggle("is-open", isOpen);
        box.classList.toggle("is-narrow", column.w < 92);
        title.textContent = only ? "" : column.w >= 92 || isOpen ? `Step ${index + 1}` : `${index + 1}`;
        time.textContent = last ? "" : `· ${seconds(step.seconds)}`;
        // A narrow step shows its number; its time shows when it is opened.
        time.hidden = last || (!isOpen && column.w < 64);
        title.hidden = column.w < 14 && !isOpen;
        box.title = last ? `Step ${index + 1}: the last` : `Step ${index + 1}: ${seconds(step.seconds)}`;
        let hx = column.x;
        if (isOpen && column.w < 92) {
          // (Beside its edge, where the words have room.)
          box.style.width = "";
          const w = box.offsetWidth;
          hx = column.x + column.w + 10;
          if (hx + w > shape.full - 8) hx = Math.max(4, column.x - w - 4);
        } else {
          box.style.width = `${column.w}px`;
        }
        box.style.transform = `translateX(${hx}px)`;
        const edge = pooled(edges, index, buildEdge);
        edge.hidden = last;
        edge.classList.toggle("is-slim", column.w < SLIM);
        edge.classList.toggle("is-close", column.w < 44);
        edge.classList.toggle("is-open", isOpen);
        edge.style.transform = `translateX(${column.x + column.w}px)`;
        edge.style.height = `${axis}px`;
        edge.setAttribute("aria-valuenow", String(step.seconds));
        edge.setAttribute("aria-valuetext", seconds(step.seconds));
        edge.setAttribute("aria-label", only ? "Time to send audio" : `Step ${index + 1}: time to send audio`);
      }
      for (const addon of step.addons) {
        const item = rows.get(addon.id);
        item.step = index;
        item.shelved = !column;
        item.row.classList.toggle("is-shelved", !column);
        item.row.style.transform = `translateY(${y}px)`;
        const together = step.addons.filter(live).length > 1 && !addon.off;
        const about = `${addon.name}${addon.off ? ", off" : ""}: step ${index + 1} of ${steps}`;
        const keys = "The arrow keys move it to another step.";
        if (column) {
          item.row.style.height = `${shape.row}px`;
          item.bar.style.transform = `translateX(${column.x + GAP}px)`;
          item.bar.style.width = `${column.w - 2 * GAP}px`;
          item.bar.classList.toggle("is-tight", column.w < 72);
          // Its trail: from the end of its step to where apps stop waiting.
          const right = column.x + column.w - GAP;
          item.trailX = right;
          item.trailW = addon.off ? 0 : Math.max(0, shape.limit - right);
          item.trail.hidden = item.trailW < 4;
          item.trail.style.transform = `translateX(${right}px)`;
          item.trail.style.width = `${item.trailW}px`;
          if (item.usual) {
            // (The bar spans its column: the last step's runs on to apps' limit.)
            const span = column.to - column.from;
            const inStep = addon.usual <= span;
            const inTime = column.from + addon.usual < data.wait;
            const onTrail = !inStep && inTime && !item.trail.hidden;
            item.usual.hidden = onTrail;
            item.usual.style.left = `${Math.min(1, addon.usual / span) * 100}%`;
            item.usual.classList.toggle("is-late", !inTime);
            item.later.hidden = !onTrail;
            if (onTrail) item.later.style.left = `${shape.place(column.from + addon.usual) - right}px`;
          }
          const next = index < steps - 1 ? `, and after ${seconds(step.seconds)} the next step joins while it keeps trying` : "";
          item.bar.setAttribute(
            "aria-label",
            `${about}, ${together ? "starting together with others" : "starting"} at ${seconds(column.from)}${next}. ${keys}`,
          );
          y += shape.row;
        } else {
          item.row.style.height = `${SHELF_ROW}px`;
          item.trail.hidden = true;
          item.trailW = 0;
          item.bar.style.transform = `translateX(${shape.full - 24 - STUB}px)`;
          item.bar.style.width = `${STUB}px`;
          item.bar.classList.remove("is-tight");
          item.num.textContent = `step ${index + 1}`;
          item.bar.setAttribute(
            "aria-label",
            `${about}, never reached: apps stop waiting at ${seconds(data.wait)}. ${keys}`,
          );
          y += SHELF_ROW;
        }
      }
      bands.push({ top, bottom: y });
    });
    for (const pool of [heads.map((head) => head.box), edges]) {
      pool.slice(columns.length).forEach((element) => {
        element.hidden = true;
      });
    }
    const bottom = late ? y + 10 : axis + FOOT;
    inner.style.height = `${bottom}px`;
    Object.assign(shape, { bands, axis, bottom });

    const stops = [{ x: shape.left, text: "0 s" }, ...columns.map((column) => (
      { x: column.x + column.w, text: seconds(column.to) }
    ))];
    let before = -1e9;
    stops.forEach((stop, index) => {
      const tick = pooled(ticks, index, buildTick);
      // (Under the limit's words: left out.)
      const covered = !late && shape.reach && stop.x > shape.limit - 180 && stop.x < shape.limit + 4;
      const shown = !covered && stop.x - before >= 34;
      tick.hidden = false;
      tick.textContent = shown ? stop.text : "";
      // (The last time stays inside the board when there is no room after it.)
      const over = shown && !rate ? Math.max(0, stop.x - 4 + tick.offsetWidth - (shape.full - 2)) : 0;
      tick.style.transform = `translate(${stop.x - 4 - over}px, ${axis + 5}px)`;
      if (shown) before = stop.x;
    });
    ticks.slice(stops.length).forEach((tick) => {
      tick.hidden = true;
    });

    // Apps' limit: a line down the board. With add-ons past it, the line turns and runs
    // under the board, and they wait below it.
    limit.hidden = !shape.reach;
    limit.style.transform = `translateX(${shape.limit}px)`;
    limit.style.height = `${late ? axis + FOOT + 8 : axis}px`;
    limit.classList.toggle("is-cutting", late > 0 || shape.limit < shape.end - 1);
    limit.classList.toggle("is-turned", late > 0);
    limitText.hidden = late > 0;
    shelf.hidden = !late;
    shelfLine.hidden = !late;
    if (late) {
      const waiting = plan.steps.slice(columns.length).flatMap((step) => step.addons).length;
      shelf.style.transform = `translateY(${axis + FOOT}px)`;
      shelf.style.width = `${shape.full}px`;
      shelfLine.style.transform = `translate(12px, ${axis + FOOT + 6}px)`;
      shelfLine.style.width = `${shape.limit - 10}px`;
      shelfText.textContent = stacked
        ? `After ${seconds(data.wait)}, never reached:`
        : `Apps stop waiting at ${seconds(data.wait)}. Never reached (${waiting}):`;
    }
    now.style.height = `${axis}px`;
    const column = open === null ? null : columns[open];
    lit.classList.toggle("is-shown", Boolean(column));
    if (column) {
      lit.style.transform = `translate(${column.x}px, ${HEAD - 4}px)`;
      lit.style.width = `${column.w}px`;
      lit.style.height = `${axis - HEAD + 4}px`;
    }
    board.classList.toggle("can-scroll", board.scrollWidth > board.clientWidth + 1);
  }

  // --- words ---------------------------------------------------------------------------

  function describe() {
    const parts = [];
    const on = (step) => step.addons.filter(live);
    const named = (step) => listed(on(step).map((addon) => addon.name));
    const steps = plan.steps.filter((step) => on(step).length);
    const off = everyone().filter((addon) => addon.off).map((addon) => addon.name);
    if (steps.length === 1 && on(steps[0]).length === 1) {
      parts.push(`${named(steps[0])} is the only add-on, so every song starts from it. It has until apps stop waiting, ${seconds(data.wait)}, to send audio.`);
    } else if (steps.length) {
      const first = steps[0];
      parts.push(on(first).length > 1
        ? `${named(first)} start together. The first to send audio plays, and the others are stopped.`
        : `${named(first)} starts first.`);
      for (let index = 1; index < steps.length;) {
        const after = seconds(steps[index - 1].seconds);
        const step = steps[index];
        let last = index;
        if (on(step).length === 1) {
          // Add-ons in turn with one time: one sentence.
          while (steps[last + 1] && on(steps[last + 1]).length === 1
                 && steps[last + 1].seconds === step.seconds) last++;
        }
        const time = seconds(step.seconds);
        if (last > index) {
          const names = steps.slice(index, last + 1).map((item) => on(item)[0].name);
          parts.push(`With no audio after ${after}, ${listed(names)} join one at a time, ${time} apart.`);
        } else if (on(step).length > 1) {
          parts.push(`With no audio after ${after}, ${named(step)} join together.`);
        } else {
          parts.push(`With no audio after ${after}, ${named(step)} joins.`);
        }
        index = last + 1;
      }
    }
    if (steps.length > 1) {
      parts.push("The add-ons already asked keep trying, and the first to send audio plays.");
    }
    if (off.length) parts.push(`${listed(off)} ${off.length === 1 ? "is" : "are"} off and passed over.`);
    if (plan.checks) {
      parts.push(plan.handover > 0
        ? `An add-on that says it has the song ready takes over after ${seconds(plan.handover)} without audio.`
        : "Add-ons that say they have the song ready go first.");
    }
    says.textContent = parts.join(" ");
    // (The add-ons never reached are listed under the board, below apps' limit.)

    const outcome = simulate();
    if (!outcome.winner) {
      guess.textContent = "With recent speeds, no add-on is likely to send audio in time.";
    } else if (outcome.measured) {
      guess.textContent = `With recent speeds, a song usually starts after about ${seconds(outcome.at)}, from ${outcome.winner.name}.`;
    } else {
      guess.textContent = "No recent speeds yet: the run shows each add-on halfway through its step.";
    }
    if (!running) watch.textContent = watched ? "Watch again" : outcome.measured ? "Watch it" : "Watch a sample start";
    markPreset();
    const chosen = presets.find((button) => button.getAttribute("aria-pressed") === "true");
    foldWords.textContent = `${chosen ? `${chosen.textContent}. ` : ""}${guess.textContent}`;
  }

  // --- a start, by recent speeds -------------------------------------------------------

  // Everyone asked so far races: the earliest to send audio, by its usual time from when
  // its step began, plays. Steps that would begin later are not asked.
  function simulate() {
    const begins = starts();
    let best = null;
    plan.steps.forEach((step, index) => {
      if (begins[index] >= data.wait) return;
      for (const addon of step.addons.filter(live)) {
        if (addon.usual === null) continue;
        const at = begins[index] + addon.usual;
        if (at < data.wait && (!best || at < best.at)) {
          best = { winner: addon, step: index, at, into: addon.usual, measured: true };
        }
      }
    });
    if (best) return best;
    // No recent speeds: the first add-on without plays yet, halfway through its step.
    for (let index = 0; index < plan.steps.length && begins[index] < data.wait; index++) {
      const step = plan.steps[index];
      const unknown = step.addons.filter(live).filter((addon) => !addon.tried && addon.light !== "error");
      if (unknown.length) {
        const into = step.seconds / 2;
        return { winner: unknown[0], step: index, at: begins[index] + into, into, measured: false };
      }
    }
    return { winner: null, at: data.wait, step: plan.steps.length };
  }

  let run = 0;
  let running = false;
  let watched = false;
  const pause = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
  function clearRun() {
    for (const item of rows.values()) {
      item.fill.style.transition = "none";
      item.fill.style.transform = "scaleX(0)";
      item.bar.classList.remove("is-winner", "is-stopped", "is-spent", "is-skipped", "is-fading");
      item.result.textContent = "";
      item.trailFill.style.transition = "none";
      item.trailFill.style.transform = "scaleX(0)";
      item.trail.classList.remove("is-winner", "is-stopped", "is-skipped", "is-fading");
    }
    now.style.transition = "none";
    now.classList.remove("is-shown");
  }
  function stopRun() {
    run++;
    running = false;
    watch.setAttribute("aria-pressed", "false");
    clearRun();
    describe();
  }
  async function show() {
    const mine = ++run;
    clearRun();
    if (!plan.steps.length) return;
    running = true;
    watched = true;
    watch.textContent = "Stop";
    watch.setAttribute("aria-pressed", "true");
    const outcome = simulate();
    const begins = starts();
    const end = outcome.winner ? outcome.at : data.wait;
    const pace = Math.min(400, 4000 / Math.max(end, 0.5)); // ms a second of the start
    // Time ``t`` of the start: every add-on asked fills its bar, then its trail.
    const draw = (t) => {
      plan.steps.forEach((step, index) => {
        if (!shape.columns[index]) return;
        const into = t - begins[index];
        for (const addon of step.addons.filter(live)) {
          const item = rows.get(addon.id);
          item.fill.style.transform = `scaleX(${Math.max(0, Math.min(1, into / step.seconds))})`;
          if (!item.trailW) continue;
          const px = into > step.seconds ? shape.place(Math.min(t, data.wait)) - item.trailX : 0;
          item.trailFill.style.transform = `scaleX(${Math.max(0, Math.min(1, px / item.trailW))})`;
        }
      });
      now.style.transform = `translateX(${shape.place(t) - 1}px)`;
      nowTime.textContent = seconds(t);
    };
    now.style.transition = "opacity 200ms ease";
    draw(0);
    void now.offsetWidth;
    now.classList.add("is-shown");
    if (still.matches) {
      draw(end); // no motion: the start's end at once
    } else {
      const total = Math.max(120, end * pace);
      const began = performance.now();
      await new Promise((resolve) => {
        const tick = (moment) => {
          if (mine !== run) return resolve();
          const part = Math.min(1, (moment - began) / total);
          draw(part * end);
          if (part < 1) window.requestAnimationFrame(tick);
          else resolve();
        };
        window.requestAnimationFrame(tick);
      });
      if (mine !== run) return;
    }
    // Who played, who was stopped, and the steps that never began.
    plan.steps.forEach((step, index) => {
      if (!shape.columns[index]) return;
      const asked = begins[index] < end || index === 0;
      for (const addon of step.addons.filter(live)) {
        const item = rows.get(addon.id);
        if (!asked) {
          item.bar.classList.add("is-skipped");
          item.trail.classList.add("is-skipped");
          item.result.textContent = "not asked";
        } else if (addon === outcome.winner) {
          item.bar.classList.add("is-winner");
          item.trail.classList.add("is-winner");
          item.result.textContent = "plays";
        } else {
          item.bar.classList.add(outcome.winner ? "is-stopped" : "is-spent");
          item.trail.classList.add("is-stopped");
          item.result.textContent = outcome.winner ? "stopped" : "no audio";
        }
      }
    });
    running = false;
    watch.textContent = "Watch again";
    watch.setAttribute("aria-pressed", "false");
    await pause(still.matches ? 3000 : 2200);
    if (mine !== run) return;
    for (const item of rows.values()) {
      item.fill.style.transition = "opacity 300ms ease";
      item.bar.classList.add("is-fading");
      item.trailFill.style.transition = "opacity 300ms ease";
      item.trail.classList.add("is-fading");
    }
    now.classList.remove("is-shown");
    await pause(320);
    if (mine === run) clearRun();
  }
  watch.addEventListener("click", () => {
    if (running) stopRun();
    else show();
  });

  // --- presets -------------------------------------------------------------------------

  const quickest = () => [...everyone()].sort((a, b) => {
    const rank = (addon) => (addon.off ? 2e9 : addon.usual === null ? 1e9 : addon.usual);
    return rank(a) - rank(b);
  });
  const alone = (addons) => addons.map((addon) => ({ seconds: data.default, addons: [addon] }));
  const PRESETS = {
    fastest: () => ({ steps: [{ seconds: data.default, addons: everyone() }], checks: false }),
    few: () => {
      const order = quickest();
      return { steps: [{ seconds: data.default, addons: order.slice(0, 3) }, ...alone(order.slice(3))], checks: false };
    },
    backups: () => {
      // The first add-on that is on is the main one.
      const order = everyone();
      const main = order.find(live) || order[0];
      const rest = order.filter((addon) => addon !== main);
      return { steps: [{ seconds: data.default, addons: [main] }, { seconds: data.default, addons: rest }], checks: false };
    },
    turns: () => ({ steps: alone(everyone()), checks: false }),
    // Ready add-ons go first: the checks are waited for (a handover at 0 s).
    ready: () => ({ steps: alone(everyone()), checks: true, handover: 0 }),
  };
  // A preset is pressed when it would make the same steps (their add-ons; times aside).
  const sameSteps = (a, b) => a.length === b.length && a.every((step, index) => {
    const other = new Set(b[index].addons.map((addon) => addon.id));
    return step.addons.length === other.size && step.addons.every((addon) => other.has(addon.id));
  });
  const presets = [...root.querySelectorAll(".plan-preset")];
  let shownHelp = null;
  function markPreset() {
    let found = null;
    for (const button of presets) {
      const made = PRESETS[button.dataset.preset]();
      const handed = made.handover === undefined || made.handover === plan.handover;
      const pressed = !found && made.checks === plan.checks && handed
        && sameSteps(made.steps, plan.steps);
      if (pressed) found = button;
      button.setAttribute("aria-pressed", String(pressed));
    }
    if (!presetHelp) return;
    const words = found ? found.dataset.help : "Your own arrangement. Choose a preset to start over from it.";
    if (words === shownHelp) return;
    if (shownHelp === null || still.matches) {
      presetHelp.textContent = words;
    } else {
      presetHelp.classList.add("is-changing");
      window.setTimeout(() => {
        presetHelp.textContent = words;
        presetHelp.classList.remove("is-changing");
      }, 160);
    }
    shownHelp = words;
  }

  // --- ready checks --------------------------------------------------------------------

  const checks = root.querySelector(".plan-checks-switch");
  const handoverRow = root.querySelector(".plan-handover");
  const handover = handoverRow && handoverRow.querySelector("input");
  function syncChecks() {
    if (!checks) return;
    checks.setAttribute("aria-checked", String(plan.checks));
    checks.querySelector(".switch-text").textContent = plan.checks ? "On" : "Off";
    if (handoverRow) handoverRow.hidden = !plan.checks;
  }
  if (checks) {
    checks.addEventListener("click", () => {
      plan.checks = !plan.checks;
      syncChecks();
      changed(false);
    });
  }
  if (handover) {
    handover.addEventListener("change", () => {
      const value = Number(handover.value.replace(",", "."));
      if (Number.isFinite(value)) plan.handover = Math.min(MAX_SECONDS, Math.max(0, value));
      handover.value = number(plan.handover);
      changed(false);
    });
  }
  for (const button of presets) {
    button.addEventListener("click", () => {
      const made = PRESETS[button.dataset.preset]();
      plan.steps = made.steps.filter((step) => step.addons.length);
      plan.checks = made.checks;
      if (made.handover !== undefined) {
        plan.handover = made.handover;
        if (handover) handover.value = number(plan.handover);
      }
      syncChecks();
      changed();
    });
  }

  // --- saving --------------------------------------------------------------------------

  let quiet = 0;
  function saved() {
    status.textContent = "Saved";
    status.classList.add("is-shown");
    card.classList.add("is-saved");
    window.setTimeout(() => card.classList.remove("is-saved"), 300);
    window.clearTimeout(quiet);
    quiet = window.setTimeout(() => status.classList.remove("is-shown"), 2400);
  }

  let saving = 0;
  function save() {
    if (data.readonly) return;
    window.clearTimeout(saving);
    saving = window.setTimeout(async () => {
      const body = new URLSearchParams({
        csrf: data.csrf,
        plan: JSON.stringify({
          steps: plan.steps.map((step) => ({ seconds: step.seconds, addons: step.addons.map((addon) => addon.id) })),
          checks: plan.checks,
          handover: plan.handover,
        }),
      });
      try {
        const response = await fetch(data.action, {
          method: "POST",
          body,
          credentials: "same-origin",
          headers: { accept: "application/json" },
        });
        const answer = response.ok ? await response.json() : null;
        if (!answer || !answer.saved) throw new Error("not saved");
        saved();
      } catch {
        window.location.reload(); // the page says why, and shows the plan as it is
      }
    }, 400);
  }

  let showing = 0;
  function changed(replay = true) {
    layout();
    describe();
    save();
    if (running) stopRun();
    if (!replay) return;
    // The run after a change, once the change has settled.
    window.clearTimeout(showing);
    showing = window.setTimeout(show, 500);
  }

  // --- pulling a bar or a step's edge --------------------------------------------------

  // On the board the columns decide: the column under ``x``, or the line between two for a
  // step of its own.
  function columnSlot(x) {
    const { columns } = shape;
    if (x < columns[0].x + 8) return { kind: "new", at: 0 };
    for (let index = 0; index < columns.length; index++) {
      const column = columns[index];
      const right = column.x + column.w;
      const margin = Math.min(14, column.w / 4);
      if (index < columns.length - 1 && x >= right - margin && x <= right + margin) {
        return { kind: "new", at: index + 1 };
      }
      // (At the far end of the last step, which runs to apps' limit: a step after it.)
      if (column.last && x >= right - margin) return { kind: "new", at: index + 1 };
      if (x < right) return { kind: "join", step: index };
    }
    return { kind: "new", at: columns.length };
  }
  // Under the board, the add-ons never reached are a plain list, so their rows decide: onto
  // a step's rows to start with them, or between two steps' rows for a step of its own.
  function rowSlot(y, from) {
    const { bands } = shape;
    for (let index = from; index < bands.length; index++) {
      const { top, bottom } = bands[index];
      const row = (bottom - top) / (plan.steps[index].addons.length || 1);
      const margin = Math.min(15, row * 0.27);
      if (y < top + margin) return { kind: "new", at: index };
      if (y <= bottom - margin) return { kind: "join", step: index };
    }
    return { kind: "new", at: bands.length };
  }
  function aim(item, x, y) {
    const { columns, bands, axis } = shape;
    let slot = null;
    if (y < axis + FOOT) {
      if (columns.length) slot = columnSlot(x);
    } else if (bands.length > columns.length) {
      slot = rowSlot(y, columns.length);
    }
    return slot && wouldMove(item.addon, slot) ? slot : null;
  }

  // Where a pulled add-on would land, in words beside it.
  function landing(addon, slot) {
    if (slot.kind === "join") {
      const others = plan.steps[slot.step].addons.filter((other) => other !== addon).map((other) => other.name);
      return `with ${others.length > 2 ? `${others[0]} and ${others.length - 1} others` : listed(others)}`;
    }
    if (slot.at === 0) return "first, on its own";
    const before = plan.steps[slot.at - 1].addons.filter((other) => other !== addon);
    return `on its own, after ${before.length === 1 ? before[0].name : `step ${slot.at}`}`;
  }

  // One highlight: the column it would join or the line where a new step would begin; in the
  // list under the board, the rows it would join or the line between two.
  function showAim(addon, slot, x, y, touch) {
    const { columns, bands } = shape;
    band.classList.remove("is-shown");
    insert.classList.remove("is-shown");
    zone.classList.remove("is-shown", "is-new");
    tip.classList.toggle("is-shown", Boolean(slot));
    if (!slot) return;
    const wide = shape.full - 12;
    zone.style.height = `${shape.axis - HEAD}px`;
    if (slot.kind === "join") {
      const column = columns[slot.step];
      if (column) {
        zone.style.transform = `translate(${column.x + GAP}px, ${HEAD}px)`;
        zone.style.width = `${column.w - 2 * GAP}px`;
        zone.classList.add("is-shown");
      } else {
        const rowsOf = bands[slot.step];
        band.style.transform = `translate(6px, ${rowsOf.top}px)`;
        band.style.width = `${wide}px`;
        band.style.height = `${rowsOf.bottom - rowsOf.top}px`;
        band.classList.add("is-shown");
      }
    } else {
      const count = columns.length;
      // A step of its own at the end of the board is reached if there is time left for it.
      const reached = slot.at < count || (slot.at === count && shape.shown < data.wait);
      if (reached) {
        const column = columns[slot.at];
        zone.style.transform = `translate(${(column ? column.x : shape.end) - 2}px, ${HEAD}px)`;
        zone.style.width = "4px";
        zone.classList.add("is-shown", "is-new");
      } else {
        let line = bands[bands.length - 1].bottom;
        if (slot.at < bands.length) line = bands[slot.at].top;
        insert.style.transform = `translate(6px, ${line - 1.5}px)`;
        insert.style.width = `${wide}px`;
        insert.classList.add("is-shown");
      }
    }
    tip.replaceChildren();
    make("strong", "", tip).textContent = addon.name;
    tip.append(` ${landing(addon, slot)}`);
    const w = tip.offsetWidth;
    const lift = touch ? 58 : 44; // (clear of the finger)
    tip.style.transform = `translate(${Math.max(6, Math.min(shape.full - w - 6, x - w / 2))}px, ${
      y - lift < 2 ? y + 30 : y - lift}px)`;
  }

  // Move ``addon`` to ``slot``: into a step ({kind: "join", step}) or as a step of its own
  // before step ``at`` ({kind: "new", at}); the indexes are the plan's before the move.
  function wouldMove(addon, slot) {
    const from = stepOf(addon);
    const single = plan.steps[from].addons.length === 1;
    if (slot.kind === "join") return slot.step !== from;
    return !(single && (slot.at === from || slot.at === from + 1));
  }
  function moveTo(addon, slot) {
    if (!wouldMove(addon, slot)) return false;
    const home = plan.steps[stepOf(addon)];
    const target = slot.kind === "join" ? plan.steps[slot.step] : null;
    const kept = home.addons.length === 1 ? home.seconds : data.default;
    home.addons.splice(home.addons.indexOf(addon), 1);
    if (target) target.addons.push(addon);
    else plan.steps.splice(slot.at, 0, { seconds: kept, addons: [addon] });
    plan.steps = plan.steps.filter((step) => step.addons.length);
    return true;
  }
  function said(addon) {
    const step = stepOf(addon);
    const together = plan.steps[step].addons.length > 1;
    announce.textContent = `${addon.name}: step ${step + 1}${together ? ", together with others" : ""}`;
  }

  let pull = null;
  let scrolling = 0;
  function track() {
    const box = inner.getBoundingClientRect();
    const x = pull.clientX - box.left;
    const y = pull.clientY - box.top;
    const dy = pull.clientY - pull.startY + window.scrollY - pull.scroll;
    pull.item.bar.style.transform = `translate(${x - pull.grab}px, ${dy}px)`;
    pull.slot = aim(pull.item, x, y);
    showAim(pull.item.addon, pull.slot, x, y, pull.touch);
  }
  // Near the top or bottom of the window the page moves along, so a long board can be crossed.
  function follow() {
    if (!pull || pull.edge) return;
    const y = pull.clientY;
    const far = window.innerHeight - 56;
    const by = y < 56 ? (y - 56) / 5 : y > far ? (y - far) / 5 : 0;
    if (by) {
      window.scrollBy(0, by);
      track();
    }
    scrolling = window.requestAnimationFrame(follow);
  }
  // (A long press on a bar or an edge pulls it: no menu of the browser's.)
  inner.addEventListener("contextmenu", (event) => {
    if (event.target.closest(".plan-bar, .plan-edge")) event.preventDefault();
  });
  inner.addEventListener("pointerdown", (event) => {
    if (data.readonly || event.button !== 0 || pull) return;
    const edge = event.target.closest(".plan-edge");
    let bar = event.target.closest(".plan-bar");
    // With a mouse, a never-reached add-on is also taken by its name (a finger there scrolls
    // the page, so it takes the handle at the row's end).
    const waiting = !bar && event.pointerType === "mouse" && event.target.closest(".plan-row.is-shelved");
    if (waiting) bar = waiting.querySelector(".plan-bar");
    if (!edge && !bar) return;
    window.clearTimeout(showing);
    if (running) {
      stopRun(); // no run while something is held
    } else {
      run++;
      clearRun();
    }
    const target = edge || bar;
    pull = {
      edge,
      item: bar ? itemOf(bar) : null,
      step: edge ? Number(edge.dataset.step) : null,
      startX: event.clientX,
      startY: event.clientY,
      clientX: event.clientX,
      clientY: event.clientY,
      scroll: window.scrollY,
      touch: event.pointerType !== "mouse",
      grab: waiting ? STUB / 2 : event.clientX - target.getBoundingClientRect().left,
      from: edge ? plan.steps[Number(edge.dataset.step)].seconds : 0,
      pps: shape.pps,
      moved: false,
      slot: null,
    };
    try {
      target.setPointerCapture(event.pointerId);
    } catch {
      // (Pointer gone already: the moves that come still arrive while over the board.)
    }
    if (edge || waiting) event.preventDefault();
  });
  inner.addEventListener("pointermove", (event) => {
    if (!pull) return;
    pull.clientX = event.clientX;
    pull.clientY = event.clientY;
    const dx = event.clientX - pull.startX;
    const dy = event.clientY - pull.startY;
    if (!pull.moved && Math.abs(dx) < 4 && Math.abs(dy) < 4) return;
    if (!pull.moved) {
      pull.moved = true;
      if (pull.edge) {
        inner.classList.add("is-sizing");
        pull.edge.classList.add("is-held");
      } else {
        inner.classList.add("is-pulling");
        pull.item.bar.classList.add("is-lifted");
        pull.item.row.classList.add("is-held");
        scrolling = window.requestAnimationFrame(follow);
      }
    }
    if (pull.edge) {
      // A pulled edge stops where apps stop waiting (the board's end); the keys go further.
      const begins = starts()[pull.step];
      let value = pull.from + dx / pull.pps;
      if (begins + pull.from <= data.wait) value = Math.min(value, data.wait - begins);
      setTime(plan.steps[pull.step], value);
      layout(pull.pps);
      describe();
      return;
    }
    track();
  });
  function release(cancelled) {
    if (!pull) return;
    const { edge, item, moved, slot, step, from } = pull;
    pull = null;
    window.cancelAnimationFrame(scrolling);
    inner.classList.remove("is-pulling", "is-sizing");
    if (edge) edge.classList.remove("is-held");
    if (item) {
      item.bar.classList.remove("is-lifted");
      item.row.classList.remove("is-held");
    }
    showAim(null, null);
    if (!moved) {
      // A tap on a bar opens its step: the time is at hand even when the step is narrow.
      if (item && !cancelled) {
        open = item.shelved || open === item.step ? null : item.step;
        layout();
      }
      return;
    }
    if (edge) {
      if (cancelled) {
        plan.steps[step].seconds = from;
        layout();
        describe();
      } else {
        changed();
        announce.textContent = `Step ${step + 1}: up to ${seconds(plan.steps[step].seconds)}`;
      }
      return;
    }
    if (!cancelled && slot && moveTo(item.addon, slot)) {
      open = null;
      changed();
      said(item.addon);
    } else {
      layout();
    }
  }
  inner.addEventListener("pointerup", () => release(false));
  inner.addEventListener("pointercancel", () => release(true));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (pull) {
      release(true);
    } else if (open !== null) {
      open = null;
      layout();
    }
  });

  // --- the keys ------------------------------------------------------------------------

  inner.addEventListener("keydown", (event) => {
    const edge = event.target.closest(".plan-edge");
    if (edge) {
      const step = plan.steps[Number(edge.dataset.step)];
      const by = event.shiftKey ? 5 : 0.5;
      const keys = {
        ArrowLeft: step.seconds - by,
        ArrowDown: step.seconds - by,
        ArrowRight: step.seconds + by,
        ArrowUp: step.seconds + by,
        Home: MIN_SECONDS,
        End: MAX_SECONDS,
      };
      if (!(event.key in keys)) return;
      event.preventDefault();
      setTime(step, keys[event.key]);
      changed(false);
      edge.focus();
      return;
    }
    const bar = event.target.closest(".plan-bar");
    // Left and up are earlier, right and down later: the same places either way.
    const direction = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[event.key];
    if (!bar || !direction) return;
    event.preventDefault();
    const item = itemOf(bar);
    // Places in turn: before step 1, in step 1, before step 2, in step 2, ... after the last.
    const places = [];
    plan.steps.forEach((_, index) => {
      places.push({ kind: "new", at: index }, { kind: "join", step: index });
    });
    places.push({ kind: "new", at: plan.steps.length });
    let index = places.findIndex((place) => place.kind === "join" && place.step === item.step);
    for (index += direction; index >= 0 && index < places.length; index += direction) {
      if (moveTo(item.addon, places[index])) {
        open = null;
        changed(false);
        bar.focus();
        said(item.addon);
        return;
      }
    }
  });

  // --- start ---------------------------------------------------------------------------

  board.addEventListener("scroll", () => {
    root.style.setProperty("--plan-scroll", `${board.scrollLeft}px`);
  });
  syncChecks();
  inner.classList.add("is-still");
  layout();
  describe();
  void inner.offsetWidth;
  inner.classList.remove("is-still");
  let width = board.clientWidth;
  new ResizeObserver(() => {
    if (board.clientWidth !== width && !pull) {
      width = board.clientWidth;
      layout();
    }
  }).observe(board);

  // On a phone the plan is folded at first, so the add-ons below are close at hand: its
  // summary and a button that shows it.
  const phone = window.matchMedia("(max-width: 40rem)");
  let unfolded = false;
  function syncFold() {
    const folded = phone.matches && !unfolded;
    const was = card.classList.contains("is-folded");
    fold.hidden = !phone.matches;
    card.classList.toggle("is-folded", folded);
    foldButton.setAttribute("aria-expanded", String(!folded));
    foldButton.textContent = folded ? "Show the plan" : "Hide the plan";
    if (was && !folded) {
      // (Laid out where it now is, without sliding in from where it was measured hidden.)
      inner.classList.add("is-still");
      layout();
      width = board.clientWidth;
      void inner.offsetWidth;
      inner.classList.remove("is-still");
    }
  }
  foldButton.addEventListener("click", () => {
    unfolded = !unfolded;
    if (!unfolded && running) stopRun();
    syncFold();
  });
  phone.addEventListener("change", syncFold);
  syncFold();
})();
