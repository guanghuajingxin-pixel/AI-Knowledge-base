"""XXL-Job admin API 客户端 + crontab→Quartz cron 转换。

- 登录：3.x POST /auth/doLogin、2.x POST /login（userName/password，自动探测），会话 cookie 复用；cookie 失效自动重登一次。
- 作业/执行器组 CRUD 走 admin 的 /jobgroup/*、/jobinfo/*、/joblog/* 表单接口（ReturnT JSON）。
- 本平台同步源为作业配置唯一事实源：scheduler 模块据此幂等同步作业到 admin。
"""
from __future__ import annotations

import logging
import threading

import requests

logger = logging.getLogger(__name__)


class XxlJobAdminError(Exception):
    """admin 不可达 / 鉴权失败 / 接口返回非 200 code。"""


# ---------- cron 转换：5 段 crontab → Quartz 6 段（秒固定 0） ----------
_DOW_NAMES = {"sun": "SUN", "mon": "MON", "tue": "TUE", "wed": "WED",
              "thu": "THU", "fri": "FRI", "sat": "SAT"}


def _dow_token(tok: str) -> str:
    """crontab 星期（数字 0-7，0/7=周日；或 mon/tue 名称）→ Quartz（1-7，1=周日 / 同名大写）。"""
    tok = tok.strip()
    if tok.isalpha():
        key = tok.lower()[:3]
        if key not in _DOW_NAMES:
            raise ValueError(f"无法识别的星期字段: {tok}")
        return _DOW_NAMES[key]
    return str(int(tok) % 7 + 1)


def _convert_dow_field(field: str) -> str:
    if field in ("*", "?"):
        return "*"
    out: list[str] = []
    for part in field.split(","):
        step = ""
        if "/" in part:
            part, _, step = part.partition("/")
            step = "/" + step
        if part == "*":
            out.append("*" + step)
        elif part.isalpha():
            key = part.lower()[:3]
            if key not in _DOW_NAMES:
                raise ValueError(f"无法识别的星期字段: {part}")
            out.append(_DOW_NAMES[key] + step)
        elif "-" in part:
            a, _, b = part.partition("-")
            out.append(f"{_dow_token(a)}-{_dow_token(b)}" + step)
        else:
            out.append(_dow_token(part) + step)
    return ",".join(out)


def crontab_to_quartz(expr: str) -> str:
    """5 段 crontab（分 时 日 月 周）→ XXL-Job(Quartz) 6 段 cron（秒 分 时 日 月 周）。

    Quartz 限制：日与周不能同时指定（本平台口径：报 ValueError 由调用方提示用户）。
    """
    fields = (expr or "").split()
    if len(fields) != 5:
        raise ValueError(f"cron 必须为 5 段 crontab 格式（分 时 日 月 周），收到: {expr!r}")
    minute, hour, dom, month, dow = fields
    if dom not in ("*", "?") and dow not in ("*", "?"):
        raise ValueError("XXL-Job(Quartz) 不支持同时指定「日」和「周」，请调整 cron 表达式")
    dom_q = dom if dom not in ("*", "?") else ("?" if dow not in ("*", "?") else "*")
    dow_q = _convert_dow_field(dow) if dow not in ("*", "?") else "?"
    return f"0 {minute} {hour} {dom_q} {month} {dow_q}"


