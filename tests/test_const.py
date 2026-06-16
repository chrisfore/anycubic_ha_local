# tests/test_const.py
from custom_components.anycubic.anycubic_local import const
from custom_components.anycubic.anycubic_local import QUERY_TYPES


def test_topic_builders():
    assert const.query_topic("20029", "devA", "info") == \
        "anycubic/anycubicCloud/v1/web/printer/20029/devA/info"
    assert const.report_prefix("20029", "devA") == \
        "anycubic/anycubicCloud/v1/printer/public/20029/devA"


def test_pause_enum():
    assert const.PAUSE_STATE[1] == "paused"
    assert const.PAUSE_STATE[0] == "printing"


def test_query_types_includes_required_entries():
    assert "info" in QUERY_TYPES
    assert "multiColorBox" in QUERY_TYPES
