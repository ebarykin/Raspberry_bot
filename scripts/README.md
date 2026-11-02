# scripts

`call_get_capabilities_loop.py` — вызов `/robot/get_capabilities` через rosbridge раз в секунду. Шлюз должен уже работать на `ws://127.0.0.1:9090`.

```bash
pip install websockets
python3 scripts/call_get_capabilities_loop.py
```

Остановка — Ctrl+C. Позже здесь же появятся `build.sh`, `check.sh` и `logs.sh`.
