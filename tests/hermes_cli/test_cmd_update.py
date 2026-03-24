"""Tests for cmd_update — branch fallback when remote branch doesn't exist."""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.main import cmd_update, PROJECT_ROOT


def _make_run_side_effect(branch="main", verify_ok=True, commit_count="0"):
    """Build a side_effect function for subprocess.run that simulates git commands."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        # git rev-parse --abbrev-ref HEAD  (get current branch)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{branch}\n", stderr="")

        # git rev-parse --verify origin/{branch}  (check remote branch exists)
        if "rev-parse" in joined and "--verify" in joined:
            rc = 0 if verify_ok else 128
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

        # git rev-list HEAD..origin/{branch} --count
        if "rev-list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

        # Fallback: return a successful CompletedProcess with empty stdout
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


@pytest.fixture
def mock_args():
    return SimpleNamespace()


class TestCmdUpdateBranchFallback:
    """cmd_update falls back to main when current branch has no remote counterpart."""

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_falls_back_to_main_when_branch_not_on_remote(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(
            branch="fix/stoicneko", verify_ok=False, commit_count="3"
        )

        cmd_update(mock_args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        # rev-list should use origin/main, not origin/fix/stoicneko
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert len(rev_list_cmds) == 1
        assert "origin/main" in rev_list_cmds[0]
        assert "origin/fix/stoicneko" not in rev_list_cmds[0]

        # pull should use main, not fix/stoicneko
        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 1
        assert "main" in pull_cmds[0]

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_uses_current_branch_when_on_remote(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="2"
        )

        cmd_update(mock_args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert len(rev_list_cmds) == 1
        assert "origin/main" in rev_list_cmds[0]

        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 1
        assert "main" in pull_cmds[0]

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_already_up_to_date(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="0"
        )

        cmd_update(mock_args)

        captured = capsys.readouterr()
        assert "Already up to date!" in captured.out

        # Should NOT have called pull
        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 0



def test_update_merges_upstream_and_pushes_origin(monkeypatch, tmp_path, capsys):
    from hermes_cli import config as hermes_config
    from hermes_cli import main as hermes_main

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hermes_main, "_stash_local_changes_if_needed", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_main, "_restore_stashed_changes", lambda *a, **kw: True)
    monkeypatch.setattr(hermes_config, "get_missing_env_vars", lambda required_only=True: [])
    monkeypatch.setattr(hermes_config, "get_missing_config_fields", lambda: [])
    monkeypatch.setattr(hermes_config, "check_config_version", lambda: (5, 5))
    monkeypatch.setattr(hermes_config, "migrate_config", lambda **kw: {"env_added": [], "config_added": []})
    monkeypatch.setattr("shutil.which", lambda name: None)

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        if cmd == ["git", "fetch", "origin"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")
        if cmd == ["git", "rev-parse", "--verify", "origin/main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd == ["git", "rev-list", "HEAD..origin/main", "--count"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        if cmd == ["git", "remote", "get-url", "upstream"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/NousResearch/hermes-agent.git\n", stderr="")
        if cmd == ["git", "fetch", "upstream"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "--verify", "upstream/main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="upstream/main\n", stderr="")
        if cmd == ["git", "rev-list", "HEAD..upstream/main", "--count"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="2\n", stderr="")
        if cmd == ["git", "merge", "--no-ff", "upstream/main", "-m", "Merge upstream/main into main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="merged\n", stderr="")
        if cmd == ["git", "push", "origin", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="pushed\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    hermes_main.cmd_update(SimpleNamespace())

    commands = [" ".join(str(a) for a in cmd) for cmd in recorded]
    assert any(cmd == "git fetch upstream" for cmd in commands)
    assert any(cmd == "git merge --no-ff upstream/main -m Merge upstream/main into main" for cmd in commands)
    assert any(cmd == "git push origin main" for cmd in commands)

