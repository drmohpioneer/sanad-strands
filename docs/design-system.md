# Sanad browser design system

Contract 18 section A is binding: Data-Dense Dashboard structure, Swiss grid discipline, Classic Elegant typography.

## Typography and spacing

Inter supplies Latin functional text; Playfair Display supplies identity and page headings. Noto Sans Arabic supplies Arabic UI; Noto Naskh Arabic supplies record and patient prose. Fonts are local variable TTF files with accompanying SIL Open Font Licenses. `font-display: optional` prevents late swapping; functional faces are preloaded. Clinical numbers use lining, tabular figures. Spacing: 4 / 8 / 12 / 16 / 24 / 32 / 48. Card radius: 14px; control radius: 8px.

## Theme tokens

| Role | Light | Dark |
| --- | --- | --- |
| `bg` | `#FAFAFB` | `#050506` |
| `surface` | `#FFFFFF` | `#0A0A0C` |
| `raised` | `#FFFFFF` | `#101014` |
| `text` | `#0F0F14` | `#F4F4F6` |
| `text-muted` | `#61616E` | `#9494A2` |
| `accent-text` | `#5E6AD2` | `#818CF8` |
| `danger-text` | `#DC2626` | `#F87171` |
| `warning-text` | `#B45309` | `#D97706` |
| `success-text` | `#047857` | `#059669` |
| `border-control` | `#858591` | `#646472` |
| `border-subtle` | `#E6E6EB` | `#1C1C22` |
| `hover` | `#F1F1F5` | `#141419` |
| `selected` | `#EEEEF8` | `#181820` |
| `pressed` | `#EEEEF4` | `#15151C` |
| `focus` | `#5E6AD2` | `#818CF8` |
| `disabled-bg` | `#F1F1F5` | `#141419` |
| `disabled-text` | `#61616E` | `#9494A2` |
| `link` | `#5E6AD2` | `#818CF8` |
| `input-boundary` | `#858591` | `#646472` |
| `validation` | `#DC2626` | `#F87171` |
| `accent-fill` | `#5E6AD2` | `#5E6AD2` |
| `on-accent` | `#FFFFFF` | `#FFFFFF` |

New pressed backgrounds are light `#EEEEF4` and dark `#15151C`; initial candidates failed control-boundary contrast. Badge backgrounds are solid surface, with no opacity or wash. Primary buttons keep accent-fill / on-accent in every state; hover/press change the outline. Other controls use text on surface/hover/pressed. Selected navigation uses text on selected. Focus: 3px outline with 3px offset. Validation: danger boundary and written error. Disabled controls retain explicit disabled semantics without opacity.

Light overlay shadow: `0 12px 32px #0f0f1414`; dark: `0 16px 40px #000000cc`. Ordinary rows have no shadow.

## Theme precedence and first paint

A blocking same-origin script applies explicit light/dark before CSS paints. System is the default and follows changes only while selected. CSS sets native-control color-scheme. No inline resources or CDN. Storage denial falls back to system. LocalStorage holds only the theme choice. The current tab's sessionStorage holds the list return URL and scroll position; no credentials or clinical record cache.

## Density, direction and states

At 960px and above, a native fixed-layout table uses 26/6/34/17/17 percent columns. Native header buttons implement stable sorting with aria-sort; patient names are links. Rows start at 4rem and grow, with 10px block / 12px inline padding. Below 960px cells are labelled and stacked, with explicit table roles retained. At 320px row text is at least 16px and controls are at least 44px. Mixed-script content uses bdi; layout uses logical CSS. Only directional arrows mirror. No row-reverse, row scaling, stagger or shimmer. Reduced motion disables transitions.

One status refers to one displayed obligation, never to a patient's overall completion. Fulfillment, review, acknowledgment, resolution and missing readings remain separate. Patient labels expose the allowed plan fields without schema names or ids.

## Computed contrast

Recomputed from the shipped stylesheet by `tests/test_dashboard18_contrast.py`. Semantic surface pairs include badges and their icons. These are token measurements, not a browser or assistive-technology acceptance claim.

