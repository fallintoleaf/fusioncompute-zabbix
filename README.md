# FusionCompute 8.6.0 → Zabbix 6.0 监控方案

本方案通过华为 FusionCompute VRM 的 REST API，采集站点、主机（CNA）、集群、数据存储、告警等监控数据，接入 Zabbix 6.0。

## 文件清单

| 文件 | 说明 |
| --- | --- |
| `fusioncompute_monitor.py` | 采集脚本（Python 3 标准库，无第三方依赖） |
| `fusioncompute.conf` | 配置文件（VRM 地址/账号/密码、Zabbix 参数） |
| `zbx_fusioncompute_template.yaml` | Zabbix 6.0 模板（可直接导入） |

## 一、工作原理

依据《FusionCompute 8.6.0 VRM接口文档 03》，关键接口如下（`<ip>:7443`，仅 HTTPS）：

| 功能 | 方法 + 路径 |
| --- | --- |
| 登录（获取 Token） | `POST /service/session`，头 `X-Auth-User` + `X-Auth-Key`（密码 SHA-256） |
| 站点列表 | `GET /service/sites` |
| 主机列表 | `GET /service/sites/{site_id}/hosts` |
| 集群列表 | `GET /service/sites/{site_id}/clusters` |
| 数据存储 | `GET /service/sites/{site_id}/datastores` |
| 实时监控指标 | `POST /service/sites/{site_id}/monitors/objectmetric-realtimedata` |
| 活动告警 | `POST /service/sites/{site_id}/alarms/activeAlarms` |

> Token 有效期 **600 秒（10 分钟）**，脚本每次调用都会重新登录，不缓存 Token，避免过期问题。

## 二、脚本用法

```bash
# 1. 自动发现（Zabbix LLD 用，输出 JSON）
python fusioncompute_monitor.py --discover hosts
python fusioncompute_monitor.py --discover clusters
python fusioncompute_monitor.py --discover datastores

# 2. 取单个指标（External Check 用）
python fusioncompute_monitor.py --get host.cpu --urn "urn:sites:xxx:hosts:1"

# 3. 批量采集并推送（Trapper 模式，调用 zabbix_sender）
python fusioncompute_monitor.py --send
```

`--get` 支持的指标类型（对应脚本内映射）：

| 参数 | 含义 | metricId |
| --- | --- | --- |
| `host.cpu` / `host.mem` / `host.disk` | 主机 CPU/内存/磁盘占用率 | cpu_usage / mem_usage / logic_disk_usage |
| `host.net_in` / `host.net_out` | 主机网络进出流速 | nic_byte_in / nic_byte_out |
| `host.dom0_cpu` / `host.dom0_mem` | Dom0 CPU/内存占用率 | dom0_cpu_usage / dom0_mem_usage |
| `cluster.cpu` / `cluster.disk` | 集群 CPU/磁盘占用率 | cpu_usage / logic_disk_usage |
| `ds.disk` / `ds.io_in` / `ds.io_out` | 数据存储占用率/IO | disk_usage / disk_io_in / disk_io_out |

## 三、部署步骤

### 方案 A：External Check（推荐，模板默认方式）

1. 将脚本和配置放到 Zabbix Server 的 ExternalScripts 目录（默认 `/usr/lib/zabbix/externalscripts/` 或 `/etc/zabbix/externalscripts/`，以 `zabbix_server.conf` 的 `ExternalScripts=` 为准）：

   ```bash
   cp fusioncompute_monitor.py fusioncompute.conf /usr/lib/zabbix/externalscripts/
   chmod +x /usr/lib/zabbix/externalscripts/fusioncompute_monitor.py
   ```

2. 编辑 `fusioncompute.conf`，填入 VRM 地址、账号、密码。

3. 测试：

   ```bash
   cd /usr/lib/zabbix/externalscripts
   python3 fusioncompute_monitor.py --discover hosts
   ```

   确认能输出 JSON（说明鉴权、网络、接口均正常）。

4. Zabbix 前端导入模板 `zbx_fusioncompute_template.yaml`（Configuration → Templates → Import）。

5. 在目标主机（如 `FusionCompute-VRM` 这台虚机/服务器）上：
   - 关联刚导入的模板；
   - 设置宏 `{$FC.SCRIPT}` = `/usr/lib/zabbix/externalscripts/fusioncompute_monitor.py`；
   - 设置宏 `{$FC.CONFIG}` = `/usr/lib/zabbix/externalscripts/fusioncompute.conf`。

> 注意：External Check 由 **Zabbix Server/Proxy** 执行，脚本必须部署在 Server/Proxy 所在机器，且 Python3 可用。若使用 Proxy，脚本需放在 Proxy 机器上。

### 方案 B：Trapper（zabbix_sender 主动推送，适合网络隔离场景）

1. 脚本部署在能访问 VRM 的任意机器（也可在 Zabbix Server 上）。

2. 编辑 `fusioncompute.conf`：
   - `zabbix.server` = Zabbix Server 或 Proxy 的 IP；
   - `zabbix.host` = 模板挂载的主机名（需与 Zabbix 中主机名一致）。

3. 定时任务（每 1 分钟）：

   ```cron
   * * * * * /usr/bin/python3 /opt/fc/fusioncompute_monitor.py --send >> /var/log/fc_monitor.log 2>&1
   ```

4. Zabbix 前端导入模板，关联到对应主机。

## 四、模板说明

模板 `FusionCompute 8.6 by REST API` 包含：

- **站点**：站点状态、活动告警总数、紧急/重要/次要告警数。
- **主机发现（LLD）**：自动发现所有 CNA 主机，生成 CPU/内存/磁盘占用率、状态、虚拟机数量监控项，含 CPU/内存/磁盘/状态触发器与图形。
- **集群发现（LLD）**：自动发现集群，监控 CPU/磁盘占用率。
- **数据存储发现（LLD）**：自动发现数据存储，监控占用率。

可调宏（阈值）：

| 宏 | 默认 | 说明 |
| --- | --- | --- |
| `{$FC.HOST.CPU.WARN}` | 85 | 主机 CPU 告警阈值 |
| `{$FC.HOST.MEM.WARN}` | 85 | 主机内存告警阈值 |
| `{$FC.HOST.DISK.WARN}` | 85 | 主机磁盘告警阈值 |
| `{$FC.CLUSTER.CPU.WARN}` | 85 | 集群 CPU 告警阈值 |
| `{$FC.DS.DISK.WARN}` | 90 | 数据存储告警阈值 |

## 五、常见问题

1. **登录失败 / 401**：检查账号密码、`X-Auth-UserType`（本地账户为 0）。脚本已按文档用 SHA-256 加密密码，无需预加密。
2. **证书错误**：VRM 默认自签名证书，配置 `verify = false` 已关闭校验；如需严格校验请填入证书路径。
3. **无数据 / 空输出**：确认 site 是否存在且 `isSelf`，或站点下有主机；确认指标 metricId 正确（参考 `监控指标列表 01.xlsx`）。
4. **接口频率**：文档建议每分钟接口调用控制在 **300 次以内**，本脚本单次采集一次登录 + 若干查询，正常远低于该限制。
