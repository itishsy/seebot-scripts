# -*- coding: utf-8 -*-
"""Generate daily robot execution statistics for standalone deployment via REST API.

不直连数据库，通过 rpa-standalone rpa-client-api 接口拉取数据后统计。

Usage:
    python scripts/statistics/standalone_daily_report.py
    python scripts/statistics/standalone_daily_report.py --date 2026-05-31
    python scripts/statistics/standalone_daily_report.py --output reports/standalone_daily_report.xlsx
    python scripts/statistics/standalone_daily_report.py --config /etc/rpa/db.conf
"""

import argparse
import configparser
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# 配置加载
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

INVALID_REASONS = ["客户端异常", "网页端异常"]

# 异常细分（顺序即优先级，先匹配先命中）
CLASSIFY_RULES = [
    ("报盘为空",        r"报盘为空|数据为空|待缴为空|费用为空|无待缴|没有待缴|未查询到待缴|无数据|没有数据"),
    ("非办理时间",      r"非办理时间|不在办理时间|办理时间|业务办理时间|系统维护|暂停办理"),
    ("UKey异常",        r"UKey|ukey|U盾|USB|usbkey|激活USB|证书过期|CA证书|ukey认证|key失效|key未插|key未找到"),
    ("登录无效",        r"登录|登录无效|登录失效|会话失效|未登录|重新登录|验证码错误|账号密码错误|登录失败"),
    ("操作指令失败",     r"win图片|图片点击失败|元素|控件|找不到元素|Selenium|Chrome|浏览器|cookie|token"),
    ("网站异常",        r"网站异常|网站超时|页面打不开|页面加载|网站无响应|网络异常|连接失败|连接超时|请求超时|接口超时|http error|502|503|504"),
    ("配置错误",        r"java.lang|NullPointerException|ClassCastException|RobotInterruptException|RobotRuntimeException|RuntimeException"),
    ("其它原因",        r"环境异常|超时|500"),
]

def load_config(conf_path: Optional[Path] = None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    path = conf_path or _DEFAULT_CONF
    if path.exists():
        cfg.read(str(path), encoding="utf-8")
    return cfg


def get_standalone_config(cfg: configparser.ConfigParser) -> dict:
    sec = cfg["standalone"] if cfg.has_section("standalone") else {}
    return {
        "base_url":     (sec.get("base_url") or os.getenv("RPA_STANDALONE_BASE_URL", "http://127.0.0.1:8080")).rstrip("/"),
        "token_url":    sec.get("token_url") or os.getenv("RPA_STANDALONE_TOKEN_URL", "http://127.0.0.1:8888/oauth/token"),
        "client_id":    sec.get("client_id") or os.getenv("RPA_STANDALONE_CLIENT_ID", "client"),
        "client_secret": sec.get("client_secret") or os.getenv("RPA_STANDALONE_CLIENT_SECRET", "secret"),
        "username":     sec.get("username") or os.getenv("RPA_STANDALONE_USERNAME", "admin"),
        "password":     sec.get("password") or os.getenv("RPA_STANDALONE_PASSWORD", ""),
    }


# --------------------------------------------------------------------------- #
# HTTP 会话
# --------------------------------------------------------------------------- #

def make_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
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
            "username": conf["username"],
            "password": conf["password"],
        },
        auth=(conf["client_id"], conf["client_secret"]),
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"获取 token 失败，响应：{resp.text}")
    return token


# --------------------------------------------------------------------------- #
# 接口调用
# --------------------------------------------------------------------------- #

def _post(session: requests.Session, url: str, token: str, payload: dict, timeout: int = 30) -> dict:
    resp = session.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    # 兼容 ResponseDTO 包装：{code, message, data}
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _extract_rows(body) -> List[dict]:
    """
    解析 /robot/taskQueue/page 响应，兼容两种结构：
      - 包装: {code, message, data: {rows: [...], records: N}}
      - 直接: {rows: [...], records: N}
      - 列表: [...]
    """
    if isinstance(body, list):
        return body
    inner = body.get("data") if isinstance(body, dict) else None
    target = inner if isinstance(inner, dict) else body
    return target.get("rows") or target.get("list") or target.get("content") or []


