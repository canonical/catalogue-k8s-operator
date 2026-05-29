import { Icon } from "@iconify/react";

export function LinkCategory({ link }) {
  const { category, items } = link;

  if (!category) return null;

  return (
    <li class="p-matrix__item">
      <div class="p-matrix__content">
        <h3 class="p-matrix__title">
          <Icon icon="mdi:bookmark" class="iconify icon md-48" style={{ marginRight: '8px', fontSize: '1rem'}} />
          
          {category}
        </h3>
        <ul class="link-list">
          {items.map((item, i) => (
            <li key={i}>
              <a href={item.url} target={item.target || undefined}>
                {item.name}
              </a>
            </li>
          ))}
        </ul>
      </div>
    </li>
  );
}
