"""Unit tests for the pure functions of bdwn_portable."""
# pylint: disable=missing-function-docstring,import-error
import os
import sys
from pathlib import Path

import pytest

import bdwn_portable as bp


def make_config(**overrides) -> bp.Config:
    values = {
        "user": "alice",
        "host": "backup.example.org",
        "path": "/srv/restic/alice",
        "key_path": "/home/alice/.ssh/id_ed25519",
        "restic_password": "s3cret-passphrase",
        "paths": ["/home/alice/docs", "/home/alice/photos"],
    }
    values.update(overrides)
    return bp.Config(**values)


# --- config file -----------------------------------------------------------

def test_save_load_config_roundtrip_returns_equal_config(tmp_path):
    cfg = make_config(port=2222, excludes=["*.tmp"], use_vss=True, lang="fr")
    path = tmp_path / "sub" / "config.json"

    bp.save_config(cfg, path)

    assert bp.load_config(path) == cfg


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_save_config_posix_sets_private_file_mode(tmp_path):
    path = tmp_path / "conf" / "config.json"

    bp.save_config(make_config(), path)

    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_save_config_posix_sets_private_dir_mode(tmp_path):
    path = tmp_path / "conf" / "config.json"

    bp.save_config(make_config(), path)

    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_load_config_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        '{"user": "bob", "host": "h", "path": "/p", "key_path": "/k",'
        ' "restic_password": "pw", "unexpected": 42}',
        encoding="utf-8",
    )

    cfg = bp.load_config(path)

    assert (cfg.user, cfg.port, cfg.keep_daily) == ("bob", 22, 7)


def test_load_config_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        bp.load_config(tmp_path / "absent.json")


def test_config_defaults_excludes_is_copy_of_default_excludes():
    cfg = make_config()
    cfg.excludes.append("added-by-test")

    assert "added-by-test" not in bp.DEFAULT_EXCLUDES


# --- default_config_dir ----------------------------------------------------

def test_default_config_dir_windows_user_uses_appdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "C:\\Users\\alice\\AppData\\Roaming")

    result = bp.default_config_dir(system=False)

    assert str(result).replace("\\", "/") == "C:/Users/alice/AppData/Roaming/bdwn-portable"


def test_default_config_dir_windows_system_uses_programdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ProgramData", "C:\\ProgramData")

    result = bp.default_config_dir(system=True)

    assert str(result).replace("\\", "/") == "C:/ProgramData/bdwn-portable"


def test_default_config_dir_linux_user_uses_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert bp.default_config_dir(system=False) == tmp_path / "bdwn-portable"


def test_default_config_dir_linux_user_without_xdg_uses_dot_config(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert bp.default_config_dir(system=False) == tmp_path / ".config" / "bdwn-portable"


def test_default_config_dir_linux_system_uses_etc(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    assert bp.default_config_dir(system=True) == Path("/etc/bdwn-portable")


# --- parse_server_url ------------------------------------------------------

@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sftp://alice@host/srv/repo", ("alice", "host", 22, "/srv/repo")),
        ("sftp://alice@host:2222/srv/repo", ("alice", "host", 2222, "/srv/repo")),
        ("alice@host:/srv/repo", ("alice", "host", 22, "/srv/repo")),
        ("alice@host:repo", ("alice", "host", 22, "repo")),
    ],
)
def test_parse_server_url_valid_returns_parts(url, expected):
    assert bp.parse_server_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "sftp://host/srv/repo",
        "sftp://@host/srv/repo",
        "sftp://alice@/srv/repo",
        "sftp://alice@host",
        "sftp://alice@host:notaport/srv/repo",
        "host:/srv/repo",
        "alice@host:",
        "alice@host",
    ],
)
def test_parse_server_url_invalid_raises_value_error(url):
    with pytest.raises(ValueError):
        bp.parse_server_url(url)


# --- restic / ssh ----------------------------------------------------------

def test_repository_url_uses_ssh_alias_and_path():
    assert bp.repository_url(make_config()) == "sftp:bdwn-portable-target:/srv/restic/alice"


