"""Verified TLS and build protection when a native bundle is relocated."""
import asyncio
import os
from pathlib import Path
import shutil
import ssl
import subprocess

import certifi
import pytest

from server import llm


@pytest.fixture(autouse=True)
def fresh_tls(monkeypatch):
    monkeypatch.setattr(llm, "_tls_context", None)


def test_missing_ssl_ca_has_no_filename(tmp_path):
    with pytest.raises(FileNotFoundError) as caught:
        ssl.create_default_context(cafile=str(tmp_path / "missing.pem"))
    assert caught.value.filename is None


def test_cached_roots_survive_ca_removal(tmp_path, monkeypatch):
    ca = tmp_path / "cacert.pem"
    shutil.copyfile(certifi.where(), ca)
    monkeypatch.setattr(certifi, "where", lambda: str(ca))
    context = llm._verified_tls_context()
    ca.unlink()
    assert llm._verified_tls_context() is context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.get_ca_certs()


def test_first_request_after_bundle_move_uses_relocated_roots(tmp_path, monkeypatch):
    runtime = tmp_path / "FinAgent.app" / "Contents" / "Resources" / "runtime"
    ca = runtime / "venv/lib/python3.13/site-packages/certifi/cacert.pem"
    ca.parent.mkdir(parents=True)
    shutil.copyfile(certifi.where(), ca)
    monkeypatch.setattr(certifi, "where", lambda: str(ca))
    monkeypatch.chdir(runtime)
    moved = tmp_path / ".Trash" / "FinAgent.app-backup"
    moved.parent.mkdir()
    runtime.parents[2].rename(moved)
    assert not ca.exists()
    context = llm._verified_tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.get_ca_certs()


@pytest.mark.parametrize("operation", ["models", "stream", "complete"])
def test_missing_roots_give_actionable_error_before_network(tmp_path, monkeypatch, operation):
    cwd = tmp_path / ".Trash" / "FinAgent.app-backup"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    ca = tmp_path / "missing-runtime/cacert.pem"
    monkeypatch.setattr(certifi, "where", lambda: str(ca))
    async def request():
        if operation == "models":
            return await llm.list_models("https://invalid.example", "secret-test-key")
        if operation == "complete":
            return await llm.complete("https://invalid.example", "secret-test-key", "test", [])
        return [piece async for piece in llm.stream_chat(
            "https://invalid.example", "secret-test-key", "test", [])]
    with pytest.raises(llm.LLMError) as caught:
        asyncio.run(request())
    message = str(caught.value)
    assert str(ca) in message
    assert "运行期间被移动或重新构建" in message
    assert "secret-test-key" not in message
    assert isinstance(caught.value.__cause__, FileNotFoundError)


def test_build_blocks_exact_running_app_before_moving_it(tmp_path):
    project = tmp_path / "project with spaces"
    build = project / "build"
    build.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "macos/build_app.sh"
    script = build / "build_app.sh"
    shutil.copyfile(source, script)
    app = project / "衡知.app"
    app.mkdir()
    marker = app / "still-running"
    marker.write_text("preserved")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ps = fake_bin / "ps"
    fake_ps.write_text('#!/bin/sh\nprintf " 21347 %s\\n" "$FAKE_NATIVE_COMMAND"\n')
    fake_ps.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}",
               FAKE_NATIVE_COMMAND=str(app / "Contents/MacOS/FinAgent"))
    result = subprocess.run(["/bin/zsh", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "当前应用仍在运行" in result.stderr
    assert "21347" in result.stderr
    assert marker.read_text() == "preserved"
    assert not (build / "runtime").exists()
