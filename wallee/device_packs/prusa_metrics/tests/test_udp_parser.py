"""Tests for UDP listener: InfluxDB line protocol parser and syslog header parser."""

import pytest
from wallee.bus.udp_listener import (
    parse_influx_line,
    parse_influx_value,
    parse_syslog_header,
    MetricsBuffer,
    Metric,
)


class TestParseInfluxValue:
    def test_integer(self):
        assert parse_influx_value("44i") == 44
        assert parse_influx_value("-8i") == -8
        assert parse_influx_value("0i") == 0

    def test_float(self):
        assert parse_influx_value("33.30") == 33.30
        assert parse_influx_value("-9.000000") == -9.0
        assert parse_influx_value("24.062366") == 24.062366

    def test_string(self):
        assert parse_influx_value('"M114"') == "M114"
        assert parse_influx_value('"6.4.0+1.LOCAL"') == "6.4.0+1.LOCAL"
        assert parse_influx_value('""') == ""

    def test_bool(self):
        assert parse_influx_value("T") is True
        assert parse_influx_value("true") is True
        assert parse_influx_value("F") is False
        assert parse_influx_value("false") is False


class TestParseInfluxLine:
    def test_simple_int(self):
        m = parse_influx_line("temp_mcu v=44i 625")
        assert m.name == "temp_mcu"
        assert m.tags == {}
        assert m.fields == {"v": 44}
        assert m.timestamp == 625

    def test_simple_float(self):
        m = parse_influx_line("temp_brd v=38.694855 13806")
        assert m.name == "temp_brd"
        assert m.fields["v"] == pytest.approx(38.694855)

    def test_with_tags(self):
        m = parse_influx_line("temp_noz,n=0,a=1 value=215.20 11816")
        assert m.name == "temp_noz"
        assert m.tags == {"n": "0", "a": "1"}
        assert m.fields["value"] == pytest.approx(215.20)

    def test_multiple_fields(self):
        m = parse_influx_line("fsensor,n=0 st=2i,f=1057502i,r=27225i,ri=1308458i 2400")
        assert m.name == "fsensor"
        assert m.tags == {"n": "0"}
        assert m.fields["st"] == 2
        assert m.fields["f"] == 1057502
        assert m.fields["r"] == 27225
        assert m.fields["ri"] == 1308458

    def test_fan_with_tags_and_multiple_fields(self):
        m = parse_influx_line("fan,fan=heatbreak state=0,pwm=200,measured=6039 8636")
        assert m.name == "fan"
        assert m.tags == {"fan": "heatbreak"}
        assert m.fields["state"] == 0  # bare int without 'i' suffix treated as float->int
        assert m.fields["pwm"] == 200
        assert m.fields["measured"] == 6039

    def test_heap_multiple_fields(self):
        m = parse_influx_line("heap free=63548i,total=81700i 8828")
        assert m.fields["free"] == 63548
        assert m.fields["total"] == 81700

    def test_string_value(self):
        m = parse_influx_line('gcode v="M114" -661')
        assert m.fields["v"] == "M114"
        assert m.timestamp == -661

    def test_bool_value(self):
        m = parse_influx_line("maintask_loop v=T 444")
        assert m.fields["v"] is True

    def test_negative_timestamp(self):
        m = parse_influx_line("temp_mcu v=44i -834")
        assert m.timestamp == -834

    def test_no_timestamp(self):
        m = parse_influx_line("temp_mcu v=44i")
        assert m.timestamp is None

    def test_empty_line(self):
        assert parse_influx_line("") is None
        assert parse_influx_line("  ") is None

    def test_xbe_fan(self):
        m = parse_influx_line("xbe_fan,fan=1 pwm=128i,rpm=3000i 11688")
        assert m.name == "xbe_fan"
        assert m.tags == {"fan": "1"}
        assert m.fields["pwm"] == 128
        assert m.fields["rpm"] == 3000

    def test_esp_out(self):
        m = parse_influx_line("esp_out sent=9530266i 251")
        assert m.fields["sent"] == 9530266


class TestParseSyslogHeader:
    def test_standard_header(self):
        raw = "<14>1 - 00:00:5e:00:53:00 buddy - - - msg=53110,tm=1309866606,v=4 temp_mcu v=44i -834"
        header, content = parse_syslog_header(raw)
        assert header["seq"] == 53110
        assert header["timestamp_us"] == 1309866606
        assert content == "temp_mcu v=44i -834"

    def test_no_header(self):
        raw = "temp_mcu v=44i 625"
        header, content = parse_syslog_header(raw)
        assert header == {}
        assert content == raw

    def test_multiline_packet(self):
        raw = "<14>1 - MAC buddy - - - msg=1,tm=100,v=4 temp_mcu v=44i -1\npos_x v=242.0 100\n"
        header, content = parse_syslog_header(raw)
        assert header["seq"] == 1
        lines = content.strip().split("\n")
        assert len(lines) == 2


class TestMetricsBuffer:
    def test_update_and_get(self):
        buf = MetricsBuffer()
        m = Metric(name="temp_bed", tags={}, fields={"v": 60.1}, timestamp=100)
        buf.update(m)
        assert buf.get("temp_bed") is m

    def test_tagged_key(self):
        buf = MetricsBuffer()
        m = Metric(name="temp_noz", tags={"n": "0", "a": "1"}, fields={"value": 215.2})
        buf.update(m)
        assert buf.get("temp_noz,a=1,n=0") is m  # tags sorted alphabetically

    def test_get_field(self):
        buf = MetricsBuffer()
        buf.update(Metric(name="volt_bed", tags={}, fields={"v": 24.06}))
        assert buf.get_field("volt_bed") == pytest.approx(24.06)
        assert buf.get_field("nonexistent") is None

    def test_overwrite(self):
        buf = MetricsBuffer()
        buf.update(Metric(name="temp_bed", tags={}, fields={"v": 60.0}))
        buf.update(Metric(name="temp_bed", tags={}, fields={"v": 61.0}))
        assert buf.get_field("temp_bed") == 61.0

    def test_stats(self):
        buf = MetricsBuffer()
        buf.update(Metric(name="a", tags={}, fields={"v": 1}))
        buf.update(Metric(name="b", tags={}, fields={"v": 2}))
        buf.record_packet(42)
        stats = buf.stats
        assert stats["unique_metrics"] == 2
        assert stats["packets_received"] == 1
        assert stats["last_seq"] == 42
