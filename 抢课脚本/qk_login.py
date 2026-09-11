# -*- coding: utf-8 -*-
"""
qk_login.py —— 登录模块

两种登录方式：
  1. 完整登录（推荐）：账号 + 密码 + 验证码（图片自动打开，手动输入）
     —— 脚本自己登录主系统，再自动进入选课页，不需要复制 Cookie。
  2. 旧方式：用 config.py 里 COOKIE_STR 的已登录 Cookie 构建会话。
"""

import base64
import html
import json
import os
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests

import config
from workflow import retry_read, check_response, Retryable, LoginExpired, Cancelled


# 进入选课批次后，页面会再列出“体育课 / 公共任选课”等入口。不同类型的
# 列表与提交接口并不相同，所以不能只动态获取批次、再写死通选课接口。
_CLOSED_MARKERS = (
    "不开放", "未开放", "尚未开放", "尚未开始", "未到选课时间",
    "不在选课时间", "禁止选课", "选课已结束", "已关闭",
    "错误提示页面", "无权进行", "没有选课权限",
)


class _LinkParser(HTMLParser):
    """提取链接以及链接显示文字；同时兼容 href 和 onclick 中的 URL。"""

    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "a":
            # href 常常只是 javascript:void(0)，真实地址放在 onclick。
            self._href = " ".join(filter(None, (attrs.get("href"), attrs.get("onclick"))))
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href = None
            self._text = []


def _page_is_closed(text):
    return any(marker in text for marker in _CLOSED_MARKERS)


def _extract_urls(text, keyword):
    """从 href/onclick/脚本字符串中提取包含 keyword 的站内 URL。"""
    found = []
    parser = _LinkParser()
    try:
        parser.feed(text)
    except Exception:
        pass
    for raw, label in parser.links:
        m = re.search(r"((?:https?://[^'\"\s]+|/?jsxsd/[^'\"\s]+))", html.unescape(raw))
        if m and keyword.lower() in m.group(1).lower():
            found.append((m.group(1), label))

    # 有些入口只写在 JavaScript 变量或 window.location 中，不在 a.href 里。
    pattern = r"((?:https?://[^'\"<>\s]+|/?jsxsd/[^'\"<>\s]+))"
    for url in re.findall(pattern, html.unescape(text)):
        if keyword.lower() in url.lower():
            found.append((url, ""))

    unique = []
    seen = set()
    for url, label in found:
        url = url.replace("&amp;", "&").rstrip(");,")
        if url not in seen:
            seen.add(url)
            unique.append((url, label))
    return unique