def fetch_task_queue_page(
    base_url: str,
    token: str,
    session: requests.Session,
    page: int,
    page_size: int = 200,
) -> List[dict]:
    """
    调用 POST /robot/taskQueue/page，按 pra_end_time 降序，取最近执行完成的记录。
    接口的 startTime/endTime 过滤的是 create_time 而非 pra_start_time，因此不传日期，
    依靠客户端按 praStartTime 过滤目标日期。
    """
    payload = {
        "page": page,
        "size": page_size,
        "sidx": "qu.pra_end_time",
        "sort": "desc",
    }
    resp = session.post(
        f"{base_url}/robot/taskQueue/page",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return _extract_rows(resp.json())


def load_all_task_queue(
    base_url: str,
    token: str,
    session: requests.Session,
    start_time: datetime,
    end_time: datetime,
) -> List[dict]:
    """
    按 pra_end_time 降序扫描，收集 praStartTime 落在统计日期的全部记录。
    当连续一整页记录的 praStartTime 都早于统计日期时提前终止，避免全量扫描。
    """
    page_size = 200
    all_rows: List[dict] = []
    page = 1
    stat_date_str = start_time.strftime("%Y-%m-%d")
    # 统计日期的前一天，用于提前终止判断
    prev_date_str = (start_time - timedelta(days=1)).strftime("%Y-%m-%d")

    while True:
        rows = fetch_task_queue_page(base_url, token, session, page, page_size)
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

        # 降序排列：若整页都早于统计日期，后续页只会更早，可以停止
        if past == len(rows):
            print(f"\n  整页记录均早于 {stat_date_str}，提前终止")
            break
        # 若整页都晚于统计日期（今天的数据），继续翻页
        if len(rows) < page_size:
            break
        page += 1

    print()
    return all_rows


# --------------------------------------------------------------------------- #
# 字段转换
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


TAX_DECLARE_SYSTEM = "10007007"
ACCFUND_DECLARE_SYSTEM = "10008001"
FORCE_CLIENT_ERROR_CITIES = {"上海", "成都"}

SYS_NAME_MAP = {
    "10007001": "社保系统", "10007002": "养老系统", "10007003": "医疗系统",
    "10007004": "单工伤",   "10007005": "工伤系统", "10007006": "备案系统",
    "10007007": "税务系统", "10007008": "金保系统", "10007009": "失业系统",
    "10007010": "市网系统", "10008001": "公积金系统", "10008002": "国管公积金系统",
}


def classify_reason(text: str) -> str:
    if not text:
        return "未记录原因"
    value = str(text)
    for name, pattern in CLASSIFY_RULES:
        if re.search(pattern, value, re.IGNORECASE):
            return name
    return "其它原因"


def classify_reason_l1(text: str, declare_system: str, city: str = "") -> str:
    """一级大类：只有客户端异常和网页端异常两类。
    客户端异常条件（满足任一即归入）：
      - 条件1：税务系统（10007007）
      - 条件2：上海 / 成都 的公积金系统（10008001）
    其余全部归为网页端异常。
    二级细分由 fail_reason_raw（CLASSIFY_RULES）提供。
    """
    ds = str(declare_system)
    if ds == TAX_DECLARE_SYSTEM:
        return "客户端异常"
    if ds == ACCFUND_DECLARE_SYSTEM and str(city) in FORCE_CLIENT_ERROR_CITIES:
        return "客户端异常"
    return "网页端异常"


def parse_stat_window(stat_date: Optional[str]) -> Tuple[datetime, datetime]:
    if stat_date:
        date_value = datetime.strptime(stat_date, "%Y-%m-%d").date()
    else:
        date_value = datetime.now().date()
    start_time = datetime.combine(date_value, datetime.min.time())
    end_time = start_time + timedelta(days=1)
    return start_time, end_time


# --------------------------------------------------------------------------- #
# 报表构建
# --------------------------------------------------------------------------- #

def build_report(
    rows: List[dict],
    start_time: datetime,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:

    if not rows:
        empty = pd.DataFrame()
        return empty, empty, empty, empty, empty, empty

    task_df = pd.DataFrame(rows)

    # 城市：接口返回的 RobotTaskQueueVO 含 addrName 字段
    if "addrName" not in task_df.columns:
        task_df["addrName"] = ""
    task_df["city"] = task_df["addrName"].fillna("").replace("", "未记录城市")

    task_df["task_type"] = task_df.get("queueItem", pd.Series(dtype=object)).apply(queue_item_name)
    task_df["status_int"] = pd.to_numeric(task_df.get("status", pd.Series(dtype=object)), errors="coerce").fillna(-1).astype(int)
    task_df["status_name"] = task_df["status_int"].apply(status_name)
    task_df["comment_text"] = task_df.get("comment", pd.Series(dtype=str)).fillna("")
    task_df["fail_reason_raw"] = task_df["comment_text"].apply(classify_reason)
    # 一级分类：税务系统 / 指定城市公积金 失败强制归为「客户端异常」
    ds_col = "declareSystem" if "declareSystem" in task_df.columns else "declare_system"
    task_df["fail_reason"] = task_df.apply(
        lambda x: classify_reason_l1(x["comment_text"], x.get(ds_col, ""), x.get("city", "")), axis=1
    )

    # 账号
    if "declareAccount" in task_df.columns:
        task_df["account"] = task_df["declareAccount"].fillna("")
    else:
        task_df["account"] = ""

    # 盒子
    if "machineCode" not in task_df.columns:
        task_df["machineCode"] = ""

    # ---------- 汇总 ----------
    total_count = len(task_df)
    success_count = int((task_df["status_int"] == 4).sum())
    fail_count = int((task_df["status_int"] == 3).sum())
    other_count = total_count - success_count - fail_count
    success_rate = round(success_count / total_count * 100, 2) if total_count else 0

    summary_df = pd.DataFrame([{
        "统计日期": start_time.strftime("%Y-%m-%d"),
        "实际执行任务数（含所有状态）": total_count,
        "执行成功任务数（status=4）": success_count,
        "执行失败任务数（status=3）": fail_count,
        "其他状态数（执行中/待执行）": other_count,
        "今日成功率（成功/总量）": f"{success_rate}%",
    }])

    status_breakdown = (
        task_df.groupby("status_name").size()
        .reset_index(name="数量")
        .sort_values("数量", ascending=False)
    )

    # ---------- 失败 Top10 ----------
    fail_df = task_df[task_df["status_int"] == 3].copy()

    # 按失败原因汇总
    failure_by_reason = (
        fail_df.groupby("fail_reason", dropna=False)
        .size()
        .reset_index(name="失败次数")
        .sort_values("失败次数", ascending=False)
        .head(10)
    )
    failure_by_reason.rename(columns={"fail_reason": "失败原因"}, inplace=True)

    # 按五维度分组
    failure_top10 = (
        fail_df.groupby(
            ["fail_reason", "city", "account", "machineCode", "task_type"],
            dropna=False,
        )
        .size()
        .reset_index(name="失败次数")
        .sort_values("失败次数", ascending=False)
        .head(10)
    )
    failure_top10.rename(columns={
        "fail_reason": "失败原因",
        "city": "城市",
        "account": "账号",
        "machineCode": "盒子",
        "task_type": "任务类型",
    }, inplace=True)

    # ---------- 空跑 ----------
    invalid_df = fail_df[fail_df["fail_reason"].isin(INVALID_REASONS)].copy()

    invalid_summary = (
        invalid_df.groupby("fail_reason", dropna=False)
        .size()
        .reset_index(name="空跑数量")
        .sort_values("空跑数量", ascending=False)
    )
    invalid_summary.rename(columns={"fail_reason": "空跑类型"}, inplace=True)

    invalid_detail = (
        invalid_df.groupby(
            ["fail_reason", "city", "account", "machineCode", "task_type"],
            dropna=False,
        )
        .size()
        .reset_index(name="空跑次数")
        .sort_values("空跑次数", ascending=False)
    )
    invalid_detail.rename(columns={
        "fail_reason": "空跑类型",
        "city": "城市",
        "account": "账号",
        "machineCode": "盒子",
        "task_type": "任务类型",
    }, inplace=True)

    # ---------- 业务分类统计 ----------
    # 计算执行时长（分钟）
    for tc in ["praStartTime", "pra_start_time"]:
        if tc in task_df.columns:
            task_df["_start"] = pd.to_datetime(task_df[tc], errors="coerce")
            break
    else:
        task_df["_start"] = pd.NaT
    for tc in ["praEndTime", "pra_end_time"]:
        if tc in task_df.columns:
            task_df["_end"] = pd.to_datetime(task_df[tc], errors="coerce")
            break
    else:
        task_df["_end"] = pd.NaT

    task_df["duration_min"] = (
        (task_df["_end"] - task_df["_start"])
        .dt.total_seconds()
        .div(60)
        .round(2)
        .clip(lower=0)
    )
    # 失败步骤：从 comment_text 提取首行（接口数据无 MongoDB stepName）
    task_df["fail_step"] = task_df["comment_text"].str.replace("\n", " ").str.strip().str[:80]

    service_stats = _build_service_stats(task_df, status_col="status_int", s4=4, s3=3,
                                         city_col="city", machine_col="machineCode",
                                         duration_col="duration_min", fail_step_col="fail_step",
                                         fail_reason_col="fail_reason")

    # ---------- 二级原因统计（通用辅助）----------
    acc_col = "account" if "account" in fail_df.columns else "declareAccount"
    mc_col  = "machineCode" if "machineCode" in fail_df.columns else "machine_code"

    def _join_unique(series, sep="、", limit=5) -> str:
        vals = sorted({str(v) for v in series.dropna() if str(v).strip()})
        return sep.join(vals[:limit]) + ("…" if len(vals) > limit else "")

    def _build_secondary(sub_df: pd.DataFrame, col_name: str) -> pd.DataFrame:
        total = len(sub_df)
        rows = []
        for raw_reason, grp in sub_df.groupby("fail_reason_raw", dropna=False):
            cnt = len(grp)
            rows.append({
                col_name:    str(raw_reason),
                "失败数量":  cnt,
                "占比":      f"{round(cnt / total * 100, 1)}%" if total else "—",
                "涉及城市":  _join_unique(grp["city"]),
                "涉及账号":  _join_unique(grp[acc_col]),
                "涉及盒子":  _join_unique(grp[mc_col]),
                "涉及业务类型": _join_unique(grp["task_type"]),
            })
        return (pd.DataFrame(rows).sort_values("失败数量", ascending=False).reset_index(drop=True)
                if rows else pd.DataFrame())

    # ---------- 客户端异常二级原因统计 ----------
    client_fail_df = fail_df[fail_df["fail_reason"] == "客户端异常"].copy()
    client_secondary_df = _build_secondary(client_fail_df, "客户端异常二级原因")

    # ---------- 网页端异常二级原因统计 ----------
    web_fail_df = fail_df[fail_df["fail_reason"] == "网页端异常"].copy()
    web_secondary_df = _build_secondary(web_fail_df, "网页端异常二级原因")

    # 原始明细：只保留关键列
    keep_cols = [c for c in [
        "id", "clientId", "executionCode", "machineCode", "taskCode",
        "declareAccount", "companyName", "businessType", "declareSystem",
        "queueItem", "task_type", "city", "status_int", "status_name",
        "loginStatus", "praStartTime", "praEndTime", "duration_min",
        "comment_text", "fail_reason_raw", "fail_reason",
    ] if c in task_df.columns]
    detail_df = task_df[keep_cols].copy()

    return detail_df, summary_df, failure_by_reason, failure_top10, invalid_summary, invalid_detail, status_breakdown, service_stats, client_secondary_df, web_secondary_df


# --------------------------------------------------------------------------- #
# 业务分类统计（与 robot_daily_report.py 共用同一逻辑）
# --------------------------------------------------------------------------- #

def _build_service_stats(
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
    records = []
    for task_type, grp in df.groupby("task_type", dropna=False):
        total   = len(grp)
        success = int((grp[status_col] == s4).sum())
        fail    = int((grp[status_col] == s3).sum())
        rate    = f"{round(success / total * 100, 1)}%" if total else "—"

        dur      = grp[duration_col].dropna()
        total_min = round(dur.sum(), 1)
        avg_min   = round(dur.mean(), 1) if len(dur) else 0
        max_min   = round(dur.max(), 1)  if len(dur) else 0

        fail_grp = grp[grp[status_col] == s3]

        main_reason = (
            fail_grp[fail_reason_col].value_counts().index[0]
            if len(fail_grp) and fail_reason_col in fail_grp.columns
            else "—"
        )

        def _count_reason(keyword):
            return int((fail_grp[fail_reason_col].str.contains(keyword, na=False)).sum()) if len(fail_grp) else 0

        client_err = _count_reason("客户端异常")
        login_err  = _count_reason("登录无效")
        ukey_err   = _count_reason("UKey异常")

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
            "任务总数":     total,
            "成功数":       success,
            "失败数":       fail,
            "成功率":       rate,
            "总执行时长(分)":   total_min,
            "平均执行时长(分)": avg_min,
            "最大执行时长(分)": max_min,
            "主要失败原因":  main_reason,
            "客户端异常数":  client_err,
            "登录无效数":    login_err,
            "UKey异常数":   ukey_err,
            "Top城市":      top_city,
            "Top盒子":      top_machine,
            "Top失败步骤":  top_step,
        })

    result = pd.DataFrame(records)
    if not result.empty:
        result.sort_values("任务总数", ascending=False, inplace=True)
        result.reset_index(drop=True, inplace=True)
    return result


# --------------------------------------------------------------------------- #
# 输出：Excel
# --------------------------------------------------------------------------- #

def write_report(
    output_file: Path,
    detail_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    failure_by_reason: pd.DataFrame,
    failure_top10: pd.DataFrame,
    invalid_summary: pd.DataFrame,
    invalid_detail: pd.DataFrame,
    status_breakdown: pd.DataFrame,
    service_stats: pd.DataFrame,
    client_secondary_df: pd.DataFrame,
    web_secondary_df: pd.DataFrame,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="总览", index=False)
        status_breakdown.to_excel(writer, sheet_name="状态分布", index=False)
        service_stats.to_excel(writer, sheet_name="业务分类统计", index=False)
        failure_by_reason.to_excel(writer, sheet_name="失败原因Top10", index=False)
        failure_top10.to_excel(writer, sheet_name="失败明细Top10", index=False)
        client_secondary_df.to_excel(writer, sheet_name="客户端异常二级原因", index=False)
        web_secondary_df.to_excel(writer, sheet_name="网页端异常二级原因", index=False)
        invalid_summary.to_excel(writer, sheet_name="空跑汇总", index=False)
        invalid_detail.to_excel(writer, sheet_name="空跑明细", index=False)
        detail_df.to_excel(writer, sheet_name="原始任务明细", index=False)


# --------------------------------------------------------------------------- #
# 输出：Markdown
# --------------------------------------------------------------------------- #

def _df_to_md(df: pd.DataFrame) -> str:
    if df.empty:
        return "_（无数据）_\n"
    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(str(v) if v is not None else "" for v in row) + " |")
    return "\n".join(lines) + "\n"


