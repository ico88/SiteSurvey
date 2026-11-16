import socket
import struct
import threading

import pytest

from sitesurvey.gps import NmeaGps, parse_nmea
from sitesurvey.radio import lrrp
from sitesurvey.radio.base import RadioError, aggregate, cai_ip
from sitesurvey.radio.hytera import (hdap_build, hdap_parse, hrnp_build, hrnp_checksum, hrnp_parse,
                                     parse_lp_gps)
from sitesurvey.radio.motorola import decode_rssi
from sitesurvey.radio import xnl


def test_cai_ip():
    assert cai_ip(1, 12) == "12.0.0.1"
    assert cai_ip(0x010203, 225) == "225.1.2.3"
    with pytest.raises(ValueError):
        cai_ip(1 << 24, 12)


def test_aggregate():
    assert aggregate([-90, -80, -100], "max") == -80
    assert aggregate([-90, -80, -100], "median") == -90
    assert aggregate([], "max") is None


@pytest.mark.parametrize("lat,lon", [(45.4642, 9.19), (-33.8688, 151.2093), (40.7128, -74.006)])
def test_lrrp_roundtrip(lat, lon):
    msg = lrrp.build_point_response(77, lat, lon, alt=130, epoch=1_700_000_000)
    d = lrrp.parse_message(msg)
    assert d["type"] == lrrp.IMMEDIATE_RSP
    assert d["request_id"] == 77
    assert d["result"] == 0
    assert d["lat"] == pytest.approx(lat, abs=1e-6)
    assert d["lon"] == pytest.approx(lon, abs=1e-6)
    assert d["alt"] == 130
    assert d["time"] == 1_700_000_000


def test_lrrp_error_result():
    d = lrrp.parse_message(lrrp.build_point_response(5, 0, 0, result=0x10))
    assert d["result"] == 0x10 and "lat" not in d


def test_lrrp_request_format():
    req = lrrp.build_immediate_request(1)
    assert req == bytes([0x05, 0x08, 0x22, 0x04, 0, 0, 0, 1, 0x51, 0x40])


def test_uintvar():
    for v in (0, 1, 127, 128, 300, 70000):
        assert lrrp.decode_uintvar(lrrp.encode_uintvar(v), 0)[0] == v


def test_decode_rssi():
    assert decode_rssi(struct.pack(">h", -87)) == -87
    assert decode_rssi(bytes([95])) == -95
    assert decode_rssi(b"") is None


def test_nmea():
    gga = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
    rmc = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
    g = parse_nmea(gga)
    assert g["sats"] == 8 and g["lat"] == pytest.approx(48.1173) and g["lon"] == pytest.approx(11.516667)
    assert parse_nmea(gga[:-1] + "0") is None  # checksum errato
    n = NmeaGps("/dev/null", 9600)
    n.feed(gga)
    n.feed(rmc)
    f = n.fix()
    assert f.valid and f.sats == 8 and f.alt == 545.4
    assert f.speed_kmh == pytest.approx(22.4 * 1.852)


def test_hrnp_hdap():
    payload = hdap_build(0x02, 0x0841, b"\x01\x02", True)
    hdr, op, data = hdap_parse(payload, True)
    assert (hdr, op, data) == (0x02, 0x0841, b"\x01\x02")
    frame = hrnp_build(0x00, 7, payload)
    assert hrnp_checksum(frame) == 0  # checksum include se stesso -> 0
    assert hrnp_parse(frame) == (0x00, 7, payload)


def test_hytera_lp_gps():
    f = parse_lp_gps(b"\x08\xa0\x02....123519A4807.0380N01131.0000E12.5084230394")
    assert f.valid
    assert f.lat == pytest.approx(48.1173, abs=1e-4)
    assert f.lon == pytest.approx(11.516667, abs=1e-4)


def test_tea_known_vector():
    # vettore standard TEA (delta 0x9E3779B9, chiave nulla, blocco nullo)
    out = xnl.tea_encrypt(b"\0" * 8, (0, 0, 0, 0), 0x9E3779B9)
    assert out.hex() == "41ea3a0a94baa940"


def test_xnl_packet_roundtrip():
    p = xnl.XnlPacket(xnl.DATA_MSG, xnl.PROTO_XCMP, 3, 0x0006, 0x0001, 0x0102, b"\x00\x0e\x02")
    enc = p.encode()
    assert struct.unpack(">H", enc[:2])[0] == len(enc) - 2
    assert xnl.XnlPacket.decode(enc[2:]) == p


class FakeXnlRadio(threading.Thread):
    """Radio XNL finta: handshake + risposta a Radio Status RSSI."""

    def __init__(self, key, delta, rssi=-88):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.key, self.delta, self.rssi = key, delta, rssi

    def run(self):
        c, _ = self.srv.accept()
        c.settimeout(5)

        def send(p):
            c.sendall(p.encode())

        def recv():
            n = struct.unpack(">H", c.recv(2))[0]
            buf = b""
            while len(buf) < n:
                buf += c.recv(n - len(buf))
            return xnl.XnlPacket.decode(buf)

        send(xnl.XnlPacket(xnl.MASTER_STATUS_BRDCST, 0, 0, 0, 0x0006, 0, b"\0" * 7))
        assert recv().opcode == xnl.DEV_AUTH_KEY_REQUEST
        seed = b"12345678"
        send(xnl.XnlPacket(xnl.DEV_AUTH_KEY_REPLY, 0, 0, 0, 0x0006, 0, b"\xff\xf1" + seed))
        req = recv()
        ok = req.payload[4:12] == xnl.tea_encrypt(seed, self.key, self.delta)
        send(xnl.XnlPacket(xnl.DEV_CONN_REPLY, 0, 0, 0xFFF1, 0x0006, 0,
                           bytes([1 if ok else 0, 0x02]) + b"\x00\x0a" + b"\x00\x0a" + b"\0" * 8))
        if not ok:
            return
        try:
            while True:
                p = recv()
                if p.opcode != xnl.DATA_MSG:
                    continue
                send(xnl.XnlPacket(xnl.DATA_MSG_ACK, 1, p.flags, p.src, 0x0006, p.txid))
                op = struct.unpack(">H", p.payload[:2])[0]
                if op == xnl.XCMP_RADIO_STATUS:
                    code = p.payload[2]
                    val = struct.pack(">h", self.rssi) if code == 2 else b"SIM\0"
                    send(xnl.XnlPacket(xnl.DATA_MSG, 1, 0, 0x000a, 0x0006, 0x0001,
                                       struct.pack(">H", 0x800E) + bytes([0, code]) + val))
        except (OSError, struct.error):
            pass


def test_xnl_client_against_fake_radio():
    key, delta = (1, 2, 3, 4), 0x12345678
    radio = FakeXnlRadio(key, delta)
    radio.start()
    c = xnl.XnlClient("127.0.0.1", radio.port, key, delta, timeout=2)
    c.connect()
    try:
        d = c.request(xnl.XCMP_RADIO_STATUS, b"\x02")
        assert decode_rssi(d[2:]) == -88
    finally:
        c.close()


def test_xnl_wrong_key_rejected():
    radio = FakeXnlRadio((1, 2, 3, 4), 0x12345678)
    radio.start()
    c = xnl.XnlClient("127.0.0.1", radio.port, (9, 9, 9, 9), 0x12345678, timeout=2)
    with pytest.raises(RadioError):
        c.connect()


def test_xnl_missing_key():
    with pytest.raises(RadioError, match="ADK"):
        xnl.XnlClient("127.0.0.1", 1, (0, 0, 0, 0), 0).connect()
