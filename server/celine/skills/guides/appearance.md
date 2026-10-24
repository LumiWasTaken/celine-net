---
name: appearance
description: Your portrait, the face {{username}} sees beside your messages. Set it with
  appearance__set_portrait from one of your images or an image in Documents.
words: portrait, avatar, appearance
---
Your portrait is one image: the face beside your messages and everywhere you speak in
the app. {{username}} can set it in Settings; you set it with
`appearance__set_portrait(image)`. It replaces the one before.

`image` is one of:

- an image's short id, like `f5ef648c`: one you made (image_generator, in any
  conversation) or one {{username}} attached to this conversation. The image results
  and `image_generator__list_images` show the short ids.
- an image in Documents: its id, path or title, as `user_documents__read` takes it.

A PNG, JPEG or WebP image works. To make a new portrait, generate the image first, then
set it by its short id.
