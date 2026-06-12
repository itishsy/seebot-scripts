# -*- coding: utf-8 -*-
"""
RPA 统计脚本数据源模块

职责
----
- 配置加载（load_config / get_mysql_config / get_mongo_config / get_standalone_config）
- 时间窗口解析（parse_stat_window / parse_stat_month）
- 公共常量与工具（QUEUE_ITEM_MAP / STATUS_MAP / queue_item_name / status_name /
                  chunks / parse_addr_name）
- DB 方式数据获取（daily / monthly 各有独立函数，不共用参数化 SQL）
- API 方式数据获取（daily 场景；monthly 场景暂无 API 版本，预留扩展）
- 通用业务统计（_build_service_stats）

命名约定
--------
  load_mysql_tasks_daily    → robot_daily_report.py 使用
  load_mysql_tasks_monthly  → robot_monthly_report.py 使用
  load_mongo_errors_daily   → robot_daily_report.py 使用
  load_mongo_errors_monthly → robot_monthly_report.py 使用
  load_api_tasks_daily      → standalone_daily_report.py 使用
  fetch_*                   → API 辅助数据（截图/run_support）
"""

import configparser
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus

import pandas as pd
import pymysql
import requests
from pymongo import MongoClient
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

_DEFAULT_CONF = Path(__file__).parent / "db.conf"

QUEUE_ITEM_MAP: Dict[int, str] = {
    1: "增减员", 6: "缴费", 7: "在册名单", 8: "费用明细",
    9: "政策通知", 10: "基数申报", 11: "登录", 12: "查审核结果",
    13: "调基名单", 14: "参保证明", 15: "获取待缴", 16: "申报核定",
    17: "缴费扣款", 18: "获取凭据", 19: "撤销核定", 20: "公司参保证明",
    21: "获取同步待缴", 22: "申报同步核定", 23: "获取缴费截图",
}

STATUS_MAP: Dict[int, str] = {
    1: "执行中",
    2: "待执行",
    3: "执行中断",
    4: "执行成功",
}


# --------------------------------------------------------------------------- #
# 配置加载
# --------------------------------------------------------------------------- #

