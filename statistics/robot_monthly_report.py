# -*- coding: utf-8 -*-
"""Generate monthly robot execution statistics from MySQL and MongoDB.

Usage:
    python scripts/statistics/robot_monthly_report.py
    python scripts/statistics/robot_monthly_report.py --month 2026-05
    python scripts/statistics/robot_monthly_report.py --output reports/robot_monthly_report.xlsx
    python scripts/statistics/robot_monthly_report.py --config /etc/rpa/db.conf
"""

import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

# 数据源、配置、工具函数统一由 data_source 模块提供
from data_source import (
    load_config,
    get_mysql_config,
    get_mongo_config,
    parse_stat_month,
    parse_addr_name,
    queue_item_name,
    build_service_stats,
    load_mysql_tasks_monthly,
    load_mongo_errors_monthly,
    QUEUE_ITEM_MAP,
    STATUS_MAP,
)

# 错误码、分类规则、工具函数统一由 error_codes 模块管理
from error_codes import (
    CLASSIFY_RULES,
    classify_reason,
    ERROR_CODE_BY_LABEL,
)

# 空跑：仅以下错误码认定为无业务产出的空跑执行（报盘为空单独处理，不纳入错误统计）
INVALID_ERROR_CODES = {"BIZ_NON_WORK_TIME_SMS"}

_REPORTS_DIR = Path(__file__).parent / "reports"

# 月报一级分类：沿用 declare_system + city 判断（月报无 run_support 字段）
_TAX_DECLARE_SYSTEM     = "10007007"
_ACCFUND_DECLARE_SYSTEM = "10008001"
_FORCE_CLIENT_CITIES    = {"上海", "成都"}


