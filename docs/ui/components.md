# Components: the kit

The kit is `web/src/ui/`, exported from `web/src/ui/index.ts`.

## The hard rule

Nothing outside `web/src/ui` draws its own button, head, menu, dialog, sheet, drawer,
toast, fold or field. `web/src/components/` and `web/src/views/` compose kit parts; they
never use a raw `<button>`, `<select>`, `<textarea>`, `<details>`, `<dialog>` or
`<input>` (except a hidden file input inside the kit's FileButton).

Also:
1. A kit class is styled only in `ui/ui.css` and the part's own stylesheet. A view's
   stylesheet styles only classes with its own prefix.
2. A missing part is added to the kit, once, and this file
   gains its section in the same commit.
3. A kit API change updates every call site in the same commit. No shims, no aliases.
4. The TypeScript types in `web/src/ui` are the truth for exact prop types; this file says
   what each prop is for. When a prop changes, this file changes in the same commit.
5. Words on parts come from [words.md](words.md).

---

## Typography

One scale, ratio 1.125, in `web/src/styles/tokens.css`. Each step is one clamp, fluid from a
380 px phone to a 1920 px desktop and flat beyond, times the Appearance text size. No CSS
outside `tokens.css` writes a font size, line height, weight or letter spacing as a number.

| Step | Phone to desktop | For | Line height | Tracking |
|---|---|---|---|---|
| `--t-xs` | 12.5 to 14 px | meta: times, counts, hints, chips, small labels, mono tool bodies | `--lh-ui` | `--tracking-meta` on muted labels (list sections, menu headings, groups) |
| `--t-md` | 14 to 16 px | every piece of UI text: rows, buttons, fields, menus, names | `--lh-ui` one line, `--lh-body` wrapping | none |
| `--t-lg` | 16 to 18 px | reading text: bubbles, prose, the composer, documents; card and empty-state heads | `--lh-body`, the story editor `--lh-story` | `--tracking-title` when it is a title |
| `--t-xl` | 20 to 22.5 px | place, section and dialog titles, prose h2 | `--lh-title` | `--tracking-title` |
| `--t-2xl` | 28 to 32 px | display: the greeting name, prose h1 | `--lh-title` | `--tracking-display` |

- On a touch screen `--t-md` is `--t-lg`: the phone UI has one text size, 16 px at 100 %, and
  a field's label matches it. A field never goes below 16 px there, at any text size, so iOS
  never zooms into it (base.css).
- Inline code in a sentence is `--t-code` (0.9em); a code block is `--t-md`.
- Weights: `--fw-regular` for text, `--fw-medium` for names, labels, buttons, rows and
  titles, `--fw-bold` for `strong` and a prose h1. Nothing else.
- Reading widths are in em of `--t-lg`: `--measure` (the chat column, 32em of text plus the
  bubble's padding) and `--measure-prose` (notes, documents, the story editor, 32em), about
  66 characters a line at every text size. Forms and card grids (`--measure-form`,
  `--measure-wide`) stay in rem.
- Text size (Appearance) moves every step by whole steps of the scale: Small one down,
  Large one up, Larger two up. Control heights, icons and faces stay.

---

## Heads

### PlaceHead
**When:** the head of every place, list and page. Nothing else draws a head.
**Props:** `title`, `count` (the server's number), `detail` (a muted state beside the
title: a terminal's program, "Deleted"), `level` (1 for a page's h1), `titleAttr`,
`back` (`label`, `onClick`: the way back to the list where list and page share one pane,
shown on a phone only), `crumbs` (the pages above a detail page, each `label` with `href`
or `onClick`: "Documents > Harbour" before the title; on a phone only a back arrow to the
last one), `lead` (before the title at every width: a link back from a page,
a face), `search` (`value`, `onChange`, `placeholder` default "Search", `label`,
`loading`, `inputRef`, `onKeyDown`), `picker` (Select),
`shelf` (`archive`, `trash`: which are offered; `value`: the one shown; `onChange`),
`more` (MenuItem list for rare actions such as "Archive older than"; one item that does not
destroy shows as its own icon button, no menu), `actions` (a page's
own controls, before More), `newLabel` with `onNew` or `newHref` (the one primary "New
X"), `newPressed` (its page is open), `newDisabled` (true, or the reason as its tip),
`tools` (a row under the head; new code uses the Toolbar and PageIndex instead).
**Order:** title and count, Search, Select, Archive, Trash, `actions`, More, then
New at the far right. On a shelf the title is "Archive" or "Trash" with the way back.
**Do:** keep it one row at every width. The part measures itself and steps down until it
fits: the words go (icon buttons with tips), then Select, Archive and Trash move into
More, then New becomes a round "+", then the crumbs become the back arrow and the name ends
in an ellipsis (its state beside it stays whole; the name keeps at least 8rem), last
`actions` take their own line under the row, at its right. In a head narrower than 640 px
Search takes its own full-width row under it. On a shelf New keeps its place unseen, so
Select, Archive and Trash never move under the pointer. Its sides sit on the page's edge
(`--edge`: the gutter, or a list page's column).
**Don't:** wrap to a second row; put a second primary in it; hand-build a head; put
filters, Sort or Group in it (they are the Toolbar's); float a New button over the list.
**Phone:** the same steps: icon buttons, a round "+", Search on its own row.
Inside a Place, below 1024 px, it starts with the place's panel button, which opens the place
sidebar as a Drawer from the left (left out on a page with `back` or `crumbs`).

### Toolbar
**When:** under a PlaceHead, for the state of a list, its filters, Sort and Group.
**Props:** `state` (`label`, `value`, `onChange`, `options` with `count`: All 48, Playing
31, Ended 17; a count of 0 is not shown), `filters` (each a `ToolFilter`: `label` "Folder", `icon`, `picked` "Noor
Haddad" or "2 folders", `items` with `current`, `onClear`), `sort` and `group` (each a
`ToolChoice` of `label`, `value`, `options`, `onChange`, shown as "Sort: Recent"), `toggles`
(each a `ToolToggle`: `label` "Favourites", `icon`, `on`, `onChange`: a filter that is on or
off, pressed while on).
**Do:** the state on the left, the filters as buttons that show what is picked (an x
clears it), Sort and Group at the right end; one row that never wraps: with no room Sort and
Group, then the filters, become icons, then the state becomes one button with its menu
("All 7"); what still has no room scrolls sideways and fades at the edge with more. A filter's menu opens from its left
edge, Sort's and Group's from their right. Its buttons are `sm`.
**Don't:** hold actions (they go in the head's More); show chips for filters nobody
picked.
While selecting, the SelectBar takes its place.

### GroupHead
**When:** above each group of a grouped list (by folder, person, state, date).
**Props:** `icon`, `title`, `detail` (what the group is, muted: "Noir, slow burn"),
`count` with its noun ("22 documents"), `href` or `onClick` (opens the group's own page),
`menu` (the group's own "..." and right-click: a folder's Rename, Move to Trash), `onDrop`
(a dragged row lands in the group; the head takes the accent wash while one is over it).
**Don't:** open or close (no fold); carry more than its "...".
`ListSection` stays the small label in a place sidebar ("Recent", "Today").

### Section
**When:** a block of a page: a settings group, a section of a page with an index.
**Props:** `id` (the PageIndex target), `title`, `count`, `search` (optional, right
aligned), `action` (one Button, such as "New entry"), `more` (its rare actions behind "..."
at the head's end: Move to Trash), children.
**Do:** sit on its own surface with `--pad-section`. A group of rows inside it gets a
`SubHead` (`children` the heading, `count`, `action`): a small heading, never a Section.
**Don't:** carry a description paragraph; collapse; nest a Section in a Section.

### PageIndex
**When:** a long page with sections (a mission, a schedule, a settings page).
**Props:** `sections` (`id`, `label`, `count`), `children` (the Sections, each with its
`id`), `label`; the current one follows the scroll.
**Do:** stay sticky beside the page; jump on click (the mark stays on the target until the
scroll ends); the current one is the last section whose top has passed the top fifth, the
last section at the very end of a scroll (a page that fits starts on the first); on a phone
it is a top bar that scrolls sideways.
**Don't:** replace sections (they stay open); become tabs.

## Actions

### Button
**When:** a labelled action.
**Props:** `variant`: `primary` (the one main action of a page or dialog: New, Start,
Save), `secondary` (everything else, the default), `ghost` (quiet in-row actions), `danger`
(tonal red, only in a dialog that destroys); `size`: `md` (default, everywhere) or `sm`
(in-row actions only); `icon`, `iconEnd`, `loading` (a spinner replaces the icon),
`pressed` (Archive or Trash open), `href` (a link that looks like a button), `wide`.
**Do:** one primary per page or dialog; the same job gets the same variant in every place;
a disabled button keeps its reason as its tip (`aria-disabled`).
**Don't:** use `sm` in heads, sections or cards; colour a button for state; use `link`
styling for an action.

### IconButton
**When:** an action in a head, a row or a toolbar where an icon says it.
**Props:** `icon`, `label` (required: aria-label and tip), `size`, `iconSize`, `variant`,
`pressed`, `loading`, `href`.
**Don't:** ship without `label`; use an icon nobody would read without its tip for a
primary action.

### SendButton
**When:** only in the Composer and the reply boxes that send a message.
**Props:** `ready`, `running`, `onSend`, `onStop`, `label`. It becomes Stop while running.

### FileButton
**When:** pick files (Upload, Attach).
**Props:** `children` (the words; left out, an icon button named by `label`), `icon`,
`variant`, `size`, `disabled`, `title`, `accept`, `multiple`, `onFiles(files)`. It wraps the
one hidden file input.

## Lists

### ListRow
**When:** every row of every list.
**Props:** `leading` (face, icon or glyph), `dot`, `icon` (a glyph on a tile: a folder;
`iconAccent` for the top one), `title`, `subtitle` (one line), `detail` (a third, smaller
line), `meta` (time on the right), `column` (beside the text on a wide screen: a terminal's
conversation, a folder's faces), `cell` (a short cell before the time: "Ended",
"20 documents"; on a phone a line under the text), `depth` (a row's steps under the row it
sits in, drawn as a line to it), `trailing` (actions, shown on hover, focus or tap),
`trailingAlways`, `selected`, `density` (`lg` only for two lines), `href`, `onClick`,
`onContextMenu`, `press` (a long press opens the menu: useContextMenu's `press`, for a
row with no "..." on touch), `tooltip`, `pick` (the check box while selecting), `muted` (ended or
settled: the title quiet), `body` (text that wraps under the title, at most three lines: a
lore entry, a found passage as a Snippet), `unread` (the title strong, an accent dot),
`checked` with `onCheck` (a check that is always there, outside Select: a channel to
include; the row toggles it), `progress` (0 to 1, null while unknown: a file on its way).
With `column` or `cell` it is a wide row: the column, the cell and the time each keep their
column, so rows line up; a list whose rows have a cell only sometimes passes `cell={null}`
on the others. The row's own width decides (a container query): from 60rem every column,
from 36rem the column goes, below that the cell is a line under the text and the time
sits on the title line, as in every row. "..." lies over the time on hover, and the time
hides meanwhile. A row is `--s-1` from its edges top and bottom (`lg`: `--s-2`), so text
that wraps never touches the next row. A terminal row and a folder row are both
ListRows.
**Do:** "..." on hover; right-click opens the same menu; an ended item is muted with its summary as its second line.
**Don't:** show an always-visible row of buttons; carry pills.

### VirtualList
**When:** any list that can pass about 50 items.
**Props:** `items`, `estimate`, `getKey`, `renderRow`, `overscan`, `onEndReached`, `before`,
`after`, `inset`, `resetOn`, `page` (the list is part of a page that scrolls, such as a
Section of a long page: no scroll box of its own, it follows the page's). Every row is
measured once drawn, so `estimate` is only the first guess and rows never overlap or gap.

### ListPage
**When:** a place's list page. Kit classes,
no component: `list-page` (the page, a column that fills the pane), `list-page__top` (the
Toolbar or SelectBar), `list-page__note` (an InlineError under it), `list-page__rows` (the
box a VirtualList fills), `list-page__empty` (an EmptyState in the middle).
**Do:** the PlaceHead inside it; head, toolbar and row text share one edge (`--edge`): the
`--gutter`, and on a wide screen the column of `--measure-wide`, centred. A row's hover
reaches `--s-3` past the text.
**Don't:** give a place its own copy of these paddings.

### SelectBar and usePicker
**When:** selecting many in a list.
**Props:** `picker` (from `usePicker`), `noun`, `actions` (each with `ask` for the confirm:
`title`, `confirm`, `body`, and `option`, the label of one Checkbox, off by default, such as
"Also delete its sub-agents"; `run(keys, option)`; `done`).
In a narrow column (under 40rem) the actions are icons with their names as tips.
**Do:** take the Toolbar's place, on `--surface-2`: "3 selected", Select all (pressed
while all are picked), then at the right the actions, then Done.
**Don't:** float over the list; show actions the selection cannot take.

### Card
**When:** a grid of things with a picture or a face (people, folders, documents,
presets), or a pick of one of a few (a preset, a picture).
**Props:** `media` (a face or a glyph before the title), `cover` (a picture across the
card's whole top, to its edges: a cover), `title`, `subtitle`, `meta`, `selected`
(a pick: the accent ring), `onClick`, `href`, `menu` (the "..." items, also right-click and
long press), `menuLabel`, `children` (a few lines under the head), `actions` (buttons at its
foot, each its own press beside the card's: Open, New folder), `pick` (the grid's
Select: a check on the card while selecting), `tooltip`.
**Don't:** hand-build a card; put more than one primary in it.

### Tree
**When:** only a real hierarchy of folders (Notebook, Documents).
**Props:** `nodes` (`id`, `label`, `icon`, `count`, `children`), `open` (the open folders),
`onOpen(id, open)`, `selected`, `onSelect`, `onMove(id, into)` (a row dragged onto a folder),
`menu(node)` (its right-click), `label`. Right and Left open and close, Up and Down move.
Folders open and close; this is the one exception to "no folds".

## Overlays

### Menu and ContextMenu
**When:** "..." menus (Menu, opened by a MenuButton) and right-click or long-press menus
(ContextMenu, through `useContextMenu().open(event, items)`).
**Props:** `items`: `label`, `icon`, `onSelect`, `current` (one of a set: a check, the
others a circle), `detail` (a muted second line), `danger`, `disabled` with its reason as
`title`, `face` (a small face in place of the icon: `<Portrait size="sm">`, `<Avatar
size={28}>`, a folder's `<Face size="sm">`), or `'sep'`, or `{ heading }` (a muted label over
the items after it: Sort, Group); `anchor` (an element, or a point for right-click);
`align`; `inline` (drawn in place, open); `search` (the placeholder of
the search field, "Search people"; "Search" when left out).
**Size:** a right-click menu is the same menu as "...": one width, one row height.
**Long:** a menu of more than twelve items has a search field at its top, focused when it
opens. Typing narrows the items by their words; Down moves into the list, Up from the first
item goes back to the field, a letter typed on an item goes to the field, Enter picks the
first item, Escape clears the field and then closes. Every menu stops at 60 % of the window
(never past the screen) and scrolls inside, the field staying on top. Above its button a
menu hangs from its bottom edge, so a narrowing search keeps it on the button.
**Phone:** a menu of more than five items is a Sheet from the bottom edge with the same
rows in the same order, and the same search at its top.
**Do:** the same items both ways for the same thing; danger last, after a separator; grow
from the origin and leave; an item that cannot run is left out or
carries its reason; a face on every item that is a person, a chat or a folder.
The Toolbar's filter menus are Menus, so they get the faces, the search and the cap too.
**Don't:** build a popover by hand; show an empty menu.

### Dialog
**When:** a task that needs a few fields or a decision, over the page.
**Props:** `title`, `onClose`, `actions` (Cancel first, then the primary or danger),
`size` (`sm` 440, `md` 560, `lg` 720), `dismissable`, `closeButton`, `inline` (drawn in
place; ConfirmDialog and Sheet take it too).
**Do:** Escape and the scrim close a dismissable dialog; focus is trapped and returned;
the server's error shows in the dialog above its actions.
**Don't:** stack dialogs; put explainer paragraphs in it.

### ConfirmDialog
**When:** only for what destroys, or for many items at once.
**Props:** `title` (a question with the number: "Move 3 conversations to Trash?"), `body` (one
short line), `confirmLabel`, `cancelLabel`, `danger`, `option` (the label of one Checkbox,
such as "Also delete its sub-agents", off by default), `onConfirm(option)`
(gets the Checkbox's state; returns error text or nothing).
**Do:** Cancel, then the action; the action is danger red when it destroys.
**Don't:** confirm a reversible single action (Archive one item); offer Undo instead.

### Sheet
**When:** on a phone, what a Dialog or a Menu is on a desktop (Go to, More, filters).
**Props:** Dialog props.
**Do:** rise from the bottom edge, `--r-xl` top corners, a 36 x 5 px grabber; dragging the
grabber down, the back gesture (the sheet is one step of history), Escape, the scrim and
the X close it. On a desktop it renders as a Dialog near the top. A list's Sheet shows
one thing at a time: a tool call's row opens the call, a run of calls alike ("Ran 8
commands in terminal main") opens its rows, and a row there opens its call in the same
Sheet with Back to the run.
**Don't:** open a Sheet from a Sheet; unfold a run in place.

### Drawer
**When:** a side panel beside the content (Celine's conversation panel, a Routine run).
**Props:** `open`, `title`, `onClose`, `actions` (head actions before the X), `label`,
`side` (`right`, or `left` only for the place sidebar), `fill` (its body is one box a
VirtualList fills, with no padding), `enter` (made on its first open, it still slides in), children. It is `--drawer-w` wide and stays mounted;
closed, it is inert. On a phone it lies over a scrim that closes it, above the bottom bar.
**Do:** slide by transform; beside the content from 1200 px, below that over the content
from the right edge, full height, over a scrim that closes it (a phone: from the bottom);
the reading column changes width once, not on every frame. A page that has room gives
Activity and the thoughts a host beside its conversation (`SideHost`), so they open as a
column like the side panel.

### Toast and ToastHost
**When:** a short result of something the user did that has no other place to show
("Created Mira 2", "Copied"), or a running import with its progress.
**Copy:** `copy(text)` puts text on the clipboard (the Clipboard API, or the old way on
plain http) and says whether it worked; a button that says Copied itself takes
`useCopied()` (`[copied, copyIt]`: Copied for a moment after it copied).
`copied(text)` copies and toasts "Copied" (a menu's Copy). Nothing calls the Clipboard
API directly.
**Props:** `toast({ text, action, progress, lead, title })` returns `{ update, close }`
(`lead` and `title`: a notification arriving, with who it is from and its first line); the host
(`ToastHost`) lives in `App.tsx`, once.
**Do:** min 48 px, `--r-lg`, `--surface-4`, `--e-overlay`; rise in, drop out; at most three;
an X on each; gone after 5 s, after 10 s with an action, and with progress only when
closed.
**Don't:** offer Undo for a delete or a move to Trash (those ask first); report errors
(errors sit where they happened).

### Tip
**When:** every tooltip. The one `TipLayer` reads `title` and `data-tip`.
**Do:** one tip at a time, after `--tip-delay`, the next at once within `--tip-skip`; at
most eight words.

### Viewer
**When:** every picture opened big.
**Props:** `src`, `name` (never a file name as a title), `caption`, `more` (menu items),
`actions` (the caller's buttons in its bar: Regenerate, Variants, Use as portrait), `panel`
(beside the picture, under it on a phone: its prompt and settings), `onClose`, `onPrev`,
`onNext`, `index`, `total`.
**Do:** grow from its thumbnail; zoom, pan, Left and Right; right-click to copy or save.

### Face
**When:** its face and every person's (`Avatar` for a chat contact).
**Props:** `name`, `src`, `color`, `size` (`sm`, `md`, `lg`), `speaking`, `away`, `info` (its
card: the big face, the name, one line, the caller's actions).
**Do:** one initial (the UI font, medium) on an OKLCH tint when there is no picture or it
fails.
**Don't:** show two letters.

## Fields

One height for every field, a hairline edge, a halo on focus.

| Part | When | Props |
|---|---|---|
| `Field` | a label above one control, with its error | `label`, `error`, `group`. No hint line |
| `SearchField` | every search box | `value`, `onChange`, `placeholder` ("Search" or "Search conversations"), `loading`, `autoFocus` |
| `Input` | one line of text | `size`, `mono`, `invalid` |
| `TextArea` | every text box: grows with its text, then scrolls | `rows`, `max`, `bare`, `fill`, `mono`, `invalid` |
| `NumberField` | a number with its unit | `label`, `value`, `onChange`, `min`, `max`, `step`, `unit` |
| `Switch` | on or off, the whole row toggles | `checked`, `onChange`, `label` |
| `Checkbox` | one of many picked, or a confirm option | `checked`, `indeterminate`, `onChange`, `label` (the whole line toggles), `ariaLabel` |
| `Choice` | one of several: a Segmented row up to six short options, else a radio list; options with `swatch` (a colour), `face` or `picture` are tiles: round swatches, faces, thumbnails (a pick of one of four variants), the chosen one ringed and checked | `value`, `onChange`, `options`, `label`, `ratio` (pictures) |
| `Picks` | pictures chosen in order, up to `max` (the images of an edit): Choice's picture tiles, each chosen one numbered by its place, a tap takes it out | `values`, `onChange`, `options`, `label`, `max`, `ratio` |
| `Segmented` | the state of a list in the Toolbar, a mode on a page; its options are `--h-sm` high inside a 2 px edge; one row while it fits, a second row when it has less room (in the Toolbar it stays one row that scrolls); a long or growing set of picks is a MenuButton instead (a terminal's Send to) | `value`, `onChange`, `options` (with counts) |
| `Slider` | a value on a range (Bias): label (always whole), track, value; stacked sliders line their tracks up at the right | `value`, `onChange`, `min`, `max`, `step`, `label`, `format` |
| `PinInput` | a PIN, one box per digit: typing moves on, Backspace goes back, a paste fills from the first box, digits only | `value`, `onChange`, `length` (8), `label`, `invalid`, `autoFocus`, `disabled` |
| `InlineEdit` | rename in place | `value`, `label`, `onSave`, `onCancel` |

Don't: use a dropdown `<select>` (a dropdown only for a list that can run to hundreds of entries, such as a provider's models); put a hint line under a field; build a field by hand.

### Form

A whole form drawn from the server's field schema (server/celine/settings/form.py): one column, each field its label above its control, a switch its label beside it. A field's `help` is its tooltip.

| Field type | Drawn as | Changes |
|---|---|---|
| `bool` | `Switch` | at once |
| `choice` | `Choice` | at once |
| `number`, `duration` | `NumberField` with its unit (`plain`: a `Field` with an `Input`, as in the onboarding's cards) | on leaving the field or Enter |
| `text` | `Input` (`mono`, `placeholder`); `long`: `TextArea` | on leaving the field or Enter |
| `list` | `TextArea`, one entry per line | on leaving the field |
| `secret` | a password `Input`; once stored "Saved" with Remove, never the secret | `onChange(key, text)`, Remove `onChange(key, null)` |
| `copy` | a read-only mono `Input` with Copy | never |

Props: `fields`, `values`, `secrets` (each secret's `{state, set_at}`), `onChange(key, value)`, `disabled`, `plain`. A field with `where: 'config'` is read-only. A form served by `/api/forms/{id}` goes through `useForm(id)` (web/src/lib/forms.ts), which keeps the version, the pending changes and the secrets apart.

Don't: draw a form's fields by hand; show a stored secret or a part of one.

## Feedback and state

| Part | When | Rule |
|---|---|---|
| `EmptyState` | nothing to show | an icon, one line, at most one short second line and one action |
| `LoadingRow` | content on its way | skeleton rows in the shape of what comes; words only for screen readers |
| `InlineError` | something failed | one line next to what failed, with the action that fixes it (Retry, Open Settings) |
| `Loaded` | a loaded resource (a `useResource` result) | `res`, `page` (a list page: centred EmptyStates), `icon`, `label`, `inline`, `compact`, `empty` (the EmptyState the caller says is due), children `(data) => ...`: Not found, the skeleton, the error with Retry while nothing came, `empty`, then the ready render; a failed reload keeps the data with its error above it. No children: only the states before the data |
| `Notice` | a banner across the top (a lost login, the server gone) | one calm line, its fix at the end |
| `StatusLine` | a fixed-height status where a reply lands | reserved height |
| `Dot`, `Dots`, `Progress` | state beside a name; its typing; a bar | a Dot is never the only carrier of meaning; `Dot text` puts the server's word beside it (a provider's health: "Connected") |
| `Chip` | one picked filter or a search token | always with an x; never a state, never a row of them |
| `EventLine` | one quiet line in a transcript (a delivery, a reminder, a tool call) | with `detail` a click opens it in a Sheet (`detailTitle`); it never unfolds; `running`: the words shimmer (Thinking, a call going on) |
| `Step` | the number of a step in a list of steps | `n`, `done` (a check), `current` (the accent) |
| `Divider` | a day in a thread | a hairline across with the words in its middle; never a chip |
| `Quote` | someone's own words shown in the app (its vault request, the message a reply answers) | a rule on the left, `who` under it; `onClick` makes it the way to the original |
| `Snippet` | a found passage | `text`, `words` (marked), `lines` (then an ellipsis) |
| `Thoughts` | its reasoning | one quiet surface, its lines kept |
| `Fold` | a block of its own that opens and closes (its thinking above a reply) | `title`, `icon`, `defaultOpen` (how it starts; the reader's click wins), `remember` (a key: this device keeps the click, lib/prefs); the head is a button with the DisclosureArrow, the body the content |
| `Facts` | what a thing is: labels and values | `items`; rows (label beside value, compact, in a card) or `grid` (label over value) |

## Places and conversations

### Place
**When:** a place with sections (Documents, the Gallery, Settings, Notebook, Missions): its
sidebar beside the page.
**Props:** `title` (the place's name), `action` (New as an icon), `head` (a sidebar that is
the place's list, such as Celine's conversations: its own PlaceHead with Search, Select,
Archive and Trash in place of the title and action), `side` (the sections with their counts
as `sm` ListRows, a ListSection "Recent" and its rows, or the list), `foot` (the user's own
row), `label`, `open` with `onOpen` (the phone's drawer, when the page decides), children
(the page). `usePlace()` gives a PlaceHead inside it the panel button.
**Do:** `--sidebar-w` on `--surface-1`; below 1024 px (a tablet, a phone) a Drawer from the
left, so the page keeps its width, opened by the panel button in the head, closed by a link
in it, the scrim, Escape and its X.
**Don't:** make it a list pane (a place's list is its page); hand-build a sidebar.

### Message
**When:** every message of a conversation: Celine's chat, an Intercom thread.
**Props:** `mine`, `face`, `name`, `head` (beside the name: to whom), `before` (pictures and files above the text),
children (the text), `time`, `versions` (`at`, `total`, `onPrev`, `onNext`), `actions` (the
caller's buttons), `menu` (its "..." and right-click), `source` (where it came from, always
shown), `shown` (the action row without a hover).
**Do:** its own on `--bubble-theirs` with its face beside it, yours as a `--bubble-mine` bubble
at the right. The action row keeps its height and shows on hover,
focus or a tap; right-click and a long press open its menu.

### ComposerBox
**When:** the box at the foot of every conversation (the chat, a thread's reply).
**Props:** `value`, `onChange`, `onSend`, `onStop`, `running`, `ready`, `placeholder`,
`label`, `start` (the plus, Attach), `tools` (before Send), `files` (on their way in, above
the text), `disabled` (why it cannot send: Send's tip), `inputRef`, `onKeyDown`, `onPaste`.
**Do:** the one raised object: `--r-xl`, `--e-float`, the halo on focus; Enter sends,
Shift+Enter is a new line; Send becomes Stop while it answers.

### Picture
**When:** every picture shown small: on a message, a Gallery tile.
**Props:** `src`, `full` (the file the Viewer shows when `src` is a smaller copy), `name` (never a file name), `ratio`, `more` (its "...", right-click and
the Viewer's menu), `gallery` with `at` (the Viewer flips through them), `viewer`
(`actions`, `panel`, `caption` for the Viewer), `onOpen` (a page of its own opens it in
place of the Viewer here), `current` (the open one: the accent ring), `pick` (a check while
selecting),
`favourite` with `onFavourite` (the heart), `making` (on its way: a breathing surface with
"Creating image").
**Do:** a click opens the Viewer, growing from it.

### Player
**When:** a track that plays while you look on (the Gallery's music, Studio).
**Props:** `src`, `title`, `detail`, `onClose`. Play and Pause, its name, how far, close.

### Screen
**When:** what its terminal or its browser shows, and its browser's shots.
**Props:** `title` (a program, an address), `mono` (a terminal's text), `current` (the one
shown now among shots), children.

## Shell parts

| Part | When |
|---|---|
| `Rail` | the app's left column of places; the phone's scrollable bottom bar of icons |
| `Tabs` | only Celine's side panel (Model, Tools, Pages, Context). Nowhere else |
| `DisclosureArrow` | only in a Tree |
