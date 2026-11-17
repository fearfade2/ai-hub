"""Telegram Bot API long polling: chat with models, keys, quotas, /start."""
from __future__ import annotations

import asyncio

import httpx

from . import config
from .pricing import Pricing
from .upstream import Upstream, UpstreamError


class Bot:
    def __init__(self, store, upstream: Upstream):
        self.store = store
        self.upstream = upstream
        self.offset = 0

    async def api(self, method: str, payload: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(base_url=config.BOT_API_BASE, timeout=40) as client:
                response = await client.post(
                    f"/bot{config.BOT_TOKEN}/{method}", json=payload or {})
                data = response.json()
                if not data.get("ok"):
                    raise RuntimeError(data.get("description", "Telegram error"))
                return data.get("result", {})
        except (httpx.HTTPError, ValueError) as error:
            raise ConnectionError(f"Telegram API: {error}") from error

    async def send(self, chat_id: int, text: str) -> None:
        await self.api("sendMessage", {"chat_id": chat_id, "text": text[:4000]})

    async def start_text(self) -> str:
        base = config.upstream_public()
        return ("AI Hub — доступ к моделям апстрима в Telegram и по API.\n\n"
                "/models — список доступных моделей\n"
                "/model <имя> — выбрать модель для чата\n"
                "/key — выпустить API-ключ (показывается один раз)\n"
                "/revoke — отозвать активный API-ключ\n"
                "/quota — остатки дневной квоты\n"
                "/reset — когда обновится дневной лимит\n\n"
                "Просто напиши сообщение — отвечу выбранной моделью.\n"
                f"API-эндпоинт: {base}/v1 (совместим с OpenAI: chat/completions, models).")

    # -- commands ---------------------------------------------------------
    async def cmd_start(self, chat_id: int, user: dict) -> None:
        is_admin = user.get("id") == config.OWNER_TELEGRAM_ID
        self.store.ensure_user(user["id"], user.get("username", ""), is_admin=is_admin)
        await self.send(chat_id, await self.start_text())

    async def cmd_models(self, chat_id: int, user: dict) -> None:
        self.store.ensure_user(user["id"])
        models = await self.upstream.models()
        if not models:
            await self.send(chat_id, "Список моделей недоступен, попробуй позже.")
            return
        names = [model["id"] for model in models][:100]
        await self.send(chat_id, "Модели апстрима:\n" + "\n".join(f"• {n}" for n in names))

    async def cmd_set_model(self, chat_id: int, user: dict, argument: str) -> None:
        record = self.store.ensure_user(user["id"])
        model = argument.strip()
        if not model:
            await self.send(chat_id,
                           f"Текущая модель: {record['chat_model'] or 'не выбрана'}.\n"
                           "Выбор: /model <имя> из /models.")
            return
        self.store.set_chat_model(user["id"], model)
        await self.send(chat_id, f"Модель чата: {model}")

    async def cmd_key(self, chat_id: int, user: dict) -> None:
        self.store.ensure_user(user["id"])
        existing = self.store.active_key(user["id"])
        if existing:
            await self.send(chat_id,
                            f"Активный ключ уже есть (префикс {existing['prefix']}…).\n"
                            "Сначала /revoke, затем /key выпустит новый.")
            return
        secret, prefix = self.store.issue_key(user["id"])
        await self.send(chat_id,
                        f"API-ключ (префикс {prefix}…):\n\n{secret}\n\n"
                        "Сохрани сразу: показываю один раз.\n"
                        f"Эндпоинт: {config.upstream_public()}/v1")

    async def cmd_revoke(self, chat_id: int, user: dict) -> None:
        record = self.store.active_key(user["id"])
        if not record:
            await self.send(chat_id, "Активного ключа нет.")
            return
        self.store.revoke_by_prefix(record["prefix"])
        await self.send(chat_id, "Ключ отозван. Новый — командой /key.")

    async def cmd_quota(self, chat_id: int, user: dict) -> None:
        record = self.store.user(user["id"])
        if not record:
            await self.send(chat_id, "Напиши /start, чтобы зарегистрироваться.")
            return
        await self.send(chat_id, self.quota_text(record))

    def quota_text(self, user_row) -> str:
        key = self.store.active_key(user_row["telegram_id"])
        if not key:
            used_requests = 0
            used_usd = 0.0
            pt = ct = 0
        else:
            totals = self.store.today_totals(key["id"])
            used_requests, used_usd = totals["requests"], totals["usd"]
            pt, ct = totals["pt"], totals["ct"]
        return (f"Сегодня: {used_requests}/{user_row['daily_requests']} запросов, "
                f"${used_usd:.4f}/${user_row['daily_usd']:.2f}.\n"
                f"Токенов: вход {pt}, выход {ct}.")

    async def cmd_reset(self, chat_id: int) -> None:
        seconds = self.store.reset_in_seconds()
        hours, remainder = divmod(seconds, 3600)
        await self.send(chat_id,
                        f"Лимит обновится через {hours} ч {remainder // 60} мин.")

    # -- chat ---------------------------------------------------------------
    async def handle_message(self, chat_id: int, user: dict, text: str) -> None:
        record = self.store.ensure_user(user["id"])
        model = record["chat_model"]
        if not model:
            models = await self.upstream.models()
            if not models:
                await self.send(chat_id, "Апстрим недоступен: попробуй позже.")
                return
            model = models[0]["id"]
            self.store.set_chat_model(user["id"], model)

        key = self.store.active_key(user["id"])
        if not key:
            secret, prefix = self.store.issue_key(user["id"])
            key = self.store.active_key(user["id"])
            await self.send(chat_id,
                            f"Создал внутренний ключ квот ({prefix}…); личный — через /key.")
        totals = self.store.today_totals(key["id"])
        if totals["requests"] >= record["daily_requests"]:
            await self.send(chat_id,
                            "Дневной лимит запросов исчерпан. Сброс: /reset.")
            return
        if record["daily_usd"] > 0 and totals["usd"] >= record["daily_usd"]:
            await self.send(chat_id,
                            "Дневной лимит стоимости исчерпан. Сброс: /reset.")
            return

        await self.api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        try:
            response = await self.upstream.completion(
                model, [{"role": "user", "content": text[:12000]}],
                max_tokens=config.MAX_OUTPUT_TOKENS)
            data = response.json()
        except UpstreamError as error:
            await self.send(chat_id, f"Апстрим: {error}")
            return
        choices = data.get("choices") or [{}]
        answer = (choices[0].get("message") or {}).get("content") or "…"
        await self.send(chat_id, answer[:4000])

        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or max(1, len(text) // 4))
        completion = int(usage.get("completion_tokens") or 0)
        pricing = Pricing(config.PRICING_FILE)
        self.store.record_usage(key["id"], model, prompt_tokens=prompt,
                                completion_tokens=completion,
                                usd=pricing.cost(model, prompt, completion))

    # -- loop ----------------------------------------------------------------
    async def run(self) -> None:
        if not config.BOT_TOKEN:
            return
        while True:
            try:
                updates = await self.api("getUpdates", {
                    "offset": self.offset, "timeout": 25,
                    "allowed_updates": ["message"]})
                for update in updates:
                    self.offset = update["update_id"] + 1
                    await self.dispatch(update.get("message") or {})
            except ConnectionError:
                await asyncio.sleep(3)
            except Exception:
                await asyncio.sleep(5)

    async def dispatch(self, message: dict) -> None:
        if not message:
            return
        chat_id = (message.get("chat") or {}).get("id")
        user = message.get("from") or {}
        text = (message.get("text") or "").strip()
        if not chat_id or not user.get("id"):
            return
        command = text.split(maxsplit=1)[0] if text else ""
        # Strip a possible @botname suffix: /models@my_bot -> /models
        if "@" in command:
            command = command.split("@", 1)[0]
        argument = text[len(command):].strip() if len(text) > len(command) else ""
        try:
            if command in {"/start", "/help"}:
                await self.cmd_start(chat_id, user)
            elif command == "/models":
                await self.cmd_models(chat_id, user)
            elif command == "/model":
                await self.cmd_set_model(chat_id, user, argument)
            elif command == "/key":
                await self.cmd_key(chat_id, user)
            elif command == "/revoke":
                await self.cmd_revoke(chat_id, user)
            elif command == "/quota":
                await self.cmd_quota(chat_id, user)
            elif command == "/reset":
                await self.cmd_reset(chat_id)
            elif command:
                await self.send(chat_id, "Неизвестная команда. Список: /help")
            else:
                await self.handle_message(chat_id, user, text)
        except Exception as error:
            try:
                await self.send(chat_id, f"Ошибка: {type(error).__name__}")
            except Exception:
                pass
