"use strict";

// Forms remain usable without scripting. Hide and disable irrelevant controls
// together so hidden fields neither submit stale values nor block validation.
for (const form of document.querySelectorAll("form")) {
  const value = (name) => {
    const control = form.elements.namedItem(name);
    return control?.type === "checkbox" ? (control.checked ? control.value : "") : control?.value;
  };
  const matches = (name, values) => values.split(" ").includes(value(name));
  const update = () => {
    for (const button of form.querySelectorAll("[data-direct-label]")) {
      button.textContent = value("review_before_publishing") === "true"
        ? button.dataset.reviewLabel : button.dataset.directLabel;
    }
    for (const group of form.querySelectorAll("[data-show-for]")) {
      group.hidden = !matches(group.dataset.showFor, group.dataset.values);
    }
    for (const control of form.querySelectorAll("input, select, textarea")) {
      control.disabled = Boolean(control.closest("[hidden]"));
      if (control.dataset.requiredFor) {
        control.required = !control.disabled && matches(
          control.dataset.requiredFor, control.dataset.requiredValues,
        );
      }
    }
  };
  form.addEventListener("change", update);
  update();
}

// A link to a folded section (#settings) opens it.
const reveal = () => {
  const target = location.hash && document.getElementById(location.hash.slice(1));
  if (target instanceof HTMLDetailsElement) target.open = true;
};
addEventListener("hashchange", reveal);
reveal();

// The agent address: a button copies it, where the browser offers the clipboard (a page
// over plain HTTP does not: a click selects the whole address instead).
for (const button of document.querySelectorAll("[data-copy]")) {
  const source = document.getElementById(button.dataset.copy);
  const said = button.parentElement.querySelector(".agent-copied");
  if (!source || !said || !navigator.clipboard) continue;
  button.hidden = false;
  let timer;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(source.textContent.trim());
      said.textContent = "Copied";
    } catch {
      said.textContent = "Select the address to copy it";
    }
    said.classList.add("is-shown");
    clearTimeout(timer);
    timer = setTimeout(() => said.classList.remove("is-shown"), 1800);
  });
}