| Foreground / background | Light ratio | Dark ratio | Required |
| --- | ---: | ---: | ---: |
| text / bg | 18.323 | 18.547 | 4.5 |
| text / surface | 19.113 | 18.008 | 4.5 |
| text / raised | 19.113 | 17.282 | 4.5 |
| text-muted / bg | 5.847 | 6.807 | 4.5 |
| text-muted / surface | 6.099 | 6.610 | 4.5 |
| text-muted / raised | 6.099 | 6.343 | 4.5 |
| accent-text / bg | 4.506 | 6.830 | 4.5 |
| accent-text / surface | 4.700 | 6.631 | 4.5 |
| accent-text / raised | 4.700 | 6.364 | 4.5 |
| danger-text / bg | 4.630 | 7.365 | 4.5 |
| danger-text / surface | 4.829 | 7.151 | 4.5 |
| danger-text / raised | 4.829 | 6.863 | 4.5 |
| warning-text / bg | 4.814 | 6.395 | 4.5 |
| warning-text / surface | 5.022 | 6.209 | 4.5 |
| warning-text / raised | 5.022 | 5.959 | 4.5 |
| success-text / bg | 5.257 | 5.407 | 4.5 |
| success-text / surface | 5.484 | 5.249 | 4.5 |
| success-text / raised | 5.484 | 5.038 | 4.5 |
| link / bg | 4.506 | 6.830 | 4.5 |
| link / surface | 4.700 | 6.631 | 4.5 |
| link / raised | 4.700 | 6.364 | 4.5 |
| validation / bg | 4.630 | 7.365 | 4.5 |
| validation / surface | 4.829 | 7.151 | 4.5 |
| validation / raised | 4.829 | 6.863 | 4.5 |
| text / hover | 16.966 | 16.714 | 4.5 |
| text / selected | 16.583 | 16.061 | 4.5 |
| text / pressed | 16.539 | 16.539 | 4.5 |
| disabled-text / disabled-bg | 5.414 | 6.134 | 4.5 |
| on-accent / accent-fill | 4.700 | 4.700 | 4.5 |
| focus / bg | 4.506 | 6.830 | 3 |
| focus / surface | 4.700 | 6.631 | 3 |
| focus / raised | 4.700 | 6.364 | 3 |
| focus / hover | 4.172 | 6.155 | 3 |
| focus / selected | 4.078 | 5.914 | 3 |
| focus / pressed | 4.067 | 6.090 | 3 |
| border-control / bg | 3.494 | 3.500 | 3 |
| border-control / surface | 3.645 | 3.398 | 3 |
| border-control / hover | 3.236 | 3.154 | 3 |
| border-control / pressed | 3.154 | 3.121 | 3 |
| border-control / disabled-bg | 3.236 | 3.154 | 3 |
| input-boundary / bg | 3.494 | 3.500 | 3 |
| input-boundary / surface | 3.645 | 3.398 | 3 |
| input-boundary / hover | 3.236 | 3.154 | 3 |
| input-boundary / pressed | 3.154 | 3.121 | 3 |
| input-boundary / disabled-bg | 3.236 | 3.154 | 3 |

## Sources and verification boundary

The local UI UX Pro Max design-system and UX searches informed implementation checks; the released contract overrides conflicting suggestions about stagger, opacity, framework choice and external resources. Native sorting follows [W3C's table example](https://www.w3.org/WAI/ARIA/apg/patterns/table/examples/sortable-table/); narrow-screen behavior follows [W3C reflow guidance](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html). Browser inspection remains separate and required.

Fonts downloaded from the Google Fonts source repository on 2026-09-09:

| Asset | Source | SHA-256 |
| --- | --- | --- |
| inter.ttf | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/inter) | `29160a80ff49ddcab2c97711247e08b1fab27a484a329ce8b813d820dc559031` |
| playfairdisplay.ttf | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/playfairdisplay) | `c40f2293766a503bc70cce9e512ef844a4ccb7cbcde792fe2ea31d191917d8d6` |
| notonaskharabic.ttf | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/notonaskharabic) | `67b5a525a661b607971fbd3f96a81b89d3a768e74534fca84f18ac97e6fab72f` |
| notosansarabic.ttf | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/notosansarabic) | `63111b5b2e074dd48cc67692e0a2726d86ee94c1c37fe8598257b7b4e87e869e` |
