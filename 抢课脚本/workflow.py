"""共享的可取消等待和只读请求重试。"""
import time
import threading
import requests


class Cancelled(Exception):
    pass


class LoginExpired(RuntimeError):
    pass


class Retryable(RuntimeError):
    pass


def pause(seconds, cancel=None):
    if (cancel or threading.Event()).wait(max(0, seconds)):
        raise Cancelled("已停止")


def retry_read(action, emit=print, cancel=None, seconds=120):
    deadline = time.monotonic() + seconds
    attempt = 0
    while True:
        if cancel and cancel.is_set():
            raise Cancelled("已停止")
        try:
            return action()
        except (requests.RequestException, Retryable) as exc:
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"等待服务器恢复超过 {seconds} 秒：{exc}") from exc
            delay = min(2 ** min(attempt, 3), remaining)
            emit(f"服务器暂时未就绪：{exc}；{delay:.0f} 秒后重试，剩余等待 {remaining:.0f} 秒")
            pause(delay, cancel)


def check_response(response):
    if response.status_code in (401, 403):
        raise LoginExpired("访问被拒绝，请检查登录状态或访问限制")
    if response.status_code == 429 or response.status_code >= 500:
        raise Retryable(f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise RuntimeError(f"请求失败：HTTP {response.status_code}")
    response.encoding = response.apparent_encoding
    text = response.text
    if ('userAccount' in text and 'userPassword' in text) or '已在别处登录' in text:
        raise LoginExpired("登录已失效，请重新登录后继续")
    return text
