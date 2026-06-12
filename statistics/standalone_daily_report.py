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
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
import requests

# 数据源、配置、工具函数统一由 data_source 模块提供
from data_source import (
    load_config,
    get_standalone_config,
    parse_stat_window,
    queue_item_name,
    status_name,
    build_service_stats,
    make_session,
    fetch_token,
    load_api_tasks_daily,
    fetch_screenshot_map_daily,
    fetch_run_support_map_daily,
    fetch_execution_errors_daily,
    QUEUE_ITEM_MAP,
    STATUS_MAP,
)

# 错误码、分类规则、工具函数统一由 error_codes 模块管理
from error_codes import (
    CLASSIFY_RULES,
    CLIENT_RUN_SUPPORT,
    classify_reason,
    classify_reason_l1,
    ERROR_CODE_BY_LABEL,
)

# 空跑：仅以下错误码认定为无业务产出的空跑执行（与 robot_daily_report 保持一致）
INVALID_ERROR_CODES = {"BIZ_NON_WORK_TIME_SMS"}

SYS_NAME_MAP = {
    "10007001": "社保系统", "10007002": "养老系统", "10007003": "医疗系统",
    "10007004": "单工伤",   "10007005": "工伤系统", "10007006": "备案系统",
    "10007007": "税务系统", "10007008": "金保系统", "10007009": "失业系统",
    "10007010": "市网系统", "10008001": "公积金系统", "10008002": "国管公积金系统",
}


# --------------------------------------------------------------------------- #
# 报表构建
# --------------------------------------------------------------------------- #

