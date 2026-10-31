---
name: user_documents
description: {{username}}'s Documents, the drawer you share with them. Files of any kind
  in folders: stories, exports, notes, PDFs, books, images, audio, anything they or you
  put there. Use it when one of those files may answer a question, and to put a text
  file there for them.
---
Documents is {{username}}'s drawer, and theirs to share with you. It holds folders and
files of any kind. Find your way with `user_documents__list(folder=…)`: a folder's
folders (with how many documents each holds), then its documents.

You see three things about every document before you read it: its title, the description
{{username}} wrote, and the AI description. Judge from those first. Then search or read.

Each document is Chunked or Full file. The app cuts a chunked document into passages:
search finds the passages, and you read one, a range, or on from one. A full-file
document has no passages: search finds it by its title and descriptions, and you read it
as one piece. A file without text (audio, an archive) is found by its name and
descriptions only. An image is described: search finds it by what is in it, and reading
it gives its description, and the image itself when your model sees images.

## Flow

1. `user_documents__list()` (the top) or `user_documents__list(folder="stories")`: the
   folders and documents there. `sort` is name, added, changed, type or size.
2. `user_documents__search(query, folder=…, documents=[…])`: passages of chunked
   documents, grouped per document, each with its reference, and the documents that
   match by name or description. Leave `folder` and `documents` empty to search all.
3. `user_documents__read(document=…)`: a full-file document at once, a chunked one from
   the start. `user_documents__read(ref=…)` reads from a passage,
   `user_documents__read(ref=…, to=…)` a range. Long parts end with `earlier` and
   `later` references: `ref=<later>` reads on, `ref=<earlier>, direction="before"` reads
   what comes before (`earlier` is the first passage of the part you have).
4. `user_documents__write(path, content)`: a text file into the drawer (a story, a
   report, notes for {{username}}), with its folders. You can replace a file you wrote.
   A file {{username}} put there stays theirs: write to a new path. What you found on
   the web goes in as facts in your own words: no citation marks, no "according to", no
   list of sources or links.

`celine export` puts its exports into the folder `exports/`.

## Citing

Passages have references like `documents:812`. Cite them as `[[documents:812]]`, like
chat messages, and say what the passage says in your own words. Quote only when the
wording itself matters. Never cite an AI description as if the document said it.

## Not for

- Your own notes.
- WhatsApp and Discord messages (the Messages place): use the Messages archive.
