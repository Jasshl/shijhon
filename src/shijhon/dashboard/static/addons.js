// The Add-ons page: pick an add-on up by its number and pull it to another place. It
// follows the pointer freely, the others slide out of its way, and on release it settles
// back into the list at its place; the new order is saved in the background. The arrows
// move a row the same way; without this script they save through the page.
(() => {
  const chain = document.querySelector("ol.chain[data-reorder]");
  const token = chain && chain.querySelector("input[name=csrf]");
  if (!chain || !token) return;
  chain.classList.add("is-sortable");

  const csrf = token.value;
  const status = document.getElementById("order-status");
  const still = window.matchMedia("(prefers-reduced-motion: reduce)");
  const SETTLE_MS = 220;
  const EDGE = 96; // px from the window's edge where the page scrolls along
  const rows = () => [...chain.querySelectorAll(":scope > li.source")];
  const idOf = (row) => row.querySelector(".source-order").dataset.addon;
  const nameOf = (row) => row.querySelector("h3").textContent.trim();

  let drag = null;
  let busy = false;

  // --- moving rows ---------------------------------------------------------------------

  // Where the rows are now, and the shift each needs for `row` to go from `from` to `to`.
  function shifts(items, boxes, from, to) {
    const height = boxes[from].height;
    return items.map((row, i) => {
      if (i === from) {
        let moved = 0;
        for (let j = Math.min(from, to); j <= Math.max(from, to); j++) {
          if (j !== from) moved += boxes[j].height;
        }
        return to > from ? moved : -moved;
      }
      if (from < i && i <= to) return -height;
      if (to <= i && i < from) return height;
      return 0;
    });
  }

  function place(items, offsets) {
    items.forEach((row, i) => {
      if (row.classList.contains("is-lifted")) return;
      row.style.transform = offsets[i] ? `translateY(${offsets[i]}px)` : "";
    });
  }

  // Animate `row` from index `from` to `to`, then put it there in the page.
  function settle(row, items, boxes, from, to) {
    const offsets = shifts(items, boxes, from, to);
    const done = () => {
      chain.classList.add("is-still");
      items.forEach((item) => {
        item.style.transform = "";
      });
      row.classList.remove("is-lifted", "is-settling");
      const after = items.filter((item) => item !== row);
      chain.insertBefore(row, after[to] || null);
      renumber();
      void chain.offsetHeight; // the new places apply before transitions come back
      chain.classList.remove("is-still");
    };
    if (still.matches || from === to) {
      done();
      return Promise.resolve();
    }
    row.classList.add("is-settling");
    place(items, offsets);
    row.style.transform = `translateY(${offsets[from]}px)`;
    return new Promise((resolve) => {
      window.setTimeout(() => {
        done();
        resolve();
      }, SETTLE_MS);
    });
  }

  function renumber() {
    const all = rows();
    all.forEach((row, index) => {
      row.querySelector(".source-order").textContent = String(index + 1);
      const [up, down] = row.querySelectorAll(".source-move button");
      if (up) up.disabled = index === 0;
      if (down) down.disabled = index === all.length - 1;
    });
  }

  // --- saving --------------------------------------------------------------------------

  let quiet = 0;
  function say(words) {
    if (!status) return;
    status.textContent = words;
    status.classList.add("is-shown");
    window.clearTimeout(quiet);
    quiet = window.setTimeout(() => status.classList.remove("is-shown"), 2400);
  }

  async function send(action, fields) {
    const body = new URLSearchParams({ csrf, ...fields });
    busy = true;
    chain.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(action, {
        method: "POST",
        body,
        credentials: "same-origin",
        headers: { accept: "application/json" },
      });
      const answer = response.ok ? await response.json() : null;
      if (!answer || !answer.saved) throw new Error("not saved");
      say("Saved");
    } catch {
      window.location.reload(); // the page says why, and shows the order as it is
    } finally {
      busy = false;
      chain.removeAttribute("aria-busy");
    }
  }

  function saveOrder() {
    const fields = {};
    rows().forEach((row, index) => {
      fields[`position-${idOf(row)}`] = String(index + 1);
    });
    return send(chain.dataset.reorder, fields);
  }

  // --- pulling a row -------------------------------------------------------------------

  chain.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest(".source-order");
    if (!handle || event.button !== 0 || drag || busy) return;
    const items = rows();
    const row = handle.closest("li.source");
    const from = items.indexOf(row);
    drag = {
      row,
      items,
      boxes: items.map((item) => item.getBoundingClientRect()),
      from,
      to: from,
      startX: event.clientX,
      startY: event.clientY,
      startScroll: window.scrollY,
      x: event.clientX,
      y: event.clientY,
      frame: 0,
    };
    try {
      handle.setPointerCapture(event.pointerId); // moves outside the list still count
    } catch {
      // (Pointer gone already: the moves that come still arrive while over the list.)
    }
    chain.classList.add("is-pulling");
    row.classList.add("is-lifted");
    event.preventDefault();
    drag.frame = window.requestAnimationFrame(follow);
  });

  // Each frame: scroll near the window's edges, move the row with the pointer, and slide
  // the others to make room where it would land.
  function follow() {
    if (!drag) return;
    const { y } = drag;
    if (y < EDGE) window.scrollBy(0, -Math.ceil((EDGE - y) / 6));
    else if (y > window.innerHeight - EDGE) {
      window.scrollBy(0, Math.ceil((y - (window.innerHeight - EDGE)) / 6));
    }
    const dy = y - drag.startY + (window.scrollY - drag.startScroll);
    const { boxes, from, items } = drag;
    const center = boxes[from].top + boxes[from].height / 2 + dy;
    let to = from;
    boxes.forEach((box, i) => {
      const middle = box.top + box.height / 2;
      if (i < from && center < middle) to = Math.min(to, i);
      if (i > from && center > middle) to = Math.max(to, i);
    });
    if (to !== drag.to) {
      drag.to = to;
      place(items, shifts(items, boxes, from, to));
    }
    const dx = drag.x - drag.startX; // free sideways too; only up and down choose the place
    drag.row.style.transform = `translate(${dx}px, ${dy}px) scale(1.015)`;
    drag.frame = window.requestAnimationFrame(follow);
  }

  chain.addEventListener("pointermove", (event) => {
    if (!drag) return;
    drag.x = event.clientX;
    drag.y = event.clientY;
  });

  async function release(cancelled) {
    if (!drag) return;
    const { row, items, boxes, from, frame } = drag;
    const to = cancelled ? from : drag.to;
    window.cancelAnimationFrame(frame);
    drag = null;
    busy = true;
    chain.classList.remove("is-pulling");
    await settle(row, items, boxes, from, to);
    if (to !== from) {
      say(`${nameOf(row)} moved to place ${to + 1}`);
      await saveOrder();
    } else busy = false;
  }
  chain.addEventListener("pointerup", () => release(false));
  chain.addEventListener("pointercancel", () => release(true));
  chain.addEventListener("lostpointercapture", () => release(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && drag) release(true);
  });

  // --- the arrows ----------------------------------------------------------------------

  chain.addEventListener("submit", async (event) => {
    const move = event.target.closest(".source-move");
    if (!move) return;
    event.preventDefault();
    if (busy || drag) return;
    const button = event.submitter;
    const direction = button && button.value;
    const items = rows();
    const row = move.closest("li.source");
    const from = items.indexOf(row);
    const to = direction === "up" ? from - 1 : from + 1;
    if (to < 0 || to >= items.length) return;
    busy = true;
    row.classList.add("is-lifted");
    await settle(row, items, items.map((item) => item.getBoundingClientRect()), from, to);
    // The press stays on its arrow, or on the other one where it can't go further.
    const [up, down] = row.querySelectorAll(".source-move button");
    const again = direction === "up" ? up : down;
    (again.disabled ? (direction === "up" ? down : up) : again).focus();
    await send(move.action, { direction });
  });
})();
