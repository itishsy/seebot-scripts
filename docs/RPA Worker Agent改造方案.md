# RPA Worker Agent 改造方案

版本：V1.1  
项目名称：seebot RPA Worker Agent 改造  
适用范围：Windows RPA 执行器、rpa-client.exe、rpa-runner.jar、Windows VM Resource Manager、城市级黄金镜像、vSphere / Horizon VM 执行池  

---

## 背景一：当前现状

当前 seebot RPA 执行器由两个核心组件组成：

```text
Windows 执行器
    |
    |-- rpa-client.exe
    |       |-- C# WinForm 程序
    |       |-- 负责启动 rpa-runner.jar
    |       |-- 负责停止 rpa-runner.jar
    |       |-- 负责报告执行器心跳
    |
    |-- rpa-runner.jar
            |-- Java 程序
            |-- 根据本机注册唯一码主动向云端获取任务
            |-- 执行环境自检
            |-- 使用 Selenium 操作浏览器
            |-- 调用 Python 第三方库操作桌面应用
            |-- 回传任务执行结果、日志、截图、附件
```

每台机器都有一个注册唯一码，例如：

```text
SR10-2604-8STY
SR10-2505-V61I
SR10-2302-87F5
```

当前任务拉取模式为：

```text
rpa-runner.jar
    ↓ 携带 machineCode / 机器唯一码
云端任务接口
    ↓ 返回该机器可执行任务
rpa-runner.jar
    ↓ 执行任务
云端
    ↓ 更新任务状态
```

---

## 背景二：核心判断

现有 `rpa-client.exe` 可以改造为 **RPA Worker Agent**，但本期不建议立即迁移 `rpa-runner.jar` 的主动拉任务职责。

本期正确改造方向是：

```text
保留：
    rpa-runner.jar 主动拉任务模式

增强：
    rpa-client.exe 改造为 RPA Worker Agent

新增：
    云端任务返回前的资源准入
    Worker 实际环境画像
    任务要求环境画像
    lease 租约机制
    VM 镜像一致性校验
```

也就是说：

```text
Agent 不负责第一阶段拉任务
Agent 负责管理机器、管理 runner、上报状态、清理环境、隔离异常

runner 继续负责按机器唯一码拉任务、执行业务、回传结果

云端任务接口负责判断：
    这台机器当前是否允许拿这个任务
```

---


## 一、改造目标

### 1.1 总体目标

将现有 `rpa-client.exe` 从“runner 启停工具 + 心跳工具”升级为标准化 **RPA Worker Agent**。

---

改造后，整体结构为：

```text
Windows VM / Windows 执行器
    |
    |-- RPA Worker Agent
    |       |-- 机器注册
    |       |-- Worker 心跳
    |       |-- runner 生命周期管理
    |       |-- runner 进程监管
    |       |-- 基础环境自检
    |       |-- VM 镜像画像上报
    |       |-- runner 当前任务感知
    |       |-- 超时熔断
    |       |-- 进程清理
    |       |-- 异常隔离
    |       |-- 资源释放通知
    |
    |-- rpa-runner.jar
            |-- 根据机器唯一码主动向云端拉取任务
            |-- 执行业务环境自检
            |-- Selenium 操作浏览器
            |-- Python 操作桌面应用
            |-- 执行具体 RPA 流程
            |-- 回传业务结果
```

---

### 1.2 最小落地顺序

```text
1. rpa-client.exe 增加 Worker 注册、心跳、actual_env_profile_code 上报
2. robot_task_queue 增加 required_env_profile_code
3. 云端 runner 拉任务接口接入 Resource Manager 准入
4. 返回任务前创建 lease，响应中返回 leaseId
5. runner 所有状态回传携带 leaseId
6. runner 通知本机 Agent task-start / task-finish
7. Agent 增加超时熔断与进程清理
8. 城市 VM 镜像增加 image-manifest.json
9. Resource Manager 按画像管理热池 / 冷池
```

### 1.3 改造后的职责边界

| 组件 | 职责 |
|---|---|
| Windows VM Resource Manager | 管 Worker 注册、Worker 状态、环境画像、lease、UKey/账号锁、VM 回滚/销毁、异常隔离 |
| rpa-client.exe / RPA Worker Agent | 管机器、管 runner、管心跳、管基础自检、管进程、管清理、管隔离 |
| rpa-runner.jar | 主动拉任务、执行业务自检、执行 RPA 流程、回传任务结果 |
| 云端任务调度中心 | 管任务队列、业务状态流转、任务筛选 |
| 云端任务拉取接口 | 在返回任务前调用 Resource Manager 做资源准入 |
| Python 桌面脚本 | 操作桌面应用、图片识别、窗口点击、输入模拟 |
| WinForm UI | 运维查看、手工启停、状态展示、调试工具 |

---

## 二、关键修正点

### 2.1 初版中需要修正的点

初版中曾提出长期目标：

```text
runner 不再自由抢任务，而是执行 Agent 指定任务
```

这可以作为远期方向，但不适合作为本期落地目标。

本版修正为：

```text
本期保留 rpa-runner.jar 主动拉任务方式。
通过云端任务拉取接口接入 Resource Manager。
在任务返回给 runner 之前，完成 Worker 状态校验、环境画像匹配、账号锁/UKey锁校验和 lease 创建。
```

---

### 2.2 新版核心原则

```text
不改变 runner 主动拉任务动作
改变云端“是否返回任务”的判断逻辑
```

也就是：

```text
旧模式：
    runner 携带 machineCode 拉任务
    云端找到任务就返回

新模式：
    runner 携带 machineCode 拉任务
    云端先查 Worker 状态
    云端先查 Worker 实际环境画像
    云端筛选与该 Worker 画像匹配的任务
    云端创建 lease
    云端再返回任务
```

---

## 三、目标架构

### 3.1 现有架构

```text
云端任务调度中心
    |
    v
rpa-runner.jar 主动拉任务
    |
    v
Windows 固定执行器
    |
    |-- rpa-client.exe
    |-- rpa-runner.jar
    |-- Selenium
    |-- Python 桌面自动化
    |-- 浏览器 / 社保客户端 / 公积金客户端
```

