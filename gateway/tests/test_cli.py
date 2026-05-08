"""Tests for the stackchan-mcp CLI entry point.

These tests focus on the no-side-effect command-line flags
(``--help``, ``--version``, ``--check``); full gateway start-up is
covered by ``test_stdio_server.py`` and ``test_gateway.py``.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from stackchan_mcp import __version__, cli
from stackchan_mcp.cli import (
    _build_arg_parser,
    _check_port,
    _format_port_status,
    _run_preflight,
    main,
)


_PREFLIGHT_ENV_VARS = (
    "STACKCHAN_TOKEN",
    "BEARER_TOKEN",
    "VISION_HOST",
    "VISION_URL",
    "VISION_TOKEN",
    "HOST",
    "WS_PORT",
    "CAPTURE_PORT",
)


def _isolate_preflight_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Make preflight tests independent of any host ``.env`` / inherited env.

    ``python-dotenv`` resolves ``.env`` via ``find_dotenv()``, which
    walks up the **calling stack frame's** file path — not the cwd —
    so simply ``chdir(tmp_path)`` is not enough to escape a developer's
    real ``gateway/.env``. We instead replace ``cli._load_dotenv`` with
    a no-op for the duration of the test, then strip the relevant env
    vars to give the preflight a deterministic baseline.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    for var in _PREFLIGHT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_arg_parser_help_long_flag(capsys: pytest.CaptureFixture[str]) -> None:
    parser = _build_arg_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    out = captured.out
    # Help text should mention prog name, the headline env vars, and a
    # pointer to the in-tree READMEs so end users know where to look next.
    assert "stackchan-mcp" in out
    assert "STACKCHAN_TOKEN" in out
    assert "VISION_URL" in out
    assert "WS_PORT" in out
    assert "README" in out


def test_arg_parser_help_short_flag() -> None:
    parser = _build_arg_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["-h"])
    assert exc.value.code == 0


def test_arg_parser_version_long_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _build_arg_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    # argparse writes --version output to stdout on Python 3.4+.
    combined = captured.out + captured.err
    assert f"stackchan-mcp {__version__}" in combined


def test_arg_parser_version_short_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _build_arg_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["-V"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert f"stackchan-mcp {__version__}" in combined


def test_main_help_exits_before_side_effects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main(['--help'])`` must exit 0 *before* load_dotenv / asyncio.run.

    The whole point of the new flag is that first-time users can run
    ``stackchan-mcp --help`` without binding port 8765 or waiting on
    stdin, so this regression test guards that contract.
    """
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "stackchan-mcp" in captured.out


def test_version_resolves_from_installed_metadata() -> None:
    """``__version__`` should be sourced from package metadata, not a literal.

    This guards against the previous failure mode where the literal in
    ``stackchan_mcp/__init__.py`` drifted away from
    ``gateway/pyproject.toml`` across releases.
    """
    assert __version__ != "0.0.0+unknown"
    # Expect a SemVer-ish leading digit; the editable install resolves
    # to whatever ``pyproject.toml`` declares.
    assert __version__[:1].isdigit()


# --- --check flag tests -----------------------------------------------------


def test_arg_parser_check_flag_is_registered() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(["--check"])
    assert args.check is True


def test_arg_parser_check_defaults_to_false() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args([])
    assert args.check is False


def test_format_port_status_available() -> None:
    assert _format_port_status(True, None) == "AVAILABLE"


def test_format_port_status_in_use_no_holder() -> None:
    assert _format_port_status(False, None) == "IN USE"


def test_format_port_status_in_use_with_holder() -> None:
    assert (
        _format_port_status(False, "pid 12345, python")
        == "IN USE (pid 12345, python)"
    )