def _classify_l1_monthly(text: str, declare_system: str, city: str = "") -> str:
    ds = str(declare_system)
    if ds == _TAX_DECLARE_SYSTEM:
        return "客户端异常"
    if ds == _ACCFUND_DECLARE_SYSTEM and str(city) in _FORCE_CLIENT_CITIES:
        return "客户端异常"
    return "网页端异常"


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
    month_label: str,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:

    print(f"  加载 MySQL 数据（{month_label}）...")
    task_df = load_mysql_tasks_monthly(start_time, end_time, mysql_cfg)
    if task_df.empty:
        print("  当月无实际执行任务。")
        empty = pd.DataFrame()
        return empty, empty, empty, empty, empty, empty

    print(f"  MySQL 加载完成，共 {len(task_df)} 条记录")

    # 城市、任务类型
    task_df["city"]      = task_df["app_args"].apply(parse_addr_name).replace("", "未记录城市").fillna("未记录城市")
    task_df["task_type"] = task_df["queue_item"].apply(queue_item_name)
    task_df["mysql_comment"] = task_df["comment"].fillna("")

    # MongoDB 补充失败信息
    execution_codes = task_df["execution_code"].dropna().astype(str).unique().tolist()
    print(f"  查询 MongoDB 失败明细（共 {len(execution_codes)} 个执行编码）...")
    mongo_df = load_mongo_errors_monthly(execution_codes, start_time, end_time, mongo_uri, mongo_db, mongo_col)
    print(f"  MongoDB 命中 {len(mongo_df)} 条")

    if mongo_df.empty:
        task_df["mongo_error_text"] = ""
        task_df["fail_step"]        = task_df["mysql_comment"].str.replace("\n", " ").str.strip().str[:80]
    else:
        mongo_df["executionCode"]   = mongo_df["executionCode"].astype(str)
        mongo_df["mongo_error_text"] = (
            mongo_df.get("stepName",   pd.Series(dtype=str)).fillna("").astype(str) + " "
            + mongo_df.get("error",    pd.Series(dtype=str)).fillna("").astype(str) + " "
            + mongo_df.get("errorStack", pd.Series(dtype=str)).fillna("").astype(str)
        )
        task_df["execution_code"] = task_df["execution_code"].astype(str)
        task_df = task_df.merge(
            mongo_df[["executionCode", "mongo_error_text", "stepName"]],
            how="left", left_on="execution_code", right_on="executionCode",
        )
        task_df["mongo_error_text"] = task_df["mongo_error_text"].fillna("")
        task_df["fail_step"] = task_df.apply(
            lambda x: str(x.get("stepName", "") or "").strip()
                      or str(x.get("mysql_comment", "") or "").replace("\n", " ").strip()[:80],
            axis=1,
        )

    task_df["mongo_error_text"] = task_df["mongo_error_text"].fillna("")

    # fail_reason_raw：优先 mysql_comment，未命中再用 mongo_error_text
    def _classify_combined(mc: str, me: str) -> str:
        raw = classify_reason(mc)
        return raw if raw != "未记录原因" else classify_reason(me)

    task_df["fail_reason_raw"] = task_df.apply(
        lambda x: _classify_combined(x["mysql_comment"], x["mongo_error_text"]), axis=1
    )

    # fail_source_text 用于一级大类判断
    task_df["fail_source_text"] = task_df.apply(
        lambda x: x["mongo_error_text"] if x["mongo_error_text"].strip() else x["mysql_comment"],
        axis=1,
    )
    task_df["fail_reason"] = task_df.apply(
        lambda x: _classify_l1_monthly(
            x["fail_source_text"],
            x.get("declare_system", ""),
            x.get("city", ""),
        ), axis=1
    )

    # 执行时长（分钟）
    task_df["pra_start_time"] = pd.to_datetime(task_df["pra_start_time"], errors="coerce")
    task_df["pra_end_time"]   = pd.to_datetime(task_df["pra_end_time"],   errors="coerce")
    task_df["duration_min"] = (
        (task_df["pra_end_time"] - task_df["pra_start_time"])
        .dt.total_seconds().div(60).round(2).clip(lower=0)
    )

    # 报盘为空（BIZ_REPORT_EMPTY）认定为成功，从 status=3 中剔除，不纳入失败统计。
    # 当且仅当：仅对 mysql_comment 单独匹配（不混入 mongo 文本），
    # BIZ_REPORT_EMPTY 已排在规则列表末尾，其他规则先命中时不会返回"下载报盘文件为空"。
    report_empty_mask = (task_df["status"] == 3) & (
        task_df["mysql_comment"].apply(classify_reason) == "下载报盘文件为空"
    )
    # 报盘为空认定为执行成功，status 数值同步改写，
    # 后续所有基于 status==3/4 的统计（业务分类统计等）自动正确
    task_df.loc[report_empty_mask, "status"] = 4
    fail_df = task_df[task_df["status"] == 3].copy()

    # ------------------------------------------------------------------ #
    # 一、执行总览
    # ------------------------------------------------------------------ #
    total_count   = len(task_df)
    # 报盘为空已改写为 status=4，直接按数值统计即可
    success_count = int((task_df["status"] == 4).sum())
    fail_count    = int((task_df["status"] == 3).sum())
    other_count   = total_count - success_count - fail_count
    success_rate  = round(success_count / total_count * 100, 2) if total_count else 0

    summary_df = pd.DataFrame([{
        "统计月份":               month_label,
        "实际执行任务总数（含所有状态）": total_count,
        "执行成功任务数": success_count,
        "执行失败任务数": fail_count,
        "报盘为空任务数（计入成功）": int(report_empty_mask.sum()),
        "其他状态数（执行中/待执行）": other_count,
        "成功率（成功/总量）":       f"{success_rate}%",
        "空跑数量": 0,
        "空跑占比（空跑/失败）": "—",
    }])

    # ------------------------------------------------------------------ #
    # 二、失败归因（按 CLASSIFY_RULES 二级原因统计，含大类拆分）
    # ------------------------------------------------------------------ #
    total_fail = len(fail_df)
    client_fail_df = fail_df[fail_df["fail_reason"] == "客户端异常"]
    web_fail_df    = fail_df[fail_df["fail_reason"] == "网页端异常"]

    attribution_rows = []
    for raw_reason, grp in fail_df.groupby("fail_reason_raw", dropna=False):
        cnt       = len(grp)
        pct       = f"{round(cnt / total_fail * 100, 1)}%" if total_fail else "—"

        cli_cnt   = int((grp["fail_reason"] == "客户端异常").sum())
        cli_pct   = f"{round(cli_cnt / cnt * 100, 1)}%" if cnt else "—"
        web_cnt   = int((grp["fail_reason"] == "网页端异常").sum())
        web_pct   = f"{round(web_cnt / cnt * 100, 1)}%" if cnt else "—"

        attribution_rows.append({
            "异常原因":          str(raw_reason),
            "失败数量":          cnt,
            "占比":             pct,
            "客户端失败数量":    cli_cnt,
            "客户端失败占比":    cli_pct,
            "网页端失败数量":    web_cnt,
            "网页端失败占比":    web_pct,
        })

    attribution_df = (
        pd.DataFrame(attribution_rows)
        .sort_values("失败数量", ascending=False)
        .reset_index(drop=True)
    )

    # ------------------------------------------------------------------ #
    # 二-b、空跑统计（与 daily 对齐：BIZ_NON_WORK_TIME_SMS）
    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
    # 三、业务分类统计（与 daily 对齐，使用共用 build_service_stats）
    # ------------------------------------------------------------------ #
    service_df = build_service_stats(
        task_df,
        status_col="status", s4=4, s3=3,
        city_col="city", machine_col="machine_code",
        duration_col="duration_min", fail_step_col="fail_step",
        fail_reason_col="fail_reason",
    )

    # ------------------------------------------------------------------ #
    # 四、原始失败任务明细（仅 Excel 输出）
    # ------------------------------------------------------------------ #
    fail_detail_cols = [c for c in [
        "id", "client_id", "execution_code", "machine_code", "task_code",
        "declare_account", "company_name", "business_type", "declare_system",
        "queue_item", "task_type", "city", "status",
        "pra_start_time", "pra_end_time", "duration_min",
        "mysql_comment", "fail_step", "fail_reason_raw", "fail_reason",
    ] if c in task_df.columns]
    fail_detail_df = fail_df[fail_detail_cols].copy()

    return summary_df, attribution_df, service_df, fail_detail_df, invalid_summary, invalid_detail


