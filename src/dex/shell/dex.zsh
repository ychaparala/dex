# dex zsh integration: puts the generated command on your next prompt to edit before you run it.
#
# Load it from ~/.zshrc with:   eval "$(dex --init zsh)"
# or, with oh-my-zsh, use the plugin in shell/zsh of the dex repo.
#
# Two ways to use it:
#   dex first 10 rows of sales.parquet   the command lands on your next prompt
#   type a request, then press Ctrl-X Ctrl-D: the request is replaced by the command
#
# Setting (before this file loads):  DEX_KEY=''  no key binding;  DEX_KEY='^[^M'  Alt-Enter

: ${DEX_KEY='^X^D'}

# Run dex and put its command on the next prompt. Plain dex for info options or when
# stdout isn't a terminal. Only leading options count, as in dex itself.
function dex {
  emulate -L zsh
  local a
  for a in "$@"; do
    case $a in
      --) break ;;
      -h|--help|--version|--show-prompt|--init|--init=*|--stop) command dex "$@"; return ;;
      -*) ;;
      *) break ;;
    esac
  done
  [[ -t 1 ]] || { command dex "$@"; return }
  local out rc
  # Ctrl-C at dex's install question means no; dex handles it, so don't abort here.
  trap ':' INT
  # DEX_ASK=1 lets dex ask whether to install a missing CLI before it answers.
  out=$(DEX_ASK=1 command dex "$@")   # warnings and questions go to the terminal on stderr
  rc=$?
  (( rc == 0 || rc == 3 )) && [[ -n $out ]] && print -rz -- "$out"
  return rc
}
# noglob keeps *, ? and [ in the request away from filename expansion.
alias dex='noglob dex'

# Widget: replace the request on the command line with the command. stderr is kept
# apart, so a warning never ends up in the buffer; it is shown below the prompt.
function _dex_widget {
  emulate -L zsh
  setopt extended_glob
  local request=$BUFFER out err errfile rc
  [[ -n ${request//[[:space:]]/} ]] || return 0
  errfile=$(command mktemp "${TMPDIR:-/tmp}/dex.XXXXXX") || return 1
  zle -R "dex: thinking..."
  out=$(command dex -- "$request" 2>$errfile)
  rc=$?
  err=$(<$errfile)
  command rm -f -- $errfile
  if (( rc == 0 || rc == 3 )) && [[ -n $out ]]; then
    print -rs -- "# dex: $request"   # keep the request in history
    BUFFER=$out
    # Put the cursor on the first <placeholder>, else at the end.
    local before=${BUFFER%%<[A-Za-z][[:alnum:]_.:/-]#>*}
    CURSOR=${#before}
  fi
  zle redisplay
  [[ -n $err ]] && zle -M -- "$err"
  (( rc == 0 || rc == 3 )) || [[ -n $err ]] || zle -M -- "dex: failed (exit $rc)"
  return 0
}
zle -N _dex_widget

if [[ -n $DEX_KEY ]]; then
  bindkey -M emacs "$DEX_KEY" _dex_widget
  bindkey -M viins "$DEX_KEY" _dex_widget
fi

# Completion for the options. They only count before the request.
function _dex {
  _arguments -S -A '-*' \
    '--no-context[send no facts about the local environment]' \
    '--show-prompt[print the prompt instead of generating]' \
    '(-q --quiet)'{-q,--quiet}'[no warnings on stderr]' \
    '--stop[stop the background model process]' \
    '--init[print the shell integration]:shell:(zsh)' \
    '(- *)'{-h,--help}'[show the help]' \
    '(- *)--version[show the version]' \
    '*::request:'
}
(( $+functions[compdef] )) && compdef _dex dex