---

### 3.2 改造后架构

```text
seebot 云端
    |
    |-- 任务调度中心
    |       |-- 任务队列
    |       |-- 业务状态流转
    |       |-- 原任务拉取接口
    |               |
    |               |-- 返回任务前调用 Resource Manager 做资源准入
    |
    |-- Windows VM Resource Manager
            |-- Worker 注册中心
            |-- Worker 状态管理
            |-- 环境画像管理
            |-- lease 租约管理
            |-- 账号锁 / UKey锁
            |-- Worker 异常隔离
            |-- VM 生命周期管理

Windows VM / Windows 执行器
    |
    |-- RPA Worker Agent
    |       |-- 注册 Worker
    |       |-- 上报心跳
    |       |-- 上报 actual_env_profile_code
    |       |-- 启停 runner
    |       |-- 监管 runner
    |       |-- 感知 runner 当前任务
    |       |-- 清理环境
    |
    |-- rpa-runner.jar
            |-- 使用 machineCode 主动拉任务
            |-- 执行业务流程
            |-- 回传结果
```

### 3.3 总体架构图

```mermaid
flowchart TB
    subgraph Cloud["云端 seebot 平台"]
        Biz["seebot 业务系统<br/>企业缴费 / 在册 / 增减员 / 凭证"]
        Scheduler["任务调度中心<br/>任务队列 / 业务状态流转"]
        PollAPI["runner 拉任务接口<br/>/robot/task/poll?machineCode=xxx"]
        RM["Windows VM Resource Manager<br/>Worker注册 / 状态 / lease / 画像 / 隔离"]
        Lock["账号锁 / UKey锁 / Worker锁"]
        FileLog["文件 / 日志 / 截图服务"]
        DB[(核心数据表<br/>robot_task_queue<br/>rpa_env_profile<br/>rpa_worker_instance<br/>rpa_worker_lease)]
        UKey["现有 UKey / CA 挂载中心"]
    end

    subgraph VMInfra["虚拟化资源池"]
        VSphere["vSphere / Horizon"]
        Image["城市级黄金镜像<br/>image-manifest.json<br/>rpa-env-{city}-{business}-{system}-{version}"]
        HotCold["热池 / 冷池 VM"]
    end

    subgraph Worker["Windows VM / Windows 执行器"]
        Agent["RPA Worker Agent<br/>rpa-client.exe 改造<br/>注册 / 心跳 / 画像上报<br/>runner启停 / 进程监管<br/>超时熔断 / 环境清理 / 隔离"]
        Runner["rpa-runner.jar<br/>保留主动拉任务<br/>Selenium 操作浏览器<br/>调用 Python 操作桌面应用<br/>业务自检 / 结果回传"]
        Runtime["本机运行环境<br/>Java / Python venv<br/>Chrome/Edge + WebDriver<br/>社保客户端 / 公积金客户端<br/>UKey/CA 驱动"]
        LocalManifest["C:\\seebot-agent\\config\\image-manifest.json<br/>声明镜像画像"]
    end

    Biz --> Scheduler
    Scheduler --> DB
    Runner -->|"携带 machineCode 主动拉任务"| PollAPI
    PollAPI -->|"返回任务前资源准入"| RM
    RM --> DB
    RM --> Lock
    RM -->|"校验 required_env_profile_code == actual_env_profile_code"| PollAPI
    PollAPI -->|"返回 task + leaseId"| Runner

    Agent -->|"注册 / 心跳 / actual_env_profile_code"| RM
    Agent -->|"读取并校验"| LocalManifest
    Agent -->|"基础环境自检"| Runtime
    Agent -->|"启动 / 停止 / 监管"| Runner
    Runner -->|"日志 / 截图 / 附件"| FileLog
    Runner -->|"业务结果 + leaseId"| Scheduler
    Agent -->|"任务结束 / 清理完成 / release lease"| RM
    Runner -->|"UKey/证书使用"| UKey
    RM -->|"启动/回滚/销毁 VM"| VSphere
    VSphere --> Image
    Image --> HotCold
    HotCold --> Worker

```

---

### 3.4 交互流程图

```mermaid
sequenceDiagram
    autonumber
    participant Biz as seebot业务系统
    participant Scheduler as 任务调度中心
    participant RM as Windows VM Resource Manager
    participant Agent as RPA Worker Agent<br/>rpa-client.exe
    participant Runner as rpa-runner.jar
    participant Poll as 云端拉任务接口
    participant Lock as 账号锁/UKey锁
    participant File as 文件日志截图服务

    Biz->>Scheduler: 创建RPA任务<br/>写入 required_env_profile_code
    Scheduler->>RM: 如无匹配Worker，可请求启动对应VM镜像
    RM-->>Agent: VM启动后 Agent 开机运行
    Agent->>Agent: 读取 image-manifest.json<br/>执行基础环境自检
    Agent->>RM: 注册Worker<br/>上报 machineCode + actual_env_profile_code + healthStatus
    Agent->>RM: 周期心跳<br/>IDLE / runnerStatus / actual_env_profile_code

    Agent->>Runner: 启动或保持 runner 运行
    Runner->>Poll: 携带 machineCode 主动拉任务

    Poll->>RM: 查询Worker状态与实际画像
    RM->>RM: 校验Worker已注册、在线、空闲、自检通过、未隔离
    RM->>RM: 校验 task.required_env_profile_code == worker.actual_env_profile_code
    RM->>Lock: 尝试锁定账号 / UKey / Worker
    Lock-->>RM: 锁定成功
    RM->>RM: 创建 lease
    RM-->>Poll: 准入通过，返回 leaseId

    Poll-->>Runner: 返回任务<br/>taskId + executionCode + leaseId + required/actual profile
    Runner->>Agent: 本机通知 task-start<br/>taskId + leaseId
    Agent->>RM: 心跳更新 RUNNING<br/>currentTaskId + currentLeaseId

    Runner->>Runner: 业务环境自检<br/>UKey/证书/浏览器/客户端
    Runner->>Runner: Selenium操作浏览器<br/>Python操作桌面应用
    Runner->>File: 上传日志/截图/附件
    Runner->>Scheduler: 回传业务结果<br/>携带 leaseId
    Runner->>Agent: 本机通知 task-finish<br/>result + exitCode

    Agent->>Agent: 清理Java/Python/浏览器/客户端残留进程
    Agent->>RM: release lease<br/>清理结果 + 是否隔离
    RM->>Lock: 释放账号锁 / UKey锁 / Worker锁
    RM-->>Agent: Worker状态回到IDLE<br/>或异常时QUARANTINED

```

