-- A scheduled message to the user lands at its time with a notice (intercom/deliver.py
-- deliver_due), which marks its delivery's delivered_at; the ones already past landed.
UPDATE intercom_deliveries
   SET delivered_at = (SELECT due_at FROM intercom WHERE id = intercom_id)
 WHERE to_addr = 'user' AND conversation_id IS NULL AND delivered_at IS NULL
   AND intercom_id IN (SELECT id FROM intercom WHERE due_at IS NOT NULL
                       AND due_at <= strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
