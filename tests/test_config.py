"""Tests for config module."""

import os
import tempfile
from pathlib import Path

from wallee.config import load_config, _load_dotenv


class TestLoadDotenv:
    def test_loads_key_value_pairs(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("FOO_TEST_VAR=bar\n")
        # Clean up after
        try:
            _load_dotenv(env_file)
            assert os.environ["FOO_TEST_VAR"] == "bar"
        finally:
            os.environ.pop("FOO_TEST_VAR", None)

    def test_skips_comments_and_blanks(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\n\nKEY_TEST_X=val\n")
        try:
            _load_dotenv(env_file)
            assert os.environ.get("KEY_TEST_X") == "val"
        finally:
            os.environ.pop("KEY_TEST_X", None)

    def test_strips_quotes(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('QUOTED_TEST="hello world"\n')
        try:
            _load_dotenv(env_file)
            assert os.environ["QUOTED_TEST"] == "hello world"
        finally:
            os.environ.pop("QUOTED_TEST", None)

    def test_does_not_overwrite_existing(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("EXISTING_TEST=new\n")
        os.environ["EXISTING_TEST"] = "old"
        try:
            _load_dotenv(env_file)
            assert os.environ["EXISTING_TEST"] == "old"
        finally:
            os.environ.pop("EXISTING_TEST", None)

    def test_missing_file_is_noop(self, tmp_path):
        _load_dotenv(tmp_path / "nonexistent")


class TestLoadConfig:
    def test_defaults(self, tmp_path):
        # Use empty env file so no env vars leak in
        env_file = tmp_path / ".env"
        env_file.write_text("")
        # Clear any relevant env vars
        saved = {}
        for key in ["OPENROUTER_API_KEY", "REDIS_URL", "OPENROUTER_MODEL"]:
            if key in os.environ:
                saved[key] = os.environ.pop(key)
        try:
            cfg = load_config(env_file)
            assert cfg.openrouter_api_key == ""
            assert cfg.redis_url == "redis://localhost:6379"
            assert cfg.openrouter_model == "google/gemini-3.1-pro-preview"
            assert cfg.agent_poll_interval_s == 5.0
        finally:
            os.environ.update(saved)

    def test_reads_from_env_file(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("OPENROUTER_API_KEY=test-key-123\nREDIS_URL=redis://other:1234\n")
        saved = {}
        for key in ["OPENROUTER_API_KEY", "REDIS_URL"]:
            if key in os.environ:
                saved[key] = os.environ.pop(key)
        try:
            cfg = load_config(env_file)
            assert cfg.openrouter_api_key == "test-key-123"
            assert cfg.redis_url == "redis://other:1234"
        finally:
            os.environ.update(saved)
            os.environ.pop("OPENROUTER_API_KEY", None)
            os.environ.pop("REDIS_URL", None)


    def test_reads_telegram_allowed_user_ids(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_ALLOWED_USER_IDS=123, 456 ,789\n")
        saved = {}
        for key in ["TELEGRAM_ALLOWED_USER_IDS"]:
            if key in os.environ:
                saved[key] = os.environ.pop(key)
        try:
            cfg = load_config(env_file)
            assert cfg.telegram_allowed_user_ids == ["123", "456", "789"]
        finally:
            os.environ.update(saved)
            os.environ.pop("TELEGRAM_ALLOWED_USER_IDS", None)
