"""Reddit 公开 .json 端点封装。

Reddit 2025-11 关闭自助 API 后仍保留公开 .json 端点。限速约 10 QPM，
我们按 config 里的 qpm 做客户端限速。遇到 429 → sleep 60s 重试一次。

特殊处理：在某些网络环境（如中国大陆无代理直连）下，本地 DNS 可能被污染，
解析 reddit.com 会返回错误 IP。本模块提供 DoH (DNS-over-HTTPS) 回退，
通过 Cloudflare 1.1.1.1/dns-query 查询真实 IP 并注入到 socket.getaddrinfo。

代理模式：Reddit 封了 GitHub Actions / 各家数据中心的 IP 段，直连会全部 403。
设置 REDDIT_PROXY_URL + REDDIT_PROXY_SECRET 后，所有请求走 Cloudflare Worker
做透明代理（见 reddit-proxy-worker 项目）。本地跑可以不设，走直连。
"""
from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx

BASE_URL = "https://www.reddit.com"
REDDIT_HOSTS = ("www.reddit.com", "reddit.com", "old.reddit.com", "oauth.reddit.com")

# Reddit 通过 CF Worker 出口 IP 时会按 UA 风控：非浏览器 UA 一律 403。
# 走代理时用浏览器 UA；直连（本地住宅 IP）时用 config 里配置的 UA。
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _read_proxy_config() -> tuple[str, str]:
    """运行时读取代理配置。不在模块加载时读，因为 load_dotenv 可能还没跑。"""
    return (
        os.getenv("REDDIT_PROXY_URL", "").rstrip("/"),
        os.getenv("REDDIT_PROXY_SECRET", ""),
    )


@dataclass
class Post:
    post_id: str           # 不带 t3_ 前缀
    subreddit: str
    title: str
    author: str
    url: str
    permalink: str         # 完整 URL
    created_utc: int
    selftext: str

    @classmethod
    def from_api(cls, child: dict[str, Any]) -> "Post":
        d = child["data"]
        return cls(
            post_id=d["id"],
            subreddit=d.get("subreddit", ""),
            title=d.get("title", ""),
            author=d.get("author", ""),
            url=d.get("url", ""),
            permalink="https://www.reddit.com" + d.get("permalink", ""),
            created_utc=int(d.get("created_utc", 0)),
            selftext=d.get("selftext", "") or "",
        )


class RateLimiter:
    """简易 QPM 限流器：保证相邻请求间隔 >= 60/qpm 秒。"""

    def __init__(self, qpm: int):
        self.min_interval = 60.0 / max(qpm, 1)
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        gap = now - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()


# --- DNS 污染回退：用 Cloudflare DoH 查询 Reddit 真实 IP ---

_DOH_CACHE: dict[str, list[str]] = {}
_DNS_PATCHED = False


