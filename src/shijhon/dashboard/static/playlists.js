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
