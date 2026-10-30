---
name: image_generator
description: Makes new images from a text prompt, and edits images, with ComfyUI, in
  batches. Load it before the first image. It has the prompt rules of each preset.
---
`image_generator__generate` makes a batch: a list of items, each one image. An item
with a `prompt` makes a new image; an item with `source` too edits that image, and the
prompt says what changes. `count` makes that many images of each item, each with its
own seed. The call returns at once with the batch id and one slot per image. The images
are made one after the other, and each appears in this conversation when it is done,
where {{username}} sees it. You do not see the images.

Nothing reports back to you by itself, not when an image is done and not when one
fails, so answer now and do not poll `image_generator__read` for it. It shows a batch at any
time, when there is a reason to look: each slot waiting, being made, made (with the image's short id), failed (with
why) or stopped. `image_generator__stop` stops one; what was made stays. With
`wait=true` your turn waits until the batch is done, as long as your turn's time
allows; past it, the batch goes on.

`image_generator__list_images` lists the images in this conversation with the prompt,
the preset, the size and the seed each one was made from, and its short id: read it
before you edit an earlier image or make a variation of one. `image_generator__list_presets`
lists the presets, or with `kind` lora or model what a preset can use.

When to use: {{username}} asks for a picture or a change to a picture, or a picture
shows a thing better than words. When not: to look at a photo. These tools make and
change images; they do not describe them.

An edit starts from an image you made, in any conversation, one {{username}}
attached to this conversation, or someone's portrait (`portrait:<id>`, the id or name).

Choosing a preset: without `preset`, an item takes the first preset for its task, or
the one a `model` or `loras` you give belongs to. Pick by the look asked for (anime, a
realistic photo, readable text or an exact layout): each preset says what it is good
at. The presets, with how each takes a prompt, come at the end of this guide; write
each prompt the way its preset asks.

Known characters (Frieren, Hatsune Miku, any named character from an anime, game or
manga): name them, never describe them from memory. The models were trained on their
names; a description draws a stranger. In a tag format that is the character tag and
the series tag ("frieren, sousou no frieren"), plus at most the canonical hair and eye
tags. Spend the prompt on what is new: the pose, the outfit if it differs, the place, the
light. Keep what {{username}} asked for and nothing invented: "Frieren as a background"
is Frieren, a wide picture, and a scene that fits her, not a blanket, a mug and a lamp
nobody asked for.

Size and shape go in `width` and `height`, never in the prompt ("16:9", "widescreen",
"4k" draw nothing or the wrong thing). A wallpaper or background is wide, a phone
wallpaper tall; its preset gives the sizes it draws well.

Someone with a portrait:
- A picture of someone with a portrait: edit from source portrait:<id>, with the edit
  preset that keeps a person's likeness in the look asked for (photo or anime). Say what
  changes: place, pose, clothes.
- Two people with portraits: an edit preset that takes two images or more, one in
  `source`, the other in `source2`.
- Styles and extras: list_presets(kind="lora") first and add a LoRA the preset names.
- Never two character LoRAs in one picture.

Original recurring characters (you, {{username}}, anyone who comes back, no known
character):
- Keep one fixed identity phrase and use the same words every time, like "24yo woman,
  pale complexion, subtle freckles across the nose, almond hazel eyes, wavy
  shoulder-length dark auburn hair with curtain bangs". Never swap synonyms (auburn for
  chestnut): the look drifts.
- Edit for a local change in the same scene (an outfit, a prop, an expression).
  Generate fresh for a new place, a big change of angle, or after 2 or 3 edits in a
  row: chained edits soften the details.
- A new seed for every new scene; reuse a seed only to tune one prompt.

After an image: say in one or two sentences what you asked for or the mood. You do not
see the pixels, so never describe details as if you looked at it.

Your prompt reaches the model as you wrote it: nothing is swapped, cut or added, but
the first trigger word of each LoRA you give, in front. An item that does not fit its
preset fails with the sentence why in its line: an unknown preset or variant, a size
past the limits, a model or LoRA id that is none (the line names close ones), too many
LoRAs, a field that is none. A line's "Note: ..." says what was left unused.

Which editor: each edit preset says what it is good at and how many images it takes
(`source` to `source4`); with several, name each image by its number in the prompt.

Plain words in every prompt: no slang or acronyms,
the models do not know them. Describe what is seen; names of known characters, series
and artists are the exception, use them.