---

图中关键控制点

1. `rpa-runner.jar` 继续按 `machineCode` 主动拉任务，不在第一阶段迁移为 Agent 拉任务。
2. 云端拉任务接口不能直接返回任务，必须先调用 `Windows VM Resource Manager` 做资源准入。
3. 每个任务创建时写入 `required_env_profile_code`。
4. 每个 Worker / VM 注册时上报 `actual_env_profile_code`。
5. 返回任务前必须校验：`task.required_env_profile_code == worker.actual_env_profile_code`。
6. 返回任务前必须创建 `lease`，并将 `leaseId` 返回给 runner。
7. runner 后续所有任务状态、日志、截图、附件回传都应携带 `leaseId`。
8. Agent 负责 runner 进程监管、超时熔断、残留进程清理和 Worker 异常隔离。
9. VM 镜像内需要包含 `image-manifest.json`，Agent 启动后读取并结合实际检测结果上报。
10. 热池 / 冷池 VM 必须按环境画像分池，避免不同城市、不同系统、不同客户端环境混用。

---

## 四、机器唯一码与 WorkerId 设计

### 4.1 保留原机器唯一码

当前每台机器已有注册唯一码，建议继续保留，不直接废弃。

原因：

1. 历史任务记录已经绑定机器编码；
2. 异常统计已经按机器编码归因；
3. runner 已经依赖机器唯一码拉任务；
4. 短期改造成本最低；
5. 便于追溯历史执行记录。

### 4.2 推荐映射方式

建议新增 Worker 概念，但保留 machineCode。

```text
worker_id = WORKER-SUZHOU-SOCIAL-CLIENT-001
machine_code = SR10-2604-8STY
```

或者第一阶段直接使用：

```text
worker_id = machine_code = SR10-2604-8STY
```

推荐第一阶段先采用后一种，降低改造成本。

---

## 五、环境画像一致性设计

保留 runner 拉任务方式后，最大风险是：

```text
某台 VM 镜像实际是“苏州社保客户端环境”
但 runner 拉到了“广州税务网页端任务”
```

因此必须引入两类画像：

```text
任务要求画像：task_required_profile
Worker 实际画像：worker_actual_profile
```

只有两者匹配，云端才允许返回任务。


### 5.1 任务要求画像

每个任务创建时，必须写入任务所需环境。

示例：

```json
{
  "taskId": 123456,
  "taskType": "PAY_FEE_GET",
  "cityCode": "suzhou",
  "businessType": "SOCIAL",
  "declareSystem": "SOCIAL_CLIENT",
  "needWindows": true,
  "needDesktop": true,
  "needUkey": true,
  "needCa": true,
  "browserType": "edge",
  "clientName": "社保费管理客户端",
  "requiredEnvProfileCode": "rpa-env-suzhou-social-client-202606"
}
```

任务表建议增加字段：

```sql
ALTER TABLE robot_task_queue
ADD COLUMN required_env_profile_code VARCHAR(100) COMMENT '任务要求的环境画像',
ADD COLUMN required_city_code VARCHAR(50) COMMENT '任务要求城市',
ADD COLUMN required_business_type VARCHAR(50) COMMENT '任务要求业务类型',
ADD COLUMN required_declare_system VARCHAR(100) COMMENT '任务要求申报系统',
ADD COLUMN required_need_ukey TINYINT DEFAULT 0 COMMENT '是否需要UKey',
ADD COLUMN required_need_desktop TINYINT DEFAULT 1 COMMENT '是否需要Windows桌面',
ADD COLUMN assigned_worker_id VARCHAR(100) COMMENT '分配Worker',
ADD COLUMN lease_id VARCHAR(100) COMMENT '资源租约ID',
ADD COLUMN resource_match_status VARCHAR(50) COMMENT '资源匹配状态';
```


### 5.2 Worker 实际环境画像

Agent 启动后必须上报当前机器或 VM 的实际环境画像。

示例：

```json
{
  "workerId": "SR10-2604-8STY",
  "machineCode": "SR10-2604-8STY",
  "vmName": "rpa-suzhou-social-client-001",
  "actualEnvProfileCode": "rpa-env-suzhou-social-client-202606",
  "imageCode": "rpa-env-suzhou-social-client",
  "imageVersion": "202606",
  "cityCode": "suzhou",
  "businessType": "SOCIAL",
  "declareSystem": "SOCIAL_CLIENT",
  "needWindows": true,
  "needDesktop": true,
  "supportUkey": true,
  "supportCa": true,
  "browserType": "edge",
  "browserVersion": "124.0.xxx",
  "clientName": "社保费管理客户端",
  "clientVersion": "x.x.x",
  "agentVersion": "2.0.0",
  "runnerVersion": "1.8.5",
  "healthStatus": "PASS"
}
```

---

### 5.3 镜像 Manifest 文件

每个城市黄金镜像中应包含固定 manifest 文件。

路径：

```text
C:\seebot-agent\config\image-manifest.json
```

内容示例：

```json
{
  "envProfileCode": "rpa-env-suzhou-social-client-202606",
  "imageCode": "rpa-env-suzhou-social-client",
  "imageVersion": "202606",
  "cityCode": "suzhou",
  "businessType": "SOCIAL",
  "declareSystem": "SOCIAL_CLIENT",
  "browserType": "edge",
  "browserVersion": "124.0.xxx",
  "clientName": "社保费管理客户端",
  "clientVersion": "x.x.x",
  "caDriverVersion": "x.x.x",
  "ukeyDriverVersion": "x.x.x",
  "agentVersion": "2.0.0",
  "runnerVersion": "1.8.5",
  "buildTime": "2026-06-05 10:00:00"
}
```