def test_check_port_against_unbound_port_reports_available() -> None:
    """Ask the OS for an ephemeral port, release it, then probe.

    Not perfectly race-free (something else could grab the port between
    ``close()`` and ``_check_port``'s bind), but the window is tiny and
    this gives confidence that ``_check_port`` plays nicely with the
    real socket layer rather than only the mocked variant.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    available, holder = _check_port("127.0.0.1", port)
    assert available is True
    assert holder is None


def test_check_port_against_held_port_reports_in_use() -> None:
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    try:
        port = held.getsockname()[1]
        available, _holder = _check_port("127.0.0.1", port)
        assert available is False
    finally:
        held.close()


def test_run_preflight_with_no_config_reports_defaults_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _isolate_preflight_env(monkeypatch, tmp_path)
    # Don't actually open sockets in the test process.
    monkeypatch.setattr(cli, "_check_port", lambda host, port: (True, None))

    exit_code = _run_preflight()
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "STACKCHAN_TOKEN     not set" in out
    assert "VISION_HOST         not set" in out
    assert "VISION_URL          not set" in out
    assert "VISION_TOKEN        not set" in out
    assert "ws://0.0.0.0:8765" in out
    assert "http://0.0.0.0:8766" in out
    assert "AVAILABLE" in out
    assert "Result: ready. Exit 0." in out


def test_run_preflight_masks_secrets_and_derives_vision_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Tokens must never be echoed; VISION_URL is derived from VISION_HOST."""
    _isolate_preflight_env(monkeypatch, tmp_path)
    monkeypatch.setenv("STACKCHAN_TOKEN", "super-secret-token-value")
    monkeypatch.setenv("VISION_HOST", "192.168.1.42")
    monkeypatch.setenv("VISION_TOKEN", "another-secret-value")
    monkeypatch.setattr(cli, "_check_port", lambda host, port: (True, None))

    exit_code = _run_preflight()
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "super-secret-token-value" not in out
    assert "another-secret-value" not in out
    # Both tokens should be reported as redacted, not as their raw value.
    assert out.count("***redacted***") == 2
    # VISION_HOST is configuration, not a secret, so it is shown as-is.
    assert "VISION_HOST         192.168.1.42" in out
    assert "(derived) http://192.168.1.42:8766/capture" in out


def test_run_preflight_explicit_vision_url_overrides_derivation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _isolate_preflight_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VISION_HOST", "192.168.1.42")
    monkeypatch.setenv("VISION_URL", "https://stackchan.example.ts.net/capture")
    monkeypatch.setattr(cli, "_check_port", lambda host, port: (True, None))

    _run_preflight()
    out = capsys.readouterr().out
    assert "VISION_URL          https://stackchan.example.ts.net/capture" in out
    # The derived line must not appear when an explicit URL is set.
    assert "(derived)" not in out


def test_run_preflight_in_use_ports_return_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _isolate_preflight_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli,
        "_check_port",
        lambda host, port: (False, f"pid 12345, mock-{port}"),
    )

    exit_code = _run_preflight()
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "IN USE (pid 12345, mock-8765)" in out
    assert "IN USE (pid 12345, mock-8766)" in out
    assert "Result: 2 issues. Exit 1." in out


def test_run_preflight_one_in_use_port_singular_phrasing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _isolate_preflight_env(monkeypatch, tmp_path)

    def fake_check(host: str, port: int) -> tuple[bool, str | None]:
        if port == 8765:
            return (False, "pid 999, fake")
        return (True, None)

    monkeypatch.setattr(cli, "_check_port", fake_check)

    exit_code = _run_preflight()
    assert exit_code == 1
    out = capsys.readouterr().out
    # Singular ``issue`` (not ``issues``) when exactly one port is held.
    assert "Result: 1 issue. Exit 1." in out


