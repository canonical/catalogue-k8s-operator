import { Navigation } from "./Navigation";
import { HeroStrip } from "./HeroStrip";
import { AppMatrix } from "./AppMatrix";
import { LinksSection } from "./LinksSection";

export function App({ config }) {
  const { title, tagline, description, apps, links } = config;

  return (
    <>
      <Navigation title={title} />
      <HeroStrip tagline={tagline} description={description} />
      <div class="p-strip">
        <div class="row">
          <div class="col-12">
            <h3>Applications</h3>
          </div>
        </div>
        <AppMatrix apps={apps} />
        <LinksSection links={links} />
      </div>
    </>
  );
}
