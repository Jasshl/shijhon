// The page shown while Shijhon restarts: asks which run answers (a short text) until it is
// another one than the run that showed the page, then goes back to where the restart was
// asked from.
(function () {
  "use strict";
  var page = document.getElementById("restarting");
  if (!page) {
    return;
  }
  var boot = page.dataset.boot;
  var began = Date.now();

  function again() {
    if (Date.now() - began > 90000) {
      document.getElementById("restart-late").hidden = false;
    }
    window.setTimeout(ask, 1000);
  }

  function ask() {
    fetch(page.dataset.alive, { cache: "no-store" })
      .then(function (answer) {
        return answer.ok ? answer.text() : boot;
      })
      .then(function (run) {
        if (run && run.trim() !== boot) {
          window.location.replace(page.dataset.next);
        } else {
          again();
        }
      })
      .catch(again);
  }

  window.setTimeout(ask, 1500);
})();