注意：

```text
image-manifest.json 只是声明
Agent 实际检测结果才是最终依据
```

Agent 注册时应同时上报：

```text
manifest 声明画像
实际检测画像
最终 actual_env_profile_code
```

---

## 六、runner 拉任务接口改造

### 6.1 当前拉任务模式

```http
GET /robot/task/poll?machineCode=SR10-2604-8STY
```

旧逻辑：

```text
1. 根据 machineCode 找任务
2. 有任务就返回
```

---

### 6.2 改造后拉任务模式

新逻辑：

```text
1. 根据 machineCode 查询 Worker
2. 判断 Worker 是否已注册
3. 判断 Worker 是否在线
4. 判断 Worker 是否空闲
5. 判断 Worker 是否已通过自检
6. 判断 Worker 是否被隔离
7. 获取 Worker 的 actual_env_profile_code
8. 查询 required_env_profile_code 与 actual_env_profile_code 匹配的任务
9. 判断账号锁是否可用
10. 判断 UKey 锁是否可用
11. 创建 lease
12. 返回任务给 runner
```

---

### 6.3 云端任务返回前准入规则

只有满足以下条件，才允许返回任务：

```text
1. Worker 已注册
2. Worker 心跳正常
3. Worker 状态为 IDLE
4. Worker 未被隔离
5. runner 状态正常
6. Worker 当前没有未释放 lease
7. Worker 自检状态为 PASS
8. task.required_env_profile_code == worker.actual_env_profile_code
9. 账号锁可用
10. UKey 锁可用
11. 当前时间符合任务执行窗口
```

第一版建议使用强匹配：

```text
task.required_env_profile_code == worker.actual_env_profile_code
```

不要第一版就做复杂兼容矩阵。

---

### 6.4 拉任务返回内容

云端返回任务时必须增加 leaseId 和画像信息。

```json
{
  "hasTask": true,
  "taskId": 123456,
  "executionCode": "EXE202606050001",
  "leaseId": "LEASE-20260605-0001",
  "machineCode": "SR10-2604-8STY",
  "requiredEnvProfileCode": "rpa-env-suzhou-social-client-202606",
  "actualEnvProfileCode": "rpa-env-suzhou-social-client-202606",
  "imageVersion": "202606",
  "taskType": "PAY_FEE_GET",
  "timeoutSeconds": 3600,
  "payload": {
    "cityCode": "suzhou",
    "businessType": "SOCIAL",
    "declareSystem": "SOCIAL_CLIENT",
    "accountId": 10086,
    "needUkey": true
  }
}
```

runner 后续所有状态回传必须携带：

```text
taskId
executionCode
machineCode
leaseId
```

---

## 七、lease 创建时机

由于本期保留 runner 主动拉任务，因此 lease 的创建时机必须调整。

### 7.1 错误做法

```text
runner 已经拿到任务并开始执行
Resource Manager 之后才知道机器被占用
```

这种方式会导致：

1. Worker 状态不准确；
2. VM 可能被误判为空闲；
3. UKey/账号锁可能滞后；
4. 超时熔断和释放逻辑不可靠。


### 7.2 正确做法

```text
runner 请求任务
    ↓
云端筛选候选任务
    ↓
云端调用 Resource Manager 做资源准入
    ↓
Resource Manager 创建 lease
    ↓
lease 创建成功
    ↓
云端返回任务给 runner
```

即：

```text
任务返回给 runner 之前，必须先创建 lease
```


### 7.3 完整链路

```text
1. Agent 启动
2. Agent 注册 Worker
3. Agent 上报心跳，状态为 IDLE
4. Agent 启动 rpa-runner.jar
5. runner 携带 machineCode 请求云端任务
6. 云端查询 Worker 状态和 actual_env_profile_code
7. 云端筛选 required_env_profile_code 匹配的任务
8. 云端调用 Resource Manager 创建 lease
9. Resource Manager 锁定 Worker、账号、UKey
10. lease 创建成功
11. 云端返回任务和 leaseId 给 runner
12. runner 开始执行任务
13. Agent 心跳上报 runnerStatus=RUNNING
14. runner 执行完成后回传任务结果和 leaseId
15. 云端更新业务任务状态
16. 云端或 Agent 调用 Resource Manager release lease
17. Agent 清理环境
18. Worker 状态回到 IDLE
```

---

## 八、Agent 如何感知 runner 当前任务

为了让 Agent 可以准确监管 runner，需要让 Agent 感知 runner 当前任务。


### 8.1 推荐方案：runner 主动通知本机 Agent

runner 拉到任务后，调用本机 Agent 接口。

任务开始：

```http
POST http://127.0.0.1:18080/local/runner/task-start
```

```json
{
  "taskId": 123456,
  "executionCode": "EXE202606050001",
  "leaseId": "LEASE-20260605-0001",
  "taskType": "PAY_FEE_GET"
}
```

任务结束：

```http
POST http://127.0.0.1:18080/local/runner/task-finish
```

```json
{
  "taskId": 123456,
  "executionCode": "EXE202606050001",
  "leaseId": "LEASE-20260605-0001",
  "result": "SUCCESS",
  "exitCode": 0
}
```

优点：

1. Agent 实时知道 runner 当前任务；
2. Agent 心跳可以准确上报 currentTaskId 和 currentLeaseId；
3. Agent 可以做任务级超时熔断；
4. Agent 能在 runner 卡死时进行兜底清理和释放。


### 8.2 过渡方案：Agent 从云端查询当前任务

如果短期不想改 runner，可以让 Agent 心跳时向云端查询：

```text
machineCode=xxx 当前是否存在运行中任务？
```

优点：

1. runner 改动小；
2. 可以快速落地。

缺点：

1. 状态有延迟；
2. Agent 对 runner 的监管不够直接；
3. runner 卡死时 Agent 可能无法准确知道任务上下文。

建议：

```text
第一阶段使用云端查询过渡
第二阶段改为 runner 主动通知本机 Agent
```

---

## 九、Agent 基础自检与 runner 业务自检

