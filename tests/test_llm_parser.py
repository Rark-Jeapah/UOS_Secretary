from __future__ import annotations

from sidae_secretary.connectors.llm import parse_summary_text


def test_parse_summary_text_structured_format() -> None:
    text = """- Midterm week starts Tuesday
- Two new assignment files posted
- One forum announcement requires response
Action: Review files tonight and submit questions."""
    result = parse_summary_text(text)
    assert len(result.bullets) == 3
    assert result.action_item.startswith("Review files")


def test_parse_summary_text_fallbacks() -> None:
    text = "Only one line"
    result = parse_summary_text(text)
    assert len(result.bullets) == 3
    assert result.bullets[0] == "Only one line"
    assert result.action_item
