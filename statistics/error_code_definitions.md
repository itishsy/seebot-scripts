# RPA 标准错误码与规则定义  v0.1

> 生成时间：2026-06-02　｜　数据来源：error_codes.py

---

## 一级分类说明

| 分类 | 含义 |
| --- | --- |
| **BIZ** | 业务数据/逻辑问题（账户不一致/数据为空/余额不足） |
| **AUTH** | 认证/登录/证书问题 |
| **CLIENT** | 客户端本地程序问题（Win窗口/图片点击/输入操作） |
| **WEB** | 网站/页面侧问题（元素变更/请求失败/业务窗口关闭） |
| **FLOW** | 流程编排问题（无操作步骤/步骤超时/循环等待超限） |
| **CONFIG** | 系统/代码配置错误（Java异常/RPA引擎报错） |
| **ENV** | 环境/基础设施问题（盒子/设备/网络/流程未同步） |

---

## 错误码完整定义表

> 规则按优先级排列（先匹配先命中），共 22 个错误码。

| 优先级 | 错误码标识 | 一级分类 | 中文名称 | 责任人 | 可自动修复 | 可前置拦截 | 可重跑 | 需人工接管 | 截图字段 | 日志字段 | 识别规则（正则） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `BIZ_NON_WORK_TIME_SMS` | BIZ | 非工作时间不发短信 | 业务运营 | 否 | 是 | 是 | 否 | file_full_path | robot_execution_detail.stepName / error | 非工作时间 \| 不发短信 \| 工作时间.*短信 \| 短信.*工作时间 |
| 2 | `BIZ_ACCOUNT_MISMATCH` | BIZ | 申报账户不一致 | 业务运营 | 否 | 是 | 否 | 是 | file_full_path | robot_execution_detail.stepName / error | 申报账户不一致 \| 账户不一致 \| 账户.*不一致 |
| 3 | `BIZ_REPORT_EMPTY` | BIZ | 下载报盘文件为空 | 业务运营 | 否 | 是 | 否 | 是 | file_full_path | robot_execution_detail.stepName / error | 报盘为空 \| 报盘文件数据为空 \| 数据为空 \| 待缴为空 \| 费用为空 \| 无待缴 \| 没有待缴 \| 未查询到待缴 \| 无数据 \| 没有数据 |
| 4 | `BIZ_DATA_INVALID` | BIZ | 业务数据异常 | 业务运营 | 否 | 是 | 否 | 是 | file_full_path | robot_execution_detail.stepName / error | 空费款所属期 \| 所属期不一致 \| 国籍.*不匹配 \| 证件类型不匹配 \| 未入账 \| 还未入账 \| 导入数据不能为空 \| 业务受理号为空 \| 日常申报获取数据异常 |
| 5 | `BIZ_PAY_FAIL` | BIZ | 业务缴费失败 | 业务运营 | 否 | 否 | 否 | 是 | file_full_path | robot_execution_detail.stepName / error | 汇缴提交失败 \| 缴费失败 \| 余额不足 \| 上传失败.*无法提交 \| 金额不一致.*退出 \| 未获取到缴费项 \| 获取凭据失败 \| 本次缴费.*还在处理中 \| 减员失败 |
| 6 | `BIZ_MANUAL_CANCEL` | BIZ | 手动撤销任务 | 业务运营 | 否 | 否 | 否 | 否 |  | robot_task_queue.comment | 手动撤销任务 \| 手动取消 |
| 7 | `BIZ_BUSINESS_WINDOW_CLOSED` | BIZ | 网站关闭业务窗口 | 业务运营 | 否 | 是 | 是 | 否 | file_full_path | robot_execution_detail.stepName / error | 非办理时间 \| 不在办理时间 \| 办理时间 \| 业务办理时间 \| 系统维护 \| 暂停办理 \| 退出申报 \| 暂未缴费 \| 业务办理失败 |
| 8 | `AUTH_UKEY_ERROR` | AUTH | UKey异常 | 硬件运维 | 否 | 是 | 否 | 是 | file_full_path | robot_execution_detail.stepName / error | UKey \| ukey \| U盾 \| usbkey \| UsbKey \| 激活USB \| 挂载.*Key \| 证书过期 \| CA证书 \| ukey认证 \| key失效 \| key未插 \| key未找到 |
| 9 | `AUTH_CERT_FAIL` | AUTH | 证书识别/读取失败 | 硬件运维 | 否 | 否 | 否 | 是 | file_full_path | robot_execution_detail.stepName / error | 证书识别失败 \| 读取证书失败 \| 证书.*失败 \| 失败.*证书 |
| 10 | `AUTH_LOGIN_INVALID` | AUTH | 登录失败/登录无效 | 账号管理 | 否 | 是 | 是 | 否 | file_full_path | robot_execution_detail.stepName / error | 登录失败 \| 登陆失败 \| 登录无效 \| 登录失效 \| 登陆失效 \| 会话失效 \| 未登录 \| 重新登录 \| 验证码错误 \| 账号密码错误 \| 超过最大重试登录次数 \| H5登录失败 \| H5登录异常 \| 纳税识别号.*密码错误 \| 登录.*超时 \| 超时.*登录 |
| 11 | `CLIENT_POPUP_NEW` | CLIENT | 客户端新增弹窗 | 客户端研发 | 否 | 否 | 是 | 是 | file_full_path | robot_execution_detail.stepName / error | 弹窗 \| 新增弹 \| pop.*up \| unexpected.*dialog \| unexpected.*window |
| 12 | `CLIENT_WIN_OP_ERROR` | CLIENT | Win窗口/输入操作异常 | 客户端研发 | 否 | 否 | 是 | 否 | file_full_path | robot_execution_detail.stepName / error | win窗口操作异常 \| win输入异常 \| win.*操作异常 \| win.*输入异常 |
| 13 | `CLIENT_IMAGE_CLICK_FAIL` | CLIENT | Win图片点击失败 | 客户端研发 | 否 | 否 | 是 | 否 | file_full_path | robot_execution_detail.stepName / error | win图片 \| 图片点击失败 \| win点击异常 \| win.*image \| image.*click.*fail \| win元素图片检查失败 |
| 14 | `WEB_HTTP_REQUEST_FAIL` | WEB | HTTP请求失败 | RPA研发 | 否 | 否 | 是 | 否 | file_full_path | robot_execution_detail.stepName / error | HTTP请求失败 \| http.*request.*fail \| 接口请求失败 |
| 15 | `WEB_ELEMENT_CHANGED` | WEB | 网站元素变更 | RPA研发 | 否 | 否 | 否 | 是 | file_full_path | robot_execution_detail.stepName / error | 找不到元素 \| 元素 \| 控件 \| Selenium \| Chrome \| 浏览器 \| cookie \| token |
| 16 | `WEB_REQUEST_ERROR` | WEB | 网站请求异常 | 网络运维 | 否 | 否 | 是 | 否 | file_full_path | robot_execution_detail.stepName / error | 网站异常 \| 网站超时 \| 页面打不开 \| 页面加载 \| 网站无响应 \| 网络异常 \| 连接失败 \| 连接超时 \| 请求超时 \| 接口超时 \| http error \| 502 \| 503 \| 504 |
| 17 | `FLOW_STEP_TIMEOUT` | FLOW | 流程步骤超时/等待超限 | RPA研发 | 否 | 否 | 是 | 否 | file_full_path | robot_execution_detail.stepName / error | 超过最大执行次数 \| 已执行次数.*超过最大 \| 执行步骤超时 \| wait.*超过 \| 超过.*wait |
| 18 | `FLOW_NO_STEP` | FLOW | 流程无操作步骤 | RPA研发 | 否 | 是 | 否 | 是 | file_full_path | robot_execution_detail.stepName / error | 无操作步骤 \| 机器人配置错误.*无操作 \| no.*step |
| 19 | `FLOW_CONFIG_ERROR` | FLOW | 流程配置问题 | RPA研发 | 否 | 否 | 否 | 是 | file_full_path | robot_execution_detail.stepName / error | 流程配置 \| 步骤配置 \| flow.*config \| step.*config \| 指令配置 \| 需要重新核定 |
| 20 | `CONFIG_ERROR` | CONFIG | 配置错误 | RPA研发 | 否 | 否 | 否 | 是 | file_full_path | robot_execution_detail.error / errorStack | java\.lang \| NullPointerException \| ClassCastException \| RobotInterruptException \| RobotRuntimeException \| RuntimeException |
| 21 | `ENV_FLOW_NOT_SYNC` | ENV | 流程未同步到盒子 | 硬件运维 | 否 | 否 | 是 | 否 | file_full_path | robot_task_queue.comment | 流程未同步到盒子 \| 流程.*未同步 \| 未同步.*盒子 |
| 22 | `ENV_BOX_OR_DEVICE_ERROR` | ENV | 设备/盒子/环境异常 | 硬件运维 | 否 | 否 | 是 | 否 | file_full_path | robot_execution_detail.stepName / error | 设备重启 \| 机器人状态异常 \| SQLiteException \| 环境异常 |

---

## 一级大类分类规则（classify_reason_l1）



---

## 空跑认定规则

> 仅以下错误码的失败记录认定为空跑（无业务产出的无效执行）：

| 错误码 | 中文名称 | 认定依据 |
| --- | --- | --- |
| `BIZ_REPORT_EMPTY` | 下载报盘文件为空 | 数据侧无可处理数据，执行无效 |
| `BIZ_NON_WORK_TIME_SMS` | 非工作时间不发短信 | 时间窗口外执行，业务侧拒绝 |