### 9.1 Agent 基础自检

Agent 负责机器级、自身级、基础运行环境自检。

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
16. image-manifest.json 是否存在
17. manifest 声明画像是否与实际检测一致
```

### 9.2 runner 业务自检

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
11. requiredEnvProfileCode 是否与本地 actualEnvProfileCode 一致
```

### 9.3 自检失败处理

Agent 基础自检失败：

```text
1. 不允许 Worker 接任务
2. Worker 状态标记为 ERROR 或 QUARANTINED
3. 云端任务拉取接口不返回任务
4. 记录 ENV_CHECK_FAILED
5. 通知运维处理
```

runner 业务自检失败：

```text
1. runner 回传业务自检失败
2. Agent 收集日志和截图
3. 释放 lease
4. Worker 清理后回到 IDLE 或进入 QUARANTINED
```

画像不一致：

```text
1. 不执行任务
2. 回传 ENV_PROFILE_MISMATCH
3. 释放 lease
4. Worker 标记为 QUARANTINED
```

---

## 十、VM 镜像与任务一致性保障

### 10.1 三层校验

#### 第一层：任务创建时写入 required_env_profile_code

任务一生成，就必须明确需要什么环境。

例如：

```text
rpa-env-suzhou-social-client-202606
```

---

#### 第二层：Agent 注册时上报 actual_env_profile_code

VM 启动后，Agent 必须上报实际环境。

例如：

```text
rpa-env-suzhou-social-client-202606
```

---

#### 第三层：任务返回前强匹配

runner 拉任务时，云端必须校验：

```text
task.required_env_profile_code == worker.actual_env_profile_code
```

不匹配则不返回任务。


### 10.2 不一致场景处理

#### 场景一：任务画像和 Worker 画像不一致

```text
required_env_profile_code != actual_env_profile_code
```

处理：

```text
1. 不返回任务
2. 记录 RESOURCE_PROFILE_NOT_MATCH
3. 任务继续等待匹配 Worker
4. 如长期无匹配 Worker，触发冷池启动
```


#### 场景二：VM 期望画像与实际画像不一致

```text
expected_env_profile_code != actual_env_profile_code
```

处理：

```text
1. Worker 标记 QUARANTINED
2. 禁止 runner 拉任务
3. 通知 Resource Manager 回滚或销毁 VM
4. 记录 IMAGE_PROFILE_MISMATCH
```

#### 场景三：镜像名匹配但自检失败

例如浏览器版本不对、证书不可枚举、客户端不存在。

处理：

```text
1. Worker 标记 ERROR 或 QUARANTINED
2. 云端不返回任务
3. 如是热池 VM，剔出热池
4. 如是冷池 VM，销毁并重新创建
```

---

## 十一、热池与冷池下的任务拉取控制

### 11.1 热池

热池 VM 长期在线，runner 会周期性拉任务。

因此热池必须按环境画像分池。

示例：

```text
苏州社保客户端热池：
    Worker-001 actual_env_profile_code = rpa-env-suzhou-social-client-202606
    Worker-002 actual_env_profile_code = rpa-env-suzhou-social-client-202606

广州税务网页端热池：
    Worker-003 actual_env_profile_code = rpa-env-guangzhou-social-taxweb-202606
    Worker-004 actual_env_profile_code = rpa-env-guangzhou-social-taxweb-202606
```

云端任务接口按 `actual_env_profile_code` 过滤任务：

```text
苏州社保客户端 VM 只能拉到苏州社保客户端任务
广州税务网页端 VM 只能拉到广州税务网页端任务
```

---

### 11.2 冷池

冷池适合“任务先出现，再启动对应 VM”。

流程：

```text
1. 云端产生待执行任务
2. 任务带 required_env_profile_code
3. Resource Manager 发现没有匹配 Worker
4. Resource Manager 根据 required_env_profile_code 启动对应 VM 镜像
5. VM 启动后 Agent 注册 actual_env_profile_code
6. Resource Manager 校验 actual == required
7. Worker 状态变成 IDLE
8. runner 开始拉任务
9. 云端只把 matching profile 的任务返回给它
```

为了避免冷池 VM 启动后抢错任务，必须依赖：

```text
runner poll 接口按 actual_env_profile_code 过滤任务
```

---

## 十二、进程形态设计

### 12.1 短期形态

短期可以继续使用现有 `rpa-client.exe`，在其内部增强 Agent 能力。

```text
rpa-client.exe
    |-- WinForm UI
    |-- Worker 注册
    |-- 心跳上报
    |-- runner 启停
    |-- runner 进程监管
    |-- 基础环境自检
    |-- 环境画像上报
    |-- 环境清理
```

适用阶段：

1. 快速验证 Resource Manager 接入；
2. 快速纳管现有 Windows 执行器；
3. 保留现有操作方式；
4. 降低第一阶段改造风险。

缺点：

1. 依赖用户桌面会话；
2. 不适合作为长期生产常驻进程；
3. 窗口关闭可能影响执行；
4. RDP 断开、用户注销、系统重启后的稳定性不足。

---

### 12.2 中期形态

抽出核心类库。

```text
Seebot.Agent.Core.dll
    |-- RegisterService
    |-- HeartbeatService
    |-- LeaseService
    |-- RunnerProcessManager
    |-- HealthCheckService
    |-- CleanupService
    |-- ImageProfileService
    |-- LogUploadService
    |-- UKeyMountAdapter

rpa-client.exe
    |-- 引用 Seebot.Agent.Core.dll
    |-- 作为 WinForm 运维界面

Seebot.Agent.Service.exe
    |-- 引用 Seebot.Agent.Core.dll
    |-- 作为生产常驻服务
```

---

### 12.3 长期形态

```text
Seebot.Agent.Service.exe
    |-- Windows Service
    |-- 注册 / 心跳 / 租约状态同步
    |-- runner 管理
    |-- 状态上报
    |-- 环境清理
    |-- 资源释放

Seebot.Agent.DesktopLauncher.exe
    |-- 在交互式桌面 Session 启动 runner
    |-- 避免 Session 0 桌面自动化问题

rpa-client.exe
    |-- WinForm 运维面板

rpa-runner.jar
    |-- 主动拉任务
    |-- 执行业务流程
```

