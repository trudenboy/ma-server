[English](#english) | [Русский](#русский)

---

<a name="english"></a>

# Music Assistant Server — Custom Fork

This is a custom fork of [music-assistant/server](https://github.com/music-assistant/server),
maintained as an integration base for a set of custom providers for Russian streaming services.

## Providers

| Provider | Repository | Type | Issues | Changelog |
|----------|-----------|------|--------|-----------|
| Yandex Music | [ma-provider-yandex-music](https://github.com/trudenboy/ma-provider-yandex-music) | Music | [Issues →](https://github.com/trudenboy/ma-provider-yandex-music/issues) | [Changelog →](https://github.com/trudenboy/ma-provider-yandex-music/blob/dev/CHANGELOG.md) |
| KION Music | [ma-provider-kion-music](https://github.com/trudenboy/ma-provider-kion-music) | Music | [Issues →](https://github.com/trudenboy/ma-provider-kion-music/issues) | [Changelog →](https://github.com/trudenboy/ma-provider-kion-music/blob/dev/CHANGELOG.md) |
| Zvuk Music | [ma-provider-zvuk-music](https://github.com/trudenboy/ma-provider-zvuk-music) | Music | [Issues →](https://github.com/trudenboy/ma-provider-zvuk-music/issues) | [Changelog →](https://github.com/trudenboy/ma-provider-zvuk-music/blob/dev/CHANGELOG.md) |
| MSX Bridge | [ma-provider-msx-bridge](https://github.com/trudenboy/ma-provider-msx-bridge) | Player | [Issues →](https://github.com/trudenboy/ma-provider-msx-bridge/issues) | [Changelog →](https://github.com/trudenboy/ma-provider-msx-bridge/blob/feat/msx-bridge-player-provider/CHANGELOG.md) |

> **Do not open issues in this repository.** File them in the affected provider's repo (see Issues column above).
> For upstream Music Assistant bugs use the [official support repo](https://github.com/music-assistant/support/issues).

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

## Running via Home Assistant Addon

The official [Music Assistant DEV SERVER addon](https://github.com/music-assistant/home-assistant-addon/tree/main/music_assistant_dev)
supports a `server_repo` option that installs directly from any fork or branch — no Docker build required.

**1. Add the addon repository to Home Assistant:**

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmusic-assistant%2Fhome-assistant-addon)

**2. Install "Music Assistant DEV SERVER"** from the addon store.

**3. Set `server_repo` in the addon configuration:**

| Goal | `server_repo` value |
|------|-------------------|
| Fork, default branch (`dev`) | `trudenboy/ma-server` |
| Fork, specific branch | `trudenboy/ma-server@integration/dev` |
| Fork, specific branch | `trudenboy/ma-server@stable` |

```yaml
# addon configuration
server_repo: "trudenboy/ma-server@integration/dev"
```

**4. Start the addon.** It will install the fork's code on startup and start MA normally.

> The addon re-installs from source on every restart, so it always picks up the latest commits
> from the specified branch. No manual steps needed after a branch update.

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

**Or use Docker Compose** — create a `docker-compose.yml` in the cloned repo:

```yaml
services:
  ma:
    image: ghcr.io/music-assistant/server:nightly
    container_name: music-assistant
    ports:
      - "8095:8095"
      - "8097:8097"
    privileged: true
    restart: unless-stopped
    volumes:
      - ./music_assistant:/mnt/ma-fork:ro
      - ma-data:/data
    entrypoint: /bin/sh
    command: >-
      -c 'cp -r /mnt/ma-fork/. "$(/app/venv/bin/python3 -c
      "import music_assistant,os; print(os.path.dirname(music_assistant.__file__))")/"
      && exec /usr/local/bin/entrypoint.sh --data-dir /data --cache-dir /data/.cache'

volumes:
  ma-data:
```

```bash
docker compose up -d          # start in background
docker compose logs -f        # follow logs
docker compose down           # stop
docker compose down -v        # stop and wipe data
```

## How Providers Are Integrated

See [trudenboy/ma-provider-tools](https://github.com/trudenboy/ma-provider-tools) for the full CI/CD architecture, wrapper distribution system, and incident pipeline.

---

<a name="русский"></a>

# Music Assistant Server — Кастомный форк

Кастомный форк [music-assistant/server](https://github.com/music-assistant/server),
служащий базой интеграции провайдеров для российских стриминговых сервисов.

## Провайдеры

| Провайдер | Репозиторий | Тип | Задачи | Changelog |
|-----------|------------|-----|--------|-----------|
| Яндекс Музыка | [ma-provider-yandex-music](https://github.com/trudenboy/ma-provider-yandex-music) | Музыка | [Issues →](https://github.com/trudenboy/ma-provider-yandex-music/issues) | [Changelog →](https://github.com/trudenboy/ma-provider-yandex-music/blob/dev/CHANGELOG.md) |
| KION Музыка | [ma-provider-kion-music](https://github.com/trudenboy/ma-provider-kion-music) | Музыка | [Issues →](https://github.com/trudenboy/ma-provider-kion-music/issues) | [Changelog →](https://github.com/trudenboy/ma-provider-kion-music/blob/dev/CHANGELOG.md) |
| Звук | [ma-provider-zvuk-music](https://github.com/trudenboy/ma-provider-zvuk-music) | Музыка | [Issues →](https://github.com/trudenboy/ma-provider-zvuk-music/issues) | [Changelog →](https://github.com/trudenboy/ma-provider-zvuk-music/blob/dev/CHANGELOG.md) |
| MSX Bridge | [ma-provider-msx-bridge](https://github.com/trudenboy/ma-provider-msx-bridge) | Плеер | [Issues →](https://github.com/trudenboy/ma-provider-msx-bridge/issues) | [Changelog →](https://github.com/trudenboy/ma-provider-msx-bridge/blob/feat/msx-bridge-player-provider/CHANGELOG.md) |

> **Не открывай задачи в этом репозитории.** Заводи их в репозитории конкретного провайдера (столбец «Задачи»).
> Для багов в upstream Music Assistant используй [официальный репозиторий поддержки](https://github.com/music-assistant/support/issues).

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

## Запуск через аддон Home Assistant

Официальный [аддон Music Assistant DEV SERVER](https://github.com/music-assistant/home-assistant-addon/tree/main/music_assistant_dev)
поддерживает опцию `server_repo`, которая устанавливает код напрямую из любого форка или ветки — без сборки Docker-образа.

**1. Добавь репозиторий аддонов в Home Assistant:**

[![Добавить репозиторий](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmusic-assistant%2Fhome-assistant-addon)

**2. Установи "Music Assistant DEV SERVER"** из магазина аддонов.

**3. Укажи `server_repo` в конфигурации аддона:**

| Цель | Значение `server_repo` |
|------|----------------------|
| Форк, ветка по умолчанию (`dev`) | `trudenboy/ma-server` |
| Форк, конкретная ветка | `trudenboy/ma-server@integration/dev` |
| Форк, конкретная ветка | `trudenboy/ma-server@stable` |

```yaml
# конфигурация аддона
server_repo: "trudenboy/ma-server@integration/dev"
```

**4. Запусти аддон.** При старте он установит код из форка и запустит MA в штатном режиме.

> Аддон переустанавливает пакет из исходников при каждом перезапуске — всегда подхватывает
> последние коммиты из указанной ветки. Никаких ручных действий после обновления ветки не нужно.

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

**Или используй Docker Compose** — создай `docker-compose.yml` в директории клонированного репо:

```yaml
services:
  ma:
    image: ghcr.io/music-assistant/server:nightly
    container_name: music-assistant
    ports:
      - "8095:8095"
      - "8097:8097"
    privileged: true
    restart: unless-stopped
    volumes:
      - ./music_assistant:/mnt/ma-fork:ro
      - ma-data:/data
    entrypoint: /bin/sh
    command: >-
      -c 'cp -r /mnt/ma-fork/. "$(/app/venv/bin/python3 -c
      "import music_assistant,os; print(os.path.dirname(music_assistant.__file__))")/"
      && exec /usr/local/bin/entrypoint.sh --data-dir /data --cache-dir /data/.cache'

volumes:
  ma-data:
```

```bash
docker compose up -d          # запустить в фоне
docker compose logs -f        # следить за логами
docker compose down           # остановить
docker compose down -v        # остановить и сбросить данные
```

## Как провайдеры интегрируются

Полная архитектура CI/CD, система дистрибуции файлов и пайплайн инцидентов описаны в [trudenboy/ma-provider-tools](https://github.com/trudenboy/ma-provider-tools).