def build_report(
    rows: List[dict],
    start_time: datetime,
    screenshot_map: dict = None,
    run_support_map: dict = None,
    mongo_df: pd.DataFrame = None,
) -> Tuple:

    if not rows:
        empty = pd.DataFrame()
        return empty, empty, empty, empty, empty, empty, empty, empty, empty, empty, empty, empty

    task_df = pd.DataFrame(rows)

    # 城市：接口返回的 RobotTaskQueueVO 含 addrName 字段
    if "addrName" not in task_df.columns:
        task_df["addrName"] = ""
    task_df["city"] = task_df["addrName"].fillna("").replace("", "未记录城市")

    task_df["task_type"] = task_df.get("queueItem", pd.Series(dtype=object)).apply(queue_item_name)
    task_df["status_int"] = pd.to_numeric(task_df.get("status", pd.Series(dtype=object)), errors="coerce").fillna(-1).astype(int)
    task_df["status_name"] = task_df["status_int"].apply(status_name)
    task_df["comment_text"] = task_df.get("comment", pd.Series(dtype=str)).fillna("")

    # 一级分类：robot_flow.run_support=10019005 为客户端异常，其余归网页端异常
    app_col = "appCode" if "appCode" in task_df.columns else "app_code"
    if run_support_map and app_col in task_df.columns:
        task_df["run_support"] = task_df[app_col].astype(str).map(
            lambda x: run_support_map.get(x, "")
        )
    else:
        task_df["run_support"] = ""

    # ---------- 合并 MongoDB execution detail ----------
    # mongo_df 由 fetch_execution_errors_daily 提供，含 executionCode / stepName / error / errorStack
    ec_col = "executionCode" if "executionCode" in task_df.columns else "execution_code"
    if mongo_df is not None and not mongo_df.empty:
        mongo_df["executionCode"] = mongo_df["executionCode"].astype(str)
        mongo_df["mongo_error_text"] = (
            mongo_df.get("stepName",    pd.Series(dtype=str)).fillna("").astype(str) + " "
            + mongo_df.get("error",     pd.Series(dtype=str)).fillna("").astype(str) + " "
            + mongo_df.get("errorStack",pd.Series(dtype=str)).fillna("").astype(str)
        )
        task_df[ec_col] = task_df[ec_col].astype(str)
        task_df = task_df.merge(
            mongo_df[["executionCode", "mongo_error_text", "stepName"]],
            how="left",
            left_on=ec_col,
            right_on="executionCode",
            suffixes=("", "_mongo"),
        )
        task_df.drop(columns=["executionCode_mongo"], errors="ignore", inplace=True)
    else:
        task_df["mongo_error_text"] = ""
        task_df["stepName"] = ""
    task_df["mongo_error_text"] = task_df["mongo_error_text"].fillna("")
    task_df["stepName"]         = task_df["stepName"].fillna("")

    # fail_reason_raw：优先从 comment_text 匹配业务关键词，
    # 未命中时用 mongo_error_text 补充技术异常原因（与 robot_daily_report 逻辑对齐）
    def _classify_combined(comment: str, mongo_error: str) -> str:
        raw = classify_reason(comment)
        if raw != "未记录原因":
            return raw
        return classify_reason(mongo_error)

    task_df["fail_reason_raw"] = task_df.apply(
        lambda x: _classify_combined(x["comment_text"], x["mongo_error_text"]), axis=1
    )

    # fail_source_text 用于一级大类判断：有 mongo 就用 mongo，否则用 comment
    task_df["fail_source_text"] = task_df.apply(
        lambda x: x["mongo_error_text"] if x["mongo_error_text"].strip() else x["comment_text"],
        axis=1,
    )
    task_df["fail_reason"] = task_df.apply(
        lambda x: classify_reason_l1(x["fail_source_text"], x.get("run_support", "")), axis=1
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
    # 报盘为空（BIZ_REPORT_EMPTY）认定为成功，从 status=3 中剔除，不纳入失败统计。
    report_empty_mask = (task_df["status_int"] == 3) & (
        task_df["comment_text"].apply(classify_reason) == "下载报盘文件为空"
    )
    task_df.loc[report_empty_mask, "status_int"]  = 4
    task_df.loc[report_empty_mask, "status_name"] = "执行成功"
    total_count   = len(task_df)
    success_count = int((task_df["status_int"] == 4).sum())
    fail_count    = int((task_df["status_int"] == 3).sum())
    other_count   = total_count - success_count - fail_count
    success_rate  = round(success_count / total_count * 100, 2) if total_count else 0

    # 空跑数量字段先占位，构建 invalid_df 后回填
    summary_df = pd.DataFrame([{
        "统计日期":               start_time.strftime("%Y-%m-%d"),
        "实际执行任务总数":       total_count,
        "执行成功任务数":         success_count,
        "执行失败任务数":         fail_count,
        "报盘为空任务数（计入成功）": int(report_empty_mask.sum()),
        "其他状态数（执行中/待执行）": other_count,
        "今日成功率（成功/总量）": f"{success_rate}%",
        "空跑数量":               0,
        "空跑占比（空跑/失败）":   "—",
    }])

    # 各状态明细
    status_breakdown = (
        task_df.groupby("status_name").size()
        .reset_index(name="数量")
        .sort_values("数量", ascending=False)
    )

    # ---------- 失败 Top10 ----------
    fail_df = task_df[task_df["status_int"] == 3].copy()

    failure_by_reason = (
        fail_df.groupby("fail_reason", dropna=False)
        .size()
        .reset_index(name="失败次数")
        .sort_values("失败次数", ascending=False)
        .head(10)
    )
    failure_by_reason.rename(columns={"fail_reason": "失败原因"}, inplace=True)

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

    # ---------- 空跑（与 robot_daily_report 对齐：基于 ERROR_CODE_BY_LABEL 精确匹配）----------
    def _is_invalid(raw_reason: str) -> bool:
        meta = ERROR_CODE_BY_LABEL.get(str(raw_reason))
        return meta is not None and meta.code in INVALID_ERROR_CODES

    invalid_df = fail_df[fail_df["fail_reason_raw"].apply(_is_invalid)].copy()

    invalid_summary = (
        invalid_df.groupby("fail_reason_raw", dropna=False)
        .size()
        .reset_index(name="空跑数量")
        .sort_values("空跑数量", ascending=False)
    )
    invalid_summary.rename(columns={"fail_reason_raw": "空跑类型"}, inplace=True)

    invalid_detail = (
        invalid_df.groupby(
            ["fail_reason_raw", "city", "account", "machineCode", "task_type"],
            dropna=False,
        )
        .size()
        .reset_index(name="空跑次数")
        .sort_values("空跑次数", ascending=False)
    )
    invalid_detail.rename(columns={
        "fail_reason_raw": "空跑类型",
        "city": "城市",
        "account": "账号",
        "machineCode": "盒子",
        "task_type": "任务类型",
    }, inplace=True)

    # 回填空跑统计到 summary_df
    invalid_total = len(invalid_df)
    invalid_rate  = f"{round(invalid_total / fail_count * 100, 1)}%" if fail_count else "—"
    summary_df.at[0, "空跑数量"]             = invalid_total
    summary_df.at[0, "空跑占比（空跑/失败）"] = invalid_rate

    # ---------- 业务分类统计 ----------
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

    # fail_step：优先用 MongoDB stepName，兜底用 comment_text 首行
    task_df["fail_step"] = task_df.apply(
        lambda x: (
            str(x.get("stepName") or "").strip()
            or str(x.get("comment_text") or "").replace("\n", " ").strip()[:80]
        ), axis=1
    )

    service_stats = build_service_stats(task_df, status_col="status_int", s4=4, s3=3,
                                        city_col="city", machine_col="machineCode",
                                        duration_col="duration_min", fail_step_col="fail_step",
                                        fail_reason_col="fail_reason")

    # ---------- 错误码归因分析（与 robot_daily_report 对齐）----------
    def _join_unique(series, sep="、", limit=5) -> str:
        vals = sorted({str(v) for v in series.dropna() if str(v).strip()})
        return sep.join(vals[:limit]) + ("…" if len(vals) > limit else "")

    total_fail_cnt = len(fail_df)
    attrib_rows = []
    for raw_reason, grp in fail_df.groupby("fail_reason_raw", dropna=False):
        cnt  = len(grp)
        meta = ERROR_CODE_BY_LABEL.get(str(raw_reason))
        attrib_rows.append({
            "错误码标识":    meta.code  if meta else "—",
            "异常原因":      str(raw_reason),
            "一级分类":      meta.l1    if meta else "—",
            "失败数量":      cnt,
            "占比":          f"{round(cnt / total_fail_cnt * 100, 1)}%" if total_fail_cnt else "—",
            "责任人":        meta.owner if meta else "—",
            "是否可自动修复": "是" if (meta and meta.auto_fix) else "否",
            "涉及城市":      _join_unique(grp["city"]),
            "涉及业务类型":  _join_unique(grp["task_type"]),
            "涉及账号":      _join_unique(grp["account"]),
            "涉及盒子":      _join_unique(grp["machineCode"]),
        })
    error_code_attribution_df = (
        pd.DataFrame(attrib_rows)
        .sort_values("失败数量", ascending=False)
        .reset_index(drop=True)
    )

    # ---------- 二级原因统计 ----------
    def _build_secondary(sub_df: pd.DataFrame, col_name: str) -> pd.DataFrame:
        total = len(sub_df)
        sec_rows = []
        for raw_reason, grp in sub_df.groupby("fail_reason_raw", dropna=False):
            cnt = len(grp)
            sec_rows.append({
                col_name:       str(raw_reason),
                "失败数量":     cnt,
                "占比":         f"{round(cnt / total * 100, 1)}%" if total else "—",
                "涉及城市":     _join_unique(grp["city"]),
                "涉及账号":     _join_unique(grp["account"]),
                "涉及盒子":     _join_unique(grp["machineCode"]),
                "涉及业务类型": _join_unique(grp["task_type"]),
            })
        return (pd.DataFrame(sec_rows).sort_values("失败数量", ascending=False).reset_index(drop=True)
                if sec_rows else pd.DataFrame())

    client_secondary_df = _build_secondary(
        fail_df[fail_df["fail_reason"] == "客户端异常"].copy(), "客户端异常二级原因"
    )
    web_secondary_df = _build_secondary(
        fail_df[fail_df["fail_reason"] == "网页端异常"].copy(), "网页端异常二级原因"
    )

    # ---------- 截图归因统计 ----------
    if screenshot_map and ec_col in task_df.columns:
        task_df["screenshot_url"] = task_df[ec_col].astype(str).map(
            lambda x: screenshot_map.get(x, "")
        )
    else:
        task_df["screenshot_url"] = ""

    task_df["has_screenshot"] = task_df["screenshot_url"].str.strip().ne("")

    total_fail       = len(fail_df)
    fail_with_shot   = int(task_df.loc[task_df["status_int"] == 3, "has_screenshot"].sum())
    client_fail_mask = (task_df["status_int"] == 3) & (task_df["fail_reason"] == "客户端异常")
    client_fail_cnt  = int(client_fail_mask.sum())
    client_with_shot = int((client_fail_mask & task_df["has_screenshot"]).sum())

    screenshot_summary_df = pd.DataFrame([{
        "失败任务数量":              total_fail,
        "有失败截图的任务数":         fail_with_shot,
        "截图覆盖率":                f"{round(fail_with_shot / total_fail * 100, 1)}%" if total_fail else "—",
        "客户端异常任务数":           client_fail_cnt,
        "客户端异常中有截图的任务数":  client_with_shot,
        "客户端截图覆盖率":           f"{round(client_with_shot / client_fail_cnt * 100, 1)}%" if client_fail_cnt else "—",
    }])

    keep_cols = [c for c in [
        "id", "clientId", "executionCode", "machineCode", "taskCode",
        "declareAccount", "companyName", "businessType", "declareSystem",
        "queueItem", "task_type", "city", "status_int", "status_name",
        "loginStatus", "praStartTime", "praEndTime", "duration_min",
        "comment_text", "mongo_error_text", "fail_reason_raw", "fail_reason", "screenshot_url",
    ] if c in task_df.columns]
    detail_df = task_df[keep_cols].copy()

    return (detail_df, summary_df, failure_by_reason, failure_top10,
            invalid_summary, invalid_detail, status_breakdown, service_stats,
            error_code_attribution_df, client_secondary_df, web_secondary_df, screenshot_summary_df)


# --------------------------------------------------------------------------- #
# 输出：Excel
# --------------------------------------------------------------------------- #

def _sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    """清理 DataFrame 中字符串列里 openpyxl/XML 1.0 不接受的非法控制字符。"""
    import re
    _ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

    def _clean(v):
        if isinstance(v, str):
            cleaned = _ILLEGAL.sub("", v)
            if len(cleaned) > 32767:
                cleaned = cleaned[:32760] + "…[截断]"
            return cleaned
        return v

    return df.apply(lambda col: col.map(_clean) if col.dtype == object else col)


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
    error_code_attribution_df: pd.DataFrame,
    client_secondary_df: pd.DataFrame,
    web_secondary_df: pd.DataFrame,
    screenshot_summary_df: pd.DataFrame,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        _sanitize_df(summary_df).to_excel(writer, sheet_name="总览", index=False)
        _sanitize_df(status_breakdown).to_excel(writer, sheet_name="状态分布", index=False)
        _sanitize_df(service_stats).to_excel(writer, sheet_name="业务分类统计", index=False)
        _sanitize_df(failure_by_reason).to_excel(writer, sheet_name="失败原因Top10", index=False)
        _sanitize_df(failure_top10).to_excel(writer, sheet_name="失败明细Top10", index=False)
        _sanitize_df(error_code_attribution_df).to_excel(writer, sheet_name="错误码归因分析", index=False)
        _sanitize_df(client_secondary_df).to_excel(writer, sheet_name="客户端异常二级原因", index=False)
        _sanitize_df(web_secondary_df).to_excel(writer, sheet_name="网页端异常二级原因", index=False)
        _sanitize_df(screenshot_summary_df).to_excel(writer, sheet_name="截图归因统计", index=False)
        _sanitize_df(invalid_summary).to_excel(writer, sheet_name="空跑汇总", index=False)
        _sanitize_df(invalid_detail).to_excel(writer, sheet_name="空跑明细", index=False)
        _sanitize_df(detail_df).to_excel(writer, sheet_name="原始任务明细", index=False)


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
    error_code_attribution_df: pd.DataFrame,
    client_secondary_df: pd.DataFrame,
    web_secondary_df: pd.DataFrame,
    screenshot_summary_df: pd.DataFrame,
) -> None:
    md_file.parent.mkdir(parents=True, exist_ok=True)

    def _val(df: pd.DataFrame, col: str, default: str = "—") -> str:
        for c in df.columns:
            if col in c:
                v = df.iloc[0][c]
                return str(v) if v is not None else default
        return default

    total     = _val(summary_df, "实际执行")
    success   = _val(summary_df, "成功任务")
    fail      = _val(summary_df, "失败任务")
    other     = _val(summary_df, "其他状态")
    rate      = _val(summary_df, "成功率")
    inv_count = _val(summary_df, "空跑数量")
    inv_rate  = _val(summary_df, "空跑占比")

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
        f"| 实际执行任务总数（含所有状态） | **{total}** |",
        f"| 执行成功任务数 | **{success}** |",
        f"| 执行失败任务数 | **{fail}** |",
        f"| 其他状态数（执行中 / 待执行） | **{other}** |",
        f"| 今日成功率（成功 / 总量） | **{rate}** |",
        f"| 空跑数量 | **{inv_count}** |",
        f"| 空跑占比（空跑 / 失败） | **{inv_rate}** |",
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
        "### 错误码归因分析",
        "",
        "> 按 error_codes.py 标准错误码对所有失败记录归类，含错误码标识、一级分类、责任人及影响范围。",
        "",
        _df_to_md(error_code_attribution_df),
        "### 客户端异常二级原因",
        "",
        "> robot_flow.run_support=10019005（客户端运行载体）的失败归入「客户端异常」大类；下表按原始分类（CLASSIFY_RULES）展示二级原因。",
        "",
        _df_to_md(client_secondary_df),
        "### 网页端异常二级原因",
        "",
        "> 网站异常、报盘为空、非办理时间等归入「网页端异常」大类；下表按原始分类展示二级原因。",
        "",
        _df_to_md(web_secondary_df),
        "### 截图归因统计",
        "",
        "> 统计失败任务中截图文件的覆盖情况，截图链接详见 Excel「原始任务明细」sheet 的 screenshot_url 列。",
        "",
        _df_to_md(screenshot_summary_df),
        "---",
        "",
        "## 四、空跑汇总",
        "",
        "> 空跑定义：错误码为 BIZ_NON_WORK_TIME_SMS（非工作时间不发短信）的无业务产出执行。报盘为空（BIZ_REPORT_EMPTY）单独统计，不计入错误。",
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
    rows = load_api_tasks_daily(conf["base_url"], token, session, start_time, end_time)
    print(f"共拉取 {len(rows)} 条记录")

    if not rows:
        print("当天没有实际执行任务")
        return

    print("正在拉取截图信息...")
    screenshot_map = fetch_screenshot_map_daily(conf["base_url"], token, session, start_time, end_time)
    print(f"截图映射加载完成，共 {len(screenshot_map)} 条")

    print("正在拉取 run_support 信息...")
    run_support_map = fetch_run_support_map_daily(conf["base_url"], token, session)
    print(f"run_support 映射加载完成，共 {len(run_support_map)} 条")

    fail_rows = [r for r in rows if int(r.get("status", -1)) == 3]
    print(f"正在拉取 {len(fail_rows)} 条失败任务的执行明细（stepName / errorStack）...")
    mongo_df = fetch_execution_errors_daily(conf["base_url"], token, session, fail_rows)
    print(f"执行明细加载完成，共 {len(mongo_df)} 条")

    result = build_report(rows, start_time, screenshot_map, run_support_map, mongo_df)
    (detail_df, summary_df, failure_by_reason, failure_top10,
     invalid_summary, invalid_detail, status_breakdown, service_stats,
     error_code_attribution_df, client_secondary_df, web_secondary_df, screenshot_summary_df) = result

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

    print("\n===== 错误码归因分析 =====")
    print(error_code_attribution_df.to_string(index=False))

    print("\n===== 客户端异常二级原因 =====")
    print(client_secondary_df.to_string(index=False))

    print("\n===== 网页端异常二级原因 =====")
    print(web_secondary_df.to_string(index=False))

    print("\n===== 截图归因统计 =====")
    print(screenshot_summary_df.to_string(index=False))

    print("\n===== 空跑数量汇总 =====")
    print(invalid_summary.to_string(index=False))

    # Excel
    xlsx_file = (
        Path(args.output) if args.output
        else _REPORTS_DIR / f"standalone_daily_report_{date_str}.xlsx"
    )
    write_report(xlsx_file, detail_df, summary_df, failure_by_reason, failure_top10,
                 invalid_summary, invalid_detail, status_breakdown, service_stats,
                 error_code_attribution_df, client_secondary_df, web_secondary_df, screenshot_summary_df)
    print(f"\nExcel 报表已生成：{xlsx_file}")

    # Markdown
    md_file = _REPORTS_DIR / f"standalone_daily_report_{date_str}.md"
    write_markdown(md_file, start_time.strftime("%Y-%m-%d"),
                   summary_df, status_breakdown, failure_by_reason, failure_top10,
                   invalid_summary, invalid_detail, service_stats,
                   error_code_attribution_df, client_secondary_df, web_secondary_df, screenshot_summary_df)
    print(f"Markdown 报表已生成：{md_file}")


if __name__ == "__main__":
    main()
