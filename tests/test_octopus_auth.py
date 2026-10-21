"""Unit tests for the Kraken login input (no Splunk needed)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "package" / "bin"))

from octopus_auth import kraken_login_input  # noqa: E402


def test_api_key_logs_in_on_its_own():
    assert kraken_login_input("me@example.com", "sk_live_abc123") == {"APIKey": "sk_live_abc123"}


def test_api_key_is_trimmed():
    assert kraken_login_input("me@example.com", "  sk_live_abc123\n") == {"APIKey": "sk_live_abc123"}


def test_password_uses_the_account_email():
    assert kraken_login_input("me@example.com", "hunter2") == {
        "email": "me@example.com", "password": "hunter2"}


def test_missing_secret_is_a_password_login_not_a_crash():
    assert kraken_login_input("me@example.com", None) == {"email": "me@example.com", "password": ""}
