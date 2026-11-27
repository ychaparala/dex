# oh-my-zsh plugin for dex.
#   ln -s /path/to/dex/shell/zsh "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/dex"
#   then add dex to plugins=(...) in ~/.zshrc
# The setting (DEX_KEY) is described in src/dex/shell/dex.zsh.

if [[ -r ${0:A:h}/../../src/dex/shell/dex.zsh ]]; then
  source ${0:A:h}/../../src/dex/shell/dex.zsh   # a checkout of the repo
elif (( $+commands[dex] )); then
  () {
    local code
    code=$(command dex --init zsh 2>/dev/null) || return
    # Evaluate only what dex itself prints, never whatever else is called dex.
    [[ $code == '# dex zsh integration:'* ]] && eval "$code"
  }
fi
