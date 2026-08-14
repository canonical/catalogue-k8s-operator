import { LinkCategory } from "./LinkCategory";

export function LinksSection({ links }) {
  if (!links || links.length === 0) return null;

  return (
    <>
      <div class="row">
        <div class="col-12">
          <h3>Links</h3>
        </div>
      </div>
      <div class="row">
        <div class="col-12">
          <ul class="p-matrix" id="links">
            {links.map((link, i) => (
              <LinkCategory key={i} link={link} />
            ))}
          </ul>
        </div>
      </div>
    </>
  );
}
