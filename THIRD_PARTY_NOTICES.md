# Third-party notices

This application includes source from **pgn-downloader**, by Simske and Chessdjw:
https://github.com/Simske/pgn-downloader

The included version is commit `d133caef155244b12e04b225ad5e1a9321d9fc1c`. Its original source is under `vendor/pgn_downloader`, with its GPL version 3 license in `COPYING`. This project's `LICENSE` contains the same license. The GUI and adapter source is included in the project so the application can be inspected, modified, and rebuilt.

Runtime dependencies include Python, FastAPI, Starlette, Pydantic, Uvicorn, pywebview, pythonnet, Requests, tqdm, and their dependencies. Their respective notices and licenses remain applicable; the executable builder collects installed package metadata/licenses where provided by its hooks.

Microsoft Edge WebView2 and .NET Framework are system components. They are not installed or redistributed by this app. Lichess and Chess.com are independent services; this app is not affiliated with either organization.

The local adapter deliberately leaves upstream modules unchanged. See `vendor/pgn_downloader/UPSTREAM.md` for the precise source and `app/downloader.py` for the compatibility adjustments.
