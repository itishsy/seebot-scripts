# RPA Worker Agent 架构图和交互流程图

版本：V1.0  
说明：本图基于修正后的《RPA Worker Agent改造方案》，保留 `rpa-runner.jar` 根据机器唯一码主动向云端拉取任务的机制。核心控制点放在云端任务返回前：Resource Manager 必须完成 Worker 状态校验、环境画像匹配、账号锁/UKey锁校验和 lease 创建，任务才允许返回给 runner。

---

## 一、总体架构图

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

## 二、交互流程图

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

## 三、图中关键控制点

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

## 四、最小落地顺序

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
