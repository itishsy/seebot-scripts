# RPA Worker Agent 

版本：V1.0  
项目名称：seebot RPA Worker Agent 改造  
适用范围：Windows RPA 执行器、rpa-client.exe、rpa-runner.jar、Windows VM Resource Manager、vSphere / Horizon VM 执行池  
改造原则：复用现有能力，最小化改造，逐步演进为标准 Worker Agent

---

## 一、项目背景

当前 seebot RPA 执行体系中，Windows 执行器主要由以下两个组件组成：

```text
rpa-client.exe
    ├── 负责启动 rpa-runner.jar
    ├── 负责停止 rpa-runner.jar
    ├── 负责上报执行器心跳
    └── 当前形态为 C# WinForm 程序

rpa-runner.jar
    ├── 负责拉取任务
    ├── 负责环境自检
    ├── 负责通过 Selenium 操作浏览器
    ├── 负责调用 Python 第三方库操作桌面应用
    └── 负责执行具体 RPA 业务流程
```

该模式已经具备基础执行能力，但仍然接近“固定 Windows 工作站执行模式”。随着执行任务量、城市数量、客户账户数量增加，该模式在资源管理、环境隔离、异常恢复、执行器统一编排方面存在明显不足。

主要问题包括：

1. 执行器资源由固定机器承载，无法按任务高峰动态扩容；
2. 执行器状态与任务资源占用关系不够清晰；
3. rpa-runner.jar 自主拉取任务，Resource Manager 无法精确掌控任务租约；
4. Windows 环境长期运行后容易出现浏览器、驱动、客户端、UKey、桌面会话等残留问题；
5. WinForm 形态依赖用户桌面窗口，不适合作为长期生产常驻 Agent；
6. 任务超时、runner 异常退出、Java/Python/浏览器残留进程缺少统一熔断和清理机制；
7. 后续接入 Windows VM Resource Manager 后，需要标准 Worker Agent 与 VM 生命周期打通。

因此，需要将现有 `rpa-client.exe` 改造为标准化的 **RPA Worker Agent**，作为 Windows 执行器与资源管理平台之间的桥接组件。

---


## 二、改造目标

### 2.1 总体目标

将现有 `rpa-client.exe` 从“runner 启停工具 + 心跳上报工具”升级为标准 **RPA Worker Agent**。

改造后，Agent 负责本机执行资源管理，runner 继续负责具体 RPA 业务执行。

目标结构如下：

```text
Windows VM / Windows 执行器
    |
    |-- RPA Worker Agent
    |       ├── Worker 注册
    |       ├── 心跳上报
    |       ├── 资源租约绑定
    |       ├── runner 生命周期管理
    |       ├── 基础环境自检
    |       ├── UKey/CA 前后置联动
    |       ├── 进程监管
    |       ├── 超时熔断
    |       ├── 环境清理
    |       ├── 日志归档
    |       └── 资源释放通知
    |
    |-- rpa-runner.jar
    |       ├── Selenium 操作浏览器
    |       ├── 调用 Python 操作桌面应用
    |       ├── 执行业务流程
    |       ├── 业务环境自检
    |       └── 业务结果回传
```

---

### 2.2 阶段目标

#### 第一阶段：现有执行器接入 Resource Manager

保留当前固定 Windows 执行器，不立即引入动态 VM 克隆。

目标：

1. rpa-client.exe 支持注册到 Resource Manager；
2. rpa-client.exe 支持上报 Worker 状态；
3. rpa-client.exe 支持上报 runner 状态；
4. Resource Manager 能看到所有执行器在线、空闲、运行、异常状态；
5. 调度中心可通过 Resource Manager 分配执行器，而不是直接绑定固定 machine_code。

---

#### 第二阶段：rpa-client.exe 增强为 Agent

目标：

1. 支持任务租约 lease；
2. 支持 runner 启停标准化；
3. 支持 runner 超时熔断；
4. 支持 Java、Python、浏览器、客户端残留进程清理；
5. 支持基础环境自检；
6. 支持异常隔离；
7. 支持执行后释放资源。

---

#### 第三阶段：WinForm 拆分为 Service + UI

目标：