---

## 十三、接口设计

### 13.1 Worker 注册接口

```http
POST /api/rpa/worker/register
```

请求：

```json
{
  "workerId": "SR10-2604-8STY",
  "machineCode": "SR10-2604-8STY",
  "workerName": "苏州社保客户端执行器01",
  "hostName": "RPA-SUZHOU-001",
  "ip": "10.10.20.15",
  "actualEnvProfileCode": "rpa-env-suzhou-social-client-202606",
  "imageCode": "rpa-env-suzhou-social-client",
  "imageVersion": "202606",
  "agentVersion": "2.0.0",
  "runnerVersion": "1.8.5",
  "javaVersion": "1.8.0_351",
  "pythonVersion": "3.9.13",
  "osVersion": "Windows 10 LTSC",
  "screenResolution": "1920x1080",
  "healthStatus": "PASS",
  "loginSessionReady": true
}
```

响应：

```json
{
  "success": true,
  "workerId": "SR10-2604-8STY",
  "status": "REGISTERED",
  "serverTime": "2026-06-05 10:00:00"
}
```

---

### 13.2 Worker 心跳接口

```http
POST /api/rpa/worker/heartbeat
```

请求：

```json
{
  "workerId": "SR10-2604-8STY",
  "machineCode": "SR10-2604-8STY",
  "status": "RUNNING",
  "runnerStatus": "RUNNING",
  "actualEnvProfileCode": "rpa-env-suzhou-social-client-202606",
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

### 13.3 runner 拉任务接口

```http
GET /robot/task/poll?machineCode=SR10-2604-8STY
```

处理逻辑：

```text
1. 查询 Worker
2. 校验 Worker 状态
3. 获取 actual_env_profile_code
4. 筛选 required_env_profile_code 匹配任务
5. 创建 lease
6. 返回任务
```

响应：

```json
{
  "hasTask": true,
  "taskId": 123456,
  "executionCode": "EXE202606050001",
  "leaseId": "LEASE-20260605-0001",
  "machineCode": "SR10-2604-8STY",
  "requiredEnvProfileCode": "rpa-env-suzhou-social-client-202606",
  "actualEnvProfileCode": "rpa-env-suzhou-social-client-202606",
  "taskType": "PAY_FEE_GET",
  "timeoutSeconds": 3600,
  "payload": {
    "cityCode": "suzhou",
    "businessType": "SOCIAL",
    "declareSystem": "SOCIAL_CLIENT",
    "accountId": 10086,
    "needUkey": true
  }
}
```

---

### 13.4 runner 本机任务开始通知

```http
POST http://127.0.0.1:18080/local/runner/task-start
```

请求：

```json
{
  "taskId": 123456,
  "executionCode": "EXE202606050001",
  "leaseId": "LEASE-20260605-0001",
  "taskType": "PAY_FEE_GET"
}
```

---

### 13.5 runner 本机任务完成通知

```http
POST http://127.0.0.1:18080/local/runner/task-finish
```

请求：

```json
{
  "taskId": 123456,
  "executionCode": "EXE202606050001",
  "leaseId": "LEASE-20260605-0001",
  "result": "SUCCESS",
  "exitCode": 0
}
```

---

### 13.6 资源释放接口

```http
POST /api/rpa/resource/release
```

请求：

```json
{
  "workerId": "SR10-2604-8STY",
  "machineCode": "SR10-2604-8STY",
  "leaseId": "LEASE-20260605-0001",
  "taskId": 123456,
  "result": "SUCCESS",
  "runnerExitCode": 0,
  "cleanupStatus": "SUCCESS",
  "needQuarantine": false
}
```

---

## 十四、数据库设计建议

### 14.1 环境画像表

```sql
CREATE TABLE rpa_env_profile (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    profile_code VARCHAR(100) NOT NULL,
    city_code VARCHAR(50) NOT NULL,
    city_name VARCHAR(100),
    business_type VARCHAR(50) NOT NULL,
    declare_system VARCHAR(100) NOT NULL,
    need_windows TINYINT DEFAULT 1,
    need_desktop TINYINT DEFAULT 1,
    need_ukey TINYINT DEFAULT 0,
    need_ca TINYINT DEFAULT 0,
    browser_type VARCHAR(50),
    browser_version VARCHAR(100),
    client_name VARCHAR(200),
    client_version VARCHAR(100),
    ca_driver_version VARCHAR(100),
    ukey_driver_version VARCHAR(100),
    vm_template_code VARCHAR(100),
    image_version VARCHAR(100),
    status VARCHAR(30) DEFAULT 'stable',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_profile_code (profile_code)
);
```


### 14.2 Worker 实例表

```sql
CREATE TABLE rpa_worker_instance (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    worker_id VARCHAR(100) NOT NULL,
    machine_code VARCHAR(100) NOT NULL,
    worker_name VARCHAR(200),
    host_name VARCHAR(200),
    ip VARCHAR(100),

    vm_name VARCHAR(200),
    expected_env_profile_code VARCHAR(100),
    actual_env_profile_code VARCHAR(100),
    image_code VARCHAR(100),
    image_version VARCHAR(100),

    os_version VARCHAR(200),
    screen_resolution VARCHAR(50),
    agent_version VARCHAR(100),
    runner_version VARCHAR(100),
    java_version VARCHAR(100),
    python_version VARCHAR(100),

    status VARCHAR(50) NOT NULL,
    runner_status VARCHAR(50),
    health_status VARCHAR(50),

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
    KEY idx_machine_code (machine_code),
    KEY idx_actual_profile_status (actual_env_profile_code, status),
    KEY idx_heartbeat (last_heartbeat_time)
);
```

### 14.3 Worker 租约表

```sql
CREATE TABLE rpa_worker_lease (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    lease_id VARCHAR(100) NOT NULL,
    task_id BIGINT NOT NULL,
    execution_code VARCHAR(100),
    worker_id VARCHAR(100) NOT NULL,
    machine_code VARCHAR(100),

    required_env_profile_code VARCHAR(100),
    actual_env_profile_code VARCHAR(100),

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
    KEY idx_task (task_id),
    KEY idx_worker (worker_id),
    KEY idx_machine_code (machine_code),
    KEY idx_status_expire (status, expire_time)
);
```

### 14.4 Worker 自检记录表

```sql
CREATE TABLE rpa_worker_health_check (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    worker_id VARCHAR(100) NOT NULL,
    machine_code VARCHAR(100),
    lease_id VARCHAR(100),
    task_id BIGINT,

    check_item VARCHAR(100) NOT NULL,
    check_status VARCHAR(30) NOT NULL,
    check_message VARCHAR(1000),
    detail_json TEXT,

    check_time DATETIME DEFAULT CURRENT_TIMESTAMP,

    KEY idx_worker_time (worker_id, check_time),
    KEY idx_machine_time (machine_code, check_time),
    KEY idx_lease (lease_id),
    KEY idx_status (check_status)
);
```

---

## 十五、Agent 本地目录规范

```text
C:\seebot-agent
    |-- config
    |   |-- agent.json
    |   |-- image-manifest.json
    |
    |-- logs
    |   |-- agent
    |   |-- runner
    |
    |-- workspace
    |   |-- {taskId}
    |       |-- input.json
    |       |-- output.json
    |       |-- screenshots
    |       |-- downloads
    |       |-- logs
    |
    |-- temp
    |-- tools

