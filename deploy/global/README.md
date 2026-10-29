# Vikunja на Global

Форк: https://github.com/evgeny-babaev/vikunja, ветка `deploy/global`.
Основа: upstream `v2.7.1`. Код приложения не изменён; запускается официальный
образ 2.7.1 с зафиксированным digest. Для будущих изменений приложения нужно
собрать собственный образ из форка и изменить `image` в Compose.

Адрес: https://tasks.45.38.3.79.sslip.io/
MCP: https://tasks.45.38.3.79.sslip.io/api/v2/mcp

Сервер: Global, 45.38.3.79. SSH-доступ хранится в общем локальном Env, не в Git.
Checkout: `/opt/apps/vikunja`. Секрет: `/opt/configs/vikunja/production.env`.
Данные SQLite и вложения: `/opt/data/vikunja`.
HTTPS настроен через существующий Traefik file provider:
`/data/coolify/proxy/dynamic/vikunja-global.yaml`.
Это Compose-развёртывание рядом с Coolify; ресурс в панели Coolify не создаётся.

Регистрация, публичные ссылки, CalDAV, Unsplash и почта выключены.
Исходящий трафик сетевым экраном не блокируется. Полный аудит кода не проводился.
У контейнера нет Docker socket, ключей SSH и доступа к каталогам других приложений.

## Обновление

После проверки изменений и резервной копии:

```sh
cd /opt/apps/vikunja
git pull --ff-only
sudo sh deploy/global/validate-env.sh
sudo docker compose config --quiet
sudo docker compose up -d
sudo docker compose ps
```

После изменения production.env контейнер нужно пересоздать:
`sudo docker compose up -d --force-recreate`.

Для локального запуска создайте игнорируемый `docker-compose.override.yml`:
замените env_file локальным `.env`, bind mount локальным каталогом данных,
публичный URL на `http://localhost:3456/`, добавьте порт `3456:3456`,
для сети coolify укажите `external: false`.

Токены MCP создаются в Settings → MCP отдельно для каждого клиента.
Не храните токены и пароли в Git. SMTP не настроен, восстановление пароля через email недоступно.

## Резервные копии

`deploy/global/backup.sh` сохраняет SQLite и вложения в `/opt/backups/vikunja`.
На время копирования контейнер кратковременно останавливается, затем запускается.
Копии старше 14 дней удаляются. Запуск настроен через cron на 02:15 UTC
(05:15 по Москве). Это локальная копия на том же VPS, не защита от потери сервера.
Для восстановления остановите контейнер, сохраните текущий каталог данных,
распакуйте выбранную копию в `/opt/data`, затем запустите контейнер.