# ---------- admin 客户端 ----------
class XxlJobAdminClient:
    def __init__(self, base_url: str, username: str, password: str, access_token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.access_token = access_token or ""
        self._session: requests.Session | None = None
        self._lock = threading.Lock()

    # ----- 会话 -----
    def _headers(self) -> dict:
        return {"XXL-JOB-ACCESS-TOKEN": self.access_token} if self.access_token else {}

    def _login(self) -> requests.Session:
        session = requests.Session()
        # 登录接口按版本探测：3.x 为 /auth/doLogin，2.x 为 /login（成功可能 200 或 302+cookie）
        last_status = 0
        for path in ("/auth/doLogin", "/login"):
            try:
                resp = session.post(
                    f"{self.base_url}{path}",
                    data={"userName": self.username, "password": self.password},
                    headers=self._headers(), timeout=10, allow_redirects=False)
            except requests.RequestException:
                continue
            last_status = resp.status_code
            ok_code = resp.status_code in (200, 302)
            ok_json = "json" in resp.headers.get("Content-Type", "") and \
                resp.json().get("code") == 200
            if ok_code and ok_json and session.cookies:
                return session
            session.cookies.clear()
        raise XxlJobAdminError(
            f"XXL-Job admin 登录失败（HTTP {last_status or 'unreachable'}），请检查地址与账号")

    def _ensure_session(self) -> requests.Session:
        with self._lock:
            if self._session is None:
                self._session = self._login()
            return self._session

    def _reset_session(self) -> None:
        with self._lock:
            self._session = None

    def ping(self) -> bool:
        """启动自检：登录一次，失败抛 XxlJobAdminError（fail fast）。"""
        self._reset_session()
        self._ensure_session()
        return True

    def _post(self, path: str, data: dict | None = None, retries: int = 1):
        """表单 POST；cookie 失效（返回登录页 HTML）自动重登重试一次。"""
        for attempt in range(retries + 1):
            session = self._ensure_session()
            try:
                resp = session.post(f"{self.base_url}{path}", data=data or {},
                                    headers=self._headers(), timeout=15)
            except requests.RequestException as exc:
                raise XxlJobAdminError(f"XXL-Job admin 请求失败 {path}: {exc}") from exc
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" not in ctype:
                # 未登录时 admin 返回登录页 HTML
                if attempt < retries:
                    self._reset_session()
                    continue
                raise XxlJobAdminError(f"XXL-Job admin 接口 {path} 未返回 JSON（会话可能已失效）")
            payload = resp.json()
            if isinstance(payload, dict) and "code" in payload:
                if payload.get("code") != 200:
                    raise XxlJobAdminError(f"XXL-Job admin {path} 返回: {payload.get('msg')}")
            return payload
        raise XxlJobAdminError(f"XXL-Job admin 接口 {path} 重试后仍失败")

    @staticmethod
    def _rows(payload: dict) -> list[dict]:
        """分页响应取行列表：2.x 为 content.data，3.x 为 data.data。"""
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, dict):
            return data.get("data") or []
        if isinstance(data, list):
            return data
        content = payload.get("content")
        if isinstance(content, dict):
            return content.get("data") or []
        return []

    # ----- 执行器组 -----
    def ensure_group(self, appname: str, title: str) -> int:
        page = self._post("/jobgroup/pageList", {"start": 0, "length": 100,
                                                  "appname": appname, "title": ""})
        for row in self._rows(page):
            if row.get("appname") == appname:
                return int(row["id"])
        # 2.4.x 创建接口为 /jobgroup/save（旧版为 /jobgroup/add），逐个尝试
        payload = {"appname": appname, "title": title, "order": 1, "addressType": 0}
        try:
            self._post("/jobgroup/save", payload)
        except XxlJobAdminError:
            self._post("/jobgroup/add", payload)
        page = self._post("/jobgroup/pageList", {"start": 0, "length": 100,
                                                  "appname": appname, "title": ""})
        for row in self._rows(page):
            if row.get("appname") == appname:
                return int(row["id"])
        raise XxlJobAdminError(f"创建执行器组失败: {appname}")

    # ----- 作业 -----
    def get_job(self, job_id: int) -> dict | None:
        """按 ID 取作业（pageList 全组遍历匹配；作业量小，可接受）。"""
        page = self._post("/jobinfo/pageList", {"start": 0, "length": 500, "jobGroup": 0,
                                                 "triggerStatus": -1, "jobDesc": "",
                                                 "executorHandler": "", "author": ""})
        for row in self._rows(page):
            if int(row.get("id", -1)) == job_id:
                return row
        return None

    def add_job(self, spec: dict) -> int:
        result = self._post("/jobinfo/add", spec)
        # 2.x 返回 content=<id>；3.x 返回 data=<id>（dict/list 为异常载荷，忽略）
        job_id = result.get("content") or (result.get("data")
                                           if not isinstance(result.get("data"), (dict, list))
                                           else None)
        if not job_id:
            raise XxlJobAdminError(f"创建作业失败: {result.get('msg')}")
        return int(job_id)

    def update_job(self, spec: dict) -> None:
        self._post("/jobinfo/update", spec)

    def start_job(self, job_id: int) -> None:
        self._post("/jobinfo/start", {"id": job_id})

    def stop_job(self, job_id: int) -> None:
        self._post("/jobinfo/stop", {"id": job_id})

    def remove_job(self, job_id: int) -> None:
        self._post("/jobinfo/remove", {"id": job_id})

    def trigger_job(self, job_id: int, param: str = "") -> None:
        """控制台「执行一次」同款：手动触发作业（验证执行器链路用）。"""
        self._post("/jobinfo/trigger", {"id": job_id, "executorParam": param, "addressList": ""})

    def job_logs(self, job_id: int, length: int = 10) -> list[dict]:
        page = self._post("/joblog/pageList", {"start": 0, "length": length, "jobGroup": 0,
                                                "jobId": job_id, "logStatus": -1, "filterTime": 0})
        return self._rows(page)
