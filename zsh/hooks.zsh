# Record each interactive command through `zh _hook`.
# The first precmd hook saves the exit code before any other hook can replace it.
# HISTFILE and keybindings are left alone. A missing zh command does not stop the shell.

typeset -g ZH_ACTIVE_ID=""
unset ZH_SAVED_OUT ZH_SAVED_ERR ZH_CAPTURE_PID

# Ask the recorder to store what it has copied. A second call does nothing.
_zh_stop_capture() {
  [[ ${ZH_CAPTURE_PID:-} == <-> ]] || return 0
  kill -USR1 "$ZH_CAPTURE_PID" 2>/dev/null || true
  unset ZH_CAPTURE_PID
}

# Put the shell's stdout and stderr back, then stop the recorder.
_zh_restore_stdio() {
  if [[ -n ${ZH_SAVED_OUT:-} ]]; then
    exec >&${ZH_SAVED_OUT} 2>&${ZH_SAVED_ERR}
    exec {ZH_SAVED_OUT}>&- {ZH_SAVED_ERR}>&-
    unset ZH_SAVED_OUT ZH_SAVED_ERR
  fi
  _zh_stop_capture
}

_zh_save_exit() {
  ZH_LAST_EXIT=$?
  _zh_restore_stdio
  return $ZH_LAST_EXIT
}

# Point later command output at a PTY slave. The recorder mirrors it to the terminal.
_zh_capture_redirect() {
  local slave="$1"
  [[ -n $slave && -c $slave ]] || return 1
  [[ $slave == /dev/* && $slave != *..* && $slave != *$'\n'* ]] || return 1
  exec {ZH_SAVED_OUT}>&1 {ZH_SAVED_ERR}>&2
  if ! exec >"$slave" 2>&1; then
    exec >&${ZH_SAVED_OUT} 2>&${ZH_SAVED_ERR}
    exec {ZH_SAVED_OUT}>&- {ZH_SAVED_ERR}>&-
    unset ZH_SAVED_OUT ZH_SAVED_ERR
    return 1
  fi
  return 0
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
  local result id slave pid rest
  if [[ -t 1 && -t 2 ]]; then
    result="$(
      print -r -- "$command_text" | command zh _hook start \
        --session "$ZH_SESSION_ID" \
        --cwd "$PWD" \
        --capture 2>/dev/null
    )" || return 0
  else
    result="$(
      print -r -- "$command_text" | command zh _hook start \
        --session "$ZH_SESSION_ID" \
        --cwd "$PWD" 2>/dev/null
    )" || return 0
  fi
  id="${result%%$'\n'*}"
  slave=""
  pid=""
  if [[ $result == *$'\n'* ]]; then
    rest="${result#*$'\n'}"
    slave="${rest%%$'\n'*}"
    if [[ $rest == *$'\n'* ]]; then
      pid="${rest#*$'\n'}"
    fi
  fi
  [[ -z $id ]] && return 0
  ZH_ACTIVE_ID="$id"
  if [[ $pid == <-> ]]; then
    ZH_CAPTURE_PID="$pid"
  fi
  _zh_capture_redirect "$slave" || _zh_stop_capture
}

zh_precmd() {
  local exit_code=$?
  _zh_restore_stdio
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
    # &! disowns the kick so job control does not print a job line.
    command zh _hook sync-kick >/dev/null 2>&1 &!
  fi
  return "$exit_code"
}

if [[ -z ${(M)precmd_functions:#_zh_save_exit} ]]; then
  precmd_functions=(_zh_save_exit ${precmd_functions:+$precmd_functions})
fi
