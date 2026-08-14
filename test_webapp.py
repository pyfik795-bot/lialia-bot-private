"""Тесты веб-дашборда (webapp.py).

Запуск:  python -m unittest test_webapp -v

Поднимается настоящее aiohttp-приложение на временном порту; торговый движок
не задействован - проверяются авторизация и формат ответов.
"""

import unittest

from aiohttp.test_utils import TestClient, TestServer

import config
import webapp


class WebappTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved_password = config.WEB_PASSWORD
        self.client = TestClient(TestServer(webapp.build_app(None)))
        await self.client.start_server()

    async def asyncTearDown(self):
        config.WEB_PASSWORD = self._saved_password
        await self.client.close()

    async def login(self, password):
        return await self.client.post("/api/login", json={"password": password})

    # ---------- вход ----------

    async def test_login_with_configured_password(self):
        """Пароль в config.py кириллический - вход обязан работать как есть.

        secrets.compare_digest на строках требует чистый ASCII и на кириллице
        бросает TypeError: каждый вход падал в 500, а браузер показывал
        невнятную ошибку разбора JSON вместо «неверный пароль».
        """
        resp = await self.login(config.WEB_PASSWORD)

        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"ok": True})

    async def test_login_with_ascii_password(self):
        config.WEB_PASSWORD = "plain-ascii-secret"

        resp = await self.login("plain-ascii-secret")

        self.assertEqual(resp.status, 200)

    async def test_wrong_password_returns_json_401(self):
        resp = await self.login("не тот пароль")

        self.assertEqual(resp.status, 401)
        self.assertIn("error", await resp.json())

    async def test_login_never_returns_500(self):
        """Любой ввод - осмысленный JSON-ответ, а не падение сервера.

        Именно 500 ломал страницу входа: браузер разбирал тело
        «500 Internal Server Error» как JSON и спотыкался на пятом символе.
        """
        for payload in ("", "𝔘𝔫𝔦𝔠𝔬𝔡𝔢", "х" * 5000, "🙂"):
            with self.subTest(payload=payload[:20]):
                resp = await self.login(payload)

                self.assertEqual(resp.status, 401)
                self.assertIn("error", await resp.json())

    async def test_non_string_password_is_rejected(self):
        for payload in ({"password": 123}, {"password": None}, {"password": []}, {}):
            with self.subTest(payload=payload):
                resp = await self.client.post("/api/login", json=payload)

                self.assertEqual(resp.status, 401)

    async def test_malformed_body_returns_400(self):
        resp = await self.client.post("/api/login", data="не json",
                                      headers={"Content-Type": "application/json"})

        self.assertEqual(resp.status, 400)

    # ---------- защита маршрутов ----------

    async def test_api_requires_auth(self):
        resp = await self.client.get("/api/status")

        self.assertEqual(resp.status, 401)
        self.assertIn("error", await resp.json())

    async def test_page_shows_login_form_when_not_authorized(self):
        resp = await self.client.get("/")

        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.headers["Content-Type"])
        self.assertIn("Пароль", await resp.text())

    async def test_api_works_after_login(self):
        await self.login(config.WEB_PASSWORD)

        resp = await self.client.get("/api/status")

        self.assertEqual(resp.status, 200)
        self.assertIn("demo", await resp.json())

    async def test_dashboard_mood_gallery_and_assets(self):
        await self.login(config.WEB_PASSWORD)

        page = await self.client.get("/")
        html = await page.text()
        self.assertIn("Ляля mood", html)
        self.assertIn("/assets/lialia-1.jpeg", html)

        for index in range(1, 5):
            with self.subTest(index=index):
                asset = await self.client.get(f"/assets/lialia-{index}.jpeg")
                self.assertEqual(asset.status, 200)
                self.assertEqual(asset.content_type, "image/jpeg")
                self.assertGreater(len(await asset.read()), 20_000)

    async def test_wrong_cookie_is_rejected(self):
        self.client.session.cookie_jar.update_cookies({webapp.COOKIE_NAME: "poddelka"})

        resp = await self.client.get("/api/status")

        self.assertEqual(resp.status, 401)

    async def test_logout_route_exists(self):
        """Кнопка выхода в панели била в неверный адрес - маршрут проверяем прямо."""
        await self.login(config.WEB_PASSWORD)

        resp = await self.client.post("/api/logout")

        self.assertEqual(resp.status, 200)


class SecretComparisonTestCase(unittest.TestCase):
    """Сравнение секретов не должно падать ни на каком вводе."""

    def test_matches_non_ascii(self):
        self.assertTrue(webapp._same_secret("пароль", "пароль"))

    def test_rejects_different_non_ascii(self):
        self.assertFalse(webapp._same_secret("пароль", "пароли"))

    def test_rejects_non_string(self):
        for value in (None, 123, [], {}):
            with self.subTest(value=value):
                self.assertFalse(webapp._same_secret(value, "пароль"))

    def test_mixed_ascii_and_cyrillic(self):
        self.assertFalse(webapp._same_secret("abc", "абв"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
