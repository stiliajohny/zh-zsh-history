# Oh My Zsh entry point. Link this directory as custom/plugins/zh
# or source this file directly. Loading it twice is a no-op.

if [[ -n ${ZH_PLUGIN_LOADED:-} ]]; then
  return 0
fi
typeset -g ZH_PLUGIN_LOADED=1

0="${${ZERO:-${0:#$ZSH_ARGZERO}}:-${(%):-%N}}"
0="${${(M)0:#/*}:-$PWD/$0}"
typeset -g ZH_PLUGIN_DIR="${0:A:h}"

source "${ZH_PLUGIN_DIR}/hooks.zsh"
source "${ZH_PLUGIN_DIR}/history.zsh"

autoload -Uz add-zsh-hook
add-zsh-hook preexec zh_preexec
add-zsh-hook precmd zh_precmd

zh_define_alias() {
  emulate -L zsh
  local name="$1"
  local value="$2"
  if (( ${+aliases[$name]} )); then
    return 0
  fi
  alias "$name=$value"
}

zh_define_alias zhs "zh search"
zh_define_alias zht "zh top"
zh_define_alias zhf "zh failed"
zh_define_alias zhr "zh recent"
zh_define_alias zhy "zh stats"
unfunction zh_define_alias

if [[ -d ${ZH_PLUGIN_DIR}/completions ]]; then
  if (( ${fpath[(Ie)${ZH_PLUGIN_DIR}/completions]} == 0 )); then
    fpath=("${ZH_PLUGIN_DIR}/completions" $fpath)
  fi
  if (( $+functions[compdef] )); then
    autoload -Uz _zh
    compdef _zh zh
  fi
fi
