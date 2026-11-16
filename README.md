# FusionCompute 8.6.0 → Zabbix 6.0 监控方案

通过华为 FusionCompute VRM 的 REST API 采集 **站点 / 主机（CNA）/ 虚拟机 / 集群 / 数据存储 / 告警**，接入 Zabbix 6.0。
只依赖 Python 3 标准库，模板为 Zabbix 6.0 导出格式，解压即用。

## 目录

- [一、文件清单](#一文件清单)
- [二、工作原理](#二工作原理)
  - [Accept 头里的 version（最常踩的坑）](#accept-头里的-version最常踩的坑)
  - [站点定位与路径](#站点定位与路径)
- [三、脚本用法](#三脚本用法)
  - [`--get` 支持的取值](#支持的取值)
  - [虚拟机状态码](#虚拟机状态码)
  - [数据存储为什么不用 monitors 接口](#数据存储为什么不用-monitors-接口)
  - [虚拟机为什么只有 1 次外部检查](#虚拟机为什么只有-1-次外部检查)
  - [采集负载评估](#采集负载评估)
  - [token 缓存](#token-缓存默认开启)
- [四、部署步骤](#四部署步骤)
  - [方案 A：External Check（推荐）](#方案-aexternal-check推荐模板默认方式)
  - [方案 B：Trapper](#方案-btrapperzabbix_sender-主动推送适合网络隔离场景)
  - [升级已有环境](#升级已有环境)
- [五、模板说明](#五模板说明)
  - [模板文件格式](#模板文件格式zabbix-各版本不兼容)
  - [可调宏](#可调宏)
- [六、排障 FAQ](#六排障-faq)
- [七、变更记录](#七变更记录)

## 一、文件清单

| 文件 | 说明 |
| --- | --- |
| `fusioncompute_monitor.py` | 采集脚本（Python 3 标准库，无第三方依赖） |
| `fusioncompute.conf` | 配置文件（VRM 地址/账号/密码、接口版本、Zabbix 参数） |
| `zbx_fusioncompute_template.yaml` | Zabbix 6.0 模板（可直接导入） |
| `FusionCompute 8.6.0 REST接口参考 03/` | 官方接口文档与监控指标清单（对照用，不参与运行） |

## 二、工作原理

依据《FusionCompute 8.6.0 VRM接口文档 03》，关键接口如下（`<ip>:7443`，仅 HTTPS）：

| 功能 | 方法 + 路径 |
| --- | --- |
| 查询支持版本 | `GET /service/versions`（不带 version 参数） |
| 登录（获取 Token） | `POST /service/session`，头 `X-Auth-User` + `X-Auth-Key`（密码 SHA-256） |
| 站点列表 | `GET /service/sites` |
| 主机列表 | `GET /service/sites/{site_id}/hosts` |
| 虚拟机列表 | `GET /service/sites/{site_id}/vms?limit&offset&detail&isTemplate` |
| 单台虚拟机 | `GET /service/sites/{site_id}/vms/{vm_id}` |
| 虚拟机数量统计 | `GET /service/sites/{site_id}/vms/statistics` |
| 集群列表 | `GET /service/sites/{site_id}/clusters` |
| 数据存储 | `GET /service/sites/{site_id}/datastores` |
| 实时监控指标 | `POST /service/sites/{site_id}/monitors/objectmetric-realtimedata` |
| 活动告警 | `POST /service/sites/{site_id}/alarms/activeAlarms` |

> Token 有效期 **600 秒（10 分钟）**。脚本默认开启 token 缓存，10 分钟内多次调用只登录一次，
> 详见 [token 缓存](#token-缓存默认开启)。

### Accept 头里的 version（最常踩的坑）

这里的 version 是**接口版本**，不是产品版本。按接口文档 1.2「接口文档版本与产品对应关系」：

| 接口版本 | 对应产品版本 |
| --- | --- |
| 6.3 | FusionCompute 6.3.0 / 6.3.1 |
| 6.5 | FusionCompute 6.5.0 / 6.5.1 |
| 8.0 | FusionCompute 8.0.0 / 8.0.0.SPC1 / 8.0.1 |
| 8.1 | FusionCompute 8.1.0 **或更高版本**（8.2 / 8.6 / 8.8 都用 8.1） |

因此 **FusionCompute 8.6.0 要填 `version = 8.1`**。填 `8.6.0` 会被 VRM 直接拒绝：

```
HTTP 400 {"errorCode":"10000022","errorDes":"版本号错误"}
```

`version = auto` 时脚本先 `GET /service/versions` 取服务端支持的版本列表再协商；
配置里写了明确版本时直接用，**只有**被判 `10000022` 才回退重试并给出 WARN 提示。
鉴权失败（密码错）不会触发版本重试，避免无意义地反复登录。

### 站点定位与路径

现场两种情况会导致「能登录但发现为空」，脚本都已自适应：

1. **站点选错**：多站点/联邦时 `sites[0]` 不一定是有主机的站点，且 `isSelf` 可能返回字符串
   `"false"`（字符串是真值，直接 `if` 判断会把所有站点都当成当前站点）。
   现在按 `true/1/yes` 严格判定，本端站点优先，取不到数据会自动切到下一个站点并打印 NOTE。
2. **站点路径拼法**：站点对象同时有 `urn`（`urn:sites:XXXX`）和 `uri`（`/service/sites/XXXX`）。
   脚本按 `uri` → urn 短 ID → 完整 urn 依次尝试，命中后打印实际使用的路径。

诊断：`--probe` 会打印每个站点的 `name / urn / uri / isSelf / status` 与最终采用的路径、
主机数、虚拟机数、集群数、数据存储数与一条数据存储容量示例。

## 三、脚本用法

```bash
# 1. 自动发现（Zabbix LLD 用，输出 JSON）
python3 fusioncompute_monitor.py --discover hosts
python3 fusioncompute_monitor.py --discover vms
python3 fusioncompute_monitor.py --discover clusters
python3 fusioncompute_monitor.py --discover datastores

# 2. 取单个值（External Check / 手工验证用）
python3 fusioncompute_monitor.py --get host.cpu --urn "urn:sites:xxx:hosts:1"
python3 fusioncompute_monitor.py --get vm.info --urn "urn:sites:xxx:vms:i-00000002"
python3 fusioncompute_monitor.py --get vm.count

# 3. 批量采集并推送（Trapper 模式，调用 zabbix_sender）
python3 fusioncompute_monitor.py --send
python3 fusioncompute_monitor.py --dump        # 只打印待推送的行，不真发

# 4. 排障
python3 fusioncompute_monitor.py --probe       # 版本/站点/路径/各类对象数量
python3 fusioncompute_monitor.py --debug --discover hosts   # 每个请求与响应原文打到 stderr
python3 fusioncompute_monitor.py --version 8.0 --discover hosts   # 临时覆盖接口版本
```

### 支持的取值

| 参数 | 含义 | 数据来源 | 需要 `--urn` |
| --- | --- | --- | --- |
| `site.status` / `site.name` | 站点状态 / 站点名称 | 站点对象 | 否 |
| `alarm.total` / `alarm.level.1..4` | 活动告警总数 / 各级告警数 | 活动告警接口 | 否 |
| `host.status` / `host.vmcount` | 主机状态码（0~7）/ 主机上虚拟机数 | 主机列表 | 是 |
| `host.cpu` / `host.mem` / `host.disk` | 主机 CPU/内存/磁盘占用率 | cpu_usage / mem_usage / logic_disk_usage | 是 |
| `host.net_in` / `host.net_out` | 主机网络进出流速 | nic_byte_in / nic_byte_out | 是 |
| `host.dom0_cpu` / `host.dom0_mem` | Dom0 CPU/内存占用率 | dom0_cpu_usage / dom0_mem_usage | 是 |
| `cluster.cpu` / `cluster.disk` | 集群 CPU/磁盘占用率 | cpu_usage / logic_disk_usage | 是 |
| `cluster.net_in` / `cluster.net_out` | 集群网络进出流速 | nic_byte_in_usage / nic_byte_out_usage | 是 |
| `ds.disk` / `ds.used_gb` / `ds.total_gb` / `ds.free_gb` | 数据存储占用率(%) / 已用GB / 总容量GB / 剩余GB | `GET /datastores` 的 `usedSizeGB`、`actualCapacityGB`（缺省用 `capacityGB`） | 是 |
| `vm.info` | **一台虚机的完整 JSON**（名称/IP/状态/CPU/内存/磁盘/vCPU/所在主机…） | 单台虚拟机 + 实时指标 | 是 |
| `vm.status` | 虚拟机状态码（0~13） | 单台虚拟机 | 是 |
| `vm.cpu` / `vm.mem` / `vm.disk` | 虚拟机 CPU/内存/磁盘占用率 | cpu_usage / mem_usage / disk_usage | 是 |
| `vm.count` / `vm.running` / `vm.stopped` / `vm.fault` / `vm.hibernated` | 虚拟机数量统计（不含模板） | `GET /vms/statistics` | 否 |

数值处理：VRM 的 `metricValue` 是字符串，可能是 `"1.65"`、`"45.6%"`、带千分位，或「小指标」的 JSON 字符串。
脚本统一规整后输出纯数字；转不出来时输出 `ZBX_NOTSUPPORTED 返回值不是数字：'NA'（...）`，
Zabbix 界面上直接显示原因，而不是报 `value of type "string" is not suitable for value type "numeric(float)"`。

缺数据时 `--get` 输出空行（Zabbix 外部检查约定），不会输出错误文本污染取值。

### 虚拟机状态码

`vm.status`、`vm.info.status_code`、模板里的 `{#VM.STATUS_CODE}` 使用同一套编码：

| 码 | 含义 | 码 | 含义 |
| --- | --- | --- | --- |
| 0 | running 运行中 | 7 | stopping 停止中 |
| 1 | stopped 已停止 | 8 | shutting-down 删除中 |
| 2 | hibernated 已休眠 | 9 | migrating 迁移中 |
| 3 | pause 已暂停 | 10 | fault-resuming 故障恢复中 |
| 4 | unknown 不明确 | 11 | hibernating 休眠中 |
| 5 | creating 创建中 | 12 | recycling 回收中 |
| 6 | starting 启动中 | 13 | fault 故障 |
| | | -1 | 未采集到 |

### 数据存储为什么不用 monitors 接口

《监控指标列表 01.xlsx》里只有**主机、虚拟机、集群**三类对象的指标，**没有数据存储**。
对 datastore 的 urn 调 `monitors/objectmetric-realtimedata`（传 `disk_usage` 之类）拿不到数字，
VRM 会回一个非数字文本，于是 Zabbix 报 `numeric(float)` 类型错。

现在数据存储的容量/占用率一律取自 `GET /sites/{id}/datastores`：

```
ds.disk = usedSizeGB / actualCapacityGB * 100     # 没有 actualCapacityGB 时用 capacityGB
```

所以 `ds.io_in` / `ds.io_out` 这类指标**不存在**，脚本会直接报错说明，而不是给个坏值。

### 虚拟机为什么只有 1 次外部检查

一台虚机要看的字段很多。如果每个字段都做成 External Check，就是「每台虚机 × 10 次脚本执行」，
几十台虚机就把 Zabbix poller 打满。所以模板用 **主监控项 + 依赖项（DEPENDENT）**：

```
主监控项（每台虚机唯一的外呼，2 分钟周期）
  fusioncompute_monitor.py["--get","vm.info","--urn","{#VM.URN}"]
     ↓ 返回一段 JSON
  {"name":"VM-Web-01","ip":"192.168.10.11","status":"running","status_code":0,
   "cpu_usage":12.3,"mem_usage":45.6,"disk_usage":33.3,
   "vcpu":4,"mem_mb":8192,"disk_total_gb":120.0,"disk_count":2,
   "host":"CNA01","cluster":"Cluster01","uuid":"..."}

依赖项（本地 JSONPath 取值，不发起任何外部检查）
  vm.name[…]           名称            $.name
  vm.ip[…]             IP地址          $.ip
  vm.status[…]         状态            $.status_code（带中文值映射）
  vm.cpu[…]            CPU占用率       $.cpu_usage
  vm.mem[…]            内存占用率       $.mem_usage
  vm.disk[…]           磁盘占用率       $.disk_usage
  vm.vcpu[…]           vCPU数          $.vcpu
  vm.mem_mb[…]         内存大小(MB)     $.mem_mb
  vm.disk_total_gb[…]  磁盘总容量(GB)   $.disk_total_gb
  vm.host[…]           所在主机         $.host
```

- 名称 / IP 是**每次采集实时取**的，不是发现时的快照 —— 虚机改名或换 IP，下一轮就更新；
- 指标取不到时（例如虚机没装 Tools），CPU/内存/磁盘三项返回 `-1`，监控项不会变 unsupported；
- 触发器原型基于依赖项：状态不明确(4) WARNING、状态故障(13) HIGH、CPU/内存/磁盘超阈值；
- 图形原型：CPU + 内存 + 磁盘 三线。

### 采集负载评估

External Check 的代价是「一个监控项 = 一次脚本执行 = 一次登录 + 一次查询」。当前设计：

- `--discover vms` 按文档翻页（单页上限 100，**不会只发现前 100 台**），并固定带 `isTemplate=false`，模板不会混进虚机列表；
- 虚机侧已压到 **每台 1 次外部检查**（`vm.info` 主监控项），10 个依赖项不再外呼；
- 即便如此，N 台虚机仍是 N 次 / 2 分钟。规模较大（200 台以上）时建议：
  - 把主监控项周期改成 `5m`；或
  - 改用 Trapper 模式（`--send` 一次运行推完全部虚机，只有一次登录）。

### token 缓存（默认开启）

文档要求在 token 有效期 600 秒内「请勿频繁登录」。脚本把 token 缓存在
`$TMPDIR/fc_token_<hash>.json`（权限 600，按 VRM 地址 + 账号区分）：

- 10 分钟内多次调用只登录一次，显著降低 VRM 压力；
- 命中缓存时**不主动退出登录**（否则会把缓存里的 token 弄失效），token 到期由 VRM 回收；
- token 被吊销（如 VRM 重启）时收到 `10100112`，自动清缓存重登一次并重试原请求；
- 关闭：`[vrm] token_cache = false`（关闭后每次调用都登录，结束时 DELETE 会话）。

## 四、部署步骤

### 方案 A：External Check（推荐，模板默认方式）

1. 把脚本和配置放到 Zabbix Server/Proxy 的 ExternalScripts 目录（以 `zabbix_server.conf` 的 `ExternalScripts=` 为准）：

   ```bash
   cp fusioncompute_monitor.py fusioncompute.conf /usr/lib/zabbix/externalscripts/
   chmod +x /usr/lib/zabbix/externalscripts/fusioncompute_monitor.py
   ```

2. 编辑 `fusioncompute.conf`：填 VRM 地址、账号、密码；`version = 8.1`。

3. 手工验证（**用 zabbix 用户跑**，能同时验证权限问题）：

   ```bash
   cd /usr/lib/zabbix/externalscripts
   sudo -u zabbix python3 fusioncompute_monitor.py --probe
   sudo -u zabbix python3 fusioncompute_monitor.py --discover hosts
   ```

   `--probe` 能一眼看到版本、站点、主机数；`--discover` 能输出 JSON 就说明鉴权、网络、接口都正常。

4. 导入模板：Configuration → Templates → Import，勾 **Create new templates** / **Create new template groups**。

5. 在目标主机（如 `FusionCompute-VRM`）上关联模板即可 —— 模板里的 key 直接写死脚本名
   `fusioncompute_monitor.py`，不需要再配 `{$FC.SCRIPT}` / `{$FC.CONFIG}` 宏。
   若配置文件不在脚本同目录，在该监控项 key 末尾追加两个参数：
   `fusioncompute_monitor.py["--get","host.cpu","--urn","{#HOST.URN}","--config","/path/fusioncompute.conf"]`。

6. **把 `zabbix_server.conf`（或 proxy 的）里 `Timeout` 调到 10**，改完重启 zabbix-server：
   外部检查默认超时很短，脚本每次要登录 + 查询，VRM 慢一点就会超时导致监控项 unsupported。

> External Check 由 **Zabbix Server/Proxy** 执行，脚本必须部署在 Server/Proxy 所在机器，且 Python3 可用。
> 用 Proxy 的话脚本要放在 Proxy 机器上。

### 方案 B：Trapper（zabbix_sender 主动推送，适合网络隔离场景）

1. 脚本部署在能访问 VRM 的任意机器（也可在 Zabbix Server 上）。

2. 编辑 `fusioncompute.conf`：
   - `zabbix.server` = Zabbix Server 或 Proxy 的 IP；
   - `zabbix.host` = 模板挂载的主机名（需与 Zabbix 中主机名一致）。

3. 定时任务（每 1 分钟）：

   ```cron
   * * * * * /usr/bin/python3 /opt/fc/fusioncompute_monitor.py --send >> /var/log/fc_monitor.log 2>&1
   ```

   推送的 key 前缀：`fc.site.*` / `fc.host.*` / `fc.cluster.*` / `fc.datastore.*` /
   `fc.vm.*`（含 `fc.vm.total`、`fc.vm.running.total`、`fc.vm.ip[名称]`、`fc.vm.cpu[名称]` 等）/ `fc.alarm.*`。
   建议先 `--dump` 看一眼待推送内容。

4. Zabbix 前端导入模板，关联到对应主机。Trapper 模式的监控项需自行建（类型选 Zabbix trapper，key 与上面一致）。

### 升级已有环境

```bash
# 1. 覆盖脚本（配置一般不用改）
cp fusioncompute_monitor.py /usr/lib/zabbix/externalscripts/
# 2. 若模板结构有变化（如虚拟机改成主监控项+依赖项），重新导入模板时
#    Configuration → Templates → Import 必须勾 "Delete missing / 删除缺失"，
#    否则旧原型会残留并与新原型重复。勾选后旧的对应监控项会被删除（历史数据一并丢失）。
# 3. 对受影响的发现规则点 "Execute now"，或等下一轮 LLD。
```

## 五、模板说明

模板名 **`FusionCompute_REST_API`**（技术名与可见名一致，避免带空格在触发器表达式里引起歧义）。

监控项两类：

- **External Check**：key = 脚本名 + 命令行参数，Zabbix 实际执行
  `.../externalscripts/fusioncompute_monitor.py --get host.cpu --urn urn:...`
- **Dependent（依赖项）**：`delay: 0`，`master_item` 指向同规则内的主监控项，用 JSONPath 预处理取值。

```
key: 'fusioncompute_monitor.py["--discover","hosts"]'              # LLD
key: 'fusioncompute_monitor.py["--get","host.cpu","--urn","{#HOST.URN}"]'   # 外部检查
key: 'vm.cpu["{#VM.URN}"]'                                          # 依赖项
```

内容：

- **站点 / 告警**：站点状态、站点名称、活动告警总数、1~4 级告警数。
- **虚拟机汇总**：虚拟机总数 / 运行中 / 已停止 / 故障数。
- **主机发现（LLD）**：状态（带中文值映射）、CPU/内存/磁盘占用率、虚拟机数量；状态故障与 CPU/内存/磁盘阈值触发器、CPU+内存图形。
- **虚拟机发现（LLD）**：11 个监控项/台 —— 1 个 `vm.info` 主监控项 + 10 个依赖项（名称、IP、状态、CPU、内存、磁盘、vCPU、内存大小、磁盘总容量、所在主机）；含 5 个触发器原型与 1 个三线图形。
- **集群发现（LLD）**：CPU/磁盘占用率 + CPU 阈值触发器。
- **数据存储发现（LLD）**：占用率 + 阈值触发器。

### 模板文件格式（Zabbix 各版本不兼容）

导入时按 `version` 严格校验，节点名不一致会直接报错：

| 版本 | 模板组节点 | 备注 |
| --- | --- | --- |
| 6.0 / 6.1（本模板） | 根下 `groups:` | 模板内也有 `groups:` |
| 6.2 及以上 | 根下 `template_groups:` | 6.2 起 `groups` 拆成 `host_groups` / `template_groups` |

把 6.2+ 的 `template_groups` 塞进 `version: '6.0'` 的文件，导入报
`标签 "/zabbix_export": 标签 "template_group" 异常`。
反向（6.2+ 导入本模板）一般能接受 `groups`，但节点名不一致时建议用对应版本前端重新导出一次。

**uuid 的两个硬要求**（否则报 `UUIDv4 is expected` 或类型错误）：

- 必须是 **UUIDv4**：32 位十六进制，第 13 位为 `4`、第 17 位为 `8/9/a/b`。
  手写的 `0000...0001` 长度对但不是 v4，会被判非法；生成用
  `python3 -c "import uuid; print(uuid.uuid4().hex)"`。
- 必须**加引号**：`uuid: '...'`，不加引号的纯数字串会被 YAML 解析成整数。

本模板 51 个 uuid 均为 v4 且带引号；uuid 是对象身份，重复导入按它匹配。

### 可调宏

| 宏 | 默认 | 说明 |
| --- | --- | --- |
| `{$FC.HOST.CPU.WARN}` | 85 | 主机 CPU 告警阈值(%) |
| `{$FC.HOST.MEM.WARN}` | 85 | 主机内存告警阈值(%) |
| `{$FC.HOST.DISK.WARN}` | 85 | 主机磁盘告警阈值(%) |
| `{$FC.CLUSTER.CPU.WARN}` | 85 | 集群 CPU 告警阈值(%) |
| `{$FC.DS.DISK.WARN}` | 90 | 数据存储占用率告警阈值(%) |
| `{$FC.VM.CPU.WARN}` | 85 | 虚拟机 CPU 告警阈值(%) |
| `{$FC.VM.MEM.WARN}` | 85 | 虚拟机内存告警阈值(%) |
| `{$FC.VM.DISK.WARN}` | 90 | 虚拟机磁盘告警阈值(%) |

## 六、排障 FAQ

1. **`FC_ERROR: HTTP 400 ... {"errorCode":"10000022","errorDes":"版本号错误"}`**
   接口版本不被 VRM 支持。8.6.0 填 `version = 8.1`（不是 8.6.0），详见
   [Accept 头里的 version](#accept-头里的-version最常踩的坑)。先跑 `--probe` 看实测可用版本。

2. **`{"data": []}`（能登录但发现为空）**
   鉴权通了、站点接口也通了，但「站点 → 主机」返回空列表。脚本已自动处理站点选错与路径拼法两类原因
   （见[站点定位与路径](#站点定位与路径)）。排查顺序：`--probe`（看站点与采用路径、主机数）→ `--debug`（看原始响应）。
   若 `--probe` 里每个站点主机数都是 0，则是 VRM 侧确实没有主机，或该账号看不到主机（换 admin 试）。

3. **导入模板报 `标签 "/zabbix_export": 标签 "template_group" 异常`**
   用了 6.2+ 的 `template_groups` 却声明 `version: '6.0'`。本模板已按 6.0 使用 `groups`；
   若你的 Zabbix 是 6.2/6.4/7.x，把根下 `groups:` 改名成 `template_groups:` 即可。

4. **导入模板报 `无效的参数 "/1/uuid": UUIDv4 is expected`**
   uuid 不是 v4（第 13 位须为 `4`，第 17 位须为 `8/9/a/b`）。重新生成并加引号。

5. **监控项报 `value of type "string" is not suitable for value type "numeric(float)"`**
   VRM 返回了非数字文本（对象不支持该指标 / 小指标 JSON / 值是 `NA`）。
   脚本现在统一规整，转不出来时输出 `ZBX_NOTSUPPORTED 返回值不是数字：'NA'...`。
   数据存储的占用率已改为用列表容量字段计算，不再走 monitors。

6. **虚拟机 CPU / 内存 / 磁盘显示「已停用」**
   说明导入的是**旧的模板版本**（早期版本为了控制外部检查次数，把 CPU/内存原型设成了 DISABLED，
   且当时还没有磁盘占用率）。当前模板里 11 个虚拟项全部为 ENABLED，重新导入即可
   （重导时勾 **Delete missing**，导入后对发现规则执行 Execute now）。
   不想重导也可以手工：在发现规则的 Item prototypes 里启用 CPU/内存，再按
   [虚拟机为什么只有 1 次外部检查](#虚拟机为什么只有-1-次外部检查) 里的字段补一个 `vm.disk` 依赖项。

7. **导入后监控项显示「不支持 / Not supported」**
   多半是外部检查没跑起来：脚本不在 ExternalScripts 目录、没有执行权限（`chmod +x`）、
   或 `Timeout` 太小。用 zabbix 用户手工验证：
   `sudo -u zabbix /usr/lib/zabbix/externalscripts/fusioncompute_monitor.py --discover hosts`。

8. **登录失败 / 401**
   检查账号密码、`X-Auth-UserType`（本地账户为 0）。脚本已按文档用 SHA-256 加密密码，无需预加密。
   密码错会立刻报 `10100103`，不会反复重试。

9. **证书错误**
   VRM 默认自签名证书，`verify = false` 已关闭校验；如需严格校验，把 `verify` 填成 CA 证书文件路径。

10. **某项指标长期为空 / 无数据**
    确认该对象的指标名在《监控指标列表 01.xlsx》里存在：数据存储**完全没有**指标，
    虚拟机以 `cpu_usage` / `mem_usage` / `disk_usage` 等为准。用 `--debug` 看 VRM 原始响应，
    或用 `--get <kind> --urn <urn>` 单独验证。

11. **接口频率**
    文档建议每分钟接口调用控制在 **300 次以内**。开启 token 缓存后，N 台虚机每轮只需 1 次登录 + N 次查询。

## 七、变更记录

对接过程中修掉的问题（都已在当前版本中生效）：

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| `10000022 版本号错误` | 把产品版本 8.6.0 当成接口版本 | 改为 8.1，并支持 `auto` 自动协商 + 失败回退 |
| `{"data": []}` 发现为空 | `isSelf` 字符串真值 / 站点路径用 urn | 严格判定 isSelf、站点与路径多候选 + 跨站点回退 |
| 导入报 `template_group` 异常 | 6.2+ 模板格式导入 6.0 | 根节点改 `groups`，整理成 6.0 结构 |
| 导入报 `UUIDv4 is expected` | uuid 只是 32 位十六进制，非 v4 | 51 个 uuid 全部重生成为 v4 并加引号 |
| 监控项 key 与脚本命令行不符 | key 写的是 `fc.host.cpu[...]`，脚本不认 | key 统一为「脚本名 + 真实命令行参数」 |
| 数据存储项 `numeric(float)` 类型错 | 文档里没有数据存储指标 | 改用列表接口 `usedSizeGB/actualCapacityGB` 计算 |
| 虚机信息项太多、负载高 | 每字段一个外部检查 | 改为 `vm.info` 主监控项 + 10 个依赖项 |
| 虚机 CPU/内存「已停用」 | 早期模板为控负载设为 DISABLED | 当前版本全部 ENABLED，并新增磁盘占用率 |
