-- A turn's tail is stored as its parts, a JSON list of [key, value]
-- (turns/prompt.py tail_parts), so a request can leave out a part that the tail before
-- it in view already said. A tail stored before this is one part, sent as it is.
UPDATE message_context SET tail = json_array(json_array('text', tail)) WHERE tail IS NOT NULL;
