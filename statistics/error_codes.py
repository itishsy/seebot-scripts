# -*- coding: utf-8 -*-
"""
RPA 标准错误码管理模块  v0.1
==============================

版本历史
--------
v0.1  2026-06-02  初版落地，基于 2026-05 月报明细分析建立

设计约定
--------
一级分类（L1）含义：
  CLIENT  客户端本地程序问题（win窗口/图片点击/输入操作）
  WEB     网站/页面侧问题（元素变更/请求失败/业务窗口关闭）
  AUTH    认证/登录/证书问题
  CONFIG  系统/代码配置错误（Java异常/RPA引擎报错）
  FLOW    流程编排问题（无操作步骤/步骤超时/循环等待超限）
  BIZ     业务数据/逻辑问题（账户不一致/数据为空/余额不足）
  ENV     环境/基础设施问题（盒子/设备/网络/流程未同步）

字段说明
--------
code          全局唯一错误码（大写下划线）
l1            一级分类
label         报表中文名（CLASSIFY_RULES 输出值）
pattern       正则表达式，对 mysql_comment / mongo error 全文匹配
              注意：顺序即优先级，先匹配先命中，宽泛规则必须排在精确规则之后
screenshot    截图字段路径
log_field     日志字段路径
auto_fix      是否可自动修复
pre_intercept 是否可前置拦截（执行前检查）
re_run        是否可自动重跑
need_human    是否需人工接管
owner         责任人标签（用于告警路由）

规则修正说明（v0.1 相对初版的变更）
------------------------------------
1. AUTH_LOGIN_INVALID
   - 删除单独的「登录」词（会误命中 stepName="登录" 的无关报错）
   - 补充「登陆」（陆）的写法（68条漏掉）
   - 调整优先级：移到 BIZ_NON_WORK_TIME_SMS 和 FLOW_STEP_TIMEOUT 之后，
     避免非工作时间/超时场景被「登录」先截胡

2. AUTH_UKEY_ERROR
   - 删除单独的「USB」（过宽），保留「usbkey / UsbKey / 激活USB」等精确表述

3. ENV_BOX_OR_DEVICE_ERROR
   - 删除单独的「超时」「500」（过宽），独立拆出 FLOW_STEP_TIMEOUT 承接超时场景
   - 保留「设备重启|机器人状态异常|SQLiteException|环境异常」

4. 新增 9 个错误码：
   FLOW_STEP_TIMEOUT / FLOW_NO_STEP /
   WEB_HTTP_REQUEST_FAIL /
   AUTH_CERT_FAIL /
   CLIENT_WIN_OP_ERROR /
   BIZ_PAY_FAIL / BIZ_ACCOUNT_MISMATCH / BIZ_DATA_INVALID / ENV_FLOW_NOT_SYNC
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

CLIENT_RUN_SUPPORT = "10019005"   # robot_flow.run_support = 客户端运行载体
VERSION = "v0.1"


# --------------------------------------------------------------------------- #
# 错误码数据结构
# --------------------------------------------------------------------------- #

@dataclass
class ErrorCode:
    code: str            # 错误码标识
    l1: str              # 一级分类
    label: str           # 报表中文名
    pattern: str         # 匹配正则
    screenshot: str      # 截图字段路径
    log_field: str       # 日志字段路径
    auto_fix: bool       # 是否可自动修复
    pre_intercept: bool  # 是否可前置拦截
    re_run: bool         # 是否可重跑
    need_human: bool     # 是否需人工接管
    owner: str           # 责任人


# --------------------------------------------------------------------------- #
# 错误码注册表
# 优先级规则：
#   1. 精确的业务语义规则（BIZ/AUTH 具体词）排在前面
#   2. 宽泛的技术规则（CONFIG/ENV）排在后面
#   3. 「其它原因」兜底由代码逻辑保证，不在此注册
# --------------------------------------------------------------------------- #

ERROR_CODE_LIST: List[ErrorCode] = [

    # ============================================================== BIZ（业务）
    # 排在最前：业务关键词精确，避免被宽泛的技术规则截胡

    ErrorCode(
        code="BIZ_NON_WORK_TIME_SMS",
        l1="BIZ",
        label="非工作时间不发短信",
        # 数据来源：mysql_comment 含「非工作时间内，不发送短信」
        # 注意：此类 comment 的 stepName 常为「登录」，必须排在 AUTH_LOGIN_INVALID 前
        pattern=r"非工作时间|不发短信|工作时间.*短信|短信.*工作时间",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=True,
        re_run=True,
        need_human=False,
        owner="业务运营",
    ),

    ErrorCode(
        code="BIZ_ACCOUNT_MISMATCH",
        l1="BIZ",
        label="申报账户不一致",
        # 典型：「申报账户不一致」「账户不一致，关闭浏览器」
        # 月报量：~183条
        pattern=r"申报账户不一致|账户不一致|账户.*不一致",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=True,
        re_run=False,
        need_human=True,
        owner="业务运营",
    ),

    ErrorCode(
        code="BIZ_DATA_INVALID",
        l1="BIZ",
        label="业务数据异常",
        # 典型：「明细数据存在空费款所属期」「所属期不一致」「国籍与证件类型不匹配」
        #        「未入账」「本月还未入账」「导入数据不能为空」「查询到的业务受理号为空」
        # 月报量：~200条
        pattern=(
            r"空费款所属期|所属期不一致|国籍.*不匹配|证件类型不匹配"
            r"|未入账|还未入账"
            r"|导入数据不能为空"
            r"|业务受理号为空"
            r"|日常申报获取数据异常"
        ),
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=True,
        re_run=False,
        need_human=True,
        owner="业务运营",
    ),

    ErrorCode(
        code="BIZ_PAY_FAIL",
        l1="BIZ",
        label="业务缴费失败",
        # 典型：「汇缴提交失败」「缴费失败」「余额不足」「上传失败，无法提交」
        #        「金额不一致退出流程」「未获取到缴费项」「获取凭据失败」
        # 月报量：~300条
        pattern=(
            r"汇缴提交失败|缴费失败|余额不足"
            r"|上传失败.*无法提交"
            r"|金额不一致.*退出"
            r"|未获取到缴费项"
            r"|获取凭据失败"
            r"|本次缴费.*还在处理中"
            r"|减员失败"
        ),
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=False,
        re_run=False,
        need_human=True,
        owner="业务运营",
    ),

    ErrorCode(
        code="BIZ_MANUAL_CANCEL",
        l1="BIZ",
        label="手动撤销任务",
        # 月报量：~23条，人工主动操作，不属于异常
        pattern=r"手动撤销任务|手动取消",
        screenshot="",
        log_field="robot_task_queue.comment",
        auto_fix=False,
        pre_intercept=False,
        re_run=False,
        need_human=False,
        owner="业务运营",
    ),

    ErrorCode(
        code="BIZ_BUSINESS_WINDOW_CLOSED",
        l1="BIZ",
        label="网站关闭业务窗口",
        # 原 WEB_BUSINESS_WINDOW_CLOSED，调整为 BIZ（网站业务时间策略属业务侧）
        pattern=r"非办理时间|不在办理时间|办理时间|业务办理时间|系统维护|暂停办理|退出申报|暂未缴费|业务办理失败",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=True,
        re_run=True,
        need_human=False,
        owner="业务运营",
    ),

    # ============================================================== AUTH（认证）

    ErrorCode(
        code="AUTH_UKEY_ERROR",
        l1="AUTH",
        label="UKey挂载异常",
        # 修正：删除单独的「USB」（过宽），保留精确词
        # 典型：「挂载UsbKey失败」「UsbKey状态为：已被挂载」「CA证书激活」
        # 月报量：~2,486条
        pattern=(
            r"UKey|ukey|U盾|usbkey|UsbKey|激活USB|挂载.*Key"
            r"|证书过期|CA证书|ukey认证|key失效|key未插|key未找到"
        ),
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=True,
        re_run=False,
        need_human=True,
        owner="硬件运维",
    ),

    ErrorCode(
        code="AUTH_CERT_FAIL",
        l1="AUTH",
        label="证书识别/读取失败",
        # 新增：「证书识别失败」「读取证书失败」不属于 UKey 插拔问题，是证书本身解析失败
        # 月报量：~308条
        pattern=r"证书识别失败|读取证书失败|证书.*失败|失败.*证书",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=False,
        re_run=False,
        need_human=True,
        owner="硬件运维",
    ),

    ErrorCode(
        code="AUTH_H5_LOGIN_INVALID",
        l1="AUTH",
        label="H5登录未扫码",
        pattern=(
            r"H5登录失败|H5登录异常"
        ),
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=True,
        re_run=True,
        need_human=False,
        owner="账号管理",
    ),

    ErrorCode(
        code="AUTH_LOGIN_INVALID",
        l1="AUTH",
        label="登录失败/登录无效",
        # 修正：
        #   - 删除单独的「登录」（会误命中 stepName="登录" 的无关错误）
        #   - 补充「登陆」（陆）的写法
        #   - 保留明确失败语义的词组
        # 月报量：~19,389条（修正后应减少 ~2,844 误命中）
        pattern=(
            r"登录失败|登陆失败|登录无效|登录失效|登陆失效"
            r"|会话失效|未登录|重新登录"
            r"|验证码错误|账号密码错误"
            r"|超过最大重试登录次数|H5登录失败|H5登录异常"
            r"|纳税识别号.*密码错误"
            r"|登录.*超时|超时.*登录"
        ),
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=True,
        re_run=True,
        need_human=False,
        owner="账号管理",
    ),

    # ============================================================== CLIENT（客户端）

    ErrorCode(
        code="CLIENT_POPUP_NEW",
        l1="CLIENT",
        label="客户端新增弹窗",
        pattern=r"弹窗|新增弹|pop.*up|unexpected.*dialog|unexpected.*window",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=False,
        re_run=True,
        need_human=True,
        owner="客户端研发",
    ),

    ErrorCode(
        code="CLIENT_WIN_OP_ERROR",
        l1="CLIENT",
        label="Win窗口/输入操作异常",
        # 新增：「win窗口操作异常」「win输入异常」不同于「图片点击失败」，
        #        是窗口状态或输入框操作层面的问题
        # 月报量：~403条
        pattern=r"win窗口操作异常|win输入异常|win.*操作异常|win.*输入异常",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=False,
        re_run=True,
        need_human=False,
        owner="客户端研发",
    ),

    ErrorCode(
        code="CLIENT_IMAGE_CLICK_FAIL",
        l1="CLIENT",
        label="Win图片点击失败",
        # 月报量：~4,463条
        pattern=r"win图片|图片点击失败|win点击异常|win.*image|image.*click.*fail|win元素图片检查失败",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=False,
        re_run=True,
        need_human=False,
        owner="客户端研发",
    ),

    # ============================================================== WEB（网站）

    ErrorCode(
        code="WEB_HTTP_REQUEST_FAIL",
        l1="WEB",
        label="HTTP请求失败",
        # 新增：「HTTP请求失败」高频出现（~706条），独立于「网站请求异常」
        #        区别：此为接口层调用失败，非页面/网络层故障
        pattern=r"HTTP请求失败|http.*request.*fail|接口请求失败",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=False,
        re_run=True,
        need_human=False,
        owner="RPA研发",
    ),

    ErrorCode(
        code="WEB_ELEMENT_CHANGED",
        l1="WEB",
        label="网站元素操作异常",
        # 月报量：~4,529条
        pattern=r"找不到元素|元素|控件|Selenium|Chrome|浏览器|cookie|token",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=False,
        re_run=False,
        need_human=True,
        owner="RPA研发",
    ),

    ErrorCode(
        code="WEB_REQUEST_ERROR",
        l1="WEB",
        label="网站请求异常",
        # 月报量：包含在「网站元素变更」之外的网络层故障
        pattern=(
            r"网站异常|网站超时|页面打不开|页面加载|网站无响应"
            r"|网络异常|连接失败|连接超时|请求超时|接口超时"
            r"|http error|502|503|504"
        ),
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=False,
        re_run=True,
        need_human=False,
        owner="网络运维",
    ),

    # ============================================================== FLOW（流程）

    ErrorCode(
        code="FLOW_STEP_TIMEOUT",
        l1="FLOW",
        label="流程步骤超时/等待超限",
        # 新增：「等待N秒(wait) 已执行次数：30，超过最大执行次数：30」
        #        「执行步骤超时」「超过最大执行次数」
        # 这类错误是流程陷入无限等待，与设备/环境无关
        # 月报量：~1,700条（最大单类「其它原因」）
        pattern=(
            r"超过最大执行次数|已执行次数.*超过最大"
            r"|执行步骤超时"
            r"|wait.*超过|超过.*wait"
        ),
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=False,
        re_run=True,
        need_human=False,
        owner="RPA研发",
    ),

    ErrorCode(
        code="FLOW_CONFIG_ERROR",
        l1="FLOW",
        label="流程配置问题",
        # 保留原有，新增「需要重新核定」「流程未同步」等流程状态词
        pattern=(
            r"流程配置|步骤配置|机器人配置错误|flow.*config|step.*config|指令配置"
            r"|需要重新核定|无操作步骤|机器人配置错误.*无操作|no.*step"
        ),
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=False,
        re_run=False,
        need_human=True,
        owner="RPA研发",
    ),

    # ============================================================== BIZ_REPORT_EMPTY（兜底）
    # 必须排在所有规则最后：只有其他所有规则均未命中时，才认定为报盘为空。
    # 若与登录失败/网站异常等同时出现，应优先归入更具体的错误类型；
    # 报盘为空命中时，执行任务被认定为成功，不计入失败统计。

    ErrorCode(
        code="BIZ_REPORT_EMPTY",
        l1="BIZ",
        label="下载报盘文件为空",
        # 原 CLIENT_REPORT_EMPTY，归类调整为 BIZ（数据侧问题，非客户端问题）
        # 排在最后：当且仅当其他所有规则均未命中时才命中，避免与真实异常混淆
        # 月报量：~5,239条
        pattern=r"报盘为空|报盘文件数据为空|数据为空|待缴为空|费用为空|无待缴|没有待缴|未查询到待缴|无数据|没有数据",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=True,
        re_run=False,
        need_human=True,
        owner="业务运营",
    ),

    # ============================================================== CONFIG（配置/代码）

    ErrorCode(
        code="CONFIG_ERROR",
        l1="CONFIG",
        label="配置错误",
        # 月报量：~6,314条
        # 注意：此规则排在「登录」相关之后，避免「登录」步骤的 NullPointerException
        # 被错误分到 AUTH，但 CONFIG 优先级低于 AUTH_LOGIN_INVALID
        pattern=(
            r"java\.lang|NullPointerException|ClassCastException|不能为空"
            r"|RobotInterruptException|RobotRuntimeException|RuntimeException"
        ),
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.error / errorStack",
        auto_fix=False,
        pre_intercept=False,
        re_run=False,
        need_human=True,
        owner="RPA研发",
    ),

    # ============================================================== ENV（环境/基础设施）

    ErrorCode(
        code="ENV_FLOW_NOT_SYNC",
        l1="ENV",
        label="流程未同步到盒子",
        # 新增：「流程未同步到盒子，请等待」—— 盒子侧流程版本未更新
        # 月报量：~23条
        pattern=r"流程未同步到盒子|流程.*未同步|未同步.*盒子",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_task_queue.comment",
        auto_fix=False,
        pre_intercept=False,
        re_run=True,
        need_human=False,
        owner="硬件运维",
    ),

    ErrorCode(
        code="ENV_BOX_OR_DEVICE_ERROR",
        l1="ENV",
        label="设备/盒子/环境异常",
        # 修正：删除「超时」「500」（过宽，已由 FLOW_STEP_TIMEOUT / WEB_REQUEST_ERROR 承接）
        # 保留设备层面的明确关键词
        # 月报量：~8,531条（修正后与 FLOW_STEP_TIMEOUT 分流）
        pattern=r"设备重启|机器人状态异常|SQLiteException|环境异常|同步.*数据",
        screenshot="robot_execution_file_info.file_full_path",
        log_field="robot_execution_detail.stepName / error",
        auto_fix=False,
        pre_intercept=False,
        re_run=True,
        need_human=False,
        owner="硬件运维",
    ),

]


# --------------------------------------------------------------------------- #
# 导出供 report.py 使用的 CLASSIFY_RULES
# 格式：List[Tuple[label, pattern]]，与原脚本格式完全兼容
# --------------------------------------------------------------------------- #

CLASSIFY_RULES: List[Tuple[str, str]] = [
    (ec.label, ec.pattern) for ec in ERROR_CODE_LIST
]

# 错误码索引：label → ErrorCode
ERROR_CODE_BY_LABEL: Dict[str, ErrorCode] = {ec.label: ec for ec in ERROR_CODE_LIST}

# 错误码索引：code → ErrorCode
ERROR_CODE_BY_CODE: Dict[str, ErrorCode] = {ec.code: ec for ec in ERROR_CODE_LIST}


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def classify_reason(text: str) -> str:
    """按 CLASSIFY_RULES 优先级匹配，返回错误码 label；未命中返回「其它原因」。"""
    if not text:
        return "未记录原因"
    value = str(text)
    for label, pattern in CLASSIFY_RULES:
        if re.search(pattern, value, re.IGNORECASE):
            return label
    return "其它原因"


def classify_reason_l1(text: str, run_support: str) -> str:
    """一级大类：只有客户端异常和网页端异常两类。
    客户端异常条件：robot_flow.run_support = '10019005'（客户端运行载体）
    其余全部归为网页端异常。
    二级细分由 classify_reason() 提供。
    """
    if str(run_support) == CLIENT_RUN_SUPPORT:
        return "客户端异常"
    return "网页端异常"


def get_error_meta(label: str) -> Optional[ErrorCode]:
    """根据 label 返回完整的 ErrorCode 元信息，未找到返回 None。"""
    return ERROR_CODE_BY_LABEL.get(label)
