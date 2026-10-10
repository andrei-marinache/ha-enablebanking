"""Tests for the reconfigure flow: a new bank session on the user's request.

The point of it is picking up accounts opened after setup, which only a fresh
authorisation at the bank can add, without losing the entry and its entities.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enablebanking.const import (
    CONF_APP_ID,
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_AUTH_CODE,
    CONF_CONSENT_EXPIRES_AT,
    CONF_JWT,
    CONF_PRIVATE_KEY,
    CONF_PSU_TYPE,
    CONF_SESSION_ID,
    DOMAIN,
    PSU_BUSINESS,
)
from custom_components.enablebanking.errors import EnableBankingConnectionError

APP_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AUTH_URL = "https://auth.enablebanking.com/ais/start?sessionid=abc"
OLD_SESSION_ID = "11111111-2222-3333-4444-555555555555"
NEW_SESSION_ID = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def client() -> MagicMock:
    """A stand-in Enable Banking client with every call succeeding."""
    mock = MagicMock()
    mock.async_start_auth = AsyncMock(return_value=AUTH_URL)
    mock.async_create_session = AsyncMock(
        return_value={
            "session_id": NEW_SESSION_ID,
            "access": {"valid_until": "2027-04-01T00:00:00+00:00"},
        }
    )
    return mock


@pytest.fixture
def entry(hass: HomeAssistant, rsa_private_key_pem: str) -> MockConfigEntry:
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        title="ASN Bank (business)",
        unique_id="abc123",
        data={
            CONF_JWT: "a.b.c",
            CONF_PRIVATE_KEY: rsa_private_key_pem,
            CONF_APP_ID: APP_ID,
            CONF_SESSION_ID: OLD_SESSION_ID,
            CONF_ASPSP_NAME: "ASN Bank",
            CONF_ASPSP_COUNTRY: "NL",
            CONF_PSU_TYPE: PSU_BUSINESS,
        },
    )
    mock_entry.add_to_hass(hass)
    return mock_entry


async def test_reconfigure_swaps_the_entry_to_a_new_session(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    """The happy path: same bank, new session, same entry."""
    with (
        patch("custom_components.enablebanking.config_flow.EnableBankingClient") as client_cls,
        patch("custom_components.enablebanking.async_setup_entry", return_value=True),
    ):
        client_cls.for_config_flow.return_value = client

        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "reconfigure_auth"
        assert result["description_placeholders"] == {"auth_url": AUTH_URL}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_AUTH_CODE: "https://enablebanking.com/?state=s&code=the-auth-code"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # Authorised at the bank the entry was set up for, as the same kind of user.
    client.async_start_auth.assert_awaited_once_with("ASN Bank", "NL", PSU_BUSINESS)
    client.async_create_session.assert_awaited_once_with("the-auth-code")
    assert entry.data[CONF_SESSION_ID] == NEW_SESSION_ID
    assert entry.data[CONF_CONSENT_EXPIRES_AT] == "2027-04-01T00:00:00+00:00"
    # The entry itself is kept: its id and unique_id are what its entities hang on.
    assert entry.unique_id == "abc123"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_recovers_from_a_failed_start(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    """A connection error on starting the authorisation can simply be retried."""
    client.async_start_auth.side_effect = EnableBankingConnectionError("down")
    with (
        patch("custom_components.enablebanking.config_flow.EnableBankingClient") as client_cls,
        patch("custom_components.enablebanking.async_setup_entry", return_value=True),
    ):
        client_cls.for_config_flow.return_value = client

        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "reconfigure"
        assert result["errors"] == {"base": "cannot_connect"}

        client.async_start_auth.side_effect = None
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "reconfigure_auth"

    assert entry.data[CONF_SESSION_ID] == OLD_SESSION_ID
