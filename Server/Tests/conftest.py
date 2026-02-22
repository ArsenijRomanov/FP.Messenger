import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent          # .../Server/Tests
SERVER_DIR = TESTS_DIR.parent                        # .../Server
PROJECT_ROOT = SERVER_DIR.parent                     # .../FP.Messenger

# На всякий случай добавляем и Server, и корень проекта
for p in (SERVER_DIR, PROJECT_ROOT):
    p = str(p)
    if p not in sys.path:
        sys.path.insert(0, p)

# Жёсткая проверка: если не видим server.py — значит conftest не там или rootdir не тот
assert (SERVER_DIR / "server.py").exists(), f"server.py not found at: {SERVER_DIR}"
print(f"[conftest] sys.path[0:3]={sys.path[0:3]}")