1. 新增 `Seebot.Agent.Service.exe` 作为生产常驻服务；
2. 保留 `rpa-client.exe` WinForm 作为本机运维控制台；
3. Agent Service 负责生产执行；
4. WinForm 只负责查看状态、手工控制、调试运维；
5. 避免因 WinForm 窗口关闭、RDP 断开、用户注销导致 Agent 中断。

---

#### 第四阶段：接入 Windows VM Resource Manager

目标：

1. Agent 作为城市黄金镜像标准组件；
2. VM 启动后 Agent 自动注册；
3. Resource Manager 可分配 VM Worker；
4. Agent 执行任务前完成自检；
5. Agent 执行任务后通知 Resource Manager 回滚、销毁或释放 VM；
6. 最终形成非持久化 Windows VM 执行池。

---

## 三、总体架构

### 3.1 当前架构

```text
调度中心
    |
    v
固定 Windows 执行器
    |
    |-- rpa-client.exe
    |       ├── 启动 runner
    |       ├── 停止 runner
    |       └── 心跳上报
    |
    |-- rpa-runner.jar
            ├── 拉取任务
            ├── 环境自检
            ├── Selenium 操作浏览器
            ├── Python 操作桌面应用
            └── 回传执行结果
```

---

### 3.2 改造后目标架构

```text
seebot 业务系统 / 调度中心
        |
        v
Windows VM Resource Manager
        |
        |-- Worker 注册中心
        |-- Worker 状态管理
        |-- 资源租约管理
        |-- 环境画像管理
        |-- UKey/账号锁联动
        |-- Worker 异常隔离
        |
        v
Windows VM / Windows 执行器
        |
        |-- RPA Worker Agent
        |       ├── 注册
        |       ├── 心跳
        |       ├── 租约绑定
        |       ├── 启动 runner
        |       ├── 监管 runner
        |       ├── 清理环境
        |       └── 释放资源
        |
        |-- rpa-runner.jar
                ├── 执行指定任务
                ├── Selenium
                ├── Python 桌面自动化
                └── 回传业务结果
```

---

## 四、组件职责边界

### 4.1 Windows VM Resource Manager

负责执行资源编排，不参与具体 RPA 业务操作。

职责：

1. 管理 Worker 注册；
2. 管理 Worker 在线状态；
3. 管理 Worker 空闲、运行、异常、隔离状态；
4. 根据任务属性匹配环境画像；
5. 分配 Worker；
6. 创建资源租约 lease；
7. 管理 UKey 锁、账号锁、Worker 锁；
8. 记录 Worker 执行历史；
9. 控制 VM 回滚、销毁或释放；
10. 对异常 Worker 执行隔离。

---

### 4.2 RPA Worker Agent

负责本机执行资源管理，不直接写业务流程。

职责：

1. 启动后注册到 Resource Manager；
2. 周期性上报 Worker 心跳；
3. 上报本机环境信息；
4. 上报 runner 状态；
5. 绑定 Resource Manager 下发的 lease；
6. 启动 rpa-runner.jar；
7. 停止 rpa-runner.jar；
8. 监控 runner 进程；
9. 执行基础环境自检；
10. 执行超时熔断；
11. 清理残留进程；
12. 清理工作目录；
13. 上传 Agent 日志；
14. 通知 Resource Manager 释放资源；
15. 必要时将本机标记为异常或隔离。

---

### 4.3 rpa-runner.jar

负责具体 RPA 业务执行。

职责：

1. 执行任务流程；
2. Selenium 操作浏览器；
3. 调用 Python 操作桌面应用；
4. 执行业务环境自检；
5. 上传任务截图、附件、日志；
6. 回传业务状态；
7. 返回标准退出码。

长期目标中，runner 不再自由抢任务，而是执行 Agent 指定的任务。

---

### 4.4 Python 桌面自动化脚本

职责：

1. 操作 Windows 桌面应用；
2. 操作社保客户端、公积金客户端等本地程序；
3. 图片识别、窗口点击、输入模拟；
4. 输出截图、错误信息和执行结果；
5. 返回明确错误码。

---

### 4.5 rpa-client.exe WinForm 运维界面

职责：

1. 查看本机 Agent 状态；
2. 查看 runner 状态；
3. 查看当前任务；
4. 查看最近心跳；
5. 查看最近自检结果；
6. 手工启动 runner；
7. 手工停止 runner；
8. 手工执行环境自检；
9. 手工清理残留进程；
10. 手工上传日志。