def load_config(conf_path: Optional[Path] = None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    path = conf_path or _DEFAULT_CONF
    if path.exists():
        cfg.read(str(path), encoding="utf-8")
    return cfg


def get_mysql_config(cfg: configparser.ConfigParser) -> dict:
    sec = cfg["mysql"] if cfg.has_section("mysql") else {}
    return {
        "host":     sec.get("host")     or os.getenv("RPA_STAT_MYSQL_HOST", "127.0.0.1"),
        "port":     int(sec.get("port") or os.getenv("RPA_STAT_MYSQL_PORT", "3306")),
        "user":     sec.get("user")     or os.getenv("RPA_STAT_MYSQL_USER", "root"),
        "password": sec.get("password") or os.getenv("RPA_STAT_MYSQL_PASSWORD", ""),
        "database": sec.get("database") or os.getenv("RPA_STAT_MYSQL_DATABASE", "rpa"),
        "charset":  sec.get("charset")  or os.getenv("RPA_STAT_MYSQL_CHARSET", "utf8mb4"),
        "cursorclass": pymysql.cursors.DictCursor,
    }


def get_mongo_config(cfg: configparser.ConfigParser) -> Tuple[str, str, str]:
    sec = cfg["mongodb"] if cfg.has_section("mongodb") else {}
    uri = sec.get("uri") or os.getenv("RPA_STAT_MONGO_URI", "mongodb://127.0.0.1:27017")
    db  = sec.get("database")   or os.getenv("RPA_STAT_MONGO_DB",  "rpa")
    col = sec.get("collection") or os.getenv("RPA_STAT_MONGO_COLLECTION", "robot_execution_detail")
    username = sec.get("username") or os.getenv("RPA_STAT_MONGO_USER", "")
    password = sec.get("password") or os.getenv("RPA_STAT_MONGO_PASSWORD", "")
    if username and password and "@" not in uri:
        uri = uri.replace("mongodb://", f"mongodb://{quote_plus(username)}:{quote_plus(password)}@", 1)
    return uri, db, col


def get_standalone_config(cfg: configparser.ConfigParser) -> dict:
    sec = cfg["standalone"] if cfg.has_section("standalone") else {}
    return {
        "base_url":      (sec.get("base_url") or os.getenv("RPA_STANDALONE_BASE_URL", "http://127.0.0.1:8080")).rstrip("/"),
        "token_url":     sec.get("token_url")     or os.getenv("RPA_STANDALONE_TOKEN_URL",    "http://127.0.0.1:8888/oauth/token"),
        "client_id":     sec.get("client_id")     or os.getenv("RPA_STANDALONE_CLIENT_ID",    "client"),
        "client_secret": sec.get("client_secret") or os.getenv("RPA_STANDALONE_CLIENT_SECRET","secret"),
        "username":      sec.get("username")      or os.getenv("RPA_STANDALONE_USERNAME",     "admin"),
        "password":      sec.get("password")      or os.getenv("RPA_STANDALONE_PASSWORD",     ""),
    }


# --------------------------------------------------------------------------- #
# 时间窗口解析
# --------------------------------------------------------------------------- #

def parse_stat_window(stat_date: Optional[str]) -> Tuple[datetime, datetime]:
    """解析 YYYY-MM-DD 参数，返回当日 [00:00:00, 次日 00:00:00)。"""
    if stat_date:
        date_value = datetime.strptime(stat_date, "%Y-%m-%d").date()
    else:
        date_value = datetime.now().date()
    start_time = datetime.combine(date_value, datetime.min.time())
    end_time   = start_time + timedelta(days=1)
    return start_time, end_time


def parse_stat_month(month_str: Optional[str]) -> Tuple[datetime, datetime, str]:
    """解析 YYYY-MM 参数，返回月份 [首日 00:00:00, 次月首日 00:00:00) 及标签。"""
    if month_str:
        base = datetime.strptime(month_str, "%Y-%m")
    else:
        now  = datetime.now()
        base = now.replace(day=1)
    start_time = base.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_time.month == 12:
        end_time = start_time.replace(year=start_time.year + 1, month=1)
    else:
        end_time = start_time.replace(month=start_time.month + 1)
    return start_time, end_time, start_time.strftime("%Y-%m")


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #

def queue_item_name(queue_item) -> str:
    try:
        return QUEUE_ITEM_MAP.get(int(queue_item), f"未知-{queue_item}")
    except (TypeError, ValueError):
        return "未知"


def status_name(status) -> str:
    try:
        return STATUS_MAP.get(int(status), f"未知-{status}")
    except (TypeError, ValueError):
        return "未知"


def parse_addr_name(app_args: Optional[str]) -> str:
    """从 robot_app.app_args JSON 中提取 addrName（参保城市）。"""
    if not app_args:
        return ""
    try:
        obj = json.loads(app_args)
        return obj.get("addrName") or obj.get("cityName") or ""
    except (json.JSONDecodeError, TypeError):
        m = re.search(r'"addrName"\s*:\s*"([^"]*)"', app_args)
        return m.group(1) if m else ""


def chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i: i + size]


# --------------------------------------------------------------------------- #
# DB 方式 — Daily（日报专用，含截图 + run_support JOIN）
# --------------------------------------------------------------------------- #

def load_mysql_tasks_daily(
    start_time: datetime,
    end_time: datetime,
    mysql_cfg: dict,
) -> pd.DataFrame:
    """
    加载当日实际执行任务（robot_daily_report 专用）。
    包含：截图 JOIN（robot_execution_file_info）、run_support JOIN（robot_flow）。
    """
    sql = """
    SELECT
        qu.id,
        qu.client_id,
        qu.execution_code,
        qu.machine_code,
        qu.fix_machine_code,
        qu.app_code,
        qu.task_code,
        qu.declare_account,
        qu.company_name,
        qu.business_type,
        qu.declare_system,
        qu.queue_item,
        qu.login_status,
        qu.status,
        qu.comment,
        qu.pra_start_time,
        qu.pra_end_time,
        qu.create_time,
        app.app_args,
        fi.screenshot_url,
        rf.run_support
    FROM robot_task_queue qu
    LEFT JOIN robot_app app ON app.app_code = qu.app_code
    LEFT JOIN (
        SELECT execution_code, MIN(file_full_path) AS screenshot_url
        FROM robot_execution_file_info
        GROUP BY execution_code
    ) fi ON fi.execution_code = qu.execution_code
    LEFT JOIN (
        SELECT app_code, declare_system, MIN(run_support) AS run_support
        FROM robot_flow
        WHERE flow_type = 1 AND run_support = '10019005'
        GROUP BY app_code, declare_system
    ) rf ON rf.app_code = qu.app_code AND rf.declare_system = qu.declare_system
    WHERE qu.pra_start_time >= %s
      AND qu.pra_start_time < %s
      AND qu.pra_start_time IS NOT NULL
    """
    conn = pymysql.connect(**mysql_cfg)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (start_time, end_time))
            rows = cursor.fetchall()
        return pd.DataFrame(rows)
    finally:
        conn.close()


