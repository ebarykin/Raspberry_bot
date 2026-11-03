# deploy

Базовый `compose.yaml` поднимает `capture`, `gateway` и `webrtc`. `motion-supervisor` и `chassis-controller` ещё не входят.

- `compose.dev.yaml` — ноут: `videotestsrc`, без privileged
- `compose.robot.yaml` — Raspberry Pi: OV5647, privileged (ADR 0009), перезапуск

```bash
docker compose -f compose.yaml -f deploy/compose.dev.yaml up --build
docker compose -f compose.yaml -f deploy/compose.robot.yaml up --no-build
```

На роботе конфиг не монтируют с диска: он уже в образе. Git и rsync на плату не нужны. С ноутбука: `docker --context raspbot compose -f compose.yaml -f deploy/compose.robot.yaml up --build`. В эксплуатации — `--no-build`.