生产执行不应依赖 WinForm 窗口是否打开。

---

## 五、进程形态设计

### 5.1 短期形态

短期可以继续使用现有 `rpa-client.exe`，在其内部增强 Agent 能力。

```text
rpa-client.exe
    ├── WinForm UI
    ├── Worker 注册
    ├── 心跳上报
    ├── runner 启停
    ├── runner 进程监管
    ├── 基础环境自检
    └── 环境清理
```

适用阶段：

1. 快速验证 Resource Manager 接入；
2. 快速纳管现有 Windows 执行器；
3. 减少第一阶段改造风险。

缺点：

1. 依赖用户桌面会话；
2. 不适合作为长期生产常驻进程；
3. 窗口关闭后可能影响执行；
4. RDP 断开、用户注销、系统重启后的稳定性不足。

---

### 5.2 中期形态

将核心逻辑抽成类库。

```text
Seebot.Agent.Core.dll
    ├── RegisterService
    ├── HeartbeatService
    ├── LeaseService
    ├── RunnerProcessManager
    ├── HealthCheckService
    ├── CleanupService
    ├── LogUploadService
    └── UKeyMountAdapter

rpa-client.exe
    └── 引用 Seebot.Agent.Core.dll
        作为 WinForm 运维界面

Seebot.Agent.Service.exe
    └── 引用 Seebot.Agent.Core.dll
        作为生产常驻服务
```

---

### 5.3 长期形态

```text
Seebot.Agent.Service.exe
    ├── Windows Service
    ├── 注册 / 心跳 / 租约
    ├── runner 管理
    ├── 状态上报
    ├── 环境清理
    └── 资源释放

Seebot.Agent.DesktopLauncher.exe
    ├── 在交互式桌面 Session 启动 runner
    └── 避免 Session 0 桌面自动化问题

rpa-client.exe
    └── WinForm 运维面板

rpa-runner.jar
    └── RPA 业务执行
```

---

## 六、Agent 核心能力设计

### 6.1 Worker 注册

Agent 启动后主动注册到 Resource Manager。

注册内容：

```json
{
  "workerId": "WORKER-SUZHOU-SOCIAL-CLIENT-001",
  "workerName": "苏州社保客户端执行器01",
  "hostName": "RPA-SUZHOU-001",
  "ip": "10.10.20.15",
  "profileCode": "rpa-env-suzhou-social-client-202606",
  "agentVersion": "2.0.0",
  "runnerVersion": "1.8.5",
  "javaVersion": "1.8.0_351",
  "pythonVersion": "3.9.13",
  "osVersion": "Windows 10 LTSC",
  "screenResolution": "1920x1080",
  "loginSessionReady": true,
  "status": "IDLE"
}
```

---

### 6.2 Worker 心跳

Agent 周期性上报心跳，建议间隔 10～30 秒。

心跳内容：

```json
{
  "workerId": "WORKER-SUZHOU-SOCIAL-CLIENT-001",
  "profileCode": "rpa-env-suzhou-social-client-202606",
  "status": "RUNNING",
  "runnerStatus": "RUNNING",
  "currentTaskId": 123456,
  "currentLeaseId": "LEASE-20260605-0001",
  "cpuUsage": 35.2,
  "memoryUsage": 61.5,
  "diskFreeGb": 42,
  "lastHealthCheckStatus": "PASS",
  "lastRunnerStartTime": "2026-06-05 10:00:00",
  "timestamp": "2026-06-05 10:05:00"
}
```

Worker 状态枚举：

```text
STARTING       启动中
REGISTERED     已注册
IDLE           空闲
LEASED         已分配租约
RUNNING        执行中
CLEANING       清理中
ERROR          异常
QUARANTINED    已隔离
OFFLINE        离线
```

runner 状态枚举：

```text
STOPPED        未运行
STARTING       启动中
RUNNING        执行中
STOPPING       停止中
EXITED         已退出
TIMEOUT        已超时
ERROR          异常
```

---

### 6.3 资源租约 lease

Agent 必须支持 lease 绑定。

lease 表示一次任务对一个 Worker 的占用关系。

租约字段建议：

