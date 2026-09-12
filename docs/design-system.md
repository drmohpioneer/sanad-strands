# Sanad browser design system

The browser uses a layered, dark-first design with an equal light theme. The
system preference remains the default; an explicit choice wins before first paint
and persists locally. This document describes the delivered presentation, not
clinical validation or acceptance of its visual quality.

## Type and space

Local variable TTFs: Inter for controls and prose, Playfair Display for the identity
and page title, Noto Sans Arabic for Arabic controls, and Noto Naskh Arabic for
Arabic prose. All retain `font-display: optional` and their bundled SIL licenses.
No CDN, remote font, framework or runtime dependency is needed.

Type scale: 12 / 14 / 16 / 18 / 24 / 32 px; weights 400 / 500 / 600.
Body and clinical rows are at least 16 px; patient prose is 18 px. Metadata uses
14 px, increasing to 16 px on narrow screens. Numbers and times use tabular,
lining figures. Spacing: 4 / 8 / 12 / 16 / 24 / 32 / 48 px. Surfaces use 16 px
radii, cards 12–14 px, controls 8 px, and status chips a pill radius.

## Colour and elevation

The three planes are page `bg`, section `surface`, and card/dialog `raised`.
The light planes are distinct. Indigo marks doctor navigation/actions; teal marks
patient navigation and controls. Success, warning and danger colours carry status,
never decoration. Every status also has written text. Fulfillment, review,
acknowledgment and resolution remain separate facts.

The ambient radial gradient appears in page gutters. Content planes are solid;
the sticky header and navigation are the only glass surfaces. Their solid fallback
uses `glass-solid`; supported browsers use `rgba(glass-rgb, glass-alpha)` and a
16 px backdrop blur. The header sits above the opaque page background. No pure
black surface or neon/purple-pink gradient is used.

| Token | Light | Dark |
|---|---|---|
| `bg` | `#FAFAFB` | `#050506` |
| `surface` | `#FDFDFE` | `#0A0A0C` |
| `raised` | `#FFFFFF` | `#101014` |
| `text` | `#0F0F14` | `#F4F4F6` |
| `text-muted` | `#61616E` | `#9494A2` |
| `accent-text` | `#5662C4` | `#818CF8` |
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
| `link` | `#5662C4` | `#818CF8` |
| `input-boundary` | `#858591` | `#646472` |
| `validation` | `#DC2626` | `#F87171` |
| `accent-fill` | `#5E6AD2` | `#5E6AD2` |
| `on-accent` | `#FFFFFF` | `#FFFFFF` |
| `secondary` | `#0F766E` | `#5EEAD4` |
| `secondary-fill` | `#0F766E` | `#0F766E` |
| `on-secondary` | `#FFFFFF` | `#FFFFFF` |
| `glass-solid` | `#FDFDFE` | `#101014` |

Glass: light RGB 253,253,254; dark RGB 16,16,20; alpha 0.9 in both themes.
The patient text accent is `#0F766E` in light and `#5EEAD4` in dark. Filled
patient controls use white on `#0F766E` in both themes, never text on `#14B8A6`.

| Elevation | Light shadow | Dark shadow | Use |
|---|---|---|---|
| Low | `0 2px 6px #0f0f140a` | `0 2px 6px #01010366` | Sections, record cards, rail |
| Middle | `0 8px 24px #0f0f1414` | `0 8px 24px #01010399` | Header, summary tiles |
| High | `0 24px 64px #0f0f1429` | `0 24px 64px #010103cc` | Drawer, dialogs, notification |

## Identity and correction history

The doctor sees patient name, recorded age and contact-status label. Doctor
conversation and last-contact time have no accepted projection and are absent.
Identifiers, hashes, versions, source keys and raw enum names are confined to a
collapsed `details.support` disclosure at the bottom of the full patient record.
Technical `href`, form values and `data-*` attributes may retain identity for
existing commands. Patient pages have no support disclosure, including initial
server-rendered content. The label catalog has no underscore-humanization fallback.

The browser test walks all body text nodes, aria-labels and title attributes,
including hidden tabs and dialogs; it excludes script/style resource code and,
only on doctor pages, the support subtree. It rejects 32-hex sequences, schema
terms, mission/objective/predicate vocabulary and underscore labels. A negative
fixture proves hidden text and accessible names are inspected. Separate offline
checks verify server shells and correction formatting.

