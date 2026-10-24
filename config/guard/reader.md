<!-- reads: line, rule, text (the user's answer on a banner); answers JSON -->
{{user}} answers Bastet, the guard who checks what {{name}} does, about one of her decisions.

Bastet's decision: {line}
The rule: {rule}

{{user}}'s answer:
<answer>{text}</answer>

What does {{user}} grant? once: allow this one call; pass: suspend a rule for a while; unfreeze: let {{name}} go on; keep: leave it as it is, or say no.
Reply as JSON only: {{"grant": "once" or "pass" or "unfreeze" or "keep", "rule": "<the rule, or all>", "minutes": <how long a pass holds>, "where": "here" or "everywhere"}}