```json
{
  "leaseId": "LEASE-20260605-0001",
  "taskId": 123456,
  "executionCode": "EXE202606050001",
  "workerId": "WORKER-SUZHOU-SOCIAL-CLIENT-001",
  "profileCode": "rpa-env-suzhou-social-client-202606",
  "accountId": 10086,
  "ukeyId": 20001,
  "timeoutSeconds": 3600,
  "leaseToken": "secure-token",
  "status": "ALLOCATED"
}
```

Agent 执行任务时，必须携带：

```text
taskId
executionCode
leaseId
workerId
profileCode
leaseToken
```

---

### 6.4 runner 启动标准

Agent 启动 runner 时使用标准命令。

```bat
java -jar C:\seebot-runtime\runner\rpa-runner.jar ^
  --taskId=123456 ^
  --executionCode=EXE202606050001 ^
  --leaseId=LEASE-20260605-0001 ^
  --workerId=WORKER-SUZHOU-SOCIAL-CLIENT-001 ^
  --profileCode=rpa-env-suzhou-social-client-202606 ^
  --payloadFile=C:\seebot-agent\workspace\123456\input.json ^
  --workDir=C:\seebot-agent\workspace\123456 ^
  --callbackUrl=http://seebot/api/rpa/callback
```

---

### 6.5 runner 退出码标准

runner 应返回标准退出码，Agent 根据退出码判断结果。

```text
0    成功
10   业务失败
20   登录失败
30   UKey/证书失败
40   Selenium/页面元素失败
50   Python/桌面操作失败
60   文件上传失败
70   超时
80   环境自检失败
90   未知异常
```

---

### 6.6 runner 进程监管

Agent 必须管理 runner 进程生命周期。

包括：

1. 启动 runner；
2. 记录启动时间；
3. 记录进程 ID；
4. 监听进程退出；
5. 识别退出码；
6. 超时强制停止；
7. runner 异常退出后上报 Resource Manager；
8. 清理 runner 产生的子进程。

需要重点清理的进程：

```text
java.exe
python.exe
pythonw.exe
chrome.exe
chromedriver.exe
msedge.exe
msedgedriver.exe
社保费客户端相关进程
公积金客户端相关进程
OCR 相关进程
```

---

### 6.7 超时熔断

Agent 必须按任务类型设置最大执行时间。

建议默认策略：

```text
登录任务：10～15 分钟
获取待缴：30～60 分钟
核定：30～60 分钟
缴费：30～60 分钟
凭证获取：15～30 分钟
在册名单：60～120 分钟
基数申报：60～120 分钟
```

超时处理流程：

```text
1. Agent 发现 runner 超时
2. 触发最后一次截图
3. 保存 runner 日志
4. kill runner 进程
5. kill Python / 浏览器 / 客户端残留进程
6. 回传 TIMEOUT 状态
7. 释放 lease
8. Worker 进入 CLEANING
9. 清理完成后回到 IDLE 或进入 QUARANTINED
```

---

### 6.8 环境清理

每次任务结束后必须清理环境。

清理内容：

```text
1. 清理 workspace
2. 清理下载目录
3. 清理临时文件
4. 清理浏览器进程
5. 清理 WebDriver 进程
6. 清理 Python 子进程
7. 清理 Java 残留进程
8. 清理社保/公积金客户端残留进程
9. 清理截图临时目录
10. 清理异常锁文件
```

清理完成后 Agent 上报：

```json
{
  "workerId": "WORKER-SUZHOU-SOCIAL-CLIENT-001",
  "leaseId": "LEASE-20260605-0001",
  "cleanupStatus": "SUCCESS",
  "status": "IDLE"
}
```

---

## 七、环境自检分层设计

### 7.1 Agent 基础自检

Agent 负责机器级自检。

检查项：

```text
1. Java 是否存在
2. runner.jar 是否存在
3. Python 环境是否存在
4. Python 第三方库是否完整
5. Chrome / Edge 是否存在
6. ChromeDriver / EdgeDriver 是否存在
7. 浏览器与 WebDriver 版本是否匹配
8. 工作目录是否可写
9. 日志目录是否可写
10. 上传接口是否可访问
11. 当前是否有交互式桌面 Session
12. 屏幕分辨率是否正确
13. 上次残留进程是否清理
14. 磁盘空间是否足够
15. Agent 配置是否完整
```

---

### 7.2 runner 业务自检

runner 继续负责业务级自检。

检查项：

