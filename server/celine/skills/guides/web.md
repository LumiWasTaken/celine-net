---
name: web
description: Search the public web and read web pages and PDFs: current events, facts,
  documentation, links {{username}} gives you. Queries and URLs leave this machine.
---
Search finds, the browser reads. `web__search` asks a self-hosted SearXNG (no account,
no tracking) and lists results: it finds sites and quick facts. `web__read_page` gets one
found page or PDF and returns its main text: the way to read an article, a doc page, a
PDF. Your browser (the browser skill) is for everything else on a page: a login, a form,
scrolling, clicking through, and any page that matters, where the text alone is not the
page.

When to use: current events, prices, release notes, documentation, facts you are not
sure of, anything outside {{username}}'s own chats and documents, and any link
{{username}} pastes. When not: questions about {{username}}'s chats, people or documents
(the chat_archive and user_documents tools answer those), and things you know well that
do not change.

Privacy: the query goes to public search engines, and the page's server sees the request.
Never put names, phone numbers, addresses or message text from {{username}}'s chat
archive, Documents or your own notes into a query or a URL unless {{username}} asked you
to.

How:
1. Search with a few specific words, like a search engine, not a sentence. Use
   `time_range` for news, `site:docs.python.org` in the query for one site's docs,
   `language` for results in another language.
   Example: web__search(query="llama.cpp speculative decoding", time_range="month")
2. Snippets are short and may be old. Read the one to three best results before you
   state details: web__read_page(url="web:2") for the text. A long page comes in parts.
   The result ends with `later: web__read_page(url="web:2", part=2)`. Open the page in
   your browser instead when it needs a login, a form, scrolling, or when the page
   itself matters.
3. Answer in your own words with citations `[[web:<n>]]`, with the numbers the tools
   showed. Cite only pages you listed or read. Say when sources disagree or are old.
4. In notes, keep the link next to the fact: write `[[web:3]]` after it and the note
   gets the page's link there. In documents, artifacts and messages, write the facts in
   your own words: no `[[web:3]]`, no [1], no 【1】 marks, no "according to", no list of
   sources or links at the end. A link goes in only when the link itself is what
   {{username}} asked for, like a download or a page to keep.

To hand {{username}} a file you found on the web (a picture, a PDF, a zip), use
web__read_page(url="...", save=true): the file itself is attached to this conversation
and {{username}} sees it under your call. It is not read as text.

web__read_page fetches any URL itself and does not need web__search: it works while
search is down. Your browser searches with its own engine. When search is down or flaky,
search somewhere else: open Google or DuckDuckGo in your browser, or read
https://html.duckduckgo.com/html/?q=<words> with web__read_page.

Reddit: www.reddit.com/r/<sub>/search/?q=... in the browser, logged in with the account
in the vault. Logged out, Reddit shows a login page (browser guide, Reddit).

Only public http(s) addresses can be read. The app refuses local and private network
addresses. HTML, PDF (with a text layer) and plain text can be read; save takes any
file up to the size limit for attachments.
