
[English](#english) | [Русский](#русский)

---

<a name="english"></a>

# Music Assistant Server — Custom Fork

This is a custom fork of [music-assistant/server](https://github.com/music-assistant/server) maintained as a base for the following custom providers:

- **Yandex Music** — [trudenboy/ma-provider-yandex-music](https://github.com/trudenboy/ma-provider-yandex-music)
- **KION Music** — [trudenboy/ma-provider-kion-music](https://github.com/trudenboy/ma-provider-kion-music)
- **Zvuk Music** — [trudenboy/ma-provider-zvuk-music](https://github.com/trudenboy/ma-provider-zvuk-music)
- **MSX Bridge** — [trudenboy/ma-provider-msx-bridge](https://github.com/trudenboy/ma-provider-msx-bridge)

The fork extends the upstream server with provider integrations and stays in sync with upstream releases.

## Branch Structure

| Branch | Purpose |
|--------|---------|
| `stable` | Production-stable. Tracks upstream stable releases. Provider releases are cut from here. |
| `dev` | Active development. Provider code is integrated here after testing. **Default branch.** |
| `integration/dev` | Staging. Provider `dev` branches are merged here for integration testing before `dev`. |
| `integration/pending-upstream` | Holds upstream patches or PRs awaiting merge into official Music Assistant. |
| `upstream/msx_bridge` | Upstream-tracking branch for MSX Bridge player provider. |
| `feat/msx-bridge-player-provider` | Active feature branch for MSX Bridge provider development. |

> `copilot/*` and `backport/*` branches are created automatically by CI — they can be ignored.

## Running with Docker

Clone any branch and start Music Assistant with the fork's code overlaid on the official nightly image:

```bash
# 1. Clone the desired branch (e.g. integration/dev)
git clone -b integration/dev https://github.com/trudenboy/ma-server.git
cd ma-server

# 2. Start
docker run -d \
  --name music-assistant \
  -p 8095:8095 -p 8097:8097 \
  --privileged \
  -v "$(pwd)/music_assistant":/mnt/ma-fork:ro \
  -v ma-data:/data \
  --entrypoint /bin/sh \
  ghcr.io/music-assistant/server:nightly \
  -c 'cp -r /mnt/ma-fork/. "$(/app/venv/bin/python3 -c "import music_assistant,os; print(os.path.dirname(music_assistant.__file__))")/" \
      && exec /usr/local/bin/entrypoint.sh --data-dir /data --cache-dir /data/.cache'
```

Open **http://localhost:8095** in the browser.

```bash
# Useful commands
docker logs -f music-assistant       # follow logs
docker stop music-assistant          # stop
docker rm music-assistant            # remove container (data volume is preserved)
docker volume rm ma-data             # wipe persistent data
```

> The `music_assistant/` directory from the cloned branch is copied over the package inside the
> container at startup. Switch branches with `git checkout <branch>` and recreate the container to apply.

Each provider repo has a `sync-to-fork.yml` workflow. On every release it rsyncs provider files
into `music_assistant/providers/<domain>/` in this fork and opens a PR against `dev`.

```
provider repo (release) → sync-to-fork.yml → PR to this repo (dev branch)
```

## Where to File Incidents

> **Do not open issues in this repository.**

File issues in the affected provider's repository:

| Provider | Issues |
|----------|--------|
| Yandex Music | [Issues →](https://github.com/trudenboy/ma-provider-yandex-music/issues) |
| KION Music | [Issues →](https://github.com/trudenboy/ma-provider-kion-music/issues) |
| Zvuk Music | [Issues →](https://github.com/trudenboy/ma-provider-zvuk-music/issues) |
| MSX Bridge | [Issues →](https://github.com/trudenboy/ma-provider-msx-bridge/issues) |

For upstream Music Assistant bugs, use the [official support repo](https://github.com/music-assistant/support/issues).

## Upstream

This fork tracks [music-assistant/server](https://github.com/music-assistant/server).
Upstream changes are pulled into `integration/pending-upstream` and merged into `dev` after validation.

---

<a name="русский"></a>

# Music Assistant Server — Кастомный форк

Это кастомный форк [music-assistant/server](https://github.com/music-assistant/server), поддерживаемый как основа для следующих кастомных провайдеров:

- **Yandex Music** — [trudenboy/ma-provider-yandex-music](https://github.com/trudenboy/ma-provider-yandex-music)
- **KION Music** — [trudenboy/ma-provider-kion-music](https://github.com/trudenboy/ma-provider-kion-music)
- **Zvuk Music** — [trudenboy/ma-provider-zvuk-music](https://github.com/trudenboy/ma-provider-zvuk-music)
- **MSX Bridge** — [trudenboy/ma-provider-msx-bridge](https://github.com/trudenboy/ma-provider-msx-bridge)

Форк расширяет upstream-сервер интеграциями провайдеров и синхронизируется с upstream-релизами.

## Структура веток

| Ветка | Назначение |
|-------|-----------|
| `stable` | Production-стабильная. Отслеживает upstream stable-релизы. Релизы провайдеров выпускаются отсюда. |
| `dev` | Активная разработка. Код провайдеров интегрируется сюда после тестирования. **Ветка по умолчанию.** |
| `integration/dev` | Стейджинг. Dev-ветки провайдеров объединяются здесь для интеграционного тестирования перед `dev`. |
| `integration/pending-upstream` | Содержит upstream-патчи или PR, ожидающие слияния в официальный Music Assistant. |
| `upstream/msx_bridge` | Upstream-ветка для провайдера MSX Bridge. |
| `feat/msx-bridge-player-provider` | Активная feature-ветка для разработки провайдера MSX Bridge. |

> Ветки `copilot/*` и `backport/*` создаются автоматически CI-пайплайном, их можно игнорировать.

## Запуск через Docker

Клонируй любую ветку и запусти Music Assistant с кодом форка поверх официального nightly-образа:

```bash
# 1. Клонируй нужную ветку (например, integration/dev)
git clone -b integration/dev https://github.com/trudenboy/ma-server.git
cd ma-server

# 2. Запусти
docker run -d \
  --name music-assistant \
  -p 8095:8095 -p 8097:8097 \
  --privileged \
  -v "$(pwd)/music_assistant":/mnt/ma-fork:ro \
  -v ma-data:/data \
  --entrypoint /bin/sh \
  ghcr.io/music-assistant/server:nightly \
  -c 'cp -r /mnt/ma-fork/. "$(/app/venv/bin/python3 -c "import music_assistant,os; print(os.path.dirname(music_assistant.__file__))")/" \
      && exec /usr/local/bin/entrypoint.sh --data-dir /data --cache-dir /data/.cache'
```

Открой **http://localhost:8095** в браузере.

```bash
# Полезные команды
docker logs -f music-assistant       # следить за логами
docker stop music-assistant          # остановить
docker rm music-assistant            # удалить контейнер (том с данными сохраняется)
docker volume rm ma-data             # сбросить постоянные данные
```

> Директория `music_assistant/` из клонированной ветки копируется поверх пакета внутри контейнера
> при старте. Чтобы переключить ветку — сделай `git checkout <branch>` и пересоздай контейнер.

В каждом репозитории провайдера есть workflow `sync-to-fork.yml`. При каждом релизе он rsync-ит
файлы провайдера в `music_assistant/providers/<domain>/` этого форка и открывает PR в ветку `dev`.

```
репозиторий провайдера (релиз) → sync-to-fork.yml → PR в этот репозиторий (ветка dev)
```

## Где заводить инциденты

> **Не открывай задачи в этом репозитории.**

Заводи задачи в репозитории конкретного провайдера:

| Провайдер | Задачи |
|-----------|--------|
| Yandex Music | [Issues →](https://github.com/trudenboy/ma-provider-yandex-music/issues) |
| KION Music | [Issues →](https://github.com/trudenboy/ma-provider-kion-music/issues) |
| Zvuk Music | [Issues →](https://github.com/trudenboy/ma-provider-zvuk-music/issues) |
| MSX Bridge | [Issues →](https://github.com/trudenboy/ma-provider-msx-bridge/issues) |

Для багов в upstream Music Assistant используй [официальный репозиторий поддержки](https://github.com/music-assistant/support/issues).

## Upstream

Этот форк отслеживает [music-assistant/server](https://github.com/music-assistant/server).
Изменения из upstream подтягиваются в `integration/pending-upstream` и вливаются в `dev` после валидации.

