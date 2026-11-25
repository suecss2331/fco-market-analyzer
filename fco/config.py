"""설정 로딩. 모든 기준값은 config/settings.json 에서 읽는다."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 테스트용: FCO_CONFIG_DIR / FCO_DB_PATH 로 다른 설정·DB 를 쓰게 할 수 있다 (실제 데이터를 건드리지 않기 위해)
CONFIG_DIR = Path(os.environ["FCO_CONFIG_DIR"]) if os.environ.get("FCO_CONFIG_DIR") else ROOT / "config"
DB_PATH = Path(os.environ["FCO_DB_PATH"]) if os.environ.get("FCO_DB_PATH") else ROOT / "data" / "fco.sqlite3"
DATA_DIR = DB_PATH.parent
BACKUP_DIR = DATA_DIR / "backups"


def load_settings() -> dict:
    with open(CONFIG_DIR / "settings.json", encoding="utf-8") as f:
        return json.load(f)


def load_api_key() -> str:
    path = CONFIG_DIR / "secrets.local.json"
    if not path.exists():
        raise SystemExit("config/secrets.local.json 이 없습니다. NEXON Open API 키를 넣어 주세요.")
    with open(path, encoding="utf-8") as f:
        key = json.load(f).get("nexonOpenApiKey", "").strip()
    if not key:
        raise SystemExit("config/secrets.local.json 에 nexonOpenApiKey 가 비어 있습니다.")
    return key


def active_price_range(settings: dict) -> tuple[int, int, str]:
    pr = settings["priceRange"]
    name = pr["activePreset"]
    p = pr["presets"][name]
    return int(p["minBp"]), int(p["maxBp"]), name