def write_markdown(
    md_file: Path,
    stat_date: str,
    summary_df: pd.DataFrame,
    status_breakdown: pd.DataFrame,
    failure_by_reason: pd.DataFrame,
    failure_top10: pd.DataFrame,
    invalid_summary: pd.DataFrame,
    invalid_detail: pd.DataFrame,
    service_stats: pd.DataFrame,
    client_secondary_df: pd.DataFrame,
    web_secondary_df: pd.DataFrame,
) -> None:
    md_file.parent.mkdir(parents=True, exist_ok=True)

    def _val(df: pd.DataFrame, col: str, default: str = "—") -> str:
        for c in df.columns:
            if col in c:
                v = df.iloc[0][c]
                return str(v) if v is not None else default
        return default

    total   = _val(summary_df, "实际执行")
    success = _val(summary_df, "成功任务")
    fail    = _val(summary_df, "失败任务")
    other   = _val(summary_df, "其他状态")
    rate    = _val(summary_df, "成功率")
    inv_total = str(invalid_detail["空跑次数"].sum()) if not invalid_detail.empty and "空跑次数" in invalid_detail.columns else "0"

    lines = [
        "# RPA 机器人每日执行统计报告（独立部署）",
        "",
        f"> 统计日期：**{stat_date}**　｜　数据来源：standalone REST 接口",
        "",
        "---",
        "",
        "## 一、执行总览",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 实际执行任务数（含所有状态） | **{total}** |",
        f"| 执行成功任务数（status=4） | **{success}** |",
        f"| 执行失败任务数（status=3 执行中断） | **{fail}** |",
        f"| 其他状态数（执行中 / 待执行） | **{other}** |",
        f"| 今日成功率（成功 / 总量） | **{rate}** |",
        f"| 空跑总数 | **{inv_total}** |",
        "",
        "---",
        "",
        "## 二、状态分布",
        "",
        _df_to_md(status_breakdown),
        "---",
        "",
        "## 三、失败 Top10（按失败原因）",
        "",
        "> 按失败原因分组，取失败次数最多的 10 种原因。",
        "",
        _df_to_md(failure_by_reason),
        "### 失败 Top10（五维度明细）",
        "",
        "> 按「失败原因 × 城市 × 账号 × 盒子 × 任务类型」分组，取失败次数最多的 10 组。",
        "",
        _df_to_md(failure_top10),
        "### 客户端异常二级原因",
        "",
        "> 税务系统（10007007）及指定城市公积金的失败均归入「客户端异常」大类；下表按原始分类（CLASSIFY_RULES）展示二级原因。",
        "",
        _df_to_md(client_secondary_df),
        "### 网页端异常二级原因",
        "",
        "> 网站异常、报盘为空、非办理时间等归入「网页端异常」大类；下表按原始分类展示二级原因。",
        "",
        _df_to_md(web_secondary_df),
        "---",
        "",
        "## 四、空跑汇总",
        "",
        "> 空跑定义：报盘为空、非办理时间、登录无效、UKey异常、网站异常、客户端异常、其它环境异常等无实际业务产出的执行。",
        "",
        _df_to_md(invalid_summary),
        "### 空跑明细",
        "",
        _df_to_md(invalid_detail),
        "---",
        "",
        "## 五、业务分类统计",
        "",
        "> 按任务类型（服务项）分组，统计执行结果、时长及主要问题。",
        "",
        _df_to_md(service_stats),
        "---",
        "",
        "_报告由 standalone_daily_report.py 自动生成_",
    ]

    md_file.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

