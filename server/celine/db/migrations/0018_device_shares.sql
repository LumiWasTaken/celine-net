-- A share of a port or a path on one of her devices (shares.py): the device's name; NULL
-- for one of her box.
ALTER TABLE shares ADD COLUMN device TEXT;
