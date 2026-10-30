---
name: music_generator
description: Makes songs and instrumental tracks with ComfyUI, in batches. Use it when
  {{username}} asks for music, a track, an ambient soundscape or a song.
---
`music_generator__generate` makes a batch: a list of items, each one track. An item has
a `style`; a song also has `lyrics` and `instrumental=false`. `count` makes that many
tracks of each item, each with its own seed. The call returns at once with the batch
id and one slot per track. The tracks are made one after the other, a few minutes
each, and each appears in this conversation when it is done, where {{username}} hears
it. It is also saved in the music folder of the data folder.

Nothing reports back to you, not when a track is done and not when one fails, so
answer now and do not poll `music_generator__read` for it. It shows a batch at any
time, when there is a reason to look; `music_generator__stop` stops one; what was made stays. With `wait=true`
your turn waits until the batch is done, as long as your turn's time allows; past it,
the batch goes on.

When to use: {{username}} asks you to compose music, make an instrumental or an
ambient background, or write a song.

The preset, with how it takes a style and an example, comes at the end of this guide.
