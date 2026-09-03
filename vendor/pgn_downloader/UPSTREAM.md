# Vendored pgn-downloader

Source: https://github.com/Simske/pgn-downloader

Copied from the local sibling repository at commit:
`d133caef155244b12e04b225ad5e1a9321d9fc1c`

Authors declared upstream: Simske and Chessdjw.

`__init__.py`, `lichess.py`, `chess_com.py`, and `date_parser.py` are unchanged source copies. `COPYING` is the upstream GNU General Public License, version 3. CLI/packaging modules are unnecessary because this GUI calls the Python provider functions directly.

All GUI adaptations live in `app/downloader.py`. They add identifying application headers, explicit response formats, bounded and validated HTTPS redirects, timeouts, checked HTTP responses, safe endpoint validation, UTF-8 output, case-insensitive Chess.com player comparisons, progress reporting, and handling of empty stream chunks. The original repository is not changed or required at runtime.