C:\seebot-runtime
    |-- java
    |-- python-venv
    |-- runner
    |   |-- rpa-runner.jar
    |-- scripts
    |-- drivers
    |-- clients
```

---

## 十六、Agent 本地配置

`C:\seebot-agent\config\agent.json`

```json
{
  "workerId": "SR10-2604-8STY",
  "machineCode": "SR10-2604-8STY",
  "workerName": "苏州社保客户端执行器01",
  "resourceManagerUrl": "http://seebot-resource-manager",

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
  "enableKillProcessOnTimeout": true,
  "enableImageProfileCheck": true
}
```

---

## 十九、runner 改造要求

保留 runner 主动拉任务，但需要补充以下能力。

### 19.1 拉任务时继续携带 machineCode

```http
GET /robot/task/poll?machineCode=SR10-2604-8STY
```

---

### 19.2 接收并保存 leaseId

runner 拉到任务后，必须保存：

```text
taskId
executionCode
machineCode
leaseId
requiredEnvProfileCode
actualEnvProfileCode
timeoutSeconds
```

---

### 19.3 所有任务回传携带 leaseId

包括：

1. 任务开始；
2. 任务执行中；
3. 任务成功；
4. 任务失败；
5. 任务超时；
6. 环境自检失败；
7. 截图上传；
8. 附件上传；
9. 业务异常回传。

---

### 19.4 runner 启动任务前校验画像

runner 本地读取：

```text
C:\seebot-agent\config\image-manifest.json
```

或调用本地 Agent 查询实际画像。

如果：

```text
requiredEnvProfileCode != actualEnvProfileCode
```

则：

```text
1. 不执行任务
2. 回传 ENV_PROFILE_MISMATCH
3. 通知本机 Agent
4. 释放 lease
```

---

### 19.5 通知本机 Agent 当前任务

推荐新增本机 HTTP 通知。

任务开始：

```http
POST http://127.0.0.1:18080/local/runner/task-start
```

任务结束：

```http
POST http://127.0.0.1:18080/local/runner/task-finish
```

---

## 二十、实施计划

### 阶段一：Worker 纳管

周期建议：1～2 周

工作内容：

1. rpa-client.exe 增加 Worker 注册；
2. rpa-client.exe 增加 Worker 心跳；
3. 心跳增加 runnerStatus；
4. 心跳增加 machineCode；
5. 心跳增加 actualEnvProfileCode；
6. Resource Manager 增加 Worker 注册接口；
7. Resource Manager 增加 Worker 心跳接口；
8. 建立 rpa_worker_instance 表；
9. 可查询 Worker 在线、离线、空闲、运行、异常状态。

交付物：

1. Worker 注册接口；
2. Worker 心跳接口；
3. rpa-client.exe Agent 协议版本；
4. Worker 状态查询接口；
5. Worker 管理页面或运维查询 SQL。

---

### 阶段二：任务拉取接口增加资源准入

周期建议：2 周

工作内容：

1. 任务表增加 required_env_profile_code；
2. Worker 表增加 actual_env_profile_code；
3. runner 拉任务接口接入 Resource Manager；
4. 返回任务前校验 Worker 状态；
5. 返回任务前校验环境画像；
6. 返回任务前校验账号锁；
7. 返回任务前校验 UKey 锁；
8. 返回任务前创建 lease；
9. 拉任务响应增加 leaseId。

交付物：

1. 改造后的任务拉取接口；
2. rpa_worker_lease 表；
3. Resource Manager 资源准入逻辑；
4. lease 创建逻辑；
5. 画像匹配逻辑。

---

### 阶段三：Agent 监管 runner

周期建议：1～2 周

工作内容：

1. rpa-client.exe 监控 runner 进程；
2. Agent 心跳上报 currentTaskId；
3. Agent 心跳上报 currentLeaseId；
4. runner 通知 Agent task-start；
5. runner 通知 Agent task-finish；
6. Agent 实现 runner 超时熔断；
7. Agent 实现残留进程清理。

交付物：

1. 本机 Agent HTTP 接口；
2. runner task-start 通知；
3. runner task-finish 通知；
4. 超时 kill 能力；
5. 进程清理能力。

---

### 阶段四：VM 镜像画像一致性

周期建议：1～2 周

工作内容：

1. 城市镜像增加 image-manifest.json；
2. Agent 读取 manifest；
3. Agent 检测实际环境；
4. Agent 上报 actualEnvProfileCode；
5. 云端校验 expected 与 actual；
6. 不一致时 Worker 进入 QUARANTINED；
7. runner 执行前再次校验画像。

交付物：

1. image-manifest.json 标准；
2. Agent 画像上报能力；
3. 画像不一致隔离机制；
4. runner 本地画像校验能力。

---

### 阶段五：Service 化改造

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

### 阶段六：接入 VM 热池 / 冷池

周期建议：3～4 周

工作内容：

1. 按 required_env_profile_code 建立 VM 模板；
2. 按 actual_env_profile_code 建立热池；
3. 冷池按任务 required_env_profile_code 启动 VM；
4. VM 启动后 Agent 自动注册；
5. runner 拉任务接口按 actual_env_profile_code 过滤；
6. 任务完成后 Agent 清理环境；
7. Resource Manager 根据策略回滚、销毁或回到热池。

交付物：

1. VM 热池试点；
2. VM 冷池试点；
3. 任务与镜像一致性报告；
4. 试点城市执行报告。

---

## 二十一、验收标准

### 21.1 功能验收

1. Agent 可注册 Worker；
2. Agent 可上报 machineCode；
3. Agent 可上报 actualEnvProfileCode；
4. Agent 可上报 runnerStatus；
5. runner 仍可按 machineCode 拉任务；
6. 云端任务拉取接口只返回画像匹配任务；
7. 任务返回前可创建 lease；
8. runner 回传时携带 leaseId；
9. Agent 可感知 runner 当前任务；
10. Agent 可超时熔断 runner；
11. Agent 可清理残留进程；
12. Worker 异常时可隔离；
13. Worker 画像不一致时不返回任务；
14. VM 镜像画像不一致时可隔离。

---

### 21.2 稳定性验收

1. Agent 连续运行 7 天无异常退出；
2. runner 异常退出后 Agent 可识别；
3. runner 超时后 Agent 可终止；
4. Java/Python/浏览器残留进程可清理；
5. RDP 断开不影响 Agent Service；
6. Windows 重启后 Agent 自动恢复；
7. Resource Manager 能准确识别 Worker 状态；
8. Worker 状态与实际 runner 状态一致。

---

### 21.3 镜像一致性验收

1. 苏州社保客户端 VM 只能获取苏州社保客户端任务；
2. 广州税务网页端 VM 只能获取广州税务网页端任务；
3. requiredEnvProfileCode 与 actualEnvProfileCode 不一致时不返回任务；
4. expectedEnvProfileCode 与 actualEnvProfileCode 不一致时 Worker 被隔离；
5. 镜像 manifest 缺失时 Worker 不允许接任务；
6. manifest 声明与实际检测不一致时 Worker 不允许接任务；
7. 自检失败时 Worker 不允许接任务。

---

### 21.4 业务兼容验收

1. 原有 runner 主动拉任务机制保留；
2. 原有 Selenium 浏览器操作不受影响；
3. 原有 Python 桌面应用操作不受影响；
4. 原有任务状态回传链路不受影响；
5. 原有日志、截图、附件回传链路不受影响；
6. 原有 UKey/CA 挂载能力可继续复用；
7. 原有机器唯一码可继续使用；
8. 历史机器维度统计不受影响。

---

## 二十二、风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| runner 继续主动拉任务，绕过 Resource Manager | 资源管理失效 | 云端任务返回前必须接入 Resource Manager 准入 |
| Worker 画像不准确 | 任务错跑到错误 VM | Agent 注册时读取 manifest，并做实际检测 |
| 任务未写 required_env_profile_code | 无法准确匹配资源 | 任务创建阶段强制生成画像 |
| VM 镜像 manifest 缺失 | 无法判断镜像身份 | manifest 缺失则 Worker 不允许接任务 |
| required 和 actual 不一致 | 任务错跑 | 不返回任务，Worker 或任务进入异常处理 |
| lease 创建滞后 | Worker 状态不准确 | 任务返回给 runner 前必须先创建 lease |
| Agent 不知道 runner 当前任务 | 无法超时熔断 | runner 通知本机 Agent task-start/task-finish |
| WinForm 作为生产 Agent 不稳定 | RDP/窗口关闭影响运行 | 中期拆分为 Windows Service |
| Service Session 0 问题 | 桌面自动化失败 | 使用 DesktopLauncher 在交互式桌面启动 runner |
| 残留进程污染后续任务 | 任务不稳定 | Agent 执行后统一清理进程和目录 |

---

## 二十三、推荐落地路线

推荐按以下路线实施：

```text
第一步：
    rpa-client.exe 增加 Worker 注册、心跳、runnerStatus、actualEnvProfileCode 上报