def _discover_course_apis(base, entry_url, page_text):
    """从课程类型页面的 JavaScript 中识别列表与提交接口。"""
    urls = []
    pattern = r"((?:https?://[^'\"<>\s]+|/?jsxsd/xsxkkc/[^'\"<>\s?]+)(?:\?[^'\"<>\s]*)?)"
    for raw in re.findall(pattern, html.unescape(page_text)):
        url = urljoin(base + "/", raw.replace("&amp;", "&").rstrip(");,"))
        if url not in urls:
            urls.append(url)

    # 兼容同目录相对地址，例如 url: 'xsxkTyk'、$.get('tykxkOper?...')。
    entry_dir = entry_url.rsplit("/", 1)[0] + "/"
    for raw in re.findall(r"['\"]([A-Za-z0-9_]*(?:xsxk|Oper)[A-Za-z0-9_]*(?:\?[^'\"]*)?)['\"]",
                          html.unescape(page_text), re.I):
        url = urljoin(entry_dir, raw.rstrip(");,"))
        if url not in urls:
            urls.append(url)

    # 必须检查最后一个路径段。公共目录 xsxkkc 本身含 xsxk，
    # 对完整 URL 做子串匹配会把 download 等任何地址都认成列表。
    lists, submits = [], []
    for url in urls:
        parsed = urlsplit(url)
        if parsed.netloc != urlsplit(base).netloc:
            continue
        if not parsed.path.startswith("/jsxsd/xsxkkc/"):
            continue
        name = parsed.path.rsplit("/", 1)[-1]
        if re.fullmatch(r"xsxk[A-Za-z0-9_]+", name, re.I) and not re.search(
                r"oper|comein|index|download", name, re.I):
            lists.append(url)
        elif re.fullmatch(r"[A-Za-z0-9_]*xkOper", name, re.I):
            # 去掉页面示例/拼接中携带的课程参数，提交时传实际所选课程。
            submits.append(url.split("?", 1)[0])
    lists = list(dict.fromkeys(lists))
    submits = list(dict.fromkeys(submits))
    # DataTables 明确声明的数据源比 URL 名称更可靠。必修页面还含有
    # xsxkBxxkCfbs、xsxkJsjjview、xsxkKcjjview，它们都不是主列表。
    sources = re.findall(r'''["']?sAjaxSource["']?\s*:\s*["']([^"']+)["']''',
                         html.unescape(page_text))
    declared = list(dict.fromkeys(urljoin(entry_dir, raw) for raw in sources))
    if declared:
        lists = [url for url in declared if url in lists]
    # 用户提供的沈阳工业大学必修页面：校区下拉框默认空值（全部校区）。
    # 只为已观察到的该接口与参数组合补充默认值，不执行网页 JavaScript。
    if len(lists) == 1 and urlsplit(lists[0]).path.endswith('/xsxkBxxk'):
        if '?skxq_xx0103=' in page_text and not urlsplit(lists[0]).query:
            lists[0] += '?skxq_xx0103='
    # 不按入口名称猜接口，也不在多个候选中随意取第一个。
    return (lists[0] if len(lists) == 1 else None,
            submits[0] if len(submits) == 1 else None)


