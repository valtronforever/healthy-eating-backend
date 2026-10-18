import httpx
import pytest
from pydantic import ValidationError

from healthy_eating.auth.social import SocialLogin, _validate_microsoft_issuer
from healthy_eating.config import Settings


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep variables from the developer's environment out of these settings tests."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


PRODUCTION = {
    "_env_file": None,
    "environment": "production",
    "base_url": "https://food.example.com",
    "secret_key": "x" * 48,
}


def test_production_settings_are_accepted_when_safe() -> None:
    Settings(**PRODUCTION)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "override",
    [
        {"secret_key": ""},
        {"secret_key": "too-short"},
        {"dev_login_enabled": True},
        {"base_url": "http://food.example.com"},
    ],
)
def test_production_rejects_unsafe_settings(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**(PRODUCTION | override))  # type: ignore[arg-type]


def test_only_configured_providers_are_enabled() -> None:
    settings = Settings(
        _env_file=None,
        google_client_id="id",
        google_client_secret="secret",
        microsoft_client_id="id-without-secret",
    )
    assert SocialLogin(settings).enabled_providers == ["google"]


def test_microsoft_issuer_must_match_tenant() -> None:
    tid = "9188040d-6c67-4c5b-b112-36a304b66dad"
    claims = {"tid": tid}
    assert _validate_microsoft_issuer(claims, f"https://login.microsoftonline.com/{tid}/v2.0")
    assert not _validate_microsoft_issuer(claims, "https://login.microsoftonline.com/other/v2.0")


async def test_health(http: httpx.AsyncClient) -> None:
    response = await http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
