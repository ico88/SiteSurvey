import struct

import pytest

from sitesurvey.gps import NmeaGps, parse_nmea
from sitesurvey.radio import lrrp
from sitesurvey.radio.ars import parse_ars
from sitesurvey.radio.base import RadioError, aggregate, cai_ip
from sitesurvey.radio import hytera_usb as hu
from sitesurvey.radio import xcmp


def test_cai_ip():
    assert cai_ip(1, 12) == "12.0.0.1"
    assert cai_ip(0x010203, 225) == "225.1.2.3"
    with pytest.raises(ValueError):
        cai_ip(1 << 24, 12)


def test_aggregate():
    assert aggregate([-90, -80, -100], "max") == -80
    assert aggregate([-90, -80, -100], "median") == -90
    assert aggregate([], "max") is None


# ---- LRRP (parser field-verified DM4601e) --------------------------------
@pytest.mark.parametrize("lat,lon", [(45.4642, 9.19), (-33.8688, 151.2093), (40.7128, -74.006)])
def test_lrrp_report_roundtrip(lat, lon):
    pkt = lrrp.build_triggered_report(lat, lon, epoch=1_700_000_000, speed_ms=10.0, heading=20.0)
    d = lrrp.parse_message(pkt)
    assert d["type"] == lrrp.TRIGGERED_REPORT
    assert d["lat"] == pytest.approx(lat, abs=1e-4)
    assert d["lon"] == pytest.approx(lon, abs=1e-4)
    assert d["speed_kmh"] == pytest.approx(10.0 * 3.6, abs=0.1)
    assert d["heading"] == pytest.approx(20.0, abs=0.2)
    assert d["time"] == 1_700_000_000
    assert d["result"] == lrrp.RESULT_OK


def test_lrrp_no_fix():
    # report senza token posizione -> nessun fix
    d = lrrp.parse_message(bytes([lrrp.TRIGGERED_REPORT, 0x00, lrrp.TOK_CURRENT_TS, 0, 0, 0, 0]))
    assert "lat" not in d and d["result"] is None


def test_lrrp_token_55_decoded():
    # posizione reale codificata col token 0x55 (lat+lon, 4+4 byte)
    body = bytes([lrrp.TRIGGERED_REPORT, 0x00, lrrp.TOK_LOC_2D]) + \
        lrrp.encode_lat(45.46) + lrrp.encode_lon(9.19)
    d = lrrp.parse_message(body)
    assert d["lat"] == pytest.approx(45.46, abs=1e-4)
    assert d["lon"] == pytest.approx(9.19, abs=1e-4)


def test_lrrp_request_format():
    req = lrrp.build_immediate_request(0x38)
    assert req.startswith(bytes.fromhex("05092202"))
    assert req.endswith(bytes.fromhex("5154625764"))


# ---- ARS -----------------------------------------------------------------
def test_ars_parse():
    pkt = b"\x00\x0b\xf0\x20\x06" + b"2621001" + b"\x00\x01\x02"
    rec = parse_ars(pkt, "12.40.0.1")
    assert rec and rec["radio_id"] == "2621001" and rec["src_ip"] == "12.40.0.1"
    assert parse_ars(b"garbage", "1.2.3.4") is None


# ---- Hytera channel-status ----------------------------------------------
def _hytera_e7_reply(rx_hz, tx_hz, level, busy):
    header = bytes([0x01, 0x02, 0x03, 0, 0, 0, 0])
    tlv = bytes([0x04]) + struct.pack("<I", rx_hz)
    tlv += bytes([0x05]) + struct.pack("<I", tx_hz)
    tlv += bytes([0x09]) + struct.pack("<I", level)
    if busy:
        tlv += bytes([0x0A]) + struct.pack("<I", 1)
    pl = header + tlv
    hdap = bytes([0x02]) + struct.pack("<H", 0x80E7) + struct.pack("<H", len(pl)) + pl
    return b"\x00" * 12 + hdap  # 12 byte header HRNP fittizio


def test_hytera_channel_status():
    reply = _hytera_e7_reply(430_012_500, 439_012_500, 120, busy=True)
    st = hu.parse_channel_status(reply)
    assert st["rx_hz"] == 430_012_500 and st["tx_hz"] == 439_012_500
    assert st["signal_level"] == 120 and st["busy"] is True
    assert hu.parse_channel_status(b"short") is None


def test_hytera_hdap_checksum_roundtrip():
    frame = hu.build_hdap(hu.HDAP_SERVICE["RCP"], hu.RCP_OPCODE["channel-status"], b"")
    assert frame[0] == 0x02 and frame[-1] == 0x03
    # checksum verificabile: (~sum(body)+0x33)&0xff
    body = frame[1:-2]
    assert frame[-2] == ((~sum(body)) + 0x33) & 0xFF


def test_hytera_hrnp_checksum():
    frame = hu.build_hrnp(hu.OP_CONNECT, 0x04, 0, 0x20, 0x10, 0)
    assert frame[0] == 0x7E and frame[3] == hu.OP_CONNECT
    assert hu._inet_checksum(frame) == 0  # il checksum include se stesso -> 0


# ---- XCMP (framing) ------------------------------------------------------
def test_xcmp_pack_parse():
    pkt = xcmp.pack_std(0x000b, 0x00, 0x0002, b"\x00\x01\x00\x0e\xb4\x0e\x55")
    d = xcmp.parse_pkt(pkt)
    assert d["type"] == 0x000b
    # il frame contiene il sotto-opcode RSSI_OR_SIGNAL
    assert d["full_body"].find((0xB40E).to_bytes(2, "big")) >= 0


def test_xcmp_challenge_response_frame():
    magic = b"\xff\xfc"
    body = magic + bytes.fromhex("2dff77997feb0b64")
    assert body[2:10].hex() in xcmp.DEFAULT_LOOKUP


# ---- NMEA ----------------------------------------------------------------
def test_nmea():
    gga = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
    rmc = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
    g = parse_nmea(gga)
    assert g["sats"] == 8 and g["lat"] == pytest.approx(48.1173) and g["lon"] == pytest.approx(11.516667)
    assert parse_nmea(gga[:-1] + "0") is None
    n = NmeaGps("/dev/null", 9600)
    n.feed(gga)
    n.feed(rmc)
    f = n.fix()
    assert f.valid and f.sats == 8 and f.alt == 545.4
    assert f.speed_kmh == pytest.approx(22.4 * 1.852)
