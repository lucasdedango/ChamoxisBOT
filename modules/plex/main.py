import os
from pathlib import Path
from dotenv import load_dotenv
import uvicorn
from filelock import FileLock
from chamoxis_common.logging import configure


def main():
    load_dotenv(os.getenv("PLEX_ENV_FILE", ".env"))
    configure(Path(os.getenv("PLEX_DATA_DIR", "data/plex")) / "plex.log")
    from modules.plex.api.server import create_app
    app = create_app()
    with FileLock(str(Path(os.getenv("PLEX_DATA_DIR", "data/plex")) / "plex.lock"), timeout=0):
        uvicorn.run(app, host=os.getenv("PLEX_MODULE_HOST", "127.0.0.1"),
                    port=int(os.getenv("PLEX_MODULE_PORT", "8761")), access_log=False)


if __name__ == "__main__":
    main()
