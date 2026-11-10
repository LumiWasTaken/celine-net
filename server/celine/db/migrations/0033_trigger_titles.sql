-- A trigger's background task is titled with the trigger's label, not its id.
UPDATE conversations SET title = 'New email'
 WHERE trigger_key = 'trigger:mail.new' AND title = 'mail.new';
