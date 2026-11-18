import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import Bot
from app.store import Store


class BotBannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.sqlite")
        self.upstream = SimpleNamespace(models=AsyncMock(return_value=[
            {"id": "glm-5.3-flash"}, {"id": "test-model"}]))
        self.bot = Bot(self.store, self.upstream)
        self.bot.api = AsyncMock(return_value={"message_id": 42})

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_start_sends_banner_and_menu_keyboard(self):
        await self.bot.dispatch({
            "chat": {"id": 123},
            "from": {"id": 123, "username": "tester"},
            "text": "/start",
        })

        method, payload = self.bot.api.await_args.args[:2]
        files = self.bot.api.await_args.kwargs["files"]
        self.assertEqual(method, "sendPhoto")
        self.assertEqual(payload["chat_id"], 123)
        self.assertEqual(files["photo"][1], self.bot.banner)
        buttons = [button["callback_data"]
                   for row in json.loads(payload["reply_markup"])["inline_keyboard"]
                   for button in row]
        self.assertIn("models", buttons)
        self.assertIn("profile", buttons)

    async def test_callback_sends_new_banner_then_deletes_old_message(self):
        await self.bot.dispatch_callback({
            "id": "callback-1",
            "data": "profile",
            "from": {"id": 123, "username": "tester"},
            "message": {"message_id": 41, "chat": {"id": 123},
                        "photo": [{"file_id": "photo-id"}]},
        })

        methods = [call.args[0] for call in self.bot.api.await_args_list]
        self.assertEqual(methods, ["answerCallbackQuery", "sendPhoto", "deleteMessage"])
        new_payload = self.bot.api.await_args_list[1].args[1]
        self.assertIn("Профиль", new_payload["caption"])
        delete_payload = self.bot.api.await_args_list[2].args[1]
        self.assertEqual(delete_payload["message_id"], 41)
        self.assertEqual(delete_payload["chat_id"], 123)

    async def test_section_includes_back_to_menu_button(self):
        user_row = self.store.ensure_user(123, "tester")
        keyboard = self.bot.keyboard_for("profile", user_row)
        callbacks = [button["callback_data"]
                     for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(callbacks[-1], "start")

    async def test_failed_replacement_keeps_existing_menu_message(self):
        self.bot.api = AsyncMock(side_effect=[{"ok": True},
                                               RuntimeError("photo send failed")])
        output = io.StringIO()
        with redirect_stdout(output):
            await self.bot.dispatch_callback({
                "id": "callback-2",
                "data": "profile",
                "from": {"id": 123, "username": "tester"},
                "message": {"message_id": 42, "chat": {"id": 123},
                            "photo": [{"file_id": "photo-id"}]},
            })
        methods = [call.args[0] for call in self.bot.api.await_args_list]
        self.assertEqual(methods, ["answerCallbackQuery", "sendPhoto"])
        self.assertIn("original message kept", output.getvalue())

    async def test_every_menu_screen_fits_telegram_photo_caption(self):
        user = {"id": 123, "username": "tester"}
        for action in ("start", "help", "models", "profile", "key",
                       "reissue", "quota", "docs", "reset"):
            with self.subTest(action=action):
                text, _ = await self.bot.render_action(action, user)
                self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 1024)


if __name__ == "__main__":
    unittest.main()