第二步：
    任务创建时增加 requiredEnvProfileCode

第三步：
    runner 拉任务接口接入 Resource Manager 准入

第四步：
    返回任务前创建 lease，并把 leaseId 返回给 runner

第五步：
    runner 所有状态回传携带 leaseId

第六步：
    Agent 感知 runner 当前任务，支持超时熔断和清理

第七步：
    城市 VM 镜像加入 image-manifest.json

第八步：
    Agent 注册时做 manifest + 实际环境检测

第九步：
    rpa-client.exe 抽离 Core，新增 Windows Service

第十步：
    接入 VM 热池、冷池、回滚和销毁机制
```

---

## 二十四、结论

在当前 seebot RPA 执行体系下，`rpa-runner.jar` 已承担根据机器唯一码主动向云端拉取任务的职责。本期不应强制迁移该职责，否则会扩大改造面并增加生产风险。

本期最合理方案是：

```text
保留 runner 主动拉任务
增强 rpa-client.exe 为 RPA Worker Agent
改造云端任务返回前的 Resource Manager 准入逻辑
用 required_env_profile_code 与 actual_env_profile_code 保证任务与 VM 镜像一致
用 lease 保证任务与 Worker 占用关系准确
```

最终职责边界为：

```text
rpa-client.exe / Agent：
    管机器、管 runner、管状态、管自检、管清理、管隔离

rpa-runner.jar：
    主动拉任务、执行业务、回传结果

Resource Manager：
    管 Worker、管画像、管 lease、管锁、管 VM 生命周期

云端任务调度中心：
    管任务队列、任务筛选、业务状态流转
```

这样既能保持现有执行链路稳定，又能逐步实现 RPA底座2.0 所需要的标准化、可编排、可隔离、可回滚、可版本化管理能力。
