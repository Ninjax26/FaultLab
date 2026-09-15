import base64

from faultlab.api.main import valid_basic_credentials


def basic_header(username: str, password: str) -> str:
    value = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {value}"


def test_basic_credentials_accept_expected_username_and_token() -> None:
    assert valid_basic_credentials(basic_header("faultlab", "correct-secret"), "correct-secret")


def test_basic_credentials_reject_invalid_inputs() -> None:
    assert not valid_basic_credentials(None, "correct-secret")
    assert not valid_basic_credentials("Bearer correct-secret", "correct-secret")
    assert not valid_basic_credentials("Basic not-base64", "correct-secret")
    assert not valid_basic_credentials(basic_header("someone", "correct-secret"), "correct-secret")
    assert not valid_basic_credentials(basic_header("faultlab", "wrong"), "correct-secret")
