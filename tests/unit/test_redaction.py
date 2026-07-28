from mnema.security.redaction import redact


def test_redacts_sensitive_keys_and_bearer_tokens() -> None:
    result = redact(
        {
            "password": "bad",
            "nested": {"api_key": "bad", "message": "Bearer abc.def"},
            "safe": "visible",
        }
    )
    assert result["password"] == "[REDACTED]"  # noqa: S105
    assert result["nested"]["api_key"] == "[REDACTED]"
    assert result["nested"]["message"] == "Bearer [REDACTED]"
    assert result["safe"] == "visible"
