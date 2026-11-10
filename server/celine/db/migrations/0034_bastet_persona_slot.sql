-- Bastet's talk and note frames name her persona's slot {{persona}}, as every other name.
UPDATE kv SET v = replace(v, '{persona}', '{{persona}}')
 WHERE k IN ('bastet:talk', 'bastet:note') AND instr(v, '{{persona}}') = 0;
