from __future__ import annotations

from wallee_v6.packs.prusa_core_one_plus.adapters import (
    PrusaCoreOneSettings,
    PrintableFile,
    flatten_prusalink_file_tree,
    parse_influx_line,
    parse_m105_response,
    quote_printer_path,
)


def test_parse_m105_response_extracts_all_supported_channels():
    text = "ok T:215.00/215.00 B:60.00/60.00 X:36.00/36.00 A:25.00/0.00 @:80 B@:50 C@:31 HBR@:255"
    parsed = parse_m105_response(text)

    assert parsed["temp_nozzle_c"] == 215.0
    assert parsed["target_nozzle_c"] == 215.0
    assert parsed["temp_bed_c"] == 60.0
    assert parsed["target_bed_c"] == 60.0
    assert parsed["temp_chamber_c"] == 36.0
    assert parsed["temp_ambient_c"] == 25.0
    assert parsed["heater_nozzle_pwm"] == 80.0
    assert parsed["heater_bed_pwm"] == 50.0
    assert parsed["heater_chamber_pwm"] == 31.0
    assert parsed["fan_heatbreak_pwm"] == 255.0


def test_parse_influx_line_parses_numeric_and_boolean_fields():
    measurement, fields = parse_influx_line(
        "printer_metrics,source=coreone temp_nozzle=215.2,temp_bed=60.1,printing=true,fan_hotend=6039i 1711800000"
    )

    assert measurement == "printer_metrics"
    assert fields == {
        "temp_nozzle": 215.2,
        "temp_bed": 60.1,
        "printing": True,
        "fan_hotend": 6039,
    }


def test_flatten_prusalink_file_tree_handles_nested_directories_and_filters_non_printables():
    payload = {
        "path": "/usb",
        "children": [
            {
                "name": "PRINTS",
                "path": "/usb/PRINTS",
                "children": [
                    {
                        "name": "BENCHY~2.BGC",
                        "display_name": "Benchy Rules.bgcode",
                        "path": "/usb/PRINTS/BENCHY~2.BGC",
                        "size": 1200000,
                    }
                ],
            },
            {
                "name": "README.TXT",
                "display_name": "README.TXT",
                "path": "/usb/README.TXT",
                "size": 128,
            },
        ],
    }

    files = flatten_prusalink_file_tree(payload)
    assert files == [
        PrintableFile(
            path="/usb/PRINTS/BENCHY~2.BGC",
            display_name="Benchy Rules.bgcode",
            size_bytes=1200000,
            printable=True,
        )
    ]


def test_quote_printer_path_accepts_multiple_input_forms():
    assert quote_printer_path("/usb/PRINTS/BENCHY~2.BGC") == "PRINTS/BENCHY~2.BGC"
    assert quote_printer_path("usb/PRINTS/BENCHY~2.BGC") == "PRINTS/BENCHY~2.BGC"
    assert quote_printer_path("PRINTS/Benchy Rules.bgcode") == "PRINTS/Benchy%20Rules.bgcode"


def test_settings_from_env_adds_http_scheme(monkeypatch):
    monkeypatch.setenv("PRUSA_CORE_ONE_HOST", "192.168.0.195")
    settings = PrusaCoreOneSettings.from_env()
    assert settings.host == "http://192.168.0.195"


def test_settings_from_env_supports_v5_host_alias(monkeypatch):
    monkeypatch.delenv("PRUSA_CORE_ONE_HOST", raising=False)
    monkeypatch.setenv("PRUSALINK_HOST", "192.168.0.196")

    settings = PrusaCoreOneSettings.from_env()

    assert settings.host == "http://192.168.0.196"


def test_settings_from_env_supports_v5_api_key_alias(monkeypatch):
    monkeypatch.delenv("PRUSA_CORE_ONE_API_KEY", raising=False)
    monkeypatch.setenv("PRUSALINK_API_KEY", "legacy-token")

    settings = PrusaCoreOneSettings.from_env()

    assert settings.api_key == "legacy-token"


def test_settings_from_env_prefers_v6_host_over_alias(monkeypatch):
    monkeypatch.setenv("PRUSA_CORE_ONE_HOST", "http://printer-v6.local")
    monkeypatch.setenv("PRUSALINK_HOST", "http://printer-v5.local")

    settings = PrusaCoreOneSettings.from_env()

    assert settings.host == "http://printer-v6.local"


def test_settings_from_env_prefers_v6_api_key_over_alias(monkeypatch):
    monkeypatch.setenv("PRUSA_CORE_ONE_API_KEY", "v6-token")
    monkeypatch.setenv("PRUSALINK_API_KEY", "v5-token")

    settings = PrusaCoreOneSettings.from_env()

    assert settings.api_key == "v6-token"
