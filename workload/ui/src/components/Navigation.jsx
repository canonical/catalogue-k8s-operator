export function Navigation({ title }) {
  return (
    <header id="navigation" class="p-navigation is-dark">
      <div class="p-navigation__row">
        <div class="p-navigation__banner">
          <div class="p-navigation__tagged-logo">
            <a class="p-navigation__link" href="#">
              <div class="p-navigation__logo-tag">
                <img
                  class="p-navigation__logo-icon"
                  src="https://assets.ubuntu.com/v1/82818827-CoF_white.svg"
                  alt=""
                />
              </div>
              <span class="p-navigation__logo-title" id="nav">
                {title}
              </span>
            </a>
          </div>
        </div>
      </div>
    </header>
  );
}
