import json
import logging
from unittest.mock import MagicMock
from src.pipeline import correlation_id, StructuredAdapter

def test_structured_logging_adapter():
    mock_logger = MagicMock()
    adapter = StructuredAdapter(mock_logger, {})

    correlation_id.set("test-12345")

    msg, kwargs = adapter.process("Test message %d", {"structured": {"event": "test"}})

    assert "Test message %d" in msg

    # Parse the json suffix
    suffix = msg.split(" | ")[1]
    # Replace %% back to % just to be sure it's valid JSON for parsing
    suffix = suffix.replace("%%", "%")
    data = json.loads(suffix)

    assert data["correlation_id"] == "test-12345"
    assert data["event"] == "test"

    # Ensure no extra kwargs are left behind that would crash standard logger
    assert "structured" not in kwargs

def test_structured_logging_escapes_percentage():
    mock_logger = MagicMock()
    adapter = StructuredAdapter(mock_logger, {})

    correlation_id.set("test-123")
    msg, kwargs = adapter.process("Test", {"structured": {"val": "100%"}})

    assert "%%" in msg
