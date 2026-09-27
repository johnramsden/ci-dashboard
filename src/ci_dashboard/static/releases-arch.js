// Per-table architecture selector for the Releases page.
//
// Every architecture's table is already in the HTML, all but the first marked
// `hidden`. This script does two things and nothing else: it reveals the
// <select> (rendered hidden, so a JS-less reader never sees a dead control),
// and on change it flips `hidden` on the panels and summary lines belonging to
// that section. No data lives here — render/releases.py emitted all of it.
//
// Revisions are numbered per architecture, and their commits sometimes differ:
// one track/risk/base can be built from one commit on amd64 and a newer one on
// arm64. Switching is therefore a real change of content, not a cosmetic filter.
(function () {
  "use strict";

  function show(section, arch) {
    var selector = '[data-section="' + section + '"]';
    var panels = document.querySelectorAll(
      ".ci-arch__panel" + selector + ", .ci-arch__summary" + selector);
    for (var i = 0; i < panels.length; i++) {
      panels[i].hidden = panels[i].getAttribute("data-arch") !== arch;
    }
  }

  var selects = document.querySelectorAll(".ci-arch__select");
  for (var i = 0; i < selects.length; i++) {
    var select = selects[i];
    select.hidden = false;
    select.addEventListener("change", function (event) {
      show(event.target.getAttribute("data-section"), event.target.value);
    });
  }
})();