def test_main_check_flag_runs_preflight_and_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``main(['--check'])`` exits with the preflight return code.

    Guards the contract that ``--check`` never reaches the asyncio
    gateway start-up below the early exit, by relying on ``main`` to
    propagate ``_run_preflight``'s return as ``SystemExit``.
    """
    _isolate_preflight_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_check_port", lambda host, port: (True, None))

    with pytest.raises(SystemExit) as exc:
        main(["--check"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "preflight" in out
    assert "Result: ready" in out


# --- Port resolution tests (must mirror gateway.py) -------------------------


def test_resolve_ws_port_defaults_to_8765(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WS_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    port, source = cli._resolve_ws_port()
    assert port == 8765
    assert source == "default"


def test_resolve_ws_port_prefers_ws_port_over_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WS_PORT", "9000")
    monkeypatch.setenv("PORT", "9001")
    port, source = cli._resolve_ws_port()
    assert port == 9000
    assert source == "WS_PORT"


def test_resolve_ws_port_falls_back_to_PORT(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gateway.py: int(os.getenv("WS_PORT", os.getenv("PORT", "8765")))."""
    monkeypatch.delenv("WS_PORT", raising=False)
    monkeypatch.setenv("PORT", "9001")
    port, source = cli._resolve_ws_port()
    assert port == 9001
    assert source == "PORT"


def test_resolve_ws_port_invalid_value_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WS_PORT", "abc")
    port, source = cli._resolve_ws_port()
    assert port is None
    assert "WS_PORT" in source
    assert "not an integer" in source


def test_resolve_capture_port_defaults_to_8766(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CAPTURE_PORT", raising=False)
    port, source = cli._resolve_capture_port()
    assert port == 8766
    assert source == "default"


def test_resolve_capture_port_invalid_value_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAPTURE_PORT", "not-a-number")
    port, source = cli._resolve_capture_port()
    assert port is None
    assert "CAPTURE_PORT" in source
    assert "not an integer" in source


def test_run_preflight_invalid_ws_port_is_blocking(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``WS_PORT=<garbage>`` must NOT silently fall back to the default.

    The gateway itself wraps the lookup in ``int(...)`` without a
    try/except — silent fallback in preflight would mean reporting
    "ready" for an environment the gateway would actually refuse to
    start. That is the exact failure mode --check is meant to catch.
    """
    _isolate_preflight_env(monkeypatch, tmp_path)
    monkeypatch.setenv("WS_PORT", "not-a-number")
    monkeypatch.setattr(cli, "_check_port", lambda host, port: (True, None))

    exit_code = _run_preflight()
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "INVALID" in out
    assert "WS_PORT" in out
    assert "Result: 1 issue. Exit 1." in out


def test_run_preflight_invalid_capture_port_is_blocking(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _isolate_preflight_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CAPTURE_PORT", "garbage")
    monkeypatch.setattr(cli, "_check_port", lambda host, port: (True, None))

    exit_code = _run_preflight()
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "INVALID" in out
    assert "CAPTURE_PORT" in out


def test_run_preflight_uses_PORT_fallback_for_ws_port(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``PORT=<value>`` must be honored when ``WS_PORT`` is unset.

    ``gateway.py`` resolves ``WS_PORT`` → ``PORT`` → ``8765``, so the
    preflight must check the same port that ``Gateway.start()`` will
    actually bind to.
    """
    _isolate_preflight_env(monkeypatch, tmp_path)
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setattr(cli, "_check_port", lambda host, port: (True, None))

    _run_preflight()
    out = capsys.readouterr().out
    assert "ws://0.0.0.0:9999" in out
    # Capture port still falls through to its own default.
    assert "http://0.0.0.0:8766" in out


def test_run_preflight_ws_and_capture_same_port_is_conflict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``WS_PORT == CAPTURE_PORT`` must be flagged even when the port is free.

    ``_check_port`` binds-and-releases each port independently, so two
    successive probes for the same free port both report AVAILABLE.
    The gateway, however, holds the WebSocket port for the entire
    process lifetime, so a subsequent capture bind would fail. The
    conflict has to be caught at the configuration layer.
    """
    _isolate_preflight_env(monkeypatch, tmp_path)
    monkeypatch.setenv("WS_PORT", "8765")
    monkeypatch.setenv("CAPTURE_PORT", "8765")
    monkeypatch.setattr(cli, "_check_port", lambda host, port: (True, None))

    exit_code = _run_preflight()
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "8765" in out
    assert "distinct ports" in out
