[English](#english) | [Русский](#русский)

---

<a name="english"></a>

# Music Assistant Server — Custom Fork

This is a custom fork of [music-assistant/server](https://github.com/music-assistant/server),
maintained as an integration base for a set of custom providers for Russian streaming services.

## Providers

| Provider | Repository | Type |
|----------|-----------|------|
| Yandex Music | [ma-provider-yandex-music](https://github.com/trudenboy/ma-provider-yandex-music) | Music |
| KION Music | [ma-provider-kion-music](https://github.com/trudenboy/ma-provider-kion-music) | Music |
| Zvuk Music | [ma-provider-zvuk-music](https://github.com/trudenboy/ma-provider-zvuk-music) | Music |
| MSX Bridge | [ma-provider-msx-bridge](https://github.com/trudenboy/ma-provider-msx-bridge) | Player |

## Branch Structure

| Branch | Purpose |
|--------|---------|
| `stable` | Production-stable. Tracks upstream stable releases. Provider releases are cut from here. |
| `dev` | Active development. Provider code lands here after testing. **Default branch.** |
| `integration/dev` | Staging. All provider `dev` branches are merged here for full-stack integration testing. |
| `integration/pending-upstream` | Upstream patches or PRs awaiting merge into official Music Assistant. |
| `upstream/msx_bridge` | Upstream-tracking branch for MSX Bridge. |
| `feat/msx-bridge-player-provider` | Active MSX Bridge feature branch. |

> `copilot/*` and `backport/*` branches are created automatically by CI — they can be ignored.

## Running with Docker

Clone any branch and start Music Assistant with the fork's code applied on top of the official nightly image — no build step required.

```bash
# 1. Clone the desired branch
git clone -b integration/dev https://github.com/trudenboy/ma-server.git
cd ma-server

# 2. Run
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
docker logs -f music-assistant        # follow logs
docker stop music-assistant           # stop
docker rm music-assistant             # remove container (data volume is preserved)
docker volume rm ma-data              # wipe persistent data
```

> To switch branches: `git checkout <branch>`, then stop and recreate the container.

## How Providers Are Integrated

Each provider repo has a `sync-to-fork.yml` workflow. On every release it rsyncs the provider
files into `music_assistant/providers/<domain>/` in this fork and opens a PR against `dev`.

```
provider repo (release) → sync-to-fork.yml → PR → dev → stable
```

The `integration/dev` branch always contains the latest `dev` of all providers merged together,
and is used for end-to-end testing before individual providers are released.

## Where to File Incidents

> **Do not open issues in this repository.**

File issues in the affected provider's repository:

| Provider | Issues |
|----------|--------|
| Yandex Music | [Issues →](https://github.com/trudenboy/ma-provider-yandex-music/issues) |
| KION Music | [Issues →](https://github.com/trudenboy/ma-provider-kion-music/issues) |
| Zvuk Music | [Issues →](https://github.com/trudenboy/ma-provider-zvuk-music/issues) |
| MSX Bridge | [Issues →](https://github.com/trudenboy/ma-provider-msx-bridge/issues) |

For upstream Music Assistant bugs use the [official support repo](https://github.com/music-assistant/support/issues).

## Upstream

This fork tracks [music-assistant/server](https://github.com/music-assistant/server).
Upstream changes are pulled into `integration/pending-upstream` and merged into `dev` after validation.

---

<a name="русский"></a>

# Music Assistant Server — Кастомный форк

Кастомный форк [music-assistant/server](https://github.com/music-assistant/server),
служащий базой интеграции провайдеров для российских стриминговых сервисов.

## Провайдеры

| Провайдер | Репозиторий | Тип |
|-----------|------------|-----|
| Яндекс Музыка | [ma-provider-yandex-music](https://github.com/trudenboy/ma-provider-yandex-music) | Музыка |
| KION Музыка | [ma-provider-kion-music](https://github.com/trudenboy/ma-provider-kion-music) | Музыка |
| Звук | [ma-provider-zvuk-music](https://github.com/trudenboy/ma-provider-zvuk-music) | Музыка |
| MSX Bridge | [ma-provider-msx-bridge](https://github.com/trudenboy/ma-provider-msx-bridge) | Плеер |

## Структура веток

| Ветка | Назначение |
|-------|-----------|
| `stable` | Production-стабильная. Отслеживает upstream stable-релизы. Здесь выпускаются релизы провайдеров. |
| `dev` | Активная разработка. Код провайдеров приземляется сюда после тестирования. **Ветка по умолчанию.** |
| `integration/dev` | Стейджинг. Dev-ветки всех провайдеров объединяются здесь для полного интеграционного тестирования. |
| `integration/pending-upstream` | Upstream-патчи или PR, ожидающие слияния в официальный Music Assistant. |
| `upstream/msx_bridge` | Upstream-ветка для отслеживания MSX Bridge. |
| `feat/msx-bridge-player-provider` | Активная feature-ветка MSX Bridge. |

> Ветки `copilot/*` и `backport/*` создаются автоматически CI-пайплайном — их можно игнорировать.

## Запуск через Docker

Клонируй любую ветку и запусти Music Assistant с кодом форка поверх официального nightly-образа — без сборки.

```bash
# 1. Клонируй нужную ветку
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
docker logs -f music-assistant        # следить за логами
docker stop music-assistant           # остановить
docker rm music-assistant             # удалить контейнер (том с данными сохраняется)
docker volume rm ma-data              # сбросить постоянные данные
```

> Чтобы переключить ветку: `git checkout <branch>`, затем останови и пересоздай контейнер.

## Как провайдеры интегрируются

В каждом репозитории провайдера есть workflow `sync-to-fork.yml`. При каждом релизе он rsync-ит
файлы провайдера в `music_assistant/providers/<domain>/` этого форка и открывает PR в ветку `dev`.

```
репозиторий провайдера (релиз) → sync-to-fork.yml → PR → dev → stable
```

Ветка `integration/dev` всегда содержит последние dev-ветки всех провайдеров, объединённые вместе,
и используется для сквозного тестирования до выпуска отдельных провайдеров.

## Где заводить инциденты

> **Не открывай задачи в этом репозитории.**

Заводи задачи в репозитории конкретного провайдера:

| Провайдер | Задачи |
|-----------|--------|
| Яндекс Музыка | [Issues →](https://github.com/trudenboy/ma-provider-yandex-music/issues) |
| KION Музыка | [Issues →](https://github.com/trudenboy/ma-provider-kion-music/issues) |
| Звук | [Issues →](https://github.com/trudenboy/ma-provider-zvuk-music/issues) |
| MSX Bridge | [Issues →](https://github.com/trudenboy/ma-provider-msx-bridge/issues) |

Для багов в upstream Music Assistant используй [официальный репозиторий поддержки](https://github.com/music-assistant/support/issues).

## Upstream

Этот форк отслеживает [music-assistant/server](https://github.com/music-assistant/server).
Изменения из upstream подтягиваются в `integration/pending-upstream` и вливаются в `dev` после валидации.
