import { AppCard } from "./AppCard";

export function AppMatrix({ apps }) {
  return (
    <div class="row">
      <div class="col-12">
        {apps && apps.length > 0 ? (
          <ul class="p-matrix" id="apps">
            {apps.map((app, i) => (
              <AppCard key={i} app={app} index={i} />
            ))}
          </ul>
        ) : (
          <div class="p-notification--caution">
            <div class="p-notification__content">
              <h5 class="p-notification__title">No items to display</h5>
              <p class="p-notification__message">
                No applications available for display yet. Add some by relating
                compatible charms to this one.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
