# tests/test_config_flow.py
from contextlib import contextmanager
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
    assert result["title"] == "Anycubic Kobra S1 Max" or result["data"]["host"] == "1.2.3.4"
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


# ------------------------------------------------- changing the printer's address
#
# A printer that gets a new address from DHCP left its entry in setup-retry for good. The
# only step that re-checked anything was reauth, and reauth re-checks the SAME address, so
# the way out was to delete the integration and add it again.

OTHER = HandshakeResult("5.6.7.8", 9883, "u", "p", "DEV2", "20029", "SER-2")
NEW = "192.168.1.60"
# Each of these would change what the handshake URL means if it were let through, because
# the address is interpolated into that URL exactly as typed.
BAD_ADDRESSES = ("http://192.168.1.60", "192.168.1.60:18910", "192.168.1 .60",
                 "192.168.1.60/info", "user@192.168.1.60", "192.168.1.60?x=1",
                 "192.168.1.60#x", "", "   ",
                 # Pasted along with an address and invisible in the field: a zero-width
                 # space, and a control character.
                 "192.168.\N{ZERO WIDTH SPACE}1.60", "192.168.1.60\x00")


class FakeTransport:
    def __init__(self, hs, on_report, **k): pass
    def connect(self): pass
    def disconnect(self): pass
    def query(self, t): pass
    def publish(self, t, p): pass


@contextmanager
def _printers(answers, otherwise=None):
    """Patch every handshake the integration makes, as a small model of the network.

    `answers` maps an address to what the printer there says (a HandshakeResult) or to the
    exception that reaching it raises. Any other address gets `otherwise`, by default
    unreachable. Yields the config flow's own handshake, so a test can assert on exactly
    what the flow tried.
    """
    def shake(host, *args, **kwargs):
        answer = answers.get(host, otherwise or OSError("no route to host"))
        if isinstance(answer, Exception):
            raise answer
        return answer

    with patch("custom_components.anycubic.config_flow.do_handshake", side_effect=shake) as flow, \
         patch("custom_components.anycubic.do_handshake", side_effect=shake), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        yield flow


def _entry(hass, unique_id="SER-1", **data):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id=unique_id, data={"host": "1.2.3.4", **data})
    entry.add_to_hass(hass)
    return entry


async def _start_reconfigure(hass, entry, **init):
    # What the frontend sends on every Home Assistant version that has this step: the
    # source and the entry id, and no data.
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        **init)


async def _submit(hass, result, host):
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": host})
    await hass.async_block_till_done()
    return result


async def test_reconfigure_moves_the_entry_to_the_new_address(hass):
    entry = _entry(hass, kept="as it was")
    with _printers({"1.2.3.4": HS, NEW: HS}) as flow_handshake:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        first_session = entry.runtime_data

        result = await _start_reconfigure(hass, entry)
        assert entry.supports_reconfigure           # what puts "Reconfigure" in the menu
        assert result["type"] == FlowResultType.FORM and result["step_id"] == "reconfigure_confirm"
        assert result["data_schema"]({}) == {"host": "1.2.3.4"}    # pre-filled with the current one
        flow_handshake.assert_not_called()          # nothing is tried until the user submits
        result = await _submit(hass, result, NEW)

    assert result["type"] == FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data == {"host": NEW, "kept": "as it was"}
    flow_handshake.assert_called_once_with(NEW)
    # Reloaded: a new coordinator, built for the new address.
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert entry.runtime_data is not first_session
    assert entry.runtime_data.host == NEW


