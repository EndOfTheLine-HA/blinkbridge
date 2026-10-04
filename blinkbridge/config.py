import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Union

__all__ = ['COMMON_FFMPEG_ARGS', 'CONFIG', 'DELAY_RESTART', 'RTSP_URL', 'PATH_VIDEOS', 'PATH_CONCAT', 'PATH_CONFIG']

COMMON_FFMPEG_ARGS = [
    '-hide_banner',
    '-loglevel', 'error',
    '-y',
]

CONFIG = None
DELAY_RESTART = None
RTSP_URL = None
PATH_VIDEOS = None
PATH_CONCAT = None
PATH_CONFIG = None

def load_config_file(file_name: Union[str, Path]) -> None:
    global CONFIG, DELAY_RESTART, RTSP_URL, PATH_VIDEOS, PATH_CONCAT, PATH_CONFIG

    file_name = Path(file_name)
    if not file_name.is_file():
        raise FileNotFoundError(
            f"Configuration not found at {file_name}. Copy config/config.example.json "
            "to config/config.json and fill in your own values."
        )

    with open(file_name, encoding='utf-8') as f:
        CONFIG = json.load(f)

    # Environment variables avoid putting the primary Blink password in a
    # tracked example. *_FILE is suitable for container secret mounts.
    login = CONFIG.setdefault('blink', {}).setdefault('login', {})
    for key, env_name in (('username', 'BLINK_USERNAME'), ('password', 'BLINK_PASSWORD')):
        secret_file = os.getenv(f'{env_name}_FILE')
        if secret_file:
            login[key] = Path(secret_file).read_text(encoding='utf-8').strip()
        elif os.getenv(env_name):
            login[key] = os.environ[env_name]

    missing = [key for key in ('username', 'password') if not login.get(key)]
    if missing:
        raise ValueError(
            "Missing Blink login value(s): " + ', '.join(missing) +
            ". Set them in the untracked config/config.json or with BLINK_* variables."
        )

    DELAY_RESTART = timedelta(seconds=CONFIG['cameras']['restart_delay_seconds'])
    RTSP_URL = f'rtsp://{CONFIG['rtsp_server']['address']}:{CONFIG['rtsp_server']['port']}'

    PATH_VIDEOS = Path(CONFIG['paths']['videos'])
    PATH_CONCAT = Path(CONFIG['paths']['concat'])
    PATH_CONFIG = Path(CONFIG['paths']['config'])

config_file = os.getenv('BLINKBRIDGE_CONFIG', 'config.json')
load_config_file(config_file)
