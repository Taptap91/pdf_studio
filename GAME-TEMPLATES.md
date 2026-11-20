# Game templates (PDF Studio + RulesForge app)

A game template = how Tag All finds a game's datacards in a PDF + how the app draws them. Kill Team is the worked example.
PDF Studio stays local; the app-side files go to GitHub as usual.

## What is already in place
| Game | PDF Studio style (step 3 "Game style") | App |
|---|---|---|
| Warhammer 40,000 | `40k`: ordinary tags only (headings, paragraphs, images) | Datasheets read by the app's own 40K parser (`js/datasheets.js`), shown in the 40K pack style |
| Age of Sigmar | not added yet (rule book comes later) | Card layouts are locked in `js/card-templates.js`: `aos-ability` (see below) and `aos` (warscroll). Not yet connected to tagging |
| Kill Team | `killteam`: Tag All finds each datacard and tags its parts as text with a role | Drawn as a Kill Team card by `js/card-templates.js` (`killTeamCard`) |

The chosen style is saved with the file (`profile` in the sidecar) and remembered in the browser.

## Kill Team card roles (what Tag All puts on each card)
`title`, `type`, `flavour`, `stat-label`, `stat-value`, `head-name/atk/hit/dmg/wr`, `weapon` (one per row), `weapon-wr`, `ability`, `ability-name`, `keywords`, `base`.
A role tag is a normal box tag with `|role=<name>` on its note. The converter (`js/tagged-rules.js`, `buildCards`) reads the text inside each box into one card:
`{ tpl, name, type, flavour, stats:[[label,value]], weapons:[{n,a,h,d,w}], abilities:[{n,t}], keywords, hot, base }`.

## Adding a new game (about an hour, not days)
1. **PDF Studio, `pdf_studio.py`:** add the id to `PROFILE_IDS` (Python) and an entry to `PROFILES` (page script): `label`, `hint`, and `cards: true` if the game has datacards to find. Write a finder like `datacardCards(P)` that returns each card's parts as `{k, role, box}`; Tag All calls it for any profile with `cards: true`.
2. **Converter, `js/tagged-rules.js` `buildCards`:** fill the same card fields from your roles and set `tpl` to the game's id.
3. **App, `js/card-templates.js`:** write `<game>Card(c)` returning the HTML, add it to `CARD_TEMPLATES`, add its CSS to `styles.css`.
4. Test with one sample PDF the volunteer supplies for that game; do not reuse another game's PDFs.

Games with no datacards (a 40K-style layout) need only step 1 with `cards: false`.

## Age of Sigmar: abilities (layout locked, tagging still to do)
Template id `aos-ability`. Card data: `{ tpl, phase, cp, name, flavour, lines:[[label, text]], keywords }`.
- Border, phase bar and keywords label take the phase colour (dusty): Hero gold `#9a8444`, Movement grey `#6f7173`, Shooting blue `#4f7394`, Charge orange `#a86d3b`, Combat/Fight red `#8c3b3b`, End of phase purple `#705c8a`. Any other phase: bronze `#a8843a`. The phase is matched from the words in the phase bar (`PHASE_COLOURS` in `js/card-templates.js`).
- Command point cost: a rounded badge at the right of the phase bar ("1 CP"); none when there is no cost.
- A rule spanning 80% of the card width, centred, between the name line and Declare, and between Declare and Effect.
- Keywords at the bottom: a tinted label cell on the left, the list on the right.
## Age of Sigmar: warscroll (layout locked, tagging still to do)
Template id `aos`. Card data: `{ tpl, kicker, name, flavour, stats:[[label,value]], weapons:[{group,n,note,a,h,w,r,d}], profile:[[label,value]], abilities:[ability cards], keywords:[lines] }`.
Order top to bottom: faction kicker and name (dark bar), italic flavour, horizontal stat row (Move, Health, Save, Control, replacing the compass picture), weapons table (ATK HIT WND RND DMG, alternating rows, weapon abilities such as "Anti-charge (+1 Rend)" clickable), abilities (phase-coloured, see above), keywords block (label cell left, one line per row), battle profile last (unit size, points, base size, can be reinforced).
The warscroll's colour is faction specific. The converter reads it from the PDF: `fillRects` (`js/pdf-reader.js`) collects the filled panels of each page for tagged files, and `factionColour(page, box)` (`js/tagged-rules.js`) picks the saturated colour that covers the most area inside the card's box (greys, black, white ignored). The result goes in the card as `colour` (`#rrggbb`) and `accentOf` in `js/card-templates.js` lifts it to a border colour; with no colour the default green `#5f8050` is used. Labels are gold `#d9b866` on near-black bars. Ability phase colours are not affected. Checked only on rebuilt pages (green and blue versions); a real PDF may paint its panels differently.
To finish AoS: add the `aos` style to `PROFILES` with a finder for abilities and warscrolls, and fill these fields in `buildCards`. Open question: whether the compass stat numbers in the PDF are text or part of the picture.
