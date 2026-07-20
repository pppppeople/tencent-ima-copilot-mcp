from types import SimpleNamespace

from query_ima import content_to_text


def test_content_to_text_joins_text_items_only():
    result = SimpleNamespace(
        content=[
            SimpleNamespace(text="first"),
            SimpleNamespace(text=None),
            SimpleNamespace(text="second"),
        ]
    )

    assert content_to_text(result) == "first\n\nsecond"


def test_content_to_text_handles_empty_result():
    assert content_to_text(SimpleNamespace(content=[])) == ""
