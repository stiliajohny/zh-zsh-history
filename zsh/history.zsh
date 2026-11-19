# Import remote commands into this shell's history list.
# The staged file is NUL-delimited command text. print -s adds it to the
# session history used by Up/Down and Ctrl+R. HISTFILE is not opened here.
# If the shell stops after print -s and before mark-history, the next prompt
# reads the same file again and those lines can appear twice in that session.

zh_refresh_history() {
  emulate -L zsh
  if ! (( $+commands[zh] )); then
    return 0
  fi
  if [[ -z ${ZH_SESSION_ID:-} ]]; then
    return 0
  fi
  local file command_text
  file="$(command zh _hook pending-history --session "$ZH_SESSION_ID" 2>/dev/null)" || return 0
  if [[ -z "$file" || ! -f "$file" ]]; then
    return 0
  fi
  while IFS= read -r -d '' command_text; do
    if [[ -n "$command_text" ]]; then
      print -s -- "$command_text"
    fi
  done < "$file"
  command zh _hook mark-history --session "$ZH_SESSION_ID" --file "$file" >/dev/null 2>&1 || true
}
