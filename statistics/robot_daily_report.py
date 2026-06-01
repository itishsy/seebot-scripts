# -*- coding: utf-8 -*-
"""Generate daily robot execution statistics from MySQL and MongoDB.

Usage:
    python scripts/statistics/robot_daily_report.py
    python scripts/statistics/robot_daily_report.py --date 2026-05-31
    python scripts/statistics/robot_daily_report.py --output reports/robot_daily_report.xlsx
    python scripts/statistics/robot_daily_report.py --config /etc/rpa/db.conf
"""

import argparse
import configparser
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import pymysql
from pymongo import MongoClient

# --------------------------------------------------------------------------- #
# 配置加载：优先 db.conf，其次环境变量，最后内置默认值
# --------------------------------------------------------------------------- #

_DEFAULT_CONF = Path(__file__).parent / "db.conf"


def load_config(conf_path: Optional[Path] = None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    path = conf_path or _DEFAULT_CONF
    if path.exists():
        cfg.read(str(path), encoding="utf-8")
    return cfg


def get_mysql_config(cfg: configparser.ConfigParser) -> dict:
    sec = cfg["mysql"] if cfg.has_section("mysql") else {}
    return {
        "host": sec.get("host") or os.getenv("RPA_STAT_MYSQL_HOST", "127.0.0.1"),
        "port": int(sec.get("port") or os.getenv("RPA_STAT_MYSQL_PORT", "3306")),
        "user": sec.get("user") or os.getenv("RPA_STAT_MYSQL_USER", "root"),
        "password": sec.get("password") or os.getenv("RPA_STAT_MYSQL_PASSWORD", ""),
        "database": sec.get("database") or os.getenv("RPA_STAT_MYSQL_DATABASE", "rpa"),
        "charset": sec.get("charset") or os.getenv("RPA_STAT_MYSQL_CHARSET", "utf8mb4"),
        "cursorclass": pymysql.cursors.DictCursor,
    }


def get_mongo_config(cfg: configparser.ConfigParser) -> Tuple[str, str, str]:
    sec = cfg["mongodb"] if cfg.has_section("mongodb") else {}
    uri = sec.get("uri") or os.getenv("RPA_STAT_MONGO_URI", "mongodb://127.0.0.1:27017")
    db = sec.get("database") or os.getenv("RPA_STAT_MONGO_DB", "rpa")
    col = sec.get("collection") or os.getenv("RPA_STAT_MONGO_COLLECTION", "robot_execution_detail")

    # 若 uri 中没有认证信息，但 conf 里单独配置了 username/password，则拼入 URI
    username = sec.get("username") or os.getenv("RPA_STAT_MONGO_USER", "")
    password = sec.get("password") or os.getenv("RPA_STAT_MONGO_PASSWORD", "")
    if username and password and "@" not in uri:
        from urllib.parse import quote_plus
        uri = uri.replace("mongodb://", f"mongodb://{quote_plus(username)}:{quote_plus(password)}@", 1)

    return uri, db, col


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

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


TAX_DECLARE_SYSTEM = "10007007"       # 税务系统编码
ACCFUND_DECLARE_SYSTEM = "10008001"   # 公积金系统编码
FORCE_CLIENT_ERROR_CITIES = {"上海", "成都"}  # 这些城市的公积金失败强制归为客户端异常


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


def parse_addr_name(app_args: Optional[str]) -> str:
    """从 robot_app.app_args JSON 中提取 addrName（参保城市）。"""
    if not app_args:
        return ""
    try:
        obj = json.loads(app_args)
        return obj.get("addrName") or obj.get("cityName") or ""
    except (json.JSONDecodeError, TypeError):
        # 部分旧数据为非标准 JSON，尝试正则兜底
        m = re.search(r'"addrName"\s*:\s*"([^"]*)"', app_args)
        return m.group(1) if m else ""


def parse_stat_window(stat_date: Optional[str]) -> Tuple[datetime, datetime]:
    if stat_date:
        date_value = datetime.strptime(stat_date, "%Y-%m-%d").date()
    else:
        date_value = datetime.now().date()
    start_time = datetime.combine(date_value, datetime.min.time())
    end_time = start_time + timedelta(days=1)
    return start_time, end_time


def chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i: i + size]


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #

