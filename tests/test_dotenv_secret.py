import json
import os

import pytest

from rca_agent import local_pilot as local


@pytest.mark.parametrize(
    "data",
    [
        b"OPENAI_API_KEY=SYNTHETIC_ONLY\n",
        b"OPENAI_API_KEY='SYNTHETIC_ONLY'\n",
        b'OPENAI_API_KEY="SYNTHETIC_ONLY" # comment\n',
        b"# comment\n export OPENAI_API_KEY = SYNTHETIC_ONLY  # comment\nOTHER=value\n",
        b"\xef\xbb\xbfOPENAI_API_KEY=SYNTHETIC_ONLY\r\n",
    ],
)
def test_supported_literal_dotenv_syntax(tmp_path, data):
    path = tmp_path / ".env"
    path.write_bytes(data)
    path.chmod(0o600)
    assert local.read_secret(tmp_path) == bytearray(b"SYNTHETIC_ONLY")


@pytest.mark.parametrize(
    "data",
    [
        b"# no key\nOTHER=value\n",
        b"OPENAI_API_KEY=\n",
        b"OPENAI_API_KEY=''\n",
        b"OPENAI_API_KEY\n",
        b"OPENAI_API_KEY='unterminated\n",
        b"OPENAI_API_KEY='SYNTHETIC_ONLY' garbage\n",
        b"OPENAI_API_KEY=SYNTHETIC_ONLY\nexport OPENAI_API_KEY=SECOND\n",
        b"OPENAI_API_KEY='line\nbreak'\n",
        b"OPENAI_API_KEY='contains space'\n",
        b"OPENAI_API_KEY=" + b"x" * 4097,
        b"OPENAI_API_KEY=\xff\n",
        "OPENAI_API_KEY=한글\n".encode(),
    ],
)
def test_invalid_dotenv_returns_only_fixed_errors(tmp_path, data, capsys):
    path = tmp_path / ".env"
    path.write_bytes(data)
    path.chmod(0o600)
    with pytest.raises(local.Blocked) as result:
        local.read_secret(tmp_path)
    assert str(result.value) in {"ENV_KEY_MISSING", "ENV_KEY_INVALID", "ENV_KEY_DUPLICATE"}
    output = capsys.readouterr()
    assert not output.out and not output.err


@pytest.mark.parametrize("change", ["permissions", "symlink", "hardlink", "fifo", "owner", "large"])
def test_unsafe_dotenv_is_rejected_before_read(tmp_path, monkeypatch, change):
    path = tmp_path / ".env"
    path.write_bytes(b"OPENAI_API_KEY=SYNTHETIC_ONLY\n")
    path.chmod(0o600)
    if change == "permissions":
        path.chmod(0o644)
    elif change in ("symlink", "hardlink"):
        original = tmp_path / "original"
        path.rename(original)
        if change == "symlink":
            path.symlink_to(original)
        else:
            os.link(original, path)
    elif change == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif change == "owner":
        uid = os.getuid()
        monkeypatch.setattr(local.os, "getuid", lambda: uid + 1)
    else:
        path.write_bytes(b"x" * 65537)
    monkeypatch.setattr(local.os, "read", lambda *args: pytest.fail("unsafe file read"))
    with pytest.raises(local.Blocked, match="ENV_FILE_UNSAFE"):
        local.read_secret(tmp_path)


def test_no_shell_expansion_or_process_environment_export(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_PROCESS_ENV")
    before = dict(os.environ)
    path = tmp_path / ".env"
    path.write_text("OTHER=$(must-not-execute)\nOPENAI_API_KEY='${SYNTHETIC_LITERAL}'\n")
    path.chmod(0o600)
    monkeypatch.setattr(local.os, "system", lambda *args: pytest.fail("shell executed"))
    monkeypatch.setattr(
        local.subprocess, "run", lambda *args, **kw: pytest.fail("command executed")
    )
    assert local.read_secret(tmp_path) == bytearray(b"${SYNTHETIC_LITERAL}")
    assert dict(os.environ) == before


def test_main_cannot_echo_dotenv_error_contents(monkeypatch, capsys):
    monkeypatch.setattr(local.sys, "argv", ["local_pilot"])
    monkeypatch.setattr(
        local, "prepare", lambda: (_ for _ in ()).throw(ValueError("SYNTHETIC_SECRET_NEVER_PRINT"))
    )
    with pytest.raises(SystemExit):
        local.main()
    text = capsys.readouterr()
    assert "SYNTHETIC_SECRET_NEVER_PRINT" not in text.out + text.err
    assert json.loads(text.out)["status"] == "LIVE_BLOCKED"