def load_mongo_errors_daily(
    execution_codes: List[str],
    start_time: datetime,
    end_time: datetime,
    mongo_uri: str,
    mongo_db: str,
    mongo_col: str,
) -> pd.DataFrame:
    """
    从 MongoDB 查询日报所需的失败明细（robot_daily_report 专用）。
    含 flowCode / stepCode 字段，用于失败步骤定位。
    """
    if not execution_codes:
        return pd.DataFrame()

    client   = MongoClient(mongo_uri)
    col      = client[mongo_db][mongo_col]
    start_ms = int(start_time.timestamp() * 1000)
    end_ms   = int(end_time.timestamp()   * 1000)
    rows = []
    try:
        for code_chunk in chunks(execution_codes, 500):
            pipeline = [
                {"$match": {
                    "executionCode": {"$in": code_chunk},
                    "startDate": {"$gte": start_ms, "$lt": end_ms},
                    "$or": [
                        {"error":      {"$exists": True, "$ne": ""}},
                        {"errorStack": {"$exists": True, "$ne": ""}},
                        {"status":     {"$nin": [1, 4]}},
                    ],
                }},
                {"$sort": {"endDate": -1}},
                {"$group": {
                    "_id":           "$executionCode",
                    "executionCode": {"$first": "$executionCode"},
                    "flowCode":      {"$first": "$flowCode"},
                    "stepCode":      {"$first": "$stepCode"},
                    "stepName":      {"$first": "$stepName"},
                    "status":        {"$first": "$status"},
                    "error":         {"$first": "$error"},
                    "errorStack":    {"$first": "$errorStack"},
                }},
            ]
            rows.extend(list(col.aggregate(pipeline, allowDiskUse=True)))
    finally:
        client.close()

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df.drop(columns=["_id"], errors="ignore", inplace=True)
    return df


# --------------------------------------------------------------------------- #
# DB 方式 — Monthly（月报专用，精简 SQL，无截图/run_support）
# --------------------------------------------------------------------------- #

def load_mysql_tasks_monthly(
    start_time: datetime,
    end_time: datetime,
    mysql_cfg: dict,
) -> pd.DataFrame:
    """
    加载整月实际执行任务（robot_monthly_report 专用）。
    精简版：无截图 JOIN、无 run_support JOIN，减少大数据量查询开销。
    """
    sql = """
    SELECT
        qu.id,
        qu.client_id,
        qu.execution_code,
        qu.machine_code,
        qu.app_code,
        qu.task_code,
        qu.declare_account,
        qu.company_name,
        qu.business_type,
        qu.declare_system,
        qu.queue_item,
        qu.status,
        qu.comment,
        qu.pra_start_time,
        qu.pra_end_time,
        app.app_args
    FROM robot_task_queue qu
    LEFT JOIN robot_app app ON app.app_code = qu.app_code
    WHERE qu.pra_start_time >= %s
      AND qu.pra_start_time < %s
      AND qu.pra_start_time IS NOT NULL
    """
    conn = pymysql.connect(**mysql_cfg)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (start_time, end_time))
            rows = cursor.fetchall()
        return pd.DataFrame(rows)
    finally:
        conn.close()


def load_mongo_errors_monthly(
    execution_codes: List[str],
    start_time: datetime,
    end_time: datetime,
    mongo_uri: str,
    mongo_db: str,
    mongo_col: str,
) -> pd.DataFrame:
    """
    从 MongoDB 查询月报所需的失败明细（robot_monthly_report 专用）。
    精简版：只取 stepName / error / errorStack，不含 flowCode / stepCode。
    """
    if not execution_codes:
        return pd.DataFrame()

    client   = MongoClient(mongo_uri)
    col      = client[mongo_db][mongo_col]
    start_ms = int(start_time.timestamp() * 1000)
    end_ms   = int(end_time.timestamp()   * 1000)
    rows = []
    try:
        for code_chunk in chunks(execution_codes, 500):
            pipeline = [
                {"$match": {
                    "executionCode": {"$in": code_chunk},
                    "startDate": {"$gte": start_ms, "$lt": end_ms},
                    "$or": [
                        {"error":      {"$exists": True, "$ne": ""}},
                        {"errorStack": {"$exists": True, "$ne": ""}},
                        {"status":     {"$nin": [1, 4]}},
                    ],
                }},
                {"$sort": {"endDate": -1}},
                {"$group": {
                    "_id":           "$executionCode",
                    "executionCode": {"$first": "$executionCode"},
                    "stepName":      {"$first": "$stepName"},
                    "error":         {"$first": "$error"},
                    "errorStack":    {"$first": "$errorStack"},
                }},
            ]
            rows.extend(list(col.aggregate(pipeline, allowDiskUse=True)))
    finally:
        client.close()

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df.drop(columns=["_id"], errors="ignore", inplace=True)
    return df


