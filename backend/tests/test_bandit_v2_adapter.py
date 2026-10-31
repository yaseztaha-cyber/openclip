"""
Tests for Bandit v2 adapter (Phase 5.3.4).
"""
import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import audio_cleanup.bandit_v2_adapter as bva


def test_check_worker_available():
    status = bva.check_worker_available()
    assert "worker_python_exists" in status
    assert "worker_script_exists" in status
    assert "checkpoint_exists" in status
    assert "checkpoint_verified" in status
    assert "ready" in status


def test_get_model_info():
    info = bva.get_model_info()
    assert info["model_name"] == "Bandit v2 (v2-multi)"
    assert info["checkpoint"] == "checkpoint-multi.ckpt"
    assert info["license"] == "CC-BY-SA-4.0 (weights)"
    assert "capabilities" in info
    assert "speech" in info["capabilities"]


def test_create_worker_script():
    script = bva.create_worker_script()
    assert "def main()" in script
    assert "BanditSession" in script
    assert "v2-multi" in script


def test_separation_result_dataclass():
    r = bva.SeparationResult()
    assert r.success is False
    assert r.speech_path == ""
    assert r.model_name == "bandit_v2"


def test_no_network_for_status():
    import socket
    original = socket.socket.connect
    def blocked(self, *args, **kwargs):
        raise RuntimeError("NETWORK")
    socket.socket.connect = blocked
    try:
        status = bva.check_worker_available()
        assert isinstance(status, dict)
    finally:
        socket.socket.connect = original
