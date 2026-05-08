"""Tests for the stackchan-mcp CLI entry point.

These tests focus on the no-side-effect command-line flags
(``--help``, ``--version``); full gateway start-up is covered by
``test_stdio_server.py`` and ``test_gateway.py``.
"""

from __future__ import annotations

import pytest

from stackchan_mcp import __version__
from stackchan_mcp.cli import _build_arg_parser, main


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