# --------------------------------------------------------------------------- #
# API 方式 — 公共基础设施
# --------------------------------------------------------------------------- #

def make_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    """创建配备指数退避重试机制的 requests.Session。"""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_token(conf: dict, session: requests.Session) -> str:
    """通过 OAuth2 password 模式获取 access_token。"""
    resp = session.post(
        conf["token_url"],
        data={
            "grant_type": "password",
            "username":   conf["username"],
            "password":   conf["password"],
        },
        auth=(conf["client_id"], conf["client_secret"]),
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"获取 token 失败，响应：{resp.text}")
    return token


def _extract_rows(body) -> List[dict]:
    """解析分页响应，兼容包装/直接/列表三种结构。"""
    if isinstance(body, list):
        return body
    inner  = body.get("data") if isinstance(body, dict) else None
    target = inner if isinstance(inner, dict) else body
    return target.get("rows") or target.get("list") or target.get("content") or []


def _post_api(session: requests.Session, url: str, token: str, payload: dict, timeout: int = 30) -> dict:
    """统一 POST 请求封装，自动处理 ResponseDTO 包装。"""
    resp = session.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


# --------------------------------------------------------------------------- #
# API 方式 — Daily（standalone_daily_report 专用）
# --------------------------------------------------------------------------- #

def _fetch_task_queue_page(
    base_url: str,
    token: str,
    session: requests.Session,
    page: int,
    start_time: datetime,
    end_time: datetime,
    page_size: int = 500,
) -> List[dict]:
    """调用 /robot/taskQueue/page，携带服务端时间过滤，按 pra_end_time 降序取单页。"""
    payload = {
        "page": page,
        "size": page_size,
        "sidx": "qu.pra_end_time",
        "sort": "desc",
        # 服务端时间过滤：让服务器只返回目标日期的记录，减少无效传输
        "praStartTimeFrom": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "praStartTimeTo":   end_time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    resp = session.post(
        f"{base_url}/robot/taskQueue/page",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return _extract_rows(resp.json())


def load_api_tasks_daily(
    base_url: str,
    token: str,
    session: requests.Session,
    start_time: datetime,
    end_time: datetime,
) -> List[dict]:
    """
    通过 API 分页加载当日任务队列（standalone_daily_report 专用）。

    优化说明：
    - page_size 从 200 提升至 500，减少总请求次数
    - payload 携带 praStartTimeFrom/praStartTimeTo，由服务端过滤日期，
      避免客户端逐页扫描历史数据；服务端若不支持该参数则降级为客户端过滤
    - 保留整页早于目标日期时提前终止的兜底逻辑
    """
    page_size     = 500
    all_rows: List[dict] = []
    page          = 1
    stat_date_str = start_time.strftime("%Y-%m-%d")

    while True:
        rows = _fetch_task_queue_page(base_url, token, session, page, start_time, end_time, page_size)
        if not rows:
            break

        matched, past, future = [], 0, 0
        for r in rows:
            pra = str(r.get("praStartTime") or r.get("pra_start_time") or "")
            if not pra:
                continue
            pra_date = pra[:10]
            if pra_date == stat_date_str:
                matched.append(r)
            elif pra_date < stat_date_str:
                past += 1
            else:
                future += 1

        all_rows.extend(matched)
        print(f"  第{page}页: 命中{len(matched)} 早于{past} 晚于{future} | 累计{len(all_rows)}", end="\r")

        # 服务端已过滤时整页均为目标日期，无 past；兜底：整页全部早于目标日期时终止
        if past == len(rows):
            print(f"\n  整页记录均早于 {stat_date_str}，提前终止")
            break
        if len(rows) < page_size:
            break
        page += 1

    print()
    return all_rows


def fetch_screenshot_map_daily(
    base_url: str,
    token: str,
    session: requests.Session,
    start_time: datetime,
    end_time: datetime,
) -> dict:
    """
    拉取当日执行记录的截图信息（standalone_daily_report 专用）。
    返回 {executionCode → screenshot_url}，取第一张截图。
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "execStartTime": start_time.strftime("%Y-%m-%d"),
        "execEndTime":   end_time.strftime("%Y-%m-%d"),
    }
    ec_to_url: dict = {}
    try:
        resp = session.post(
            f"{base_url}/robot/app/client/executionList",
            json=payload, headers=headers, timeout=60,
        )
        resp.raise_for_status()
        body    = resp.json()
        records = body if isinstance(body, list) else (body.get("data") or body)
        if isinstance(records, dict):
            records = records.get("rows") or records.get("list") or records.get("records") or []
        for rec in (records or []):
            ec    = rec.get("executionCode") or rec.get("execution_code")
            files = rec.get("reportFile") or []
            if ec and files:
                for f in files:
                    url = f.get("fileFullPath") or f.get("filePath") or f.get("fileName")
                    if url:
                        ec_to_url[str(ec)] = url
                        break
    except Exception as e:
        print(f"  截图信息拉取失败（跳过）: {e}")
    return ec_to_url


def fetch_run_support_map_daily(
    base_url: str,
    token: str,
    session: requests.Session,
    max_workers: int = 5,
    request_interval: float = 0.1,
) -> dict:
    """
    获取 appCode → run_support 映射（standalone_daily_report 专用）。
    调用 getRobotAppList + getScheduleFlow 两级 API，取主流程的 run_support。

    优化说明：
    - 原实现对每个 app 串行发 getScheduleFlow，N 个 app = N 次串行 HTTP 请求
    - 改为 ThreadPoolExecutor 并发，默认 5 个线程，大幅减少总耗时
    - max_workers 限制并发数，避免瞬间打爆服务器连接
    - request_interval 每个任务提交前的小间隔，进一步分散请求压力
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    app_code_to_rs: dict = {}

    try:
        resp = session.post(
            f"{base_url}/robot/app/client/getRobotAppList",
            json={}, headers=headers, timeout=30,
        )
        resp.raise_for_status()
        body  = resp.json()
        inner = body.get("data", body) if isinstance(body, dict) else body
        apps  = inner if isinstance(inner, list) else (inner.get("rows") or inner.get("list") or [])
    except Exception as e:
        print(f"  run_support 映射拉取失败（跳过）: {e}")
        return app_code_to_rs

    app_codes = [
        str(app.get("appCode") or app.get("app_code"))
        for app in (apps or [])
        if app.get("appCode") or app.get("app_code")
    ]

    def _fetch_one(app_code: str) -> tuple:
        try:
            flow_resp = session.post(
                f"{base_url}/robot/flow/getScheduleFlow",
                params={"appCode": app_code},
                headers=headers, timeout=15,
            )
            flow_resp.raise_for_status()
            flow_body  = flow_resp.json()
            flow_inner = flow_body.get("data", flow_body) if isinstance(flow_body, dict) else flow_body
            flows      = flow_inner if isinstance(flow_inner, list) else []
            for f in flows:
                rs = f.get("runSupport") or f.get("run_support")
                if rs:
                    return app_code, str(rs)
        except Exception:
            pass
        return app_code, None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for app_code in app_codes:
            futures[executor.submit(_fetch_one, app_code)] = app_code
            time.sleep(request_interval)  # 分散提交，避免瞬间并发

        for future in as_completed(futures):
            app_code, rs = future.result()
            if rs:
                app_code_to_rs[app_code] = rs

    return app_code_to_rs


def fetch_execution_errors_daily(
    base_url: str,
    token: str,
    session: requests.Session,
    fail_rows: List[dict],
    max_workers: int = 5,
    request_interval: float = 0.05,
) -> pd.DataFrame:
    """
    批量调用 /robot/app/client/client/execution/detail，
    为每条失败任务补充 stepName / error / errorStack（standalone_daily_report 专用）。

    返回结构与 load_mongo_errors_daily 对齐：
      columns: executionCode, stepName, error, errorStack

    实现说明：
    - 每个失败任务需要 executionCode + flowCode 才能查明细；
      flowCode 优先取 task 的 flowCode，缺失则跳过。
    - 取最后一个 status=0（失败）的步骤作为代表步骤，兜底取最后一步。
    - ThreadPoolExecutor 并发，避免串行逐条请求产生高延迟。
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _fetch_one(row: dict) -> Optional[dict]:
        ec        = str(row.get("executionCode") or row.get("execution_code") or "").strip()
        flow_code = str(row.get("flowCode")      or row.get("flow_code")       or "").strip()
        if not ec or not flow_code:
            return None
        try:
            resp = session.post(
                f"{base_url}/robot/app/client/client/execution/detail",
                params={"executionCode": ec, "flowCode": flow_code},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            body    = resp.json()
            details = body.get("data") or body if isinstance(body, dict) else body
            if not isinstance(details, list) or not details:
                return None
            # 优先取最后一个失败步骤（status=0），兜底取最后一步
            fail_steps = [d for d in details if d.get("status") == 0]
            target     = fail_steps[-1] if fail_steps else details[-1]
            return {
                "executionCode": ec,
                "stepName":      str(target.get("stepName")   or "").strip(),
                "error":         str(target.get("error")       or "").strip(),
                "errorStack":    str(target.get("errorStack")  or "").strip(),
            }
        except Exception:
            return None

    results: List[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for row in fail_rows:
            futures[executor.submit(_fetch_one, row)] = row
            time.sleep(request_interval)
        for future in as_completed(futures):
            r = future.result()
            if r:
                results.append(r)

    if not results:
        return pd.DataFrame(columns=["executionCode", "stepName", "error", "errorStack"])
    return pd.DataFrame(results)


# --------------------------------------------------------------------------- #
# 通用业务统计（daily / standalone 共用）
# --------------------------------------------------------------------------- #

def build_service_stats(
    df: pd.DataFrame,
    status_col: str,
    s4: int,
    s3: int,
    city_col: str,
    machine_col: str,
    duration_col: str,
    fail_step_col: str,
    fail_reason_col: str,
) -> pd.DataFrame:
    """
    按 task_type（业务类型）分组，汇总执行结果、时长及主要问题。
    被 robot_daily_report 和 standalone_daily_report 共同引用。
    """
    records = []
    for task_type, grp in df.groupby("task_type", dropna=False):
        total   = len(grp)
        success = int((grp[status_col] == s4).sum())
        fail    = int((grp[status_col] == s3).sum())
        rate    = f"{round(success / total * 100, 1)}%" if total else "—"

        dur       = grp[duration_col].dropna()
        total_min = round(dur.sum(),  1)
        avg_min   = round(dur.mean(), 1) if len(dur) else 0
        max_min   = round(dur.max(),  1) if len(dur) else 0

        fail_grp = grp[grp[status_col] == s3]

        main_reason = (
            fail_grp[fail_reason_col].value_counts().index[0]
            if len(fail_grp) and fail_reason_col in fail_grp.columns
            else "—"
        )

        def _count_reason(keyword: str) -> int:
            return int((fail_grp[fail_reason_col].str.contains(keyword, na=False)).sum()) if len(fail_grp) else 0

        top_city = (
            fail_grp[city_col].value_counts().index[0]
            if len(fail_grp) and city_col in fail_grp.columns else "—"
        )
        top_machine = (
            fail_grp[machine_col].value_counts().index[0]
            if len(fail_grp) and machine_col in fail_grp.columns else "—"
        )
        if len(fail_grp) and fail_step_col in fail_grp.columns:
            step_counts = (
                fail_grp[fail_step_col]
                .dropna()
                .loc[lambda s: s.str.strip() != ""]
                .value_counts()
            )
            top_step = step_counts.index[0][:80] if len(step_counts) else "—"
        else:
            top_step = "—"

        records.append({
            "业务类型（服务项）": task_type,
            "任务总数":         total,
            "成功数":           success,
            "失败数":           fail,
            "成功率":           rate,
            "总执行时长(分)":   total_min,
            "平均执行时长(分)": avg_min,
            "最大执行时长(分)": max_min,
            "主要失败原因":     main_reason,
            "客户端异常数":     _count_reason("客户端异常"),
            "登录无效数":       _count_reason("登录无效"),
            "UKey异常数":       _count_reason("UKey异常"),
            "Top城市":          top_city,
            "Top盒子":          top_machine,
            "Top失败步骤":      top_step,
        })

    result = pd.DataFrame(records)
    if not result.empty:
        result.sort_values("任务总数", ascending=False, inplace=True)
        result.reset_index(drop=True, inplace=True)
    return result
