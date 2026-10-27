"""A client for the web app under test, logged in with the test configuration's password."""

import http.client
from typing import NamedTuple
from urllib.parse import urlencode

from pinkas import web
from tests.db import DSN

PASSWORD = "correct horse"
CONF = web.Config(DSN, "test-secret", web.hash_password(PASSWORD))
TOKEN = web.form_token(CONF)
XSS = "</script><img src=x onerror=alert(1)>"
XSS_ESCAPED = "&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;"


class Response(NamedTuple):
    status: int
    headers: http.client.HTTPMessage
    body: str


class Client:
    def __init__(self, port: int):
        self.port = port
        self.cookie: str | None = None

    def request(self, method: str, path: str, form: dict | None = None, cookie: str | None = None) -> Response:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}

        if cookie or self.cookie:
            headers["Cookie"] = cookie or self.cookie

        body = None

        if form is not None:
            body = urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = Response(response.status, response.headers, response.read().decode())
        conn.close()

        return result

    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, form: dict, **kw) -> Response:
        return self.request("POST", path, form, **kw)

    def login(self) -> None:
        response = self.post("/login", {"password": PASSWORD, "next": "/"})
        assert response.status == 303
        self.cookie = response.headers["Set-Cookie"].split(";")[0]