# --------------------------------------------------------------------------- #
# 输出：Excel
# --------------------------------------------------------------------------- #

_ILLEGAL_CHARS_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff�]"
)


def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """清除 DataFrame 中 openpyxl 不支持的非法字符，避免写入 Excel 时报错。"""
    def _clean_val(v):
        if isinstance(v, str):
            return _ILLEGAL_CHARS_RE.sub("", v)
        return v
    return df.applymap(_clean_val)


def write_report(
    output_file: Path,
    summary_df: pd.DataFrame,
    attribution_df: pd.DataFrame,
    service_df: pd.DataFrame,
    fail_detail_df: pd.DataFrame,
    invalid_summary: pd.DataFrame,
    invalid_detail: pd.DataFrame,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        _clean_df(summary_df).to_excel(writer,      sheet_name="一、执行总览",       index=False)
        _clean_df(attribution_df).to_excel(writer,  sheet_name="二、失败归因",       index=False)
        _clean_df(service_df).to_excel(writer,      sheet_name="三、业务分类统计",   index=False)
        _clean_df(invalid_summary).to_excel(writer, sheet_name="四、空跑汇总",       index=False)
        _clean_df(invalid_detail).to_excel(writer,  sheet_name="五、空跑明细",       index=False)
        _clean_df(fail_detail_df).to_excel(writer,  sheet_name="六、原始失败任务明细", index=False)


# --------------------------------------------------------------------------- #
# 输出：Markdown（前三章，不含原始明细）
# --------------------------------------------------------------------------- #

def _df_to_md(df: pd.DataFrame) -> str:
    if df.empty:
        return "_（无数据）_\n"
    cols = list(df.columns)
    lines = [
        "| " + " | ".join(str(c) for c in cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(str(v) if v is not None else "" for v in row) + " |")
    return "\n".join(lines) + "\n"


def write_markdown(
    md_file: Path,
    month_label: str,
    summary_df: pd.DataFrame,
    attribution_df: pd.DataFrame,
    service_df: pd.DataFrame,
    fail_detail_df: pd.DataFrame,
    invalid_summary: pd.DataFrame,
    invalid_detail: pd.DataFrame,
) -> None:
    md_file.parent.mkdir(parents=True, exist_ok=True)

    def _val(col: str, default: str = "—") -> str:
        for c in summary_df.columns:
            if col in c:
                v = summary_df.iloc[0][c]
                return str(v) if v is not None else default
        return default

    total      = _val("实际执行")
    success    = _val("成功任务")
    fail       = _val("失败任务")
    other      = _val("其他状态")
    rate       = _val("成功率")
    inv_count  = _val("空跑数量")
    inv_rate   = _val("空跑占比")
    fail_total = str(fail_detail_df.shape[0]) if not fail_detail_df.empty else "0"

    lines = [
        f"# RPA 机器人月度执行统计报告",
        f"",
        f"> 统计月份：**{month_label}**　｜　数据来源：SaaS MySQL + MongoDB",
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
        f"| 成功率（成功 / 总量） | **{rate}** |",
        f"| 空跑数量 | **{inv_count}** |",
        f"| 空跑占比（空跑 / 失败） | **{inv_rate}** |",
        f"",
        f"---",
        f"",
        f"## 二、失败归因",
        f"",
        f"> 按 CLASSIFY_RULES 异常细分规则统计，含客户端 / 网页端大类拆分。",
        f"",
        _df_to_md(attribution_df),
        f"---",
        f"",
        f"## 三、业务分类统计",
        f"",
        f"> 按任务类型（服务项）分组统计执行结果、时长及主要问题。",
        f"",
        _df_to_md(service_df),
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
        f"## 五、原始失败任务明细",
        f"",
        f"> 共 **{fail_total}** 条失败记录，详见 Excel「六、原始失败任务明细」Sheet。",
        f"",
        f"---",
        f"",
        f"_报告由 robot_monthly_report.py 自动生成_",
    ]

    md_file.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="生成 RPA 机器人月度执行统计报表（SaaS 直连数据库版）")
    parser.add_argument("--month",  dest="month",  help="统计月份，格式 YYYY-MM；默认统计上个自然月")
    parser.add_argument("--output", dest="output", help="输出 Excel 文件路径；默认保存到 reports/ 目录")
    parser.add_argument("--config", dest="config", help="db.conf 路径；默认读取脚本同目录的 db.conf")
    args = parser.parse_args()

    cfg = load_config(Path(args.config) if args.config else None)
    mysql_cfg = get_mysql_config(cfg)
    mongo_uri, mongo_db, mongo_col = get_mongo_config(cfg)

    start_time, end_time, month_label = parse_stat_month(args.month)
    print(f"统计月份：{month_label}（{start_time.date()} ~ {end_time.date()}，左闭右开）")

    summary_df, attribution_df, service_df, fail_detail_df, invalid_summary, invalid_detail = build_report(
        start_time, end_time, mysql_cfg, mongo_uri, mongo_db, mongo_col, month_label
    )

    if summary_df.empty:
        print("当月没有实际执行任务。")
        return

    # 控制台摘要
    print("\n===== 月度执行总览 =====")
    print(summary_df.to_string(index=False))

    print("\n===== 失败归因 =====")
    print(attribution_df.to_string(index=False))

    print("\n===== 业务分类统计 =====")
    print(service_df.to_string(index=False))

    print("\n===== 空跑数量汇总 =====")
    print(invalid_summary.to_string(index=False))

    print(f"\n原始失败任务明细：{len(fail_detail_df)} 条（仅写入 Excel）")

    # Excel
    xlsx_file = (
        Path(args.output) if args.output
        else _REPORTS_DIR / f"robot_monthly_report_{month_label.replace('-', '')}.xlsx"
    )
    write_report(xlsx_file, summary_df, attribution_df, service_df, fail_detail_df, invalid_summary, invalid_detail)
    print(f"\nExcel 报表已生成：{xlsx_file}")

    # Markdown
    md_file = _REPORTS_DIR / f"robot_monthly_report_{month_label.replace('-', '')}.md"
    write_markdown(md_file, month_label, summary_df, attribution_df, service_df, fail_detail_df, invalid_summary, invalid_detail)
    print(f"Markdown 报表已生成：{md_file}")


if __name__ == "__main__":
    main()