Corrections are a presentation-only timeline: structured scalar fields, fact
payloads, evidence value rows and instruction fields become “Field changed from
old to new by the doctor on date.” Values are escaped and zero is preserved.
No predicate result or protected notice text enters this formatter. Missing
structured changes produce “Correction recorded on date.” The raw notice remains
in support. Confirmation and server version/authority guards are unchanged;
a successful action produces a dismissible live notification linking to the record.
There is no undo or inferred compensating clinical action.

## Pages and interaction

- Patients and demo: four outstanding-work counters, instant search/filter,
  stable sort with visible direction and `aria-sort`, 50-row paging, Arrow Up/Down
  navigation and Enter to open the detail drawer. Modified link clicks retain
  normal navigation. The drawer has a scrim, Escape/close and focus return, with
  an explicit full-record link to the existing deep-linkable patient route.
- Full record: sticky identity header and Plan, Requests, Evidence, History tabs.
  Tabs support arrows, Home and End, roving focus and explicit panel associations.
  Correction/reopening actions retain their confirmations and existing commands.
- Inbox: Danger and Needs attention groups with expandable items and count badges.
  Patient questions are a section of `/a/inbox#questions`, using the existing
  listing and Send/Answer/Defer APIs. This adds no route. Sending a proposed reply
  retains its listing and reusable-answer versions. Answers and deferrals require
  inline confirmation, and failures leave the card available for correction.
- History: the same filterable, sortable review table; source identities stay in
  the patient support disclosure. Empty states give a next step.
- Preferences: language and daily digest time/packing forms; native validation,
  inline errors, version-bound saves and a saved notification.
- Administrator: the same theme, identity, account cards and application counts.
  Existing account-only actions, reason selectors and global sign-out remain.
- Patient: teal accents, larger text, conversation bubbles, a drag/drop upload
  card backed by the existing file picker/validation/progress path, reminders
  switch and the existing explicit resume confirmation, quiet-hours form.
The neutral login continuation, invitation and refusal shells remain script-free
under the existing session boundary. The interactive administrator shell uses
the shared stylesheet and theme script. No new route is introduced.

Images open in a native dialog only through an existing same-origin scoped media
stream. A missing/refused image shows “Original unavailable.” PDFs retain a new-tab
link with `noopener noreferrer`; no frame/CSP relaxation is needed. Evidence cards
link to their original when the accepted media projection provides the association.

## Motion and accessibility

Only transform/opacity animate, for 150–300 ms with ease-out entrance and ease-in
press. Cards lift 2 px and cards/buttons press to 0.97; table rows never scale.
The first eight rows have 30 ms stagger steps, capped at 240 ms; later rows appear
immediately. Tab replacement fades in; drawer entry slides with a fading scrim.
No page-transition overlay or loading shimmer is used. Reduced-motion disables
all animations/transitions and press/lift transforms.

Targets are at least 44 px, focus rings are visible with a 3 px offset, and action
notifications use `aria-live`. Layout uses logical properties and native direction;
only the drawer entry and directional navigation mirror, never a document scan.
The shell uses `100dvh`; the table becomes labelled stacked rows under 960 px.
Breakpoints cover 375 / 768 / 1024 / 1440; the rendered suite runs 375 and 1440
at DPR 2 in each theme. Hidden tab panels use native `hidden` semantics.

## Computed contrast

`tests/test_dashboard18_contrast.py` reads the delivered tokens. Glass composition
is computed from the RGB and alpha tokens over `bg`, rounded to opaque hex before
WCAG luminance/contrast calculation. These are token measurements, not a claim
that a browser or assistive-technology acceptance run passed.

56 pairs per theme, 112 measured combinations.