async def test_reconfigure_works_while_the_entry_is_stuck_in_setup_retry(hass):
    # The state a user is really in when they need this. The old address is dead, so the
    # entry never loaded: there is no runtime_data, and the only thing that still says
    # which printer this is, is the entry's unique id.
    entry = _entry(hass)
    with _printers({NEW: HS}):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY

        result = await _submit(hass, await _start_reconfigure(hass, entry), NEW)

    assert result["type"] == FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data == {"host": NEW}
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_reconfigure_needs_nothing_newer_than_home_assistant_2024_9(hass):
    # hacs.json declares 2024.9 as the minimum, and the tests run on something much newer
    # that quietly fills in what the old one lacks. So run the step against the 2024.9
    # surface: there async_update_reload_and_abort takes no data_updates and its reason
    # defaults to "reauth_successful", and the reconfigure helpers of 2024.11 do not exist.
    from homeassistant.config_entries import ConfigFlow
    from homeassistant.helpers.typing import UNDEFINED

    def as_in_2024_9(self, entry, *, unique_id=UNDEFINED, title=UNDEFINED, data=UNDEFINED,
                     options=UNDEFINED, reason="reauth_successful",
                     reload_even_if_entry_is_unchanged=True):
        changed = self.hass.config_entries.async_update_entry(
            entry=entry, unique_id=unique_id, title=title, data=data, options=options)
        if reload_even_if_entry_is_unchanged or changed:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason=reason)

    def not_in_2024_9(*args, **kwargs):
        raise AttributeError("added in Home Assistant 2024.11")

    entry = _entry(hass, kept="as it was")
    with _printers({NEW: HS}), \
         patch.object(ConfigFlow, "async_update_reload_and_abort", as_in_2024_9), \
         patch.object(ConfigFlow, "_get_reconfigure_entry", not_in_2024_9), \
         patch.object(ConfigFlow, "_abort_if_unique_id_mismatch", not_in_2024_9):
        result = await _submit(hass, await _start_reconfigure(hass, entry), NEW)

    assert result["type"] == FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data == {"host": NEW, "kept": "as it was"}
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_reconfigure_started_with_the_entry_data_shows_the_form_first(hass):
    # Home Assistant 2024.9 and 2024.10 could also start this flow by handing the first
    # step a copy of the entry's data. That is not something the user typed, and treating
    # it as a submission would act on the old address before any form was shown.
    entry = _entry(hass)
    with _printers({}, otherwise=HS) as flow_handshake:
        result = await _start_reconfigure(hass, entry, data=dict(entry.data))
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "reconfigure_confirm"
    assert not result["errors"]
    flow_handshake.assert_not_called()
    assert entry.data == {"host": "1.2.3.4"}


async def test_reconfigure_to_an_address_nothing_answers_at_changes_nothing(hass):
    from custom_components.anycubic.anycubic_local.exceptions import HandshakeError
    entry = _entry(hass)
    with _printers({"192.168.1.61": HandshakeError("not a printer")}) as flow_handshake:
        result = await _submit(hass, await _start_reconfigure(hass, entry), NEW)
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}
        # The form comes back holding what was typed, so a typo can be corrected.
        assert result["data_schema"]({}) == {"host": NEW}
        result = await _submit(hass, result, "192.168.1.61")
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}
    assert [c.args for c in flow_handshake.call_args_list] == [(NEW,), ("192.168.1.61",)]
    assert entry.data == {"host": "1.2.3.4"}


async def test_reconfigure_refuses_an_address_that_is_a_different_printer(hass):
    # DHCP gives one printer's old address to another, and people mistype. Either way the
    # serial that answers is not this entry's.
    mine = _entry(hass)
    # The other printer is configured too, at a different address from the one typed.
    # Re-pointing ITS entry at what was typed is exactly what Home Assistant's
    # _abort_if_unique_id_configured(updates=...) would do if this step called it.
    other = _entry(hass, unique_id="SER-2", host="5.6.7.8")
    with _printers({NEW: OTHER}):
        result = await _submit(hass, await _start_reconfigure(hass, mine), NEW)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_printer"}
    assert mine.data == {"host": "1.2.3.4"}
    assert other.data == {"host": "5.6.7.8"}


async def test_reconfigure_refuses_a_printer_that_does_not_say_who_it_is(hass):
    entry = _entry(hass)
    nameless = HandshakeResult(NEW, 9883, "u", "p", "DEV", "20029", "")
    with _printers({NEW: nameless}):
        result = await _submit(hass, await _start_reconfigure(hass, entry), NEW)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data == {"host": "1.2.3.4"}


