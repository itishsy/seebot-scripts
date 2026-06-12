# -*- coding: utf-8 -*-
"""Generate daily robot execution statistics from MySQL and MongoDB.

Usage:
    python scripts/statistics/robot_daily_report.py
    python scripts/statistics/robot_daily_report.py --date 2026-05-31
    python scripts/statistics/robot_daily_report.py --output reports/robot_daily_report.xlsx
    python scripts/statistics/robot_daily_report.py --config /etc/rpa/db.conf
"""

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

# 数据源、配置、工具函数统一由 data_source 模块提供
from data_source import (
    load_config,
    get_mysql_config,
    get_mongo_config,
    parse_stat_window,
    parse_addr_name,
    queue_item_name,
    status_name,
    build_service_stats,
    load_mysql_tasks_daily,
    load_mongo_errors_daily,
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

# 空跑：仅以下错误码认定为无业务产出的空跑执行（报盘为空单独处理，不纳入错误统计）
INVALID_ERROR_CODES = {"BIZ_NON_WORK_TIME_SMS"}


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
    file_base_url: str = "",
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:

    task_df = load_mysql_tasks_daily(start_time, end_time, mysql_cfg)
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
    mongo_df = load_mongo_errors_daily(execution_codes, start_time, end_time, mongo_uri, mongo_db, mongo_col)

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

    # 一级分类：robot_flow.run_support=10019005 为客户端异常，其余归网页端异常
    task_df["fail_reason"] = task_df.apply(
        lambda x: classify_reason_l1(
            x["fail_source_text"],
            x.get("run_support", ""),
        ), axis=1
    )

    # ---------- 汇总指标 ----------
    # 报盘为空（BIZ_REPORT_EMPTY）认定为成功，从 status=3 中剔除，不纳入失败统计。
    # 当且仅当：仅对 mysql_comment 单独匹配（不混入 mongo 文本），
    # BIZ_REPORT_EMPTY 已排在规则列表末尾，其他规则先命中时不会返回"下载报盘文件为空"。
    report_empty_mask = (task_df["status"] == 3) & (
        task_df["mysql_comment"].apply(classify_reason) == "下载报盘文件为空"
    )
    # 报盘为空认定为执行成功，status 数值和 status_name 同步改写，
    # 后续所有基于 status==s4/s3 的统计（build_service_stats 等）自动正确
    task_df.loc[report_empty_mask, "status"] = 4
    task_df.loc[report_empty_mask, "status_name"] = "执行成功"
    total_count = len(task_df)
    # 报盘为空已改写为 status=4，直接按数值统计即可
    success_count = int((task_df["status"] == 4).sum())
    fail_count    = int((task_df["status"] == 3).sum())
    other_count   = total_count - success_count - fail_count
    success_rate = round(success_count / total_count * 100, 2) if total_count else 0

    # 各状态明细（report_empty_mask 任务的 status_name 已改写为"执行成功"，直接 groupby）
    status_breakdown = (
        task_df.groupby("status_name").size()
        .reset_index(name="数量")
        .sort_values("数量", ascending=False)
    )

    # 空跑总数在 invalid_df 构建后回填，此处先用占位值
    summary_df = pd.DataFrame([{
        "统计日期": start_time.strftime("%Y-%m-%d"),
        "实际执行任务总数": total_count,
        "执行成功任务数": success_count,
        "执行失败任务数": fail_count,
        "报盘为空任务数（计入成功）": int(report_empty_mask.sum()),
        "其他状态数（执行中/待执行）": other_count,
        "今日成功率（成功/总量）": f"{success_rate}%",
        "空跑数量": 0,
        "空跑占比（空跑/失败）": "—",
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
    # 空跑定义：fail_reason_raw 对应的错误码属于 INVALID_ERROR_CODES（非工作时间）
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
            ["fail_reason_raw", "city", "declare_account", "machine_code", "task_type"],
            dropna=False,
        )
        .size()
        .reset_index(name="空跑次数")
        .sort_values("空跑次数", ascending=False)
    )
    invalid_detail.rename(columns={
        "fail_reason_raw": "空跑类型",
        "city": "城市",
        "declare_account": "账号",
        "machine_code": "盒子",
        "task_type": "任务类型",
    }, inplace=True)

    # 回填空跑统计到 summary_df
    invalid_total = len(invalid_df)
    invalid_rate  = f"{round(invalid_total / fail_count * 100, 1)}%" if fail_count else "—"
    summary_df.at[0, "空跑数量"] = invalid_total
    summary_df.at[0, "空跑占比（空跑/失败）"] = invalid_rate

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

    service_stats = build_service_stats(task_df, status_col="status", s4=4, s3=3,
                                        city_col="city", machine_col="machine_code",
                                        duration_col="duration_min", fail_step_col="fail_step",
                                        fail_reason_col="fail_reason")

    # ---------- 错误码归因表（替代原客户端/网页端二级原因拆分）----------
    def _join_unique(series, sep="、", limit=5) -> str:
        vals = sorted({str(v) for v in series.dropna() if str(v).strip()})
        return sep.join(vals[:limit]) + ("…" if len(vals) > limit else "")

    total_fail_cnt = len(fail_df)
    attrib_rows = []
    for raw_reason, grp in fail_df.groupby("fail_reason_raw", dropna=False):
        cnt  = len(grp)
        meta = ERROR_CODE_BY_LABEL.get(str(raw_reason))
        attrib_rows.append({
            "错误码标识":   meta.code  if meta else "—",
            "异常原因":     str(raw_reason),
            "一级分类":     meta.l1    if meta else "—",
            "失败数量":     cnt,
            "占比":         f"{round(cnt / total_fail_cnt * 100, 1)}%" if total_fail_cnt else "—",
            "责任人":       meta.owner if meta else "—",
            "是否可自动修复": "是" if (meta and meta.auto_fix) else "否",
            "涉及城市":     _join_unique(grp["city"]),
            "涉及业务类型": _join_unique(grp["task_type"]),
            "涉及账号":     _join_unique(grp["declare_account"]),
            "涉及盒子":     _join_unique(grp["machine_code"]),
        })
    error_code_attribution_df = (
        pd.DataFrame(attrib_rows)
        .sort_values("失败数量", ascending=False)
        .reset_index(drop=True)
    )

    # ---------- 截图归因统计 ----------
    # 截图标记必须在 fail_df 构建之后打，否则 fail_df（copy）中没有该列
    # 同时支持 file_base_url 拼接完整访问地址
    # file_base_url 由调用方传入（来自 db.conf [mysql] file_base_url 配置项）
    if "screenshot_url" in task_df.columns:
        # 拼接完整 URL（若 file_base_url 已配置且路径不以 http 开头）
        def _build_url(path):
            if not path or str(path).strip() in ("", "None", "nan"):
                return ""
            s = str(path).strip()
            if s.startswith("http"):
                return s
            return file_base_url.rstrip("/") + "/" + s if file_base_url else s
        task_df["screenshot_url"] = task_df["screenshot_url"].apply(_build_url)

    task_df["has_screenshot"] = (
        task_df["screenshot_url"].notna()
        & (task_df["screenshot_url"].astype(str).str.strip() != "")
    )
    fail_mask = task_df["status"] == 3
    total_fail      = int(fail_mask.sum())
    fail_with_shot  = int((fail_mask & task_df["has_screenshot"]).sum())
    client_mask     = fail_mask & (task_df["fail_reason"] == "客户端异常")
    client_fail_cnt = int(client_mask.sum())
    client_with_shot = int((client_mask & task_df["has_screenshot"]).sum())

    screenshot_summary_df = pd.DataFrame([{
        "失败任务数量":             total_fail,
        "有失败截图的任务数":        fail_with_shot,
        "截图覆盖率":               f"{round(fail_with_shot / total_fail * 100, 1)}%" if total_fail else "—",
        "客户端异常任务数":          client_fail_cnt,
        "客户端异常中有截图的任务数": client_with_shot,
        "客户端截图覆盖率":          f"{round(client_with_shot / client_fail_cnt * 100, 1)}%" if client_fail_cnt else "—",
    }])

    # 原始明细去掉大字段，保留关键列（失败任务，含截图链接）
    export_cols = [
        c for c in [
            "id", "client_id", "execution_code", "machine_code", "task_code",
            "declare_account", "company_name", "business_type", "declare_system",
            "queue_item", "task_type", "city", "status", "status_name",
            "login_status", "pra_start_time", "pra_end_time", "duration_min",
            "mysql_comment", "fail_reason_raw", "fail_reason", "screenshot_url",
        ] if c in task_df.columns
    ]
    detail_df = task_df[export_cols].copy()

    return detail_df, summary_df, failure_by_reason, failure_top10, invalid_summary, invalid_detail, status_breakdown, service_stats, error_code_attribution_df, screenshot_summary_df


# --------------------------------------------------------------------------- #
# 输出：Excel
# --------------------------------------------------------------------------- #

def _sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    """清理 DataFrame 中字符串列里 openpyxl/XML 1.0 不接受的非法控制字符。"""
    import re
    # XML 1.0 合法字符范围之外的控制字符（保留 \t \n \r）
    _ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

    def _clean(v):
        if isinstance(v, str):
            cleaned = _ILLEGAL.sub("", v)
            # 若清理后字符串超过 32767（Excel 单元格上限），截断并加省略
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
    screenshot_summary_df: pd.DataFrame,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    # 写入前对所有 DataFrame 做字符清洗，避免 openpyxl IllegalCharacterError
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        _sanitize_df(summary_df).to_excel(writer, sheet_name="总览", index=False)
        _sanitize_df(status_breakdown).to_excel(writer, sheet_name="状态分布", index=False)
        _sanitize_df(service_stats).to_excel(writer, sheet_name="业务分类统计", index=False)
        _sanitize_df(failure_by_reason).to_excel(writer, sheet_name="失败原因Top10", index=False)
        _sanitize_df(failure_top10).to_excel(writer, sheet_name="失败明细Top10", index=False)
        _sanitize_df(error_code_attribution_df).to_excel(writer, sheet_name="错误码归因分析", index=False)
        _sanitize_df(screenshot_summary_df).to_excel(writer, sheet_name="截图归因统计", index=False)
        _sanitize_df(invalid_summary).to_excel(writer, sheet_name="空跑汇总", index=False)
        _sanitize_df(invalid_detail).to_excel(writer, sheet_name="空跑明细", index=False)
        _sanitize_df(detail_df).to_excel(writer, sheet_name="原始任务明细", index=False)


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
    error_code_attribution_df: pd.DataFrame,
    screenshot_summary_df: pd.DataFrame,
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

    inv_count = _val(summary_df, "空跑数量")
    inv_rate  = _val(summary_df, "空跑占比")

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
        f"| 实际执行任务总数（含所有状态） | **{total}** |",
        f"| 执行成功任务数 | **{success}** |",
        f"| 执行失败任务数 | **{fail}** |",
        f"| 其他状态数（执行中 / 待执行） | **{other}** |",
        f"| 今日成功率（成功 / 总量） | **{rate}** |",
        f"| 空跑数量 | **{inv_count}** |",
        f"| 空跑占比（空跑 / 失败） | **{inv_rate}** |",
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
        f"### 错误码归因分析",
        f"",
        f"> 按 error_codes.py 标准错误码对所有失败记录归类，含错误码标识、一级分类、责任人及影响范围。",
        f"",
        _df_to_md(error_code_attribution_df),
        f"### 截图归因统计",
        f"",
        f"> 统计失败任务中截图文件的覆盖情况，截图链接详见 Excel「原始任务明细」sheet 的 screenshot_url 列。",
        f"",
        _df_to_md(screenshot_summary_df),
        f"---",
        f"",
        f"## 四、空跑汇总",
        f"",
        f"> 空跑定义：错误码为 BIZ_NON_WORK_TIME_SMS（非工作时间不发短信）的无业务产出执行。报盘为空（BIZ_REPORT_EMPTY）单独统计，不计入错误。",
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

    # file_base_url：FastDFS/OSS 访问域名，用于拼接截图完整 URL
    sec_mysql = cfg["mysql"] if cfg.has_section("mysql") else {}
    file_base_url = sec_mysql.get("file_base_url", "") or os.getenv("RPA_STAT_FILE_BASE_URL", "")

    result = build_report(start_time, end_time, mysql_cfg, mongo_uri, mongo_db, mongo_col, file_base_url)
    detail_df, summary_df, failure_by_reason, failure_top10, invalid_summary, invalid_detail, status_breakdown, service_stats, error_code_attribution_df, screenshot_summary_df = result

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

    print("\n===== 错误码归因分析 =====")
    print(error_code_attribution_df.to_string(index=False))

    print("\n===== 空跑数量汇总 =====")
    print(invalid_summary.to_string(index=False))

    # Excel
    xlsx_file = (
        Path(args.output) if args.output
        else _REPORTS_DIR / f"robot_daily_report_{date_str}.xlsx"
    )
    write_report(xlsx_file, detail_df, summary_df, failure_by_reason, failure_top10, invalid_summary, invalid_detail, status_breakdown, service_stats, error_code_attribution_df, screenshot_summary_df)
    print(f"\nExcel 报表已生成：{xlsx_file}")

    # Markdown（始终输出到 reports/ 目录）
    md_file = _REPORTS_DIR / f"robot_daily_report_{date_str}.md"
    write_markdown(md_file, start_time.strftime("%Y-%m-%d"),
                   summary_df, status_breakdown, failure_by_reason, failure_top10, invalid_summary, invalid_detail, service_stats, error_code_attribution_df, screenshot_summary_df)
    print(f"Markdown 报表已生成：{md_file}")


if __name__ == "__main__":
    main()