| Foreground / background | Light | Dark | Minimum |
|---|---:|---:|---:|
| text / bg | 18.323 | 18.547 | 4.5 |
| text / surface | 18.801 | 18.008 | 4.5 |
| text / raised | 19.113 | 17.282 | 4.5 |
| text-muted / bg | 5.847 | 6.807 | 4.5 |
| text-muted / surface | 5.999 | 6.610 | 4.5 |
| text-muted / raised | 6.099 | 6.343 | 4.5 |
| accent-text / bg | 5.110 | 6.830 | 4.5 |
| accent-text / surface | 5.243 | 6.631 | 4.5 |
| accent-text / raised | 5.330 | 6.364 | 4.5 |
| danger-text / bg | 4.630 | 7.365 | 4.5 |
| danger-text / surface | 4.751 | 7.151 | 4.5 |
| danger-text / raised | 4.829 | 6.863 | 4.5 |
| warning-text / bg | 4.814 | 6.395 | 4.5 |
| warning-text / surface | 4.940 | 6.209 | 4.5 |
| warning-text / raised | 5.022 | 5.959 | 4.5 |
| success-text / bg | 5.257 | 5.407 | 4.5 |
| success-text / surface | 5.395 | 5.249 | 4.5 |
| success-text / raised | 5.484 | 5.038 | 4.5 |
| link / bg | 5.110 | 6.830 | 4.5 |
| link / surface | 5.243 | 6.631 | 4.5 |
| link / raised | 5.330 | 6.364 | 4.5 |
| validation / bg | 4.630 | 7.365 | 4.5 |
| validation / surface | 4.751 | 7.151 | 4.5 |
| validation / raised | 4.829 | 6.863 | 4.5 |
| text / hover | 16.966 | 16.714 | 4.5 |
| text / selected | 16.583 | 16.061 | 4.5 |
| text / pressed | 16.539 | 16.539 | 4.5 |
| disabled-text / disabled-bg | 5.414 | 6.134 | 4.5 |
| on-accent / accent-fill | 4.700 | 4.700 | 4.5 |
| focus / bg | 4.506 | 6.830 | 3 |
| focus / surface | 4.623 | 6.631 | 3 |
| focus / raised | 4.700 | 6.364 | 3 |
| focus / hover | 4.172 | 6.155 | 3 |
| focus / selected | 4.078 | 5.914 | 3 |
| focus / pressed | 4.067 | 6.090 | 3 |
| border-control / bg | 3.494 | 3.500 | 3 |
| border-control / surface | 3.586 | 3.398 | 3 |
| border-control / hover | 3.236 | 3.154 | 3 |
| border-control / pressed | 3.154 | 3.121 | 3 |
| border-control / disabled-bg | 3.236 | 3.154 | 3 |
| input-boundary / bg | 3.494 | 3.500 | 3 |
| input-boundary / surface | 3.586 | 3.398 | 3 |
| input-boundary / hover | 3.236 | 3.154 | 3 |
| input-boundary / pressed | 3.154 | 3.121 | 3 |
| input-boundary / disabled-bg | 3.236 | 3.154 | 3 |
| secondary / bg | 5.247 | 13.772 | 4.5 |
| secondary / surface | 5.384 | 13.372 | 4.5 |
| secondary / raised | 5.473 | 12.832 | 4.5 |
| on-secondary / secondary-fill | 5.473 | 5.473 | 4.5 |
| text / glass-composite | 18.801 | 17.411 | 4.5 |
| text-muted / glass-composite | 5.999 | 6.390 | 4.5 |
| link / glass-composite | 5.243 | 6.412 | 4.5 |
| secondary / glass-composite | 5.384 | 12.928 | 4.5 |
| focus / glass-composite | 4.623 | 6.412 | 3 |
| text / glass-solid | 18.801 | 17.282 | 4.5 |
| text-muted / glass-solid | 5.999 | 6.343 | 4.5 |

Rendered browser execution and the architect’s before/after screenshot set remain
separate review gates. The owner’s walkthrough judges the visual result.

## Bundled font provenance

The unchanged local assets retain their adjacent OFL license files. SHA-256 of
the delivered bytes:

| Font | SHA-256 |
|---|---|
| Inter | `29160a80ff49ddcab2c97711247e08b1fab27a484a329ce8b813d820dc559031` |
| Playfair Display | `c40f2293766a503bc70cce9e512ef844a4ccb7cbcde792fe2ea31d191917d8d6` |
| Noto Naskh Arabic | `67b5a525a661b607971fbd3f96a81b89d3a768e74534fca84f18ac97e6fab72f` |
| Noto Sans Arabic | `63111b5b2e074dd48cc67692e0a2726d86ee94c1c37fe8598257b7b4e87e869e` |
