"use strict";

// The Library page works without scripting: the search is a form, the lists are paged
// with links, and an action for several albums shows how far it is at each reload. This
// adds a search that narrows the lists while it is typed, progress that updates itself,
// and a count of the albums selected.

const search = document.getElementById("library-search");
const POLL_MS = 2000;
const TYPING_MS = 250;
let latest = 0; // the newest request for the lists: an older answer is dropped
let poll;

const lists = () => document.getElementById("albums");

const fetched = async (url) => {
  const turn = ++latest;
  try {
    const response = await fetch(url, { credentials: "same-origin", headers: { accept: "text/html" } });
    if (!response.ok || turn !== latest) return null;
    const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
    return turn === latest ? parsed.getElementById("albums") : null;
  } catch {
    return null; // offline, or signed out: the page stays as it is
  }
};

const listsUrl = () => {
  const url = new URL(location.href);
  url.searchParams.set("part", "lists");
  url.hash = "";
  return url;
};

// The actions of a selection stay quiet until an album is ticked.
const selection = (form) => {
  if (form.dataset.wired) return;
  form.dataset.wired = "1";
  const all = form.querySelector("[data-bulk-all]");
  const boxes = [...document.querySelectorAll(`[data-bulk-album][form="${form.id}"]`)];
  const count = form.querySelector("[data-bulk-count]");
  const update = () => {
    const ticked = boxes.filter((box) => box.checked).length;
    for (const box of boxes) box.disabled = all.checked;
    for (const button of form.querySelectorAll("button")) button.disabled = !all.checked && !ticked;
    form.classList.toggle("is-idle", !all.checked && !ticked);
    count.textContent = all.checked
      ? "For all of them:"
      : ticked
        ? `For the ${ticked} selected:`
        : "Tick albums to act on several at once.";
  };
  all.addEventListener("change", update);
  for (const box of boxes) box.addEventListener("change", update);
  update();
};

const busy = () => {
  const region = lists();
  if (!region) return true;
  const active = document.activeElement;
  return (
    region.querySelector("[data-bulk-album]:checked, [data-bulk-all]:checked, details[open]") !==
      null ||
    (active instanceof HTMLSelectElement && region.contains(active))
  );
};

const watch = () => {
  clearTimeout(poll);
  if (!lists()?.hasAttribute("data-running")) return;
  poll = setTimeout(async () => {
    const fresh = await fetched(listsUrl());
    const region = lists();
    if (fresh && region) {
      const done = !fresh.hasAttribute("data-running");
      if (done && !busy()) {
        region.replaceWith(fresh); // the lists as the action left them
      } else {
        // Only the progress: what is ticked or chosen in the lists stays.
        region.querySelector("#library-batch")?.replaceWith(fresh.querySelector("#library-batch"));
        region.toggleAttribute("data-running", !done);
      }
      ready();
    } else {
      watch();
    }
  }, POLL_MS);
};

const ready = () => {
  for (const form of document.querySelectorAll("form[data-bulk]")) selection(form);
  for (const hint of document.querySelectorAll("[data-reload-hint]")) hint.hidden = true;
  watch();
};

if (search) {
  let typing;
  const form = search.form;
  for (const button of form.querySelectorAll("[data-search-button]")) button.hidden = true;
  const show = async () => {
    const url = new URL(location.href);
    const words = search.value.trim();
    const filled = url.searchParams.has("filled"); // the filled albums stay shown
    url.search = "";
    if (words) url.searchParams.set("q", words);
    if (filled) url.searchParams.set("filled", "1");
    url.hash = "";
    history.replaceState(null, "", url);
    for (const reset of form.querySelectorAll("[data-search-reset]")) reset.hidden = !words;
    const fresh = await fetched(listsUrl());
    if (fresh) {
      lists()?.replaceWith(fresh);
      ready();
    }
  };
  search.addEventListener("input", () => {
    clearTimeout(typing);
    typing = setTimeout(show, TYPING_MS);
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    clearTimeout(typing);
    show();
  });
}

ready();