def _verify_course_list(session, url, referer):
    """只查询一条数据验证接口，绝不调用选课提交接口。"""
    try:
        response = session.post(url, data={
            "sEcho": "1", "iDisplayStart": "0", "iDisplayLength": "1",
            "iColumns": "14", "sColumns": "",
        }, headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"}, timeout=15)
        response.encoding = response.apparent_encoding
        check_response(response)
        try:
            data = json.loads(response.text)
        except ValueError:
            return False, "返回非 JSON，可能是未开放提示、登录页或错误页面"
        if not isinstance(data, dict) or not isinstance(data.get("aaData"), list):
            return False, "返回内容不是课程列表（缺少 aaData 数组）"
        return True, ""
    except requests.RequestException as exc:
        return False, f"列表验证请求失败：{type(exc).__name__}"


def _new_session() -> requests.Session:
    """创建一个带浏览器样式的 Session。"""
    s = requests.Session()
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    s.headers["Referer"] = config.BASE_URL + "/"
    return s


def build_session() -> requests.Session:
    """旧方式：用 config 里的 Cookie 构建"已登录"的 Session。
       Cookie 为空/占位时会抛异常，提示改用完整登录。"""
    if not config.COOKIE_STR or "你的" in config.COOKIE_STR or "在这里粘贴" in config.COOKIE_STR:
        raise ValueError(
            "config.py 里没有有效 Cookie。\n"
            "推荐在 config.py 填好 USERNAME/PASSWORD，脚本会自动用"
            " 账号+密码+验证码 完整登录（更省事、更可靠）。"
        )
    s = _new_session()
    s.headers["Cookie"] = config.COOKIE_STR
    return s


def whoami(s: requests.Session) -> str:
    """请求一个需要登录的页面，确认会话是否真的有效。
       登录页和正常页状态码都是 200，所以用页面内容判断，不能只看状态码。"""
    url = config.BASE_URL + "/jsxsd/framework/xsMain.jsp"
    try:
        r = s.get(url, timeout=10)
        r.encoding = r.apparent_encoding
        text = r.text
        is_login_page = "登录" in text[:600] or "login" in text.lower()
        if "xsMain" in text or (r.status_code == 200 and not is_login_page):
            return f"✅ 会话有效（状态码 {r.status_code}）"
        if is_login_page:
            return "⚠️ 会话已失效！返回的是【登录页】。"
        return f"状态码 {r.status_code}，页面异常"
    except Exception as e:
        return f"请求出错: {e}"


# ==================== 完整登录（账号+密码+验证码） ====================

def _predict_captcha(img_bytes, ocr=None):
    """用 ddddocr 识别验证码。返回识别到的字符串；失败/未装库返回 ''。"""
    try:
        if ocr is None:
            import ddddocr
            ocr = ddddocr.DdddOcr(show_ad=False)
        text = ocr.classification(img_bytes)
        return (text or "").strip()
    except Exception:
        return ""


def _get_captcha(session, path="captcha.png"):
    """下载验证码图片到本地文件，返回 (文件路径, 图片bytes)。"""
    r = session.get(config.BASE_URL + "/jsxsd/verifycode.servlet", timeout=10)
    with open(path, "wb") as f:
        f.write(r.content)
    return os.path.abspath(path), r.content


def login_with_account(username=None, password=None, max_tries=5) -> requests.Session:
    """
    完整登录：账号 + 密码 + 验证码。
    验证码图片会自动打开（captcha.png），你看图手动输入。

    ⚠️ 注意：登录会踢掉浏览器里同账号的旧会话（教务系统单会话限制）。

    返回已登录的 Session；多次失败抛 RuntimeError。
    """
    username = (username or config.USERNAME or "").strip()
    password = password or config.PASSWORD or ""
    if not username or "你的" in username:
        raise ValueError("请先在 config.py 填 USERNAME/PASSWORD，或运行到提示时输入账号密码")

    for attempt in range(1, max_tries + 1):
        print(f"\n===== 登录尝试 {attempt}/{max_tries} =====")
        s = _new_session()

        # 1. 访问登录页，拿初始 Cookie（会话和验证码绑定）
        s.get(config.BASE_URL + "/jsxsd/", timeout=10)

        # 2. 下载验证码，打开图片人工输入
        img_path, img_bytes = _get_captcha(s)
        print(f"验证码图片已保存并打开: {img_path}")
        try:
            os.startfile(img_path)  # Windows 下用默认看图程序打开
        except Exception:
            print("（未能自动打开图片，请手动打开上面的路径查看）")
        code = input("请看图片输入验证码（输完按回车）: ").strip()

        # 3. 构造登录参数：encoded = base64(账号) + "%%%" + base64(密码)
        encoded = (
            base64.b64encode(username.encode("utf-8")).decode("utf-8")
            + "%%%"
            + base64.b64encode(password.encode("utf-8")).decode("utf-8")
        )
        data = {
            "userAccount": username,
            "userPassword": password,
            "RANDOMCODE": code,
            "encoded": encoded,
        }
        r = s.post(config.BASE_URL + "/jsxsd/xk/LoginToXk", data=data, timeout=10)
        r.encoding = r.apparent_encoding

        # 4. 判断结果：登录成功会 302 跳转到系统主页(xsMain.jsp)
        if "xsMain" in r.url or "成功" in r.text[:200]:
            print("✅ 登录成功！")
            return s
        # 失败：可能是验证码错/账号密码错，打印服务器提示帮助判断
        print(f"登录未成功，服务器返回片段: {r.text[:200].strip()}")
        if "验证码" in r.text[:300] or "RANDOMCODE" in r.text[:300]:
            print("（看起来是验证码问题，将重新生成验证码再试）")
        elif attempt == max_tries:
            print("多次失败，请检查账号密码是否正确。")

    raise RuntimeError("登录失败次数过多，请检查账号密码后重试")


def _enter_xk_once(session, batch_id=None, entry_url_target=None, emit=print, cancel=None) -> bool:
    """自动寻找当前开放的批次和课程类型入口，并建立对应选课会话。

       成功后把批次、入口、列表及提交 URL 保存到 session._xk_context，
       qk_list/qk_grab 会使用这组动态接口。返回 True=正常进入。
    """
    base = config.BASE_URL
    unidentified = False
    session._xk_context = None

    # 1. 选课中心：不能 re.search 后直接取第一个，页面可能同时存在多个批次。
    r = session.get(base + "/jsxsd/xsxk/xklc_list", timeout=10,
                    headers={"Referer": base + "/jsxsd/framework/xsMain.jsp"})
    check_response(r)
    batch_links = _extract_urls(r.text, "xsxk_index")
    batches = []
    for raw_url, label in batch_links:
        m = re.search(r"jx0502zbid=([0-9A-Fa-f]+)", raw_url, re.I)
        if m and all(item[0] != m.group(1) for item in batches):
            batches.append((m.group(1), label))
    if batch_id:
        batches = [item for item in batches if item[0] == batch_id]
    if not batches:
        emit("⚠️ 找不到当前可用的选课批次（可能未到选课时间或无选课任务）。")
        raise Retryable("目标批次尚未出现或尚未开放")

    # 2. 逐个批次读取类型入口；服务器明确说未开放就跳过。
    for zbid, batch_label in batches:
        if cancel and cancel.is_set():
            raise Cancelled()
        index_url = base + f"/jsxsd/xsxk/xsxk_index?jx0502zbid={zbid}"
        index = session.get(index_url, timeout=10,
                            headers={"Referer": base + "/jsxsd/xsxk/xklc_list"})
        check_response(index)
        if "已在别处登录" in index.text:
            emit("⚠️ 系统提示：当前账号已在别处登录，请先在其他设备/浏览器退出登录再试。")
            return False
        entries = _extract_urls(index.text, "comeIn")
        entries = [(urljoin(base + "/", u), label) for u, label in entries
                   if "xsxkkc" in u.lower()]
        if entry_url_target:
            entries = [item for item in entries if item[0] == entry_url_target]
        for entry_url, entry_label in entries:
            if cancel and cancel.is_set():
                raise Cancelled()
            page = session.get(entry_url, timeout=10, headers={"Referer": index_url})
            check_response(page)
            if "已在别处登录" in page.text:
                emit("⚠️ 当前账号已在别处登录，请先在其他设备/浏览器退出登录再试。")
                return False
            if page.status_code != 200 or _page_is_closed(page.text):
                continue

            list_url, grab_url = _discover_course_apis(base, entry_url, page.text)
            kind = entry_label or batch_label or entry_url.rsplit("/", 1)[-1]
            if not list_url or not grab_url:
                unidentified = True
                emit(f"⚠️ 跳过 {kind}：页面中未能唯一识别列表及提交接口。入口：{entry_url}")
                continue
            if cancel and cancel.is_set():
                raise Cancelled()
            valid, reason = _verify_course_list(session, list_url, entry_url)
            if not valid:
                emit(f"⚠️ 跳过 {kind}：{reason}。列表接口：{list_url}")
                continue
            session._xk_context = {
                "batch_id": zbid,
                "batch_label": batch_label,
                "course_type": kind,
                "entry_url": entry_url,
                "list_url": list_url,
                "grab_url": grab_url,
                "referer": entry_url,
            }
            emit(f"✅ 已自动进入选课系统（批次 {zbid}，{kind}）")
            emit(f"   列表接口：{list_url}")
            emit(f"   提交接口：{grab_url}")
            return True

    if unidentified:
        raise RuntimeError("目标页面接口无法识别，请保留页面响应用于排查")
    raise Retryable("目标入口尚未开放或列表暂时不可用")


def enter_xk(session, batch_id=None, entry_url_target=None, emit=print, cancel=None, wait_seconds=120):
    return retry_read(lambda: _enter_xk_once(session, batch_id, entry_url_target, emit, cancel),
                      emit, cancel, wait_seconds)


def get_session() -> requests.Session:
    """统一入口：优先用 Cookie；没有有效 Cookie 就完整登录。"""
    # 有 Cookie 就先试 Cookie
    if config.COOKIE_STR and "你的" not in config.COOKIE_STR and "在这里粘贴" not in config.COOKIE_STR:
        s = build_session()
        if "会话有效" in whoami(s):
            return s
        print("已有 Cookie 失效，改走完整登录...")
    return login_with_account()


if __name__ == "__main__":
    session = get_session()
    print(whoami(session))
    input("（按回车键退出...）")
