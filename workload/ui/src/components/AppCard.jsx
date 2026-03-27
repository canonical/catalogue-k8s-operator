import { useState } from "preact/hooks";
import { Icon } from "@iconify/react";
import { EndpointsModal } from "./EndpointsModal";

export function AppCard({ app, index }) {
  const { name, icon, url, api_endpoints, api_docs, description } = app;
  const [modalOpen, setModalOpen] = useState(false);
  const hasEndpoints =
    api_endpoints && Object.keys(api_endpoints).length > 0;

  return (
    <li class="p-matrix__item">
      <div class="p-matrix__content">
        <h3 class="p-matrix__title">
          <Icon icon={`mdi:${icon}`} class="iconify icon md-48" style={{ marginRight: '8px', fontSize: '1rem'}} />
          {name}
        </h3>
        <div style={{ display: "flex" }}>
          <button
            class="p-tooltip--btm-center openLinkBtn customBtn"
            disabled={!url}
            onClick={() => url && window.open(url, "_blank")}
          >
            <Icon icon="mdi:web" class="iconify custom-icon" />
            <span class="p-tooltip__message" role="tooltip">
              Visit UI
            </span>
          </button>

          <button
            class="p-tooltip--btm-center customBtn"
            disabled={!hasEndpoints}
            onClick={() => setModalOpen(true)}
          >
            <Icon icon="mdi:api" class="iconify custom-icon" />
            <span class="p-tooltip__message" role="tooltip">
              View endpoints
            </span>
          </button>

          {hasEndpoints && modalOpen && (
            <EndpointsModal
              name={name}
              endpoints={api_endpoints}
              index={index}
              onClose={() => setModalOpen(false)}
            />
          )}

          <button
            class="p-tooltip--btm-center openLinkBtn customBtn"
            disabled={!api_docs}
            onClick={() => api_docs && window.open(api_docs, "_blank")}
          >
            <Icon icon="mdi:file-document" class="iconify custom-icon" />
            <span class="p-tooltip__message" role="tooltip">
              Read docs
            </span>
          </button>
        </div>
        {description && <p class="p-matrix__desc">{description}</p>}
      </div>
    </li>
  );
}