```text
1. 目标网站是否可访问
2. 账号配置是否完整
3. UKey 是否挂载成功
4. 证书是否可枚举
5. 社保客户端是否可打开
6. 公积金客户端是否可打开
7. 城市业务入口是否可进入
8. Selenium 是否可创建浏览器会话
9. Python 桌面自动化库是否可调用
10. 当前任务输入参数是否完整
```

---

### 7.3 自检失败处理

```text
Agent 基础自检失败：
    不启动 runner
    上报 ENV_CHECK_FAILED
    Worker 进入 ERROR 或 QUARANTINED

runner 业务自检失败：
    runner 返回 80 或对应业务错误码
    Agent 收集日志
    释放 lease
    Worker 清理后回到 IDLE 或 QUARANTINED
```

---

## 八、任务拉取模式改造

### 8.1 当前模式

```text
rpa-runner.jar 自主拉取任务
```

问题：

```text
1. Resource Manager 无法准确控制 Worker 是否空闲
2. 无法建立强租约关系
3. UKey 锁和任务执行时机可能不一致
4. VM 回滚前无法确认 runner 是否仍在执行
5. 难以实现统一超时熔断
```

---

### 8.2 目标模式

```text
Resource Manager 分配任务 lease
    ↓
Agent 获取任务
    ↓
Agent 启动 runner 执行指定任务
    ↓
runner 执行业务
    ↓
Agent 监管 runner
    ↓
Agent 释放 lease
```

目标模式下，runner 不再自由抢任务，而是执行 Agent 指定的 taskId。

---

### 8.3 兼容过渡模式

为降低改造风险，可分两步。

#### 第一阶段：runner 继续拉任务

```text
Agent 负责：
1. 注册
2. 心跳
3. 启停 runner
4. runner 进程监管
5. 超时熔断
6. 清理环境

runner 继续：
1. 拉取任务
2. 业务自检
3. 执行业务
4. 回传结果
```

#### 第二阶段：Agent 接管任务拉取

```text
Agent 负责：
1. 获取 lease
2. 拉取任务 payload
3. 启动 runner 执行指定任务
4. 监管 runner
5. 释放 lease

runner 负责：
1. 执行指定任务
2. 输出结果
3. 返回退出码
```

---

## 九、接口设计

### 9.1 Worker 注册接口

```http
POST /api/rpa/worker/register
```

请求：

```json
{
  "workerId": "WORKER-SUZHOU-SOCIAL-CLIENT-001",
  "workerName": "苏州社保客户端执行器01",
  "hostName": "RPA-SUZHOU-001",
  "ip": "10.10.20.15",
  "profileCode": "rpa-env-suzhou-social-client-202606",
  "agentVersion": "2.0.0",
  "runnerVersion": "1.8.5",
  "javaVersion": "1.8.0_351",
  "pythonVersion": "3.9.13",
  "osVersion": "Windows 10 LTSC",
  "screenResolution": "1920x1080",
  "loginSessionReady": true
}
```

响应：

```json
{
  "success": true,
  "workerId": "WORKER-SUZHOU-SOCIAL-CLIENT-001",
  "status": "REGISTERED",
  "serverTime": "2026-06-05 10:00:00"
}
```

---

### 9.2 Worker 心跳接口

```http
POST /api/rpa/worker/heartbeat
```

请求：

```json
{
  "workerId": "WORKER-SUZHOU-SOCIAL-CLIENT-001",
  "status": "RUNNING",
  "runnerStatus": "RUNNING",
  "currentTaskId": 123456,
  "currentLeaseId": "LEASE-20260605-0001",
  "cpuUsage": 35.2,
  "memoryUsage": 61.5,
  "diskFreeGb": 42,
  "lastHealthCheckStatus": "PASS",
  "timestamp": "2026-06-05 10:05:00"
}
```

---

### 9.3 Agent 拉取任务接口

```http
GET /api/rpa/worker/task/poll?workerId=WORKER-SUZHOU-SOCIAL-CLIENT-001
```

响应：

```json
{
  "hasTask": true,
  "lease": {
    "leaseId": "LEASE-20260605-0001",
    "leaseToken": "secure-token",
    "timeoutSeconds": 3600
  },
  "task": {
    "taskId": 123456,
    "executionCode": "EXE202606050001",
    "taskType": "PAY_FEE_GET",
    "payload": {
      "cityCode": "suzhou",
      "businessType": "SOCIAL",
      "declareSystem": "SOCIAL_CLIENT",
      "accountId": 10086,
      "operationMonth": "2026-06"
    }
  }
}
```

