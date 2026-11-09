# Review: the design criteria and how a screen is checked

A UI change counts as done when its screens pass these criteria at 1440x720 and at
400 px wide, in the running app. The kit ([components.md](components.md)) says how a part
is drawn and [words.md](words.md) which words it uses; this file says what a screen must do
for the person using it. Each criterion is one check. Where an outside guideline and a
house rule disagree, the house rule is written here and wins.

## The format of a finding

One line per finding, in the review's list:

`place | the user faces | the user can do | criterion | fix | status`

- **place**: the screen and the part ("Settings, Models, Add provider").
- **the user faces**: what they see or cannot do, in their words, not the code's.
- **the user can do**: what they can do about it today ("nothing", "scroll past 11 roles").
- **criterion**: the number below it breaks.
- **fix**: at the root (a kit part, the server's state, a word in words.md), never a patch
  on one screen.
- **status**: open, fixed, or kept (with the reason).

## Criteria

### State and feedback
1. Every control changes visibly within 0.1 s of a press; a wait of 1 to 10 s shows the
   kit's spinner or shimmer at the place of the action, a shorter one shows nothing.
2. A wait that can pass 10 s says what it waits on (the model loading, the queue, the
   provider's own words) and offers Stop where stopping is possible.
3. A thing that lives on (a chat, a run, a mission, a request) shows its state in its row
   without opening it, as a StateWord. No dots, no lights, no uptime or "down since".
4. One state has one word in every place: a running reply reads the same in the chat
   list, the chat and Events.
5. Loading, empty, nothing-matches and failed are four different looks: the skeleton,
   EmptyState with its action, "No (things) match" with Clear search, InlineError with
   Retry. "Nothing yet" never shows while data is still on its way.
6. A setting applies when it changes (switch, choice) or when its field is left; the
   screen never mixes that with a Save button. A failed save puts the control back and
   says why beside it.
7. A form or dialog that holds typed input asks before it drops it (Discard, Cancel).

### Errors
8. Every error says what failed and the next step (Retry, Open Settings, the field to
   fix). A provider's error shows the provider's own words after the app's short line.
9. An error appears beside its cause (the field, the row, the reply, the run), never only
   in a toast.
10. The user's input survives an error: a failed send keeps the composer's text.
11. App lines never blame or apologise: no "invalid", "please", "sorry", "oops".

### Words
12. One concept has one word everywhere; one word never means two things (words.md; the
    `words-banned` block is checked by tests/test_words.py).
13. A button starts with a verb, is 1 to 3 words, and names the object when the verb alone
    is unclear ("Add provider", "Allow for 7 days"). No OK, Yes or No on a decision.
14. A dialog's title and its confirm button read as question and answer ("Move to
    Trash?", "Move to Trash").
15. No code names on screen: no ids, snake_case, internal states or setting keys.
16. Explanation lives behind a "?" (Help, text from the server). A screen carries no
    explainer sentences; an EmptyState has one line and its action.
17. Numbers carry their unit; 0 that means "no limit" reads "No limit".
18. Times: inside a day group a row shows the time of day, not the group's name again.

### Layout and visual
19. One primary (filled) button per screen; other actions are lower emphasis.
20. Every size, gap, colour and duration comes from tokens.css; at most three text sizes
    on a screen.
21. The page never scrolls; one inner region does. No region scrolls inside another
    scrolling region (a dialog's list shows whole or the dialog is the one scroller).
22. A dialog's title and close button stay in view while its body scrolls.
23. What the user needs first comes first: the thing that others depend on (a provider
    before the roles that use its models) sits above them.
24. A count or rank shows only when it tells something (a rank on a list of one does not).
25. Targets are at least 24 px on a pointer and 44 px on touch; a hover-only action also
    opens from a long press or a menu.
26. Text contrast is at least 4.5:1 (3:1 for large text and control edges) in every
    Appearance contrast and tint.

### Keyboard and overlays
27. Everything works by keyboard; a dialog takes focus, keeps Tab inside, closes on Esc
    and gives focus back to the control that opened it.
28. A destructive dialog does not confirm on Enter; its confirm names the action.
29. A phone menu or short choice opens as a Sheet that closes by its Close button, Back
    and a swipe.

### Chat and the agent
30. After Send the message shows at once and the reply's slot within 1 s; the reply
    streams.
31. While the agent works the line says what it is doing now (the tool row's words), not
    only "Thinking".
32. Stop sits where Send was, stops within about 1 s and keeps the partial reply as
    "Stopped.".
33. A failed reply keeps what it said, names the cause and offers Retry or Regenerate at
    that place.
34. The transcript never jumps while the user reads above; it follows only at the bottom.
35. Tool calls are compact rows (what, target, state); thinking is one Fold; a reply reads
    whole without either.
36. An approval names the action, its target and what follows, with buttons that say it
    (Approve, Deny, Allow once, Allow for 7 days); a standing pass stops repeat asks.
37. Every background run shows its state and its reason when it failed, with Retry on the
    run.

### Phone
38. At 400 px the app is one column with no sideways scroll and nothing lost.
39. The composer stays above the keyboard and clear of the home indicator.
40. Inputs set the keyboard they need (url, email, number).

## The walk

1. Start the demo (`tools/testdata/demo.sh`) and, for real replies, add a provider in
   Settings, Models. Data stays dummy data.
2. Per place: the first screen, an item opened, its menu, each dialog, an error (stop the
   model or block a route), empty and nothing-matches, then the same at 400 px.
3. Write each finding in the format above; fix at the root; check the screen again.

Sources: NN/g heuristics and articles, Apple HIG (incl. Generative AI), Material 3,
Microsoft Writing Style Guide and HAX guidelines, GOV.UK Design System, Shopify Polaris,
Atlassian Design, IBM Carbon (incl. AI Chat), Google PAIR, WCAG 2.2 and the ARIA APG.
