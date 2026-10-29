"""Values saved before v1.2 reach their new place: a contrast above the cap."""
from __future__ import annotations


from celine.settings import appearance, files


def test_a_contrast_saved_above_the_cap_reads_as_the_cap(tmp_path):
    files.write(tmp_path, "appearance", {"contrast": 150, "tint": 50})
    assert appearance.read(tmp_path) == {"contrast": appearance.CONTRAST_MAX, "tint": 50}