---

### 9.4 runner 状态上报接口

```http
POST /api/rpa/worker/runner/status
```

请求：

```json
{
  "workerId": "WORKER-SUZHOU-SOCIAL-CLIENT-001",
  "leaseId": "LEASE-20260605-0001",
  "taskId": 123456,
  "runnerStatus": "RUNNING",
  "processId": 5688,
  "startTime": "2026-06-05 10:01:00"
}
```

---

### 9.5 资源释放接口

```http
POST /api/rpa/resource/release
```

请求：

```json
{
  "workerId": "WORKER-SUZHOU-SOCIAL-CLIENT-001",
  "leaseId": "LEASE-20260605-0001",
  "taskId": 123456,
  "result": "SUCCESS",
  "runnerExitCode": 0,
  "cleanupStatus": "SUCCESS",
  "needQuarantine": false
}
```

---

## 十、目录规范

建议统一 Windows 执行器目录。

```text
C:\seebot-agent
    ├── config
    │   └── agent.json
    ├── logs
    │   ├── agent
    │   └── runner
    ├── workspace
    │   └── {taskId}
    │       ├── input.json
    │       ├── output.json
    │       ├── screenshots
    │       ├── downloads
    │       └── logs
    ├── temp
    └── tools

C:\seebot-runtime
    ├── java
    ├── python-venv
    ├── runner
    │   └── rpa-runner.jar
    ├── scripts
    ├── drivers
    └── clients
```

---

## 十一、本地配置文件

`C:\seebot-agent\config\agent.json`

```json
{
  "workerId": "WORKER-SUZHOU-SOCIAL-CLIENT-001",
  "workerName": "苏州社保客户端执行器01",
  "resourceManagerUrl": "http://seebot-resource-manager",
  "profileCode": "rpa-env-suzhou-social-client-202606",

  "javaPath": "C:\\seebot-runtime\\java\\bin\\java.exe",
  "runnerJarPath": "C:\\seebot-runtime\\runner\\rpa-runner.jar",
  "pythonPath": "C:\\seebot-runtime\\python-venv\\Scripts\\python.exe",

  "workspaceDir": "C:\\seebot-agent\\workspace",
  "agentLogDir": "C:\\seebot-agent\\logs\\agent",
  "runnerLogDir": "C:\\seebot-agent\\logs\\runner",

  "heartbeatIntervalSeconds": 15,
  "maxRunnerAliveMinutes": 60,
  "enableAutoRestart": true,
  "enableCleanupAfterTask": true,
  "enableKillProcessOnTimeout": true
}
```

---

## 十二、数据库表建议

### 12.1 Worker 实例表

```sql
CREATE TABLE rpa_worker_instance (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    worker_id VARCHAR(100) NOT NULL,
    worker_name VARCHAR(200),
    profile_code VARCHAR(100) NOT NULL,

    host_name VARCHAR(200),
    ip VARCHAR(100),
    os_version VARCHAR(200),
    screen_resolution VARCHAR(50),

    agent_version VARCHAR(100),
    runner_version VARCHAR(100),
    java_version VARCHAR(100),
    python_version VARCHAR(100),

    status VARCHAR(50) NOT NULL,
    runner_status VARCHAR(50),
    current_task_id BIGINT,
    current_lease_id VARCHAR(100),

    last_heartbeat_time DATETIME,
    last_health_check_time DATETIME,
    last_health_check_status VARCHAR(50),

    consecutive_fail_count INT DEFAULT 0,
    total_run_count INT DEFAULT 0,

    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_worker_id (worker_id),
    KEY idx_profile_status (profile_code, status),
    KEY idx_heartbeat (last_heartbeat_time)
);
```

---

### 12.2 Worker 租约表

```sql
CREATE TABLE rpa_worker_lease (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    lease_id VARCHAR(100) NOT NULL,
    worker_id VARCHAR(100) NOT NULL,
    task_id BIGINT NOT NULL,
    execution_code VARCHAR(100),

    profile_code VARCHAR(100),
    account_id BIGINT,
    ukey_id BIGINT,

    lease_token VARCHAR(200),
    status VARCHAR(50) NOT NULL,

    request_time DATETIME,
    allocated_time DATETIME,
    start_time DATETIME,
    finish_time DATETIME,
    expire_time DATETIME,

    runner_exit_code INT,
    error_code VARCHAR(100),
    error_message VARCHAR(1000),

    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_lease_id (lease_id),
    KEY idx_worker (worker_id),
    KEY idx_task (task_id),
    KEY idx_status_expire (status, expire_time)
);
```

