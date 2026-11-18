"""Telegram Bot API: menu-driven buttons like popular AI resellers, plus chat."""
from __future__ import annotations

import asyncio
import html
import json

import httpx

from . import config
from .pricing import Pricing
from .upstream import Upstream, UpstreamError


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}




class Bot:
    def __init__(self, store, upstream: Upstream):
        self.store = store
        self.upstream = upstream
        self.offset = 0
        self.banner = config.BANNER_FILE.read_bytes()

    # -- transport -----------------------------------------------------------
    async def api(self, method: str, payload: dict | None = None,
                  files: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(base_url=config.BOT_API_BASE,
                                         timeout=40) as client:
                if files:
                    response = await client.post(
                        f"/bot{config.BOT_TOKEN}/{method}",
                        data=payload or {}, files=files)
                else:
                    response = await client.post(
                        f"/bot{config.BOT_TOKEN}/{method}",
                        json=payload or {})
                data = response.json()
                if not data.get("ok"):
                    raise RuntimeError(data.get("description", "Telegram error"))
                return data.get("result", {})
        except (httpx.HTTPError, ValueError) as error:
            raise ConnectionError(f"Telegram API: {error}") from error

    @staticmethod
    def _caption(text: str) -> str:
        if len(text.encode("utf-16-le")) // 2 > 1024:
            raise ValueError("Telegram photo caption exceeds 1024 UTF-16 units")
        return text

    async def send_screen(self, chat_id: int, text: str,
                          keyboard: dict | None = None):
        payload = {"chat_id": chat_id, "caption": self._caption(text),
                   "parse_mode": "HTML"}
        if keyboard:
            payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
        return await self.api("sendPhoto", payload,
                              files={"photo": (config.BANNER_FILE.name,
                                               self.banner, "image/png")})

    async def delete_screen(self, chat_id: int, message_id: int):
        return await self.api("deleteMessage", {
            "chat_id": chat_id, "message_id": message_id})

    async def send(self, chat_id: int, text: str,
                   keyboard: dict | None = None):
        payload = {"chat_id": chat_id, "text": text[:4000],
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = keyboard
        return await self.api("sendMessage", payload)

    async def answer(self, callback_query_id: str) -> None:
        try:
            await self.api("answerCallbackQuery",
                           {"callback_query_id": callback_query_id})
        except (ConnectionError, RuntimeError):
            pass

    # -- keyboards -------------------------------------------------------------
    def keyboard_for(self, action: str, user_row) -> dict:
        if action == "start":
            rows = [
                [_btn("Каталог моделей", "models")],
                [_btn("Профиль", "profile"), _btn("Использование", "quota")],
                [_btn("API-ключ", "key"), _btn("Подключение", "docs")],
                [_btn("Помощь", "help")],
            ]
        else:
            rows = []
            if action in {"key", "reissue"} and self.store.active_key(user_row["telegram_id"]):
                rows.append([_btn("Перевыпустить ключ", "reissue")])
            rows.append([_btn("Назад в меню", "start")])
        return {"inline_keyboard": rows}

    # -- screens -----------------------------------------------------------------
    async def start_text(self) -> str:
        base = html.escape(config.upstream_public())
        return ("<b>VibeComAI</b>\n\n"
                "Каталог моделей, лимиты и ключ для подключения к API.\n\n"
                f"Адрес API\n<code>{base}/v1</code>")

    async def models_text(self, user_row) -> str:
        models = await self.upstream.models()
        title = "<b>Каталог моделей</b>\n"
        if not models:
            return f"{title}\nНе удалось загрузить список моделей. Попробуйте позже."
        current = user_row["chat_model"]
        hint = "\nВыбор модели: <code>/model имя-модели</code>"
        lines = [title]
        size = len(title) + len(hint) + 1
        for model in models:
            name = model["id"]
            line = f"<code>{html.escape(name)}</code>"
            if name == current:
                line += " (выбрана)"
            size += len(line.encode("utf-16-le")) // 2 + 1
            if size > 900:
                break
            lines.append(line)
        shown = len(lines) - 1
        if shown < len(models):
            lines.append(f"\nПоказано {shown} из {len(models)}. Полный список: <code>/v1/models</code>.")
        lines.append(hint)
        return "\n".join(lines)

    def profile_text(self, user_row) -> str:
        key = self.store.active_key(user_row["telegram_id"])
        requests, usd = self._today_pair(key["id"]) if key else (0, 0.0)
        username = user_row["username"]
        lines = [
            "<b>Профиль</b>\n",
            f"@{html.escape(username)}" if username else None,
            f"Telegram ID: <code>{user_row['telegram_id']}</code>",
            "Тариф: Free\n",
            f"Сегодня: {requests} запросов / ${usd:.4f}",
            f"Лимит на день: {user_row['daily_requests']} запросов / ${user_row['daily_usd']:.2f}",
            f"Обновление лимита через {self._reset_text()}",
        ]
        return "\n".join([line for line in lines if line])

    def quota_text(self, user_row) -> str:
        key = self.store.active_key(user_row["telegram_id"])
        if key:
            totals = self.store.today_totals(key["id"])
            top = self.store.today_by_model(key["id"], limit=3)
            lines = [
                "<b>Использование</b>\n",
                f"Запросы: {totals['requests']} из {user_row['daily_requests']}",
                f"Расход: ${totals['usd']:.4f} из ${user_row['daily_usd']:.2f}",
                "Токенов: вход {:,} · выход {:,}".format(
                    totals["pt"], totals["ct"]).replace(",", " "),
            ]
            joined = ", ".join(
                f"<code>{html.escape(item['model'][:80])}</code> (${item['usd']:.4f})"
                for item in top if item["usd"] > 0)
            if joined:
                lines.append(f"\nМодели по расходу: {joined}")
        else:
            lines = ["<b>Использование</b>\n", "Ключ ещё не выпущен."]
        lines.append(f"\nОбновление лимита через {self._reset_text()}")
        return "\n".join(line for line in lines if line is not None)

    def key_text(self, user_row) -> str:
        existing = self.store.active_key(user_row["telegram_id"])
        if not existing:
            secret, _ = self.store.issue_key(user_row["telegram_id"], "telegram")
            return self._key_issued_text(secret)
        return ("<b>API-ключ</b>\n\n"
                f"Активный ключ: <code>{html.escape(existing['prefix'])}…</code>\n\n"
                "Полный ключ показывается только при выпуске. "
                "При перевыпуске текущий ключ перестанет работать.")

    def reissue_text(self, user_row) -> str:
        existing = self.store.active_key(user_row["telegram_id"])
        if existing:
            self.store.revoke_by_prefix(existing["prefix"])
        secret, _ = self.store.issue_key(user_row["telegram_id"], "reissue")
        return self._key_issued_text(secret)

    def _key_issued_text(self, secret: str) -> str:
        base = html.escape(config.upstream_public())
        return ("<b>Новый API-ключ</b>\n\n"
                f"<code>{html.escape(secret)}</code>\n\n"
                "Скопируйте ключ: повторно он не показывается.\n\n"
                f"Адрес API\n<code>{base}/v1</code>\n\n"
                "Заголовок: <code>Authorization: Bearer &lt;ключ&gt;</code>")

    def docs_text(self) -> str:
        base = html.escape(config.upstream_public())
        return ("<b>Подключение</b>\n\n"
                "В OpenAI-совместимом клиенте укажите Base URL и ключ из раздела «API-ключ».\n\n"
                f"Base URL\n<code>{base}/v1</code>\n\n"
                "Проверка подключения\n"
                f"<pre>curl {base}/v1/models \\\n"
                "  -H 'Authorization: Bearer sk-...'</pre>\n\n"
                "Для чата: <code>POST /chat/completions</code>.")

    def reset_text(self) -> str:
        return f"Лимит обновится через {self._reset_text()}."

    def _reset_text(self) -> str:
        seconds = self.store.reset_in_seconds()
        hours, remainder = divmod(seconds, 3600)
        return f"{hours} ч {remainder // 60} мин"

    def _today_pair(self, key_id: int) -> tuple[int, float]:
        totals = self.store.today_totals(key_id)
        return totals["requests"], totals["usd"]

    # -- chat ------------------------------------------------------------------
    async def handle_message(self, chat_id: int, user: dict, text: str) -> None:
        record = self.store.ensure_user(
            user["id"], user.get("username", ""),
            is_admin=user.get("id") == config.OWNER_TELEGRAM_ID)
        model = record["chat_model"]
        if not model:
            models = await self.upstream.models()
            if not models:
                await self.send(chat_id, "Список моделей недоступен. Попробуйте позже.")
                return
            model = models[0]["id"]
            self.store.set_chat_model(user["id"], model)

        key = self.store.active_key(user["id"])
        if not key:
            secret, prefix = self.store.issue_key(user["id"], "chat")
            key = self.store.active_key(user["id"])
        totals = self.store.today_totals(key["id"])
        if totals["requests"] >= record["daily_requests"]:
            await self.send(chat_id, "Дневной лимит запросов исчерпан. " + self.reset_text())
            return
        if record["daily_usd"] > 0 and totals["usd"] >= record["daily_usd"]:
            await self.send(chat_id, "Дневной лимит расходов исчерпан. " + self.reset_text())
            return

        await self.api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        try:
            response = await self.upstream.completion(
                model, [{"role": "user", "content": text[:12000]}],
                max_tokens=config.MAX_OUTPUT_TOKENS)
            data = response.json()
        except UpstreamError as error:
            await self.send(chat_id, html.escape(str(error)))
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

    # -- rendering / dispatch --------------------------------------------------
    async def render_action(self, action: str, user: dict) -> tuple[str, object]:
        user_row = self.store.ensure_user(
            user["id"], user.get("username", ""),
            is_admin=user.get("id") == config.OWNER_TELEGRAM_ID)
        if action == "start":
            text = await self.start_text()
        elif action == "help":
            text = ("<b>Помощь</b>\n\n"
                    "<code>/models</code> — каталог моделей\n"
                    "<code>/model имя-модели</code> — выбрать модель\n"
                    "<code>/key</code> — открыть раздел API-ключа\n"
                    "<code>/usage</code> — посмотреть расход и лимиты")
        elif action == "models":
            text = await self.models_text(user_row)
        elif action == "profile":
            text = self.profile_text(user_row)
        elif action == "key":
            text = self.key_text(user_row)
        elif action == "reissue":
            text = self.reissue_text(user_row)
        elif action == "quota":
            text = self.quota_text(user_row)
        elif action == "docs":
            text = self.docs_text()
        elif action == "reset":
            text = f"<b>Дневной лимит</b>\n\n{self.reset_text()}"
        else:
            text = await self.start_text()
        return text, user_row

    async def dispatch_callback(self, query: dict) -> None:
        action = (query.get("data") or "help").split(":", 1)[0]
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        user = query.get("from") or {}
        if not chat_id or not user.get("id"):
            return
        await self.answer(query["id"])
        try:
            text, user_row = await self.render_action(action, user)
            keyboard = self.keyboard_for(action, user_row)
            await self.send_screen(chat_id, text, keyboard)
            try:
                await self.delete_screen(chat_id, message_id)
            except (ConnectionError, RuntimeError) as error:
                print("bot: new section sent but old menu could not be deleted:",
                      error, flush=True)
        except (ConnectionError, RuntimeError, ValueError) as error:
            print("bot: menu callback failed; original message kept:", error,
                  flush=True)

    async def dispatch(self, message: dict) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        user = message.get("from") or {}
        text = (message.get("text") or "").strip()
        if not chat_id or not user.get("id"):
            return
        command = text.split(maxsplit=1)[0] if text else ""
        if "@" in command:
            command = command.split("@", 1)[0]
        argument = text[len(command):].strip() if len(text) > len(command) else ""
        try:
            if command == "/model":
                user_row = self.store.ensure_user(
                    user["id"], user.get("username", ""))
                if argument:
                    self.store.set_chat_model(user["id"], argument)
                    await self.send_screen(
                        chat_id,
                        f"Модель чата: <code>{html.escape(argument[:80])}</code>",
                        self.keyboard_for("start", user_row))
                else:
                    await self.send_screen(
                        chat_id,
                        "Текущая модель: "
                        f"<code>{html.escape(user_row['chat_model'] or 'не выбрана')}</code>.\n"
                        "Выбор: /model &lt;имя&gt; из /models.",
                        self.keyboard_for("start", user_row))
            elif command == "/revoke":
                user_row = self.store.ensure_user(user["id"], user.get("username", ""))
                existing = self.store.active_key(user["id"])
                if existing:
                    self.store.revoke_by_prefix(existing["prefix"])
                    await self.send_screen(chat_id,
                                           "Ключ отозван. Выпустите новый в разделе «API-ключ».",
                                           self.keyboard_for("start", user_row))
                else:
                    await self.send_screen(chat_id, "Активного ключа нет.",
                                           self.keyboard_for("start", user_row))
            elif command in {"/start", "/help", "/profile", "/models", "/key",
                             "/quota", "/usage", "/docs", "/reset"}:
                action = {"/start": "start", "/help": "help", "/profile": "profile",
                          "/models": "models", "/key": "key", "/usage": "quota",
                          "/quota": "quota", "/docs": "docs",
                          "/reset": "reset"}[command]
                text_body, user_row = await self.render_action(action, user)
                await self.send_screen(chat_id, text_body,
                                       self.keyboard_for(action, user_row))
            elif command:
                user_row = self.store.ensure_user(
                    user["id"], user.get("username", ""))
                await self.send_screen(chat_id, "Неизвестная команда. Выберите раздел в меню.",
                                       self.keyboard_for("start", user_row))
            else:
                await self.handle_message(chat_id, user, text)
        except (ConnectionError, RuntimeError) as error:
            print("bot: dispatch failed:", error, flush=True)
            try:
                await self.send(chat_id, "Не удалось обработать запрос. Попробуйте ещё раз.")
            except (ConnectionError, RuntimeError):
                pass

    # -- loop --------------------------------------------------------------------
    async def run(self) -> None:
        if not config.BOT_TOKEN:
            return
        while True:
            try:
                updates = await self.api("getUpdates", {
                    "offset": self.offset, "timeout": 25,
                    "allowed_updates": ["message", "callback_query"]})
                for update in updates:
                    self.offset = update["update_id"] + 1
                    if "callback_query" in update:
                        await self.dispatch_callback(update["callback_query"])
                    else:
                        await self.dispatch(update.get("message") or {})
            except ConnectionError as error:
                print("bot: connection error:", error, flush=True)
                await asyncio.sleep(3)
            except Exception:
                import traceback
                traceback.print_exc()
                await asyncio.sleep(5)
