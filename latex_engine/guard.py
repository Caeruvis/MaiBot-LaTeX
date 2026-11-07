"""与配套 Host 补丁共用的回执协议，不依赖 SDK。"""

import hashlib
import json

GUARD_KEY = "caeruvis_latex_guard_v1"


def fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def receipt(stage: str, value: object) -> dict:
    return {GUARD_KEY: stage, "sha256": fingerprint(value)}


def verified(receipts: list, stage: str, value: object) -> bool:
    expected = receipt(stage, value)
    return any(item == expected for item in receipts)