# 默认输出目录：脚本同目录下的 standalone-reports/
_REPORTS_DIR = Path(__file__).parent / "standalone-reports"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 RPA 独立部署每日执行统计报表（走接口版）")
    parser.add_argument("--date", dest="stat_date", help="统计日期，格式 YYYY-MM-DD；默认统计今天")
    parser.add_argument("--output", dest="output", help="输出 Excel 文件路径；默认保存到 standalone-reports/ 目录")
    parser.add_argument("--config", dest="config", help="db.conf 路径；默认读取脚本同目录的 db.conf")
    args = parser.parse_args()

    cfg = load_config(Path(args.config) if args.config else None)
    conf = get_standalone_config(cfg)
    start_time, end_time = parse_stat_window(args.stat_date)
    date_str = start_time.strftime("%Y%m%d")

    session = make_session()

    print("正在获取 OAuth2 token...")
    token = fetch_token(conf, session)
    print("token 获取成功")

    print(f"正在拉取 {start_time.strftime('%Y-%m-%d')} 执行队列数据...")
    rows = load_all_task_queue(conf["base_url"], token, session, start_time, end_time)
    print(f"共拉取 {len(rows)} 条记录")

    if not rows:
        print("当天没有实际执行任务")
        return

    result = build_report(rows, start_time)
    detail_df, summary_df, failure_by_reason, failure_top10, invalid_summary, invalid_detail, status_breakdown, service_stats, client_secondary_df, web_secondary_df = result

    print("\n===== 今日执行总览 =====")
    print(summary_df.to_string(index=False))

    print("\n===== 状态分布 =====")
    print(status_breakdown.to_string(index=False))

    print("\n===== 业务分类统计 =====")
    print(service_stats.to_string(index=False))

    print("\n===== 失败 Top10（按失败原因）=====")
    print(failure_by_reason.to_string(index=False))

    print("\n===== 失败 Top10（五维度明细）=====")
    print(failure_top10.to_string(index=False))

    print("\n===== 客户端异常二级原因 =====")
    print(client_secondary_df.to_string(index=False))

    print("\n===== 网页端异常二级原因 =====")
    print(web_secondary_df.to_string(index=False))

    print("\n===== 空跑数量汇总 =====")
    print(invalid_summary.to_string(index=False))

    # Excel
    xlsx_file = (
        Path(args.output) if args.output
        else _REPORTS_DIR / f"standalone_daily_report_{date_str}.xlsx"
    )
    write_report(xlsx_file, detail_df, summary_df, failure_by_reason, failure_top10, invalid_summary, invalid_detail, status_breakdown, service_stats, client_secondary_df, web_secondary_df)
    print(f"\nExcel 报表已生成：{xlsx_file}")

    # Markdown
    md_file = _REPORTS_DIR / f"standalone_daily_report_{date_str}.md"
    write_markdown(md_file, start_time.strftime("%Y-%m-%d"),
                   summary_df, status_breakdown, failure_by_reason, failure_top10, invalid_summary, invalid_detail, service_stats, client_secondary_df, web_secondary_df)
    print(f"Markdown 报表已生成：{md_file}")


if __name__ == "__main__":
    main()
