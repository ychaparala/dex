# Security

dex never runs the command it prints, but it does run a CLI install when you answer y, and it treats model output
as untrusted. If you find a way around either (a generated command that dex runs, an install that doesn't come from
dex's own table, a destructive command printed without a warning, or anything that reaches the network), please
report it privately through GitHub's [private vulnerability reporting](https://github.com/ychaparala/dex/security/advisories/new)
rather than in a public issue.

Only the latest release gets fixes.
