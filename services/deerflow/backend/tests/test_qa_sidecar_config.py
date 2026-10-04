"""配置页配置项对 DeerFlow 问答链生效的回归测试。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app import qa_server
from extensions import kb_tools


class TestWriteConfigAndPersona:
    def test_top_p_written_when_provided(self, tmp_path, monkeypatch):
        target = tmp_path / "config.yaml"
        monkeypatch.setattr(qa_server, "CONFIG_PATH", target)
        qa_server.write_config({"model": "m", "api_key": "k", "base_url": "http://x/v1",
                                "temperature": 0, "top_p": 0.85, "max_tokens": 4096})
        text = target.read_text(encoding="utf-8")
        assert "top_p: 0.85" in text

    def test_top_p_omitted_when_absent(self, tmp_path, monkeypatch):
        target = tmp_path / "config.yaml"
        monkeypatch.setattr(qa_server, "CONFIG_PATH", target)
        qa_server.write_config({"model": "m", "api_key": "k", "base_url": "http://x/v1",
                                "temperature": 0, "max_tokens": 4096})
        assert "top_p" not in target.read_text(encoding="utf-8")

    def test_persona_replaces_agent_name(self, tmp_path, monkeypatch):
        custom = tmp_path / "persona.custom.md"
        custom.write_text("你是「杰克百晓生」，助手。", encoding="utf-8")
        monkeypatch.setattr(qa_server, "PERSONA_CUSTOM_PATH", custom)
        monkeypatch.setattr(qa_server, "_BOOT_AGENT_NAME", "小杰")
        assert "小杰" in qa_server._effective_persona()
        monkeypatch.setattr(qa_server, "_BOOT_AGENT_NAME", "")
        assert "杰克百晓生" in qa_server._effective_persona()
