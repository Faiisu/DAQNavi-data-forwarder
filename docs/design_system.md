# Interface design system

The Portal and DAQNavi Config Center use related light dashboard layouts with different brand accents. The Portal uses a restrained green accent. DAQNavi uses Advantech blue for navigation and orange for primary highlights. Both use a warm off-white page, white cards, dark text, muted supporting labels, and thin borders. The DAQNavi login screen has a separate dark background.

## Shared visual rules

- Keep content grouped by task, with clear labels and concise help text.
- Use the existing semantic colors for success, warning, and error states.
- Preserve readable contrast, visible keyboard focus, responsive layouts, and control labels.
- Use the existing sans, display, and monospace typefaces for UI text, headings, and technical values.

## Implementation sources

CSS variables are maintained separately in `services/portal/style.css` and `services/daq_navi/web/static/config_center/style.css`. Keep shared neutrals and type choices coherent, but do not force each service to use the same accent tokens. The service HTML and CSS are authoritative for component behavior.
