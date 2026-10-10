"""统一 HTTP 抓取层：浏览器请求头、限速、重试、超时、gzip 解压。

刻意只用标准库，保证任何人拿到代码都能直接跑，不需要 pip install。
"""

from __future__ import annotations

import gzip
import io
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_last_request_at = 0.0


class FetchError(RuntimeError):
    """网络请求最终失败（重试耗尽）。"""


def _politeness_wait(delay: float) -> None:
    """全局串行限速，避免短时间内高频打到同一站点触发风控。"""
    global _last_request_at
    if delay <= 0:
        return
    elapsed = time.time() - _last_request_at
    remaining = delay - elapsed
    if remaining > 0:
        time.sleep(remaining + random.uniform(0, 0.25))
    _last_request_at = time.time()


def fetch(
    url: str,
    *,
    headers: dict | None = None,
    data: dict | None = None,
    delay: float = 1.0,
    retries: int = 3,
    timeout: int = 20,
    encoding: str = "utf-8",
    insecure: bool = False,
) -> str:
    """抓取一个 URL 并返回文本。失败时抛 FetchError。

    insecure=True 会关闭 TLS 证书校验。默认 False；只有在你所在网络存在
    TLS 中间人（公司代理、安全软件）导致证书主机名不匹配时才需要在
    config.json 里打开，请注意它会降低连接安全性。
    """
    hdrs = {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        hdrs.update(headers)

    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")

    opener = None
    if insecure:
        import ssl

        context = ssl._create_unverified_context()  # noqa: S323 - 由配置显式开启
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    last_error = None
    for attempt in range(1, retries + 1):
        _politeness_wait(delay)
        try:
            request = urllib.request.Request(url, data=body, headers=hdrs)
            if opener is not None:
                response = opener.open(request, timeout=timeout)
            else:
                response = urllib.request.urlopen(request, timeout=timeout)
            with response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw.decode(encoding, "replace")
        except Exception as error:  # noqa: BLE001 - 网络层需要兜住所有异常
            last_error = error
            if attempt < retries:
                time.sleep(1.5 * attempt)

    hint = ""
    if "CERTIFICATE_VERIFY_FAILED" in str(last_error):
        hint = "（TLS 证书校验失败，若你所在网络有代理拦截，可把 config.json 里该数据源的 insecure_ssl 设为 true）"
    raise FetchError("请求失败（已重试 %d 次）%s%s：%s" % (retries, url, hint, last_error))


def fetch_json(url: str, **kwargs) -> object:
    """抓取并解析 JSON。"""
    text = fetch(url, **kwargs)
    stripped = text.strip()
    if not stripped:
        raise FetchError("返回内容为空：%s" % url)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        head = stripped[:120].replace("\n", " ")
        raise FetchError("返回内容不是 JSON（开头为 %r）：%s" % (head, url)) from None
