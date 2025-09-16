(function () {
  var currentDialog = null;
  var lastFocus = null;
  var ignoreFocusChanges = false;
  var focusAfterClose = null;

  function trapFocus(event) {
    if (ignoreFocusChanges) return;

    if (currentDialog.contains(event.target)) {
      lastFocus = event.target;
    } else {
      focusFirstDescendant(currentDialog);
      if (lastFocus == document.activeElement) {
        focusLastDescendant(currentDialog);
      }
      lastFocus = document.activeElement;
    }
  }

  function attemptFocus(child) {
    if (child.focus) {
      ignoreFocusChanges = true;
      child.focus();
      ignoreFocusChanges = false;
      return document.activeElement === child;
    }
    return false;
  }

  function focusFirstDescendant(element) {
    for (var i = 0; i < element.childNodes.length; i++) {
      var child = element.childNodes[i];
      if (attemptFocus(child) || focusFirstDescendant(child)) {
        return true;
      }
    }
    return false;
  }

  function focusLastDescendant(element) {
    for (var i = element.childNodes.length - 1; i >= 0; i--) {
      var child = element.childNodes[i];
      if (attemptFocus(child) || focusLastDescendant(child)) {
        return true;
      }
    }
    return false;
  }

  function toggleModal(modal, sourceEl, open) {
    if (modal && modal.classList.contains("p-modal")) {
      if (typeof open === "undefined") {
        open = modal.style.display === "none";
      }

      if (open) {
        currentDialog = modal;
        modal.style.display = "flex";
        focusFirstDescendant(modal);
        focusAfterClose = sourceEl;
        document.addEventListener("focus", trapFocus, true);
      } else {
        modal.style.display = "none";
        if (focusAfterClose && focusAfterClose.focus) {
          focusAfterClose.focus();
        }
        document.removeEventListener("focus", trapFocus, true);
        currentDialog = null;
      }
    }
  }

  function closeModals() {
    var modals = [].slice.call(document.querySelectorAll(".p-modal"));
    modals.forEach(function (modal) {
      toggleModal(modal, false, false);
    });
  }

  document.addEventListener("click", function (event) {
    var targetControls = event.target.getAttribute("aria-controls");
    if (targetControls) {
      toggleModal(document.getElementById(targetControls), event.target);
    }
  });

  document.addEventListener("keydown", function (e) {
    e = e || window.event;
    if (e.code === "Escape" || e.keyCode === 27) {
      closeModals();
    }
  });

  Handlebars.registerHelper("getKeys", function (obj, options) {
    return Object.keys(obj || {}).length > 0
      ? options.fn(this)
      : options.inverse(this);
  });

  Handlebars.registerHelper("hasKeys", function (obj) {
    return Object.keys(obj || {}).length > 0;
  });
})();
