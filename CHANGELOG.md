# Changelog

## Unreleased

- A grammar download that stalls no longer hangs every command. Grammars a
  run needs are fetched first, in a child process killed after 120 s; stderr
  names them, and a grammar that could not be had skips its files and is
  tried again on the next run (#5, syalia-srl/aegis#618).

## v0.1.0

- First release as its own package. The map was `aegis map` in aegis up to
  2.11; it brings its history, the receiver types, `--exclude`, and the
  `metrics`, `cycles`, `unused` and `diff` views.
