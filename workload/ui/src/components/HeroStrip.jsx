export function HeroStrip({ tagline, description }) {
  return (
    <div class="p-strip--suru">
      <div class="row">
        <div class="col-12">
          {tagline && <h1>{tagline}</h1>}
          {description && <p>{description}</p>}
        </div>
      </div>
    </div>
  );
}
