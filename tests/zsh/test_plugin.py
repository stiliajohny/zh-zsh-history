"""Tests for the session-only Zsh plugin loader."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "zsh" / "zh.plugin.zsh"


def run_zsh(script: str) -> subprocess.CompletedProcess[str]:
    """Run a Zsh script without the user startup files."""
    env = os.environ.copy()
    env.pop("ZH_PLUGIN_LOADED", None)
    env.pop("ZH_PLUGIN_DIR", None)
    return subprocess.run(
        ["zsh", "-f", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def require_zsh() -> None:
    """Stop these tests when the zsh binary is absent."""
    if shutil.which("zsh") is None:
        pytest.fail("zsh is required to load zh.plugin.zsh")


def test_plugin_file_parses(require_zsh: None) -> None:
    """The plugin file is valid Zsh."""
    result = run_zsh(f"zsh -n {shlex.quote(str(PLUGIN))}")
    assert result.returncode == 0, result.stderr


def test_source_is_idempotent_and_leaves_shell_state(require_zsh: None) -> None:
    """Sourcing twice adds the ZH hooks once and leaves keybindings and HISTFILE unchanged."""
    plugin = shlex.quote(str(PLUGIN))
    script = f"""
    typeset -a preexec_functions precmd_functions
    preexec_functions=(keeper_preexec)
    precmd_functions=(keeper_precmd)
    before=$(bindkey -L)
    hist=${{HISTFILE-}}
    source {plugin}
    ZH_PLUGIN_DIR=/tmp/zh-sentinel
    source {plugin}
    after=$(bindkey -L)
    print -r -- "loaded=$ZH_PLUGIN_LOADED"
    print -r -- "dir=$ZH_PLUGIN_DIR"
    print -r -- "preexec=${{(j:,:)preexec_functions}}"
    print -r -- "precmd=${{(j:,:)precmd_functions}}"
    [[ $before == $after ]] && print -r -- "bindkey=same"
    [[ ${{HISTFILE-}} == $hist ]] && print -r -- "histfile=same"
    """
    result = run_zsh(script)
    assert result.returncode == 0, result.stderr
    lines = set(result.stdout.splitlines())
    assert "loaded=1" in lines
    assert "dir=/tmp/zh-sentinel" in lines
    assert "preexec=keeper_preexec,zh_preexec" in lines
    assert "precmd=_zh_save_exit,keeper_precmd,zh_precmd" in lines
    assert "bindkey=same" in lines
    assert "histfile=same" in lines


def test_first_source_records_the_plugin_directory(require_zsh: None) -> None:
    """The first source records the directory that contains the plugin file."""
    plugin = shlex.quote(str(PLUGIN))
    result = run_zsh(f"source {plugin}; print -r -- $ZH_PLUGIN_DIR")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(PLUGIN.parent.resolve())


def test_oh_my_zsh_plugin_name_loads_the_same_file(require_zsh: None, tmp_path: Path) -> None:
    """Oh My Zsh plugin name zh sources the linked zh.plugin.zsh."""
    custom = tmp_path / "custom"
    plugin_dir = custom / "plugins" / "zh"
    plugin_dir.mkdir(parents=True)
    link = plugin_dir / "zh.plugin.zsh"
    link.symlink_to(PLUGIN)
    custom_quoted = shlex.quote(str(custom))
    script = f"""
    plugins=(zh)
    ZSH_CUSTOM={custom_quoted}
    for plugin in $plugins; do
      source "$ZSH_CUSTOM/plugins/$plugin/$plugin.plugin.zsh"
    done
    print -r -- $ZH_PLUGIN_LOADED
    print -r -- $ZH_PLUGIN_DIR
    """
    result = run_zsh(script)
    assert result.returncode == 0, result.stderr
    loaded, directory = result.stdout.splitlines()
    assert loaded == "1"
    assert directory == str(PLUGIN.parent.resolve())