---

### 12.3 Worker 自检记录表

```sql
CREATE TABLE rpa_worker_health_check (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    worker_id VARCHAR(100) NOT NULL,
    lease_id VARCHAR(100),
    task_id BIGINT,

    check_item VARCHAR(100) NOT NULL,
    check_status VARCHAR(30) NOT NULL,
    check_message VARCHAR(1000),
    detail_json TEXT,

    check_time DATETIME DEFAULT CURRENT_TIMESTAMP,

    KEY idx_worker_time (worker_id, check_time),
    KEY idx_lease (lease_id),
    KEY idx_status (check_status)
);
```

---

## 十三、实施计划

### 阶段一：Agent 协议接入

周期建议：1～2 周

工作内容：

1. rpa-client.exe 增加 Worker 注册；
2. rpa-client.exe 增加 Worker 心跳；
3. 心跳中增加 runnerStatus；
4. Resource Manager 增加 Worker 注册接口；
5. Resource Manager 增加 Worker 心跳接口；
6. 建立 rpa_worker_instance 表；
7. 实现 Worker 在线、离线、空闲、运行状态展示。

交付物：

1. Worker 注册接口；
2. Worker 心跳接口；
3. rpa-client.exe Agent 协议版本；
4. Worker 状态管理页面或查询接口。

---

### 阶段二：runner 生命周期标准化

周期建议：1～2 周

工作内容：

1. rpa-client.exe 标准化启动 runner；
2. runner 支持命令行参数；
3. runner 支持标准 workDir；
4. runner 支持标准日志目录；
5. runner 支持标准退出码；
6. rpa-client.exe 支持 runner 退出码识别；
7. rpa-client.exe 支持 runner 超时 kill；
8. rpa-client.exe 支持残留进程清理。

交付物：

1. runner 标准启动协议；
2. runner 标准退出码；
3. runner 进程监管能力；
4. 环境清理脚本。

---

### 阶段三：租约 lease 接入

周期建议：2 周

工作内容：

1. Resource Manager 增加 lease 创建；
2. Agent 支持 lease 绑定；
3. Agent 启动 runner 时传入 leaseId；
4. runner 回传结果时携带 leaseId；
5. Agent 执行结束释放 lease；
6. Resource Manager 记录任务与 Worker 占用关系；
7. 异常情况下支持 lease 超时释放。

交付物：

1. rpa_worker_lease 表；
2. lease 接口；
3. Agent lease 绑定能力；
4. 资源释放接口；
5. lease 超时补偿机制。

---

### 阶段四：WinForm 拆分为 Service + UI

周期建议：2～3 周

工作内容：

1. 抽取 Seebot.Agent.Core.dll；
2. 新增 Seebot.Agent.Service.exe；
3. 将注册、心跳、runner 管理迁移到 Service；
4. rpa-client.exe 改为运维 UI；
5. Service 支持开机自启；
6. Service 支持崩溃自动恢复；
7. UI 通过本地 IPC 或 HTTP 调用 Service 状态。

交付物：

1. Agent Core 类库；
2. Windows Service；
3. WinForm 运维界面；
4. Service 安装脚本；
5. Service 升级脚本。

---

### 阶段五：接入 Windows VM 执行池

周期建议：3～4 周

工作内容：

1. Agent 写入城市黄金镜像；
2. VM 启动后自动注册；
3. Resource Manager 分配 VM Worker；
4. Agent 执行任务前自检；
5. Agent 执行任务后清理；
6. Resource Manager 根据结果决定 VM 回滚、销毁或回到热池；
7. 完成 1 个城市试点。

交付物：

1. VM Worker 自动注册；
2. VM Worker 生命周期管理；
3. 城市镜像 Agent 标准组件；
4. 试点城市执行报告。

---

## 十四、验收标准

### 14.1 功能验收

