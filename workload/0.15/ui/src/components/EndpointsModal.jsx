import { useEffect, useRef } from "preact/hooks";

export function EndpointsModal({ name, endpoints, index, onClose }) {
  const dialogRef = useRef(null);
  const previousFocus = useRef(null);

  useEffect(() => {
    previousFocus.current = document.activeElement;

    const dialog = dialogRef.current;
    if (dialog) {
      const first = dialog.querySelector("button, [href], input, select, textarea, [tabindex]");
      if (first) first.focus();
    }

    function onKeyDown(e) {
      if (e.key === "Escape") {
        onClose();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (previousFocus.current && previousFocus.current.focus) {
        previousFocus.current.focus();
      }
    };
  }, [onClose]);

  return (
    <div class="p-modal" style={{ display: "flex" }}>
      <section
        class="p-modal__dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`modal-title-${index}`}
        ref={dialogRef}
      >
        <header class="p-modal__header">
          <h2 class="p-modal__title" id={`modal-title-${index}`}>
            API Endpoints for {name}
          </h2>
          <button
            class="p-modal__close"
            aria-label="Close active modal"
            onClick={onClose}
          >
            Close
          </button>
        </header>
        <div class="p-modal__content">
          {Object.entries(endpoints).map(([key, value]) => (
            <p key={key}>
              <strong>{key}</strong>: <span>{value}</span>
            </p>
          ))}
        </div>
      </section>
    </div>
  );
}
