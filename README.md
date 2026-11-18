# AI Hub

Телеграм-бот-агрегатор моделей и OpenAI-совместимый API-шлюз с квотами — на одном
сервере, без внешних платных сервисов (кроме самого апстрима моделей).

## Что делает

- **Бот в Telegram** [@VibeComAI_bot](https://t.me/VibeComAI_bot): чат с любой моделью
  апстрима, выбор модели, выдача/отзыв личного API-ключа, просмотр остатка квоты.
  Во всех разделах меню используется одна иллюстрация-баннер; кнопки меняют
  подпись к фото на месте, а в разделах есть кнопка возврата в меню.
  Bot API long polling, без вебхуков.
- **API-шлюз** `/v1/models`, `/v1/chat/completions` (обычный и streaming) —
  совместим с OpenAI SDK и Claude Code: подставь свой эндпоинт и выданный ключ.
  Эндпоинт: `https://8791-agq2pkibmx77e7svypqdtkt2je.instabox.tech/v1`
- **Квоты**: дневные лимиты запросов и долларов на пользователя; расчёт стоимости
  по `data/pricing.json` (паттерны имён моделей, fallback-тариф). Ключи хранятся
  только SHA-256-хешами. Дневная граница — 03:00 UTC.
- **Биллинг**: usage пишется по каждому запросу; стриминг списывает по usage из
  последнего чанка (или оценке); апстрим-ключ никогда не покидает сервер.

## Запуск локально

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # укажи апстрим и токен бота
.venv/bin/python -m uvicorn app.api:app --port 8791
```

Проверки: `.venv/bin/python -m unittest discover -s tests -v`

## Деплой на сервере

`deploy/ai-hub.service` поднимает шлюз на `0.0.0.0:8791`; публичный адрес —
port publish инстабокса. Файл `.env` на сервере содержит секреты и не выкладывается.

## Структура

| Файл | Назначение |
| --- | --- |
| `app/config.py` | Переменные окружения, порты, лимиты |
| `app/api.py` | FastAPI: сборка, health, запуск поллинга бота |
| `app/gateway.py` | OpenAI-совместимые эндпоинты, квоты, стриминг |
| `app/bot.py` | Telegram Bot API long polling и команды |
| `app/upstream.py` | Клиент апстрима, ретрай-ошибки |
| `app/store.py` | SQLite WAL: пользователи, ключи, usage |
| `app/pricing.py` | Тарифы моделей из `data/pricing.json` |

## Развёрнутая инсталляция

Работает на instabox-сервере рядом с другими сервисами:

- Публичный API: `https://8791-agq2pkibmx77e7svypqdtkt2je.instabox.tech/v1`
- Health: `/health` (используется keepalive для удержания сервера)
- Бот: [@VibeComAI_bot](https://t.me/VibeComAI_bot)
- Апстрим и токен бота — в `/data/workspace/ai-hub/.env` (не в репозитории)

Сменить апстрим = заменить `HUB_UPSTREAM_BASE_URL` и `ALISA_AI_KEY` в `.env`,
затем `sudo systemctl restart ai-hub`.