def test_ssh_config_text_contains_required_directives(tmp_path):
    text = bp.ssh_config_text(make_config(port=2222), tmp_path / "known_hosts")

    for expected in (
        "Host bdwn-portable-target",
        "HostName backup.example.org",
        "User alice",
        "Port 2222",
        "IdentityFile",
        "IdentitiesOnly yes",
        "UserKnownHostsFile",
        "StrictHostKeyChecking accept-new",
        "BatchMode yes",
    ):
        assert expected in text


def test_ssh_config_text_paths_with_spaces_are_double_quoted(tmp_path):
    key = "/home/my user/.ssh/id key"
    known_hosts = tmp_path / "dir with space" / "known_hosts"

    text = bp.ssh_config_text(make_config(key_path=key), known_hosts)

    assert f'"{key}"' in text and f'"{known_hosts}"' in text


def test_restic_env_sets_repository_and_password(monkeypatch):
    monkeypatch.setenv("BDWN_TEST_MARKER", "kept")
    cfg = make_config()

    env = bp.restic_env(cfg)

    assert (env["RESTIC_REPOSITORY"], env["RESTIC_PASSWORD"], env["BDWN_TEST_MARKER"]) == (
        bp.repository_url(cfg), "s3cret-passphrase", "kept")


def test_restic_env_does_not_modify_os_environ():
    bp.restic_env(make_config())

    assert os.environ.get("RESTIC_PASSWORD") != "s3cret-passphrase"


@pytest.mark.parametrize("on_windows", [False, True])
def test_restic_args_never_contain_password(on_windows):
    cfg = make_config(use_vss=True)

    args = bp.backup_args(cfg, on_windows) + bp.forget_args(cfg)

    assert not any("s3cret-passphrase" in arg for arg in args)


def test_backup_args_contains_tag_excludes_and_paths():
    cfg = make_config(excludes=["*.tmp", "cache"])

    args = bp.backup_args(cfg, on_windows=False)

    assert args[0] == "backup"
    assert "--tag" in args and "bdwn-portable" in args
    pairs = [args[i + 1] for i, arg in enumerate(args) if arg == "--exclude"]
    assert pairs == ["*.tmp", "cache"]
    assert all(path in args for path in cfg.paths)