def load_mysql_tasks(start_time: datetime, end_time: datetime, mysql_cfg: dict) -> pd.DataFrame:
    """
    加载当日实际执行任务：pra_start_time 不为空即视为已产生实际执行，包含所有 status。
    同时 LEFT JOIN robot_app 取 app_args，用于解析城市名。
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


def load_mongo_errors(
    execution_codes: List[str],
    start_time: datetime,
    end_time: datetime,
    mongo_uri: str,
    mongo_db: str,
    mongo_col: str,
) -> pd.DataFrame:
    if not execution_codes:
        return pd.DataFrame()

    client = MongoClient(mongo_uri)
    col = client[mongo_db][mongo_col]
    start_ms = int(start_time.timestamp() * 1000)
    end_ms = int(end_time.timestamp() * 1000)
    rows = []

    try:
        for code_chunk in chunks(execution_codes, 500):
            pipeline = [
                {
                    "$match": {
                        "executionCode": {"$in": code_chunk},
                        "startDate": {"$gte": start_ms, "$lt": end_ms},
                        "$or": [
                            {"error": {"$exists": True, "$ne": ""}},
                            {"errorStack": {"$exists": True, "$ne": ""}},
                            {"status": {"$nin": [1, 4]}},
                        ],
                    }
                },
                {"$sort": {"endDate": -1}},
                {
                    "$group": {
                        "_id": "$executionCode",
                        "executionCode": {"$first": "$executionCode"},
                        "flowCode": {"$first": "$flowCode"},
                        "stepCode": {"$first": "$stepCode"},
                        "stepName": {"$first": "$stepName"},
                        "status": {"$first": "$status"},
                        "error": {"$first": "$error"},
                        "errorStack": {"$first": "$errorStack"},
                    }
                },
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
# 报表构建
# --------------------------------------------------------------------------- #

def build_report(
    start_time: datetime,
    end_time: datetime,
    mysql_cfg: dict,
    mongo_uri: str,
    mongo_db: str,
    mongo_col: str,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:

    task_df = load_mysql_tasks(start_time, end_time, mysql_cfg)
    if task_df.empty:
        empty = pd.DataFrame()
        return task_df, empty, empty, empty, empty

    # 城市名：从 app_args JSON 解析
    task_df["city"] = task_df["app_args"].apply(parse_addr_name)
    task_df["city"] = task_df["city"].replace("", "未记录城市").fillna("未记录城市")

    task_df["task_type"] = task_df["queue_item"].apply(queue_item_name)
    task_df["status_name"] = task_df["status"].apply(status_name)
    task_df["mysql_comment"] = task_df["comment"].fillna("")

    # 从 MongoDB 补充失败原因
    execution_codes = task_df["execution_code"].dropna().astype(str).unique().tolist()
    mongo_df = load_mongo_errors(execution_codes, start_time, end_time, mongo_uri, mongo_db, mongo_col)

    if mongo_df.empty:
        task_df["mongo_error_text"] = ""
    else:
        mongo_df["executionCode"] = mongo_df["executionCode"].astype(str)
        mongo_df["mongo_error_text"] = (
            mongo_df.get("stepName", pd.Series(dtype=str)).fillna("").astype(str) + " "
            + mongo_df.get("error", pd.Series(dtype=str)).fillna("").astype(str) + " "
            + mongo_df.get("errorStack", pd.Series(dtype=str)).fillna("").astype(str)
        )
        task_df["execution_code"] = task_df["execution_code"].astype(str)
        task_df = task_df.merge(
            mongo_df[["executionCode", "mongo_error_text"]],
            how="left",
            left_on="execution_code",
            right_on="executionCode",
        )
    task_df["mongo_error_text"] = task_df["mongo_error_text"].fillna("")

    # fail_reason_raw：优先从 mysql_comment 匹配业务关键词（报盘为空/非办理时间/登录无效等），
    # mysql_comment 未命中时再用 mongo_error_text 补充技术异常原因，保证二级分类完整覆盖 CLASSIFY_RULES
    def _classify_combined(mysql_comment: str, mongo_error: str) -> str:
        raw = classify_reason(mysql_comment)
        if raw != "未记录原因":
            return raw
        return classify_reason(mongo_error)

    task_df["fail_reason_raw"] = task_df.apply(
        lambda x: _classify_combined(x["mysql_comment"], x["mongo_error_text"]), axis=1
    )

    # fail_source_text 用于一级大类判断（保持原有逻辑：有 mongo 就用 mongo，否则用 mysql）
    task_df["fail_source_text"] = task_df.apply(
        lambda x: x["mongo_error_text"] if x["mongo_error_text"].strip() else x["mysql_comment"],
        axis=1,
    )

    # 一级分类：税务系统 / 指定城市公积金 失败强制归为「客户端异常」，其余归「网页端异常」
    task_df["fail_reason"] = task_df.apply(
        lambda x: classify_reason_l1(
            x["fail_source_text"],
            x.get("declare_system", ""),
            x.get("city", ""),
        ), axis=1
    )

    # ---------- 汇总指标 ----------
    total_count = len(task_df)
    success_count = int((task_df["status"] == 4).sum())
    # 执行中断（status=3）为失败；status=1/2 为进行中，不计入失败
    fail_count = int((task_df["status"] == 3).sum())
    other_count = total_count - success_count - fail_count
    success_rate = round(success_count / total_count * 100, 2) if total_count else 0

    # 各状态明细
    status_breakdown = (
        task_df.groupby("status_name").size()
        .reset_index(name="数量")
        .sort_values("数量", ascending=False)
    )

    summary_df = pd.DataFrame([{
        "统计日期": start_time.strftime("%Y-%m-%d"),
        "实际执行任务数（含所有状态）": total_count,
        "执行成功任务数（status=4）": success_count,
        "执行失败任务数（status=3）": fail_count,
        "其他状态数（执行中/待执行）": other_count,
        "今日成功率（成功/总量）": f"{success_rate}%",
    }])

    # ---------- 失败 Top10 ----------
    fail_df = task_df[task_df["status"] == 3].copy()

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
            ["fail_reason", "city", "declare_account", "machine_code", "task_type"],
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
        "declare_account": "账号",
        "machine_code": "盒子",
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
            ["fail_reason", "city", "declare_account", "machine_code", "task_type"],
            dropna=False,
        )
        .size()
        .reset_index(name="空跑次数")
        .sort_values("空跑次数", ascending=False)
    )
    invalid_detail.rename(columns={
        "fail_reason": "空跑类型",
        "city": "城市",
        "declare_account": "账号",
        "machine_code": "盒子",
        "task_type": "任务类型",
    }, inplace=True)

    # ---------- 业务分类统计 ----------
    # 计算执行时长（分钟）
    task_df["pra_start_time"] = pd.to_datetime(task_df["pra_start_time"], errors="coerce")
    task_df["pra_end_time"]   = pd.to_datetime(task_df["pra_end_time"],   errors="coerce")
    task_df["duration_min"] = (
        (task_df["pra_end_time"] - task_df["pra_start_time"])
        .dt.total_seconds()
        .div(60)
        .round(2)
        .clip(lower=0)
    )
    # 失败步骤来源：mongo_error_text 中的 stepName（merge 后已在 task_df），兜底用 mysql_comment 第一行
    if "mongo_error_text" in task_df.columns:
        task_df["fail_step"] = task_df.apply(
            lambda x: (
                str(x.get("stepName", "") or "").strip()
                or str(x.get("mysql_comment", "") or "").replace("\n", " ").strip()[:60]
            ), axis=1
        )
    else:
        task_df["fail_step"] = task_df["mysql_comment"].str.replace("\n", " ").str.strip().str[:60]

    service_stats = _build_service_stats(task_df, status_col="status", s4=4, s3=3,
                                         city_col="city", machine_col="machine_code",
                                         duration_col="duration_min", fail_step_col="fail_step",
                                         fail_reason_col="fail_reason")

    # ---------- 二级原因统计（通用辅助）----------
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
                "涉及账号":  _join_unique(grp["declare_account"]),
                "涉及盒子":  _join_unique(grp["machine_code"]),
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

    # 原始明细去掉大字段，保留关键列
    export_cols = [
        c for c in [
            "id", "client_id", "execution_code", "machine_code", "task_code",
            "declare_account", "company_name", "business_type", "declare_system",
            "queue_item", "task_type", "city", "status", "status_name",
            "login_status", "pra_start_time", "pra_end_time", "duration_min",
            "mysql_comment", "fail_reason_raw", "fail_reason",
        ] if c in task_df.columns
    ]
    detail_df = task_df[export_cols].copy()

    return detail_df, summary_df, failure_by_reason, failure_top10, invalid_summary, invalid_detail, status_breakdown, service_stats, client_secondary_df, web_secondary_df


# --------------------------------------------------------------------------- #
# 业务分类统计（通用，robot / standalone 共用逻辑）
# --------------------------------------------------------------------------- #

def _build_service_stats(
    df: pd.DataFrame,
    status_col: str,
    s4: int,        # 成功状态值
    s3: int,        # 失败状态值
    city_col: str,
    machine_col: str,
    duration_col: str,
    fail_step_col: str,
    fail_reason_col: str,
) -> pd.DataFrame:
    """按 task_type（业务类型）分组，汇总各项执行指标。"""
    records = []
    for task_type, grp in df.groupby("task_type", dropna=False):
        total    = len(grp)
        success  = int((grp[status_col] == s4).sum())
        fail     = int((grp[status_col] == s3).sum())
        rate     = f"{round(success / total * 100, 1)}%" if total else "—"

        dur = grp[duration_col].dropna()
        total_min = round(dur.sum(), 1)
        avg_min   = round(dur.mean(), 1) if len(dur) else 0
        max_min   = round(dur.max(), 1)  if len(dur) else 0

        fail_grp = grp[grp[status_col] == s3]

        # 主要失败原因
        main_reason = (
            fail_grp[fail_reason_col].value_counts().index[0]
            if len(fail_grp) and fail_reason_col in fail_grp.columns
            else "—"
        )

        def _count_reason(keyword):
            return int((fail_grp[fail_reason_col].str.contains(keyword, na=False)).sum()) if len(fail_grp) else 0

        client_err  = _count_reason("客户端异常")
        login_err   = _count_reason("登录无效")
        ukey_err    = _count_reason("UKey异常")

        # Top 城市（失败中出现最多的城市）
        top_city = (
            fail_grp[city_col].value_counts().index[0]
            if len(fail_grp) and city_col in fail_grp.columns
            else "—"
        )

        # Top 盒子（失败中出现最多的盒子）
        top_machine = (
            fail_grp[machine_col].value_counts().index[0]
            if len(fail_grp) and machine_col in fail_grp.columns
            else "—"
        )

        # Top 失败步骤（取非空、非空白的最高频）
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
            "总执行时长(分)":  total_min,
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
    """将 DataFrame 转成 Markdown 表格字符串。"""
    if df.empty:
        return "_（无数据）_\n"
    lines = []
    cols = list(df.columns)
    lines.append("| " + " | ".join(str(c) for c in cols) + " |")
    lines.append("| " + " | ".join("---" for _ in cols) + " |")
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

    # 从 summary_df 取关键指标，兼容列名
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
        f"# RPA 机器人每日执行统计报告",
        f"",
        f"> 统计日期：**{stat_date}**　｜　数据来源：SaaS MySQL + MongoDB",
        f"",
        f"---",
        f"",
        f"## 一、执行总览",
        f"",
        f"| 指标 | 数值 |",
        f"| --- | --- |",
        f"| 实际执行任务数（含所有状态） | **{total}** |",
        f"| 执行成功任务数（status=4） | **{success}** |",
        f"| 执行失败任务数（status=3 执行中断） | **{fail}** |",
        f"| 其他状态数（执行中 / 待执行） | **{other}** |",
        f"| 今日成功率（成功 / 总量） | **{rate}** |",
        f"| 空跑总数 | **{inv_total}** |",
        f"",
        f"---",
        f"",
        f"## 二、状态分布",
        f"",
        _df_to_md(status_breakdown),
        f"---",
        f"",
        f"## 三、失败 Top10（按失败原因）",
        f"",
        f"> 按失败原因分组，取失败次数最多的 10 种原因。",
        f"",
        _df_to_md(failure_by_reason),
        f"### 失败 Top10（五维度明细）",
        f"",
        f"> 按「失败原因 × 城市 × 账号 × 盒子 × 任务类型」分组，取失败次数最多的 10 组。",
        f"",
        _df_to_md(failure_top10),
        f"### 客户端异常二级原因",
        f"",
        f"> 税务系统（10007007）及指定城市公积金的失败均归入「客户端异常」大类；下表按原始分类（CLASSIFY_RULES）展示二级原因。",
        f"",
        _df_to_md(client_secondary_df),
        f"### 网页端异常二级原因",
        f"",
        f"> 网站异常、报盘为空、非办理时间等归入「网页端异常」大类；下表按原始分类展示二级原因。",
        f"",
        _df_to_md(web_secondary_df),
        f"---",
        f"",
        f"## 四、空跑汇总",
        f"",
        f"> 空跑定义：报盘为空、非办理时间、登录无效、UKey异常、网站异常、Selenium操作异常、客户端异常、其它环境异常、配置错误等无实际业务产出的执行。",
        f"",
        _df_to_md(invalid_summary),
        f"### 空跑明细",
        f"",
        _df_to_md(invalid_detail),
        f"---",
        f"",
        f"## 五、业务分类统计",
        f"",
        f"> 按任务类型（服务项）分组，统计执行结果、时长及主要问题。",
        f"",
        _df_to_md(service_stats),
        f"---",
        f"",
        f"_报告由 robot_daily_report.py 自动生成_",
    ]

    md_file.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

# 默认输出目录：脚本同目录下的 reports/
_REPORTS_DIR = Path(__file__).parent / "reports"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 RPA 机器人每日执行统计报表（SaaS 直连数据库版）")
    parser.add_argument("--date", dest="stat_date", help="统计日期，格式 YYYY-MM-DD；默认统计今天")
    parser.add_argument("--output", dest="output", help="输出 Excel 文件路径；默认保存到 reports/ 目录")
    parser.add_argument("--config", dest="config", help="db.conf 路径；默认读取脚本同目录的 db.conf")
    args = parser.parse_args()

    cfg = load_config(Path(args.config) if args.config else None)
    mysql_cfg = get_mysql_config(cfg)
    mongo_uri, mongo_db, mongo_col = get_mongo_config(cfg)

    start_time, end_time = parse_stat_window(args.stat_date)
    date_str = start_time.strftime("%Y%m%d")

    result = build_report(start_time, end_time, mysql_cfg, mongo_uri, mongo_db, mongo_col)
    detail_df, summary_df, failure_by_reason, failure_top10, invalid_summary, invalid_detail, status_breakdown, service_stats, client_secondary_df, web_secondary_df = result

    if detail_df.empty:
        print("当天没有实际执行任务（pra_start_time 为空或无记录）")
        return

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
        else _REPORTS_DIR / f"robot_daily_report_{date_str}.xlsx"
    )
    write_report(xlsx_file, detail_df, summary_df, failure_by_reason, failure_top10, invalid_summary, invalid_detail, status_breakdown, service_stats, client_secondary_df, web_secondary_df)
    print(f"\nExcel 报表已生成：{xlsx_file}")

    # Markdown（始终输出到 reports/ 目录）
    md_file = _REPORTS_DIR / f"robot_daily_report_{date_str}.md"
    write_markdown(md_file, start_time.strftime("%Y-%m-%d"),
                   summary_df, status_breakdown, failure_by_reason, failure_top10, invalid_summary, invalid_detail, service_stats, client_secondary_df, web_secondary_df)
    print(f"Markdown 报表已生成：{md_file}")


if __name__ == "__main__":
    main()
