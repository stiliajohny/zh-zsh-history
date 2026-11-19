# Record each interactive command through `zh _hook`.
# The first precmd hook saves the exit code before any other hook can replace it.
# HISTFILE and keybindings are left alone. A missing zh command does not stop the shell.

typeset -g ZH_ACTIVE_ID=""

_zh_save_exit() {
  ZH_LAST_EXIT=$?
  return $ZH_LAST_EXIT
}

zh_preexec() {
  emulate -L zsh
  local command_text="$1"
  [[ -z $command_text ]] && command_text="$3"
  [[ -z $command_text ]] && return 0
  (( $+commands[zh] )) || return 0
  if [[ -z ${ZH_SESSION_ID:-} ]]; then
    ZH_SESSION_ID="$(command zh _hook session 2>/dev/null)" || return 0
    [[ -z $ZH_SESSION_ID ]] && return 0
  fi
  local id
  id="$(
    print -r -- "$command_text" | command zh _hook start \
      --session "$ZH_SESSION_ID" \
      --cwd "$PWD" 2>/dev/null
  )" || return 0
  [[ -z $id ]] && return 0
  ZH_ACTIVE_ID="$id"
}

zh_precmd() {
  local exit_code=$?
  if [[ -n ${ZH_LAST_EXIT+x} ]]; then
    exit_code=$ZH_LAST_EXIT
    unset ZH_LAST_EXIT
  fi
  emulate -L zsh
  if [[ -n ${ZH_ACTIVE_ID:-} ]]; then
    local id="$ZH_ACTIVE_ID"
    ZH_ACTIVE_ID=""
    command zh _hook finish --id "$id" --exit-code "$exit_code" >/dev/null 2>&1 || true
  fi
  if typeset -f zh_refresh_history >/dev/null; then
    zh_refresh_history
  fi
  if (( $+commands[zh] )); then
    command zh _hook sync-kick >/dev/null 2>&1 &
  fi
  return "$exit_code"
}

if [[ -z ${(M)precmd_functions:#_zh_save_exit} ]]; then
  precmd_functions=(_zh_save_exit ${precmd_functions:+$precmd_functions})
fi
