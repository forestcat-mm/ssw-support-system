#!/usr/bin/env python3
"""手元の .env を作る・補う（make env）。Mac 標準の python3 だけで動く（標準ライブラリのみ）。

- .env が無ければ .env.example をコピーする。
- .env.example にあって .env に無いキーを追加する（値は .env.example のもの）。
- 値が REPLACE_ME で始まるキーのうち、生成できるもの（秘密鍵・暗号化鍵）を生成した値で埋める。
- 既存の値は変えない。生成した値は画面に出さない。
"""

import base64
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"
PLACEHOLDER = "REPLACE_ME"


def _urlsafe_32_bytes() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


GENERATORS = {
    "DJANGO_SECRET_KEY": lambda: secrets.token_urlsafe(50),
    "FIELD_ENCRYPTION_KEYS": _urlsafe_32_bytes,  # Fernet の鍵の形式
    "BLIND_INDEX_KEY": _urlsafe_32_bytes,
}

LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def parse(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in map(LINE.match, text.splitlines()) if m}


def main() -> int:
    if not TARGET.exists():
        TARGET.write_text(EXAMPLE.read_text())
        print(f"created {TARGET.name} from {EXAMPLE.name}")
    text = TARGET.read_text()
    current = parse(text)

    added = [key for key in parse(EXAMPLE.read_text()) if key not in current]
    if added:
        example = parse(EXAMPLE.read_text())
        lines = "\n".join(f"{key}={example[key]}" for key in added)
        text = text.rstrip("\n") + "\n\n# --- make env で追加 ---\n" + lines + "\n"
        print("added keys:", ", ".join(added))

    generated = []
    for key, generate in GENERATORS.items():
        if parse(text).get(key, "").startswith(PLACEHOLDER):
            text = re.sub(rf"^{key}=.*$", f"{key}={generate()}", text, flags=re.M)
            generated.append(key)
    if generated:
        print("generated values for:", ", ".join(generated))

    TARGET.write_text(text)
    remaining = [k for k, v in parse(text).items() if v.startswith(PLACEHOLDER)]
    if remaining:
        print("still REPLACE_ME (set manually):", ", ".join(remaining))
        return 1
    print(f"{TARGET.name} is ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
