import os
from pathlib import Path
from dotenv import load_dotenv


def initialize():
    load_dotenv(os.getenv("MANAGER_ENV_FILE", ".env"))
    return Path(os.getenv("MANAGER_DATA_DIR", "data/manager"))
