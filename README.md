Automatic naming for `tmux` windows and sessions upon `<leader>O` ("organize"), powered by `opencode` sub-shells.

Optional window-level rename upon `<leader>T` (idiosyncrasy of my configuration).

## status bar integration

`torganize` sets a tmux session option `@torganize` while it's working, and clears it when done. add a conditional to your `status-right` to display it:

```tmux
set -g status-right '#{?@torganize,#{@torganize} | ,}%H:%M %d-%b-%y'
```

- while organizing: `organizing... | 14:30 24-Feb-26`
- when idle: `14:30 24-Feb-26`
- on failure: `organize failed | 14:30 24-Feb-26` (persists until next run or manual `set-option -u @torganize`)

the `#{?@torganize,...,}` conditional renders nothing when the option is unset, so there's zero visual overhead when torganize isn't running.

`ses_36fa2b174ffemuufp30wSv53cU`
