# tests/test_config_flow.py
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.anycubic.const import DOMAIN
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult

HS = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER-1")


async def _start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_user_flow_success(hass):
    result = await _start(hass)
    assert result["type"] == FlowResultType.FORM
    with patch("custom_components.anycubic.config_flow.do_handshake", return_value=HS):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "1.2.3.4"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "AnyCubic Kobra S1 Max" or result["data"]["host"] == "1.2.3.4"
    assert result["result"].unique_id == "SER-1"


async def test_cloud_mode_error(hass):
    from custom_components.anycubic.anycubic_local.exceptions import HandshakeError
    result = await _start(hass)
    with patch("custom_components.anycubic.config_flow.do_handshake",
               side_effect=HandshakeError("Printer is in CLOUD mode — enable LAN Mode")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "1.2.3.4"})
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_already_configured(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"}).add_to_hass(hass)
    result = await _start(hass)
    with patch("custom_components.anycubic.config_flow.do_handshake", return_value=HS):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "9.9.9.9"})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def _reauth_entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    return entry


async def _start_reauth(hass, entry):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )


async def test_reauth_success(hass):
    entry = _reauth_entry(hass)
    result = await _start_reauth(hass, entry)
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "reauth_confirm"
    with patch("custom_components.anycubic.config_flow.do_handshake", return_value=HS):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_still_cloud(hass):
    from custom_components.anycubic.anycubic_local.exceptions import CloudModeError
    entry = _reauth_entry(hass)
    result = await _start_reauth(hass, entry)
    with patch("custom_components.anycubic.config_flow.do_handshake",
               side_effect=CloudModeError("Printer is in CLOUD mode — enable LAN Mode")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_setup_cloud_mode_starts_reauth(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.anycubic.anycubic_local.exceptions import CloudModeError
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake",
               side_effect=CloudModeError("Printer is in CLOUD mode — enable LAN Mode")):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert any(f["context"]["source"] == config_entries.SOURCE_REAUTH
               for f in hass.config_entries.flow.async_progress())