@pytest.mark.parametrize(
    ("on_windows", "use_vss", "expected"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_backup_args_fs_snapshot_only_on_windows_with_vss(on_windows, use_vss, expected):
    args = bp.backup_args(make_config(use_vss=use_vss), on_windows)

    assert ("--use-fs-snapshot" in args) is expected


def test_forget_args_uses_retention_settings():
    cfg = make_config(keep_daily=3, keep_weekly=2, keep_monthly=12)

    assert bp.forget_args(cfg) == [
        "forget", "--prune", "--tag", "bdwn-portable",
        "--keep-daily", "3", "--keep-weekly", "2", "--keep-monthly", "12",
    ]


# --- messages --------------------------------------------------------------

def test_messages_fr_and_en_have_same_keys():
    assert set(bp.MESSAGES["fr"]) == set(bp.MESSAGES["en"])


def test_messages_values_are_ascii():
    non_ascii = [
        (lang, key)
        for lang, table in bp.MESSAGES.items()
        for key, value in table.items()
        if not value.isascii()
    ]

    assert not non_ascii


def test_t_unknown_lang_falls_back_to_english():
    key = next(k for k, v in bp.MESSAGES["en"].items() if "{" not in v)

    assert bp.t(key, "xx") == bp.t(key, "en")


def test_t_extra_kwargs_are_ignored():
    key, template = next(
        (k, v) for k, v in bp.MESSAGES["en"].items() if "{" not in v
    )

    assert bp.t(key, "en", unused="ignored") == template


# --- list helpers ----------------------------------------------------------

def test_add_item_new_value_is_stripped_and_appended():
    items = ["/a"]

    assert bp.add_item(items, "  /b  ") is True and items == ["/a", "/b"]


@pytest.mark.parametrize("value", ["", "   ", "/a", " /a "])
def test_add_item_empty_or_duplicate_returns_false(value):
    items = ["/a"]

    assert bp.add_item(items, value) is False and items == ["/a"]


def test_remove_item_returns_removed_value():
    items = ["/a", "/b", "/c"]

    assert bp.remove_item(items, 1) == "/b" and items == ["/a", "/c"]


@pytest.mark.parametrize("index", [-1, 2, 10])
def test_remove_item_out_of_range_raises_index_error(index):
    items = ["/a", "/b"]

    with pytest.raises(IndexError):
        bp.remove_item(items, index)


def test_remove_item_empty_list_raises_index_error():
    with pytest.raises(IndexError):
        bp.remove_item([], 0)


def test_replace_item_replaces_with_stripped_value():
    items = ["/a", "/b"]

    bp.replace_item(items, 0, " /z ")

    assert items == ["/z", "/b"]


def test_replace_item_out_of_range_raises_index_error():
    with pytest.raises(IndexError):
        bp.replace_item(["/a"], 1, "/z")


def test_replace_item_empty_value_raises_value_error():
    items = ["/a"]

    with pytest.raises(ValueError):
        bp.replace_item(items, 0, "   ")


# --- passphrase ------------------------------------------------------------

def test_generate_passphrase_is_long():
    assert len(bp.generate_passphrase()) >= 32


def test_generate_passphrase_differs_between_calls():
    assert bp.generate_passphrase() != bp.generate_passphrase()


# --- run_with_retries ------------------------------------------------------

class FakeTask:
    """Callable failing a given number of times before succeeding."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self.failures


def test_run_with_retries_first_success_no_sleep():
    task, sleeps = FakeTask(0), []

    result = bp.run_with_retries(task, attempts=3, delay=5, sleep=sleeps.append)

    assert (result, task.calls, sleeps) == (True, 1, [])


def test_run_with_retries_success_after_failures_sleeps_between():
    task, sleeps = FakeTask(2), []

    result = bp.run_with_retries(task, attempts=3, delay=5, sleep=sleeps.append)

    assert (result, task.calls, sleeps) == (True, 3, [5, 5])


def test_run_with_retries_all_failures_no_sleep_after_last():
    task, sleeps = FakeTask(99), []

    result = bp.run_with_retries(task, attempts=3, delay=2.5, sleep=sleeps.append)

    assert (result, task.calls, sleeps) == (False, 3, [2.5, 2.5])


# --- scheduler generation --------------------------------------------------

def test_systemd_units_service_runs_scheduled_backup(tmp_path):
    script, config_path = tmp_path / "bdwn_portable.py", tmp_path / "config.json"

    service, _ = bp.systemd_units(make_config(), "/usr/bin/python3", script, config_path)

    exec_line = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    assert "Type=oneshot" in service
    for part in ("/usr/bin/python3", str(script), "--config", str(config_path),
                 "backup", "--scheduled"):
        assert part in exec_line


def test_systemd_units_timer_uses_schedule_time(tmp_path):
    cfg = make_config(schedule_time="03:15")

    _, timer = bp.systemd_units(cfg, "/usr/bin/python3", tmp_path / "s.py", tmp_path / "c.json")

    for expected in ("OnCalendar=*-*-* 03:15:00", "Persistent=true", "WantedBy=timers.target"):
        assert expected in timer


def test_systemd_units_default_schedule_is_12_30(tmp_path):
    _, timer = bp.systemd_units(make_config(), "python3", tmp_path / "s.py", tmp_path / "c.json")

    assert "OnCalendar=*-*-* 12:30:00" in timer


def test_windows_task_script_contains_task_definition(tmp_path):
    cfg = make_config(schedule_time="09:45")

    text = bp.windows_task_script(cfg, "C:\\Python\\python.exe", tmp_path / "s.py",
                                  tmp_path / "c.json")

    for expected in ("Register-ScheduledTask", "bdwn-portable", "-Daily", "09:45",
                     "-AtLogOn", "-StartWhenAvailable", "backup --scheduled"):
        assert expected in text


@pytest.mark.parametrize("system", [False, True])
def test_windows_task_script_system_account_only_when_system(tmp_path, system):
    cfg = make_config(system=system)

    text = bp.windows_task_script(cfg, "python.exe", tmp_path / "s.py", tmp_path / "c.json")

    assert ("SYSTEM" in text) is system


# --- cli -------------------------------------------------------------------

def test_main_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        bp.main(["--help"])

    assert exc.value.code == 0 and capsys.readouterr().out


def test_version_is_semver():
    parts = bp.VERSION.split(".")

    assert len(parts) == 3 and all(part.isdigit() for part in parts)
