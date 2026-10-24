# Author: Kai0421
"""BOT_HOST: pick the machine the bot runs on (mac or windows) and install its scheduler."""

from __future__ import annotations

import pytest

from bot import host


@pytest.mark.parametrize(
    ("platform", "expected"), [("darwin", "mac"), ("win32", "windows"), ("linux", "linux")]
)
def test_current_os(platform, expected):
    assert host.current_os(platform) == expected


def test_configured_host_defaults_to_this_machine():
    assert host.configured_host({}, "darwin") == "mac"
    assert host.configured_host({"BOT_HOST": ""}, "win32") == "windows"
    assert host.configured_host({"BOT_HOST": " Windows "}, "win32") == "windows"


def test_unknown_host_is_refused():
    with pytest.raises(host.HostError, match="BOT_HOST"):
        host.configured_host({"BOT_HOST": "linux"}, "linux")


def test_mismatch_is_refused_so_the_wrong_machine_never_trades():
    assert host.host_mismatch({"BOT_HOST": "mac"}, "darwin") is None
    assert host.host_mismatch({}, "linux") is None  # unset: GitHub Actions / tests
    msg = host.host_mismatch({"BOT_HOST": "windows"}, "darwin")
    assert msg is not None and "windows" in msg and "mac" in msg


def test_install_command_per_host(tmp_path):
    mac = host.scheduler_command("mac", tmp_path, uninstall=False, overnight_shadow=False)
    assert mac == ["/bin/bash", str(tmp_path / "scripts" / "install_launchd.sh")]
    mac_un = host.scheduler_command("mac", tmp_path, uninstall=True, overnight_shadow=True)
    assert mac_un[-2:] == ["--overnight-shadow", "--uninstall"]
    win = host.scheduler_command("windows", tmp_path, uninstall=True, overnight_shadow=False)
    assert win[:4] == ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"]
    assert win[4:6] == ["-File", str(tmp_path / "scripts" / "install_windows_task.ps1")]
    assert win[-1] == "-Uninstall"


def test_main_refuses_install_on_the_wrong_machine(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(host.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    monkeypatch.setattr(host.sys, "platform", "darwin")
    monkeypatch.setenv("BOT_HOST", "windows")
    assert host.main(["install"], repo=tmp_path) == 2
    assert calls == []
    assert "BOT_HOST" in capsys.readouterr().err


def test_main_runs_the_hosts_installer(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(host.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    monkeypatch.setattr(host.sys, "platform", "win32")
    monkeypatch.delenv("BOT_HOST", raising=False)
    assert host.main(["install", "--overnight-shadow"], repo=tmp_path) == 0
    assert calls and calls[0][-1] == "-OvernightShadow"


def test_engine_refuses_to_run_on_the_wrong_machine(monkeypatch, capsys):
    from bot import engine

    def boom():
        raise AssertionError("engine must not start")

    monkeypatch.setattr(engine.Engine, "from_env", staticmethod(boom))
    monkeypatch.setattr(host.sys, "platform", "win32")
    monkeypatch.setenv("BOT_HOST", "mac")
    assert engine.main(["tick"]) == 2
    assert "BOT_HOST=mac" in capsys.readouterr().err