def _doh_resolve(host: str, timeout: float = 5.0) -> list[str]:
    """用 Cloudflare DoH 查询 A 记录，返回 IPv4 列表。"""
    if host in _DOH_CACHE:
        return _DOH_CACHE[host]
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(
                "https://1.1.1.1/dns-query",
                params={"name": host, "type": "A"},
                headers={"accept": "application/dns-json"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception:
        return []
    ips = [
        a["data"]
        for a in data.get("Answer", [])
        if a.get("type") == 1 and "data" in a
    ]
    _DOH_CACHE[host] = ips
    return ips


def _enable_dns_fallback(
    probe_host: str, extra_hosts: tuple[str, ...] = ()
) -> bool:
    """检测 probe_host 是否可直连，若被 DNS 污染则 monkey-patch socket.getaddrinfo。

    patched 仅对 probe_host + extra_hosts 生效，其他域名走原生解析。
    """
    global _DNS_PATCHED
    if _DNS_PATCHED:
        return True
    try:
        test_sock = socket.create_connection((probe_host, 443), timeout=4)
        test_sock.close()
        return False  # 直连 OK
    except OSError:
        pass

    ips = _doh_resolve(probe_host)
    if not ips:
        return False

    patched_hosts = {probe_host, *extra_hosts}
    print(
        f"[dns] 本地 DNS 无法直连 {probe_host}，启用 DoH 回退 "
        f"(patched_hosts={sorted(patched_hosts)})"
    )

    original_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host in patched_hosts:
            cached = _DOH_CACHE.get(host) or _doh_resolve(host) or ips
            port = args[0] if args else 0
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in cached
            ]
        return original_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = patched_getaddrinfo  # type: ignore[assignment]
    _DNS_PATCHED = True
    return True


def _enable_dns_fallback_if_needed() -> bool:
    """直连 Reddit 时的 DNS 回退（向后兼容入口）。"""
    return _enable_dns_fallback("old.reddit.com", REDDIT_HOSTS)


class RedditClient:
    def __init__(self, user_agent: str, qpm: int = 6, timeout: float = 20.0):
        self._proxy_url, self._proxy_secret = _read_proxy_config()
        self.use_proxy = bool(self._proxy_url and self._proxy_secret)

        if self.use_proxy:
            # 走代理：所有请求打到 Worker URL；Reddit 流量走 CF 边缘 IP，
            # Reddit 会按 UA 风控，所以必须用浏览器 UA（config 里的 radar UA 会被封）。
            # 如果本地 DNS 被污染，Worker 域名也会被污染，需要对 Worker 域名启用 DoH。
            print(f"[proxy] routing Reddit via {self._proxy_url}")
            self._ua_for_reddit = _BROWSER_UA
            from urllib.parse import urlparse

            worker_host = urlparse(self._proxy_url).hostname
            if worker_host:
                _enable_dns_fallback(worker_host)
        else:
            # 直连（本地住宅 IP）：保留原有 DNS 污染回退 + config 里的 UA。
            _enable_dns_fallback_if_needed()
            self._ua_for_reddit = user_agent

        # 关键：禁用 keep-alive。Reddit 对同一 TCP 连接上的重复请求有反爬机制，
        # 第二次请求起会返回 403 HTML challenge 页。每请求新建连接绕过。
        # 浏览器风格的 headers，降低云端 IP 被风控概率。
        # 不手动设 Accept-Encoding，让 httpx 用默认值（自动带 gzip/deflate 并解压）。
        # 手动指定 `gzip, deflate, br` 时 httpx 不会解压 br，导致 json.decode 失败。
        self.client = httpx.Client(
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Connection": "close",
            },
            timeout=timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        self.limiter = RateLimiter(qpm)

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _build_reddit_url(self, path: str, params: dict[str, Any] | None) -> str:
        url = f"{BASE_URL}{path}"
        if params:
            url += "?" + urlencode(params)
        return url

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.limiter.wait()
        reddit_url = self._build_reddit_url(path, params)

        if self.use_proxy:
            request_url = f"{self._proxy_url}/?url={quote(reddit_url, safe='')}"
            headers = {
                "X-Proxy-Secret": self._proxy_secret,
                "User-Agent": self._ua_for_reddit,
            }
        else:
            request_url = reddit_url
            headers = {"User-Agent": self._ua_for_reddit}

        resp = self.client.get(request_url, headers=headers)
        if resp.status_code == 429:
            print(f"[limit] 429 from {reddit_url}, sleeping 60s then retry once...")
            time.sleep(60)
            self.limiter.wait()
            resp = self.client.get(request_url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def fetch_new_posts(self, subreddit: str, limit: int = 25) -> list[Post]:
        data = self._get_json(f"/r/{subreddit}/new.json", {"limit": limit})
        children = data.get("data", {}).get("children", [])
        return [Post.from_api(c) for c in children if c.get("kind") == "t3"]

    def fetch_comments(self, post_id: str, limit: int = 20) -> list[str]:
        """返回顶层评论的 body 列表。"""
        data = self._get_json(f"/comments/{post_id}.json", {"limit": limit})
        # data 是 [post_listing, comments_listing]
        if not isinstance(data, list) or len(data) < 2:
            return []
        comments_listing = data[1]
        children = comments_listing.get("data", {}).get("children", [])
        bodies: list[str] = []
        for c in children:
            if c.get("kind") != "t1":
                continue
            body = c.get("data", {}).get("body", "")
            if body and body not in ("[deleted]", "[removed]"):
                bodies.append(body)
        return bodies[:limit]