async def test_reconfigure_refuses_an_entry_that_has_no_unique_id(hass):
    # With nothing to compare the answer against, no answer can be accepted.
    for missing in (None, ""):
        entry = _entry(hass, unique_id=missing)
        with _printers({NEW: HS}):
            result = await _submit(hass, await _start_reconfigure(hass, entry), NEW)
        assert result["type"] == FlowResultType.FORM, repr(missing)
        assert result["errors"] == {"base": "cannot_connect"}, repr(missing)
        assert entry.data == {"host": "1.2.3.4"}


async def test_a_malformed_address_is_refused_without_a_handshake_when_adding(hass):
    with _printers({}, otherwise=HS) as flow_handshake:
        for bad in BAD_ADDRESSES:
            result = await _submit(hass, await _start(hass), bad)
            assert result["type"] == FlowResultType.FORM, bad
            assert result["errors"] == {"base": "cannot_connect"}, bad
    flow_handshake.assert_not_called()
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_a_malformed_address_is_refused_without_a_handshake_when_reconfiguring(hass):
    entry = _entry(hass)
    with _printers({}, otherwise=HS) as flow_handshake:
        result = await _start_reconfigure(hass, entry)
        for bad in BAD_ADDRESSES:
            result = await _submit(hass, result, bad)
            assert result["type"] == FlowResultType.FORM, bad
            assert result["errors"] == {"base": "cannot_connect"}, bad
    flow_handshake.assert_not_called()
    assert entry.data == {"host": "1.2.3.4"}


async def test_whitespace_round_an_address_is_trimmed_in_both_steps(hass):
    # Round the outside it is only ever a paste artefact. Left in, it used to reach the
    # handshake URL and fail in a way no error message explained.
    with _printers({NEW: HS, "192.168.1.61": HS}) as flow_handshake:
        result = await _submit(hass, await _start(hass), f"  {NEW}\n")
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == {"host": NEW}

        entry = result["result"]
        result = await _submit(hass, await _start_reconfigure(hass, entry), "\t192.168.1.61 ")
        assert result["type"] == FlowResultType.ABORT
        assert entry.data == {"host": "192.168.1.61"}
    assert [c.args for c in flow_handshake.call_args_list] == [(NEW,), ("192.168.1.61",)]


async def test_adding_a_printer_again_at_its_new_address_moves_the_existing_entry(hass):
    # The other thing people try when a printer has moved: add it again. That answered
    # "already configured" and left the entry pointing at the dead address.
    entry = _entry(hass, kept="as it was")
    with _printers({NEW: HS}):
        assert not await hass.config_entries.async_setup(entry.entry_id)    # old address is dead
        await hass.async_block_till_done()
        assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY

        result = await _submit(hass, await _start(hass), NEW)

    assert result["type"] == FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data == {"host": NEW, "kept": "as it was"}
    assert entry.state is config_entries.ConfigEntryState.LOADED          # and reloaded there
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_adding_a_printer_with_no_serial_cannot_move_another_entry(hass):
    # Moving an existing entry is only safe once the answer has a real serial to match on.
    # An entry with an empty unique id would otherwise be "the same printer" as any
    # printer that does not say who it is.
    legacy = _entry(hass, unique_id="")
    nameless = HandshakeResult(NEW, 9883, "u", "p", "DEV", "20029", "")
    with _printers({NEW: nameless}):
        result = await _submit(hass, await _start(hass), NEW)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert legacy.data == {"host": "1.2.3.4"}


def test_the_reconfigure_texts_are_the_approved_ones_in_both_files():
    import json
    import pathlib

    root = pathlib.Path(__file__).parent.parent / "custom_components" / "anycubic"
    text = (root / "strings.json").read_text()
    assert text == (root / "translations" / "en.json").read_text()
    config = json.loads(text)["config"]
    # A title and the field's label, and nothing else: no description under the title.
    assert config["step"]["reconfigure_confirm"] == {
        "title": "Change printer address", "data": {"host": "Printer IP address or hostname"}}
    assert config["error"]["wrong_printer"] == "That address belongs to a different printer."
    assert config["abort"]["reconfigure_successful"] == "Printer address updated."
    # The two this flow reuses keep the wording they had.
    assert config["error"]["cannot_connect"] == \
        "Could not reach the printer. Check the IP and that LAN Mode is on."
    assert config["abort"]["already_configured"] == "This printer is already configured."