1. Agent 可自动注册到 Resource Manager；
2. Resource Manager 可查看 Worker 在线状态；
3. Agent 可周期性上报心跳；
4. Agent 可上报 runner 状态；
5. Agent 可启动 runner；
6. Agent 可停止 runner；
7. Agent 可识别 runner 异常退出；
8. Agent 可执行 runner 超时熔断；
9. Agent 可清理残留进程；
10. Agent 可释放 lease；
11. Agent 可将异常 Worker 标记为隔离；
12. WinForm UI 可查看 Agent 状态。

---

### 14.2 稳定性验收

1. Agent 连续运行 7 天无异常退出；
2. runner 异常退出后 Agent 可自动识别；
3. 任务超时后 Agent 可强制终止 runner；
4. Java/Python/浏览器残留进程可被清理；
5. RDP 断开后 Agent Service 不受影响；
6. Windows 重启后 Agent Service 自动恢复；
7. Resource Manager 能准确识别 Worker 在线、离线、运行、异常状态。

---

### 14.3 业务兼容验收

1. 原有 Selenium 浏览器操作不受影响；
2. 原有 Python 桌面应用操作不受影响；
3. 原有业务任务可继续执行；
4. 原有日志、截图、附件回传链路不受影响；
5. 原有 UKey/CA 挂载能力可继续复用；
6. 原有任务状态流转不受影响。

---

## 十五、风险与应对

| 风险                    | 影响                      | 应对                                        |
| --------------------- | ----------------------- | ----------------------------------------- |
| WinForm 继续作为生产 Agent  | 稳定性不足                   | 中期拆成 Windows Service                      |
| runner 自主拉取任务         | Resource Manager 难以管控资源 | 过渡期兼容，后续改为 Agent 指定任务                     |
| Service 运行在 Session 0 | 桌面自动化失败                 | 增加 DesktopLauncher，在交互式 Session 启动 runner |
| runner 超时无法退出         | 占用机器资源                  | Agent 增加超时熔断和进程树 kill                     |
| Python/浏览器残留          | 后续任务不稳定                 | 执行后统一清理                                   |
| UKey 挂载成功但证书不可读       | 任务失败                    | runner 业务自检增加证书枚举                         |
| Agent 与 runner 状态不一致  | Resource Manager 判断错误   | Agent 必须监管 runner 进程并上报真实状态               |
| 直接全量改造风险大             | 影响生产任务                  | 分阶段灰度，先纳管，再租约，再 Service 化                 |
| VM 回滚时 runner 未退出     | 任务数据不完整                 | 回滚前必须由 Agent 释放 lease 并完成清理               |

---

## 十六、推荐落地路线

推荐采用以下顺序：

```text
第一步：rpa-client.exe 增加 Worker 注册和心跳协议
第二步：Resource Manager 纳管现有固定 Windows 执行器
第三步：rpa-client.exe 增加 runner 进程监管、超时熔断、环境清理
第四步：引入 lease，实现任务与 Worker 的资源占用绑定
第五步：rpa-runner.jar 从自主拉任务逐步改为执行指定任务
第六步：抽取 Agent Core，新增 Windows Service
第七步：rpa-client.exe 保留为运维 UI
第八步：Agent 写入城市黄金镜像
第九步：接入 Windows VM 执行池
第十步：支持 VM 回滚、销毁、热池/冷池管理
```

---

## 十七、结论

现有 `rpa-client.exe` 可以改造成 RPA Worker Agent，而且是当前最合适的改造基础。

原因：

```text
1. 已经具备 runner 启停能力；
2. 已经具备心跳上报能力；
3. 已经与 rpa-runner.jar 形成现有执行链路；
4. 改造成本低；
5. 可最大程度复用现有 Java + Selenium + Python 自动化能力；
6. 适合逐步接入 Windows VM Resource Manager。
```

但不建议长期继续以 WinForm 窗口程序作为生产 Agent 主体。

最终建议形态：

```text
Seebot.Agent.Service.exe
    作为生产常驻 Worker Agent

Seebot.Agent.DesktopLauncher.exe
    负责在交互式桌面启动 runner

rpa-client.exe
    保留为本机运维控制台

rpa-runner.jar
    保留为业务执行器
```

该路线能在不重写 RPA 主流程的前提下，完成执行器标准化、资源租约化、环境可控化、异常可隔离化，为后续 Windows 非持久化 VM 执行池和城市级黄金镜像体系打基础。
