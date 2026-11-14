#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FusionCompute 8.6.0 Zabbix 6.0 监控采集脚本

功能：
    通过华为 FusionCompute VRM REST API 采集站点/主机/集群/存储/告警等监控数据，
    以 Zabbix External Check 或 Trapper 方式输出。

依赖：
    仅使用 Python 3 标准库（urllib / json / hashlib / argparse），无需第三方库。

鉴权说明（依据《FusionCompute 8.6.0 VRM接口文档 03》）：
    - 登录接口：POST /service/session
    - 请求头：X-Auth-User（用户名）、X-Auth-Key（密码的 SHA-256 十六进制密文）
    - 响应头 X-Auth-Token 为鉴权标识，有效期 600000ms（10 分钟），请勿频繁登录
    - 其余接口需携带头：X-Auth-Token、Accept: application/json;version=<version>
    - VRM 端口：7443，仅支持 HTTPS

使用方式：
    1) 一次性发现（Low-Level Discovery），供 Zabbix 自动发现主机/集群/存储：
        python fusioncompute_monitor.py --discover hosts
        python fusioncompute_monitor.py --discover clusters
        python fusioncompute_monitor.py --discover datastores

    2) 取单个指标（External Check，key 由 Zabbix 传入）：
        python fusioncompute_monitor.py --get host.cpu --urn "urn:sites:xxx:hosts:1"
        python fusioncompute_monitor.py --get cluster.mem --urn "urn:sites:xxx:clusters:1"

    3) 批量采集并推送（Trapper 模式，配合 zabbix_sender）：
        python fusioncompute_monitor.py --send

配置文件：
    默认读取同目录 fusioncompute.conf，也可用 --config 指定。
"""

import argparse
import configparser
import hashlib
import json
import os
import ssl
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 常量：FusionCompute 8.6.0 REST 接口定义（依据官方接口文档）
# ---------------------------------------------------------------------------
API_PORT = 7443
API_VERSION = "8.6.0"            # Accept 头中的 version，可按需改为 "1.0" 等
SESSION_TIMEOUT = 600            # token 有效期 600 秒

# URI 模板（base_uri = /service）
URI_VERSIONS = "/service/versions"
URI_SESSION = "/service/session"
URI_SITES = "/service/sites"
URI_HOSTS = "/service/sites/{site_id}/hosts"
URI_CLUSTERS = "/service/sites/{site_id}/clusters"
URI_DATASTORES = "/service/sites/{site_id}/datastores"
URI_ALARMS = "/service/sites/{site_id}/alarms/activeAlarms"
URI_MONITOR_REALTIME = "/service/sites/{site_id}/monitors/objectmetric-realtimedata"

# 主机状态 -> 数字（供 Zabbix 数值型监控/触发器使用）
HOST_STATUS_MAP = {
    "normal": 0,
    "initial": 1,
    "fault": 2,
    "unknow": 3,
    "unknown": 3,
    "poweroff": 4,
    "booting": 5,
    "shutdowning": 6,
    "rebooting": 7,
}

# 默认监控的指标 metricId（依据《监控指标列表 01.xlsx》）
DEFAULT_HOST_METRICS = ["cpu_usage", "mem_usage", "logic_disk_usage",
                        "nic_byte_in", "nic_byte_out",
                        "dom0_cpu_usage", "dom0_mem_usage"]
DEFAULT_CLUSTER_METRICS = ["cpu_usage", "logic_disk_usage",
                           "nic_byte_in_usage", "nic_byte_out_usage"]
DEFAULT_DS_METRICS = ["disk_usage", "disk_io_in", "disk_io_out"]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
class FCError(Exception):
    """FusionCompute 接口错误。"""
    pass


def sha256_hex(text):
    """按文档要求对密码做 SHA-256 加密，返回十六进制小写密文。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FusionComputeClient:
    """FusionCompute VRM REST 客户端，负责登录、鉴权与请求。"""

    def __init__(self, host, username, password, port=API_PORT, version=API_VERSION,
                 timeout=10, verify=False):
        self.base = "https://%s:%d" % (host, port)
        self.username = username
        self.password = password
        self.version = version
        self.timeout = timeout
        self.token = None
        # VRM 默认使用自签名证书，默认关闭校验；如需校验请将 verify 设为证书路径
        self.ctx = ssl.create_default_context()
        if not verify:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    # -- 底层请求 ----------------------------------------------------------
    def _request(self, method, path, body=None, headers=None):
        url = self.base + path
        h = {
            "Accept": "application/json;version=%s;charset=UTF-8" % self.version,
            "Content-Type": "application/json;charset=UTF-8",
        }
        if headers:
            h.update(headers)
        if self.token:
            h["X-Auth-Token"] = self.token

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            raise FCError("HTTP %d %s: %s" % (e.code, e.reason, detail))
        except urllib.error.URLError as e:
            raise FCError("连接失败 %s: %s" % (url, e.reason))

        resp_headers = dict(resp.headers)
        raw = resp.read().decode("utf-8", errors="ignore")
        return resp_headers, (json.loads(raw) if raw.strip() else {})

    # -- 登录 --------------------------------------------------------------
    def login(self):
        h = {
            "X-Auth-User": self.username,
            "X-Auth-Key": sha256_hex(self.password),
            "X-Auth-UserType": "0",          # 0: 本地账户
            "X-Auth-AuthType": "0",          # 0: 本地认证
            "X-ENCRYPT-ALGORITHM": "0",      # 0: SHA-256 加密
            "Accept-Language": "zh_CN",
        }
        headers, body = self._request("POST", URI_SESSION, headers=h)
        token = headers.get("X-Auth-Token") or headers.get("x-auth-token")
        if not token:
            raise FCError("登录失败：响应头缺少 X-Auth-Token，body=%s" % body)
        self.token = token
        return body

    def logout(self):
        if self.token:
            try:
                self._request("DELETE", URI_SESSION)
            except FCError:
                pass
            self.token = None

    # -- 业务接口 ----------------------------------------------------------
    def get_sites(self):
        _, body = self._request("GET", URI_SITES)
        return body.get("sites", [])

    def get_hosts(self, site_id):
        _, body = self._request("GET", URI_HOSTS.format(site_id=site_id))
        return body.get("hosts", [])

    def get_clusters(self, site_id):
        _, body = self._request("GET", URI_CLUSTERS.format(site_id=site_id))
        return body.get("clusters", [])

    def get_datastores(self, site_id):
        _, body = self._request("GET", URI_DATASTORES.format(site_id=site_id))
        return body.get("datastores", [])

    def get_active_alarms(self, site_id):
        _, body = self._request("POST", URI_ALARMS.format(site_id=site_id), body={})
        return body

    def get_realtime_metrics(self, site_id, objects):
        """批量查询对象实时指标。

        objects: [{"urn": "...", "metricId": ["cpu_usage", ...]}, ...]
        返回 {urn: {metricId: metricValue}}
        """
        _, body = self._request(
            "POST", URI_MONITOR_REALTIME.format(site_id=site_id), body=objects)
        result = {}
        for item in body.get("items", []):
            urn = item.get("urn")
            vals = {}
            for v in item.get("value", []):
                vals[v.get("metricId")] = v.get("metricValue")
            result[urn] = vals
        return result


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config(path):
    cfg = configparser.ConfigParser()
    if not os.path.exists(path):
        print("配置文件不存在：%s" % path, file=sys.stderr)
        sys.exit(2)
    cfg.read(path, encoding="utf-8")
    return cfg


def build_client(cfg):
    return FusionComputeClient(
        host=cfg.get("vrm", "host"),
        username=cfg.get("vrm", "username"),
        password=cfg.get("vrm", "password"),
        port=cfg.getint("vrm", "port", fallback=API_PORT),
        version=cfg.get("vrm", "version", fallback=API_VERSION),
        timeout=cfg.getint("vrm", "timeout", fallback=10),
        verify=cfg.getboolean("vrm", "verify", fallback=False),
    )


# ---------------------------------------------------------------------------
# 发现（Low-Level Discovery）
# ---------------------------------------------------------------------------
def discover(cfg, dtype):
    client = build_client(cfg)
    client.login()
    sites = client.get_sites()
    # 取当前站点（isSelf）或第一个站点
    site_id = None
    for s in sites:
        if s.get("isSelf"):
            site_id = s.get("urn")
            break
    if not site_id and sites:
        site_id = sites[0].get("urn")
    if not site_id:
        raise FCError("未找到可用站点")

    data = []
    try:
        if dtype == "hosts":
            items = client.get_hosts(site_id)
            for h in items:
                data.append({
                    "{#HOST.NAME}": h.get("name", ""),
                    "{#HOST.URN}": h.get("urn", ""),
                    "{#HOST.IP}": h.get("ip", ""),
                    "{#HOST.CLUSTER}": h.get("clusterName", ""),
                })
        elif dtype == "clusters":
            items = client.get_clusters(site_id)
            for c in items:
                data.append({
                    "{#CLUSTER.NAME}": c.get("name", ""),
                    "{#CLUSTER.URN}": c.get("urn", ""),
                })
        elif dtype == "datastores":
            items = client.get_datastores(site_id)
            for d in items:
                data.append({
                    "{#DATASTORE.NAME}": d.get("name", ""),
                    "{#DATASTORE.URN}": d.get("urn", ""),
                })
        else:
            raise FCError("未知发现类型：%s" % dtype)
    finally:
        client.logout()

    print(json.dumps({"data": data}, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 取单个指标（External Check）
# ---------------------------------------------------------------------------
# metric 类型 -> (接口调用, metricId)
def _resolve_metric(kind):
    # kind 形如 host.cpu / cluster.mem / ds.disk
    obj, _, name = kind.partition(".")
    mapping = {
        "host": {
            "cpu": "cpu_usage", "mem": "mem_usage", "disk": "logic_disk_usage",
            "net_in": "nic_byte_in", "net_out": "nic_byte_out",
            "dom0_cpu": "dom0_cpu_usage", "dom0_mem": "dom0_mem_usage",
        },
        "cluster": {
            "cpu": "cpu_usage", "disk": "logic_disk_usage",
            "net_in": "nic_byte_in_usage", "net_out": "nic_byte_out_usage",
        },
        "ds": {
            "disk": "disk_usage", "io_in": "disk_io_in", "io_out": "disk_io_out",
        },
    }
    if obj not in mapping or name not in mapping[obj]:
        raise FCError("未知指标类型：%s" % kind)
    return obj, mapping[obj][name]


def get_metric(cfg, kind, urn):
    client = build_client(cfg)
    client.login()
    try:
        sites = client.get_sites()
        site_id = None
        for s in sites:
            if s.get("isSelf"):
                site_id = s.get("urn")
                break
        if not site_id and sites:
            site_id = sites[0].get("urn")
        if not site_id:
            raise FCError("未找到可用站点")

        obj, metric_id = _resolve_metric(kind)
        result = client.get_realtime_metrics(
            site_id, [{"urn": urn, "metricId": [metric_id]}])
        vals = result.get(urn, {})
        val = vals.get(metric_id)
        if val is None:
            # Zabbix 外部检查约定：无数据输出空行（或 ZBX_NOTSUPPORTED）
            print("")
        else:
            print(val)
    finally:
        client.logout()


# ---------------------------------------------------------------------------
# Trapper 批量采集
# ---------------------------------------------------------------------------
def collect_and_format(cfg, host_name):
    """采集并返回 [(zbx_host, zbx_key, value), ...]，供 zabbix_sender 使用。"""
    client = build_client(cfg)
    client.login()
    rows = []
    try:
        sites = client.get_sites()
        site_id = None
        for s in sites:
            if s.get("isSelf"):
                site_id = s.get("urn")
                break
        if not site_id and sites:
            site_id = sites[0].get("urn")
        if not site_id:
            raise FCError("未找到可用站点")

        # 站点状态
        for s in sites:
            rows.append((host_name, "fc.site.status", s.get("status", "")))
            rows.append((host_name, "fc.site.name", s.get("name", "")))

        # 主机
        hosts = client.get_hosts(site_id)
        host_urns = []
        for h in hosts:
            urn = h.get("urn")
            host_urns.append(urn)
            rows.append((host_name, "fc.host.status[%s]" % h.get("name"),
                         HOST_STATUS_MAP.get(h.get("status"), -1)))
            # 主机列表接口直接返回占用率
            for key, val in (("cpu", h.get("cpuUsage")), ("mem", h.get("memUsage")),
                             ("disk", h.get("logicDiskUsage"))):
                if val is not None:
                    rows.append((host_name, "fc.host.%s[%s]" % (key, h.get("name")), val))
            rows.append((host_name, "fc.host.vmcount[%s]" % h.get("name"),
                         h.get("vmCount", 0)))

        # 主机实时指标（dom0 等列表接口不返回的指标）
        if host_urns:
            metrics = client.get_realtime_metrics(
                site_id, [{"urn": u, "metricId": DEFAULT_HOST_METRICS} for u in host_urns])
            name_map = {h.get("urn"): h.get("name") for h in hosts}
            for urn, vals in metrics.items():
                nm = name_map.get(urn, urn)
                for mid, mval in vals.items():
                    if mval is not None:
                        rows.append((host_name, "fc.host.%s[%s]" % (mid, nm), mval))

        # 集群
        clusters = client.get_clusters(site_id)
        cluster_urns = []
        for c in clusters:
            urn = c.get("urn")
            cluster_urns.append(urn)
        if cluster_urns:
            metrics = client.get_realtime_metrics(
                site_id, [{"urn": u, "metricId": DEFAULT_CLUSTER_METRICS} for u in cluster_urns])
            name_map = {c.get("urn"): c.get("name") for c in clusters}
            for urn, vals in metrics.items():
                nm = name_map.get(urn, urn)
                for mid, mval in vals.items():
                    if mval is not None:
                        rows.append((host_name, "fc.cluster.%s[%s]" % (mid, nm), mval))

        # 数据存储
        datastores = client.get_datastores(site_id)
        ds_urns = []
        for d in datastores:
            urn = d.get("urn")
            ds_urns.append(urn)
        if ds_urns:
            metrics = client.get_realtime_metrics(
                site_id, [{"urn": u, "metricId": DEFAULT_DS_METRICS} for u in ds_urns])
            name_map = {d.get("urn"): d.get("name") for d in datastores}
            for urn, vals in metrics.items():
                nm = name_map.get(urn, urn)
                for mid, mval in vals.items():
                    if mval is not None:
                        rows.append((host_name, "fc.datastore.%s[%s]" % (mid, nm), mval))

        # 活动告警汇总
        alarms = client.get_active_alarms(site_id)
        total = alarms.get("total", 0)
        rows.append((host_name, "fc.alarm.total", total))
        # 按级别统计（1紧急 2重要 3次要 4提示）
        level_count = {"1": 0, "2": 0, "3": 0, "4": 0}
        for a in alarms.get("items", []):
            lv = str(a.get("iAlarmLevel", ""))
            if lv in level_count:
                level_count[lv] += 1
        for lv, cnt in level_count.items():
            rows.append((host_name, "fc.alarm.level[%s]" % lv, cnt))

    finally:
        client.logout()
    return rows


def send_trapper(cfg):
    """生成 zabbix_sender 输入并调用 zabbix_sender 推送。"""
    zbx_server = cfg.get("zabbix", "server", fallback=None)
    zbx_host = cfg.get("zabbix", "host", fallback=None)
    zabbix_sender = cfg.get("zabbix", "sender_bin", fallback="zabbix_sender")
    if not zbx_host:
        print("配置缺少 zabbix.host", file=sys.stderr)
        sys.exit(2)

    rows = collect_and_format(cfg, zbx_host)
    if not rows:
        print("未采集到数据", file=sys.stderr)
        return

    # 生成输入行：<host> <key> <value>
    lines = ["%s %s %s" % (h, k, v) for h, k, v in rows]
    input_data = "\n".join(lines)

    if zbx_server:
        cmd = [zabbix_sender, "-z", zbx_server, "-s", zbx_host, "-i", "-"]
    else:
        # 使用 agent 配置文件（主动 agent）
        cmd = [zabbix_sender, "-c",
               cfg.get("zabbix", "agent_conf", fallback="/etc/zabbix/zabbix_agentd.conf"),
               "-i", "-"]

    import subprocess
    proc = subprocess.run(cmd, input=input_data, capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="FusionCompute Zabbix 监控采集")
    p.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fusioncompute.conf"))
    p.add_argument("--discover", choices=["hosts", "clusters", "datastores"],
                   help="输出 LLD JSON")
    p.add_argument("--get", help="取单个指标，如 host.cpu")
    p.add_argument("--urn", help="配合 --get 使用，对象 urn")
    p.add_argument("--send", action="store_true", help="Trapper 批量采集并推送")
    args = p.parse_args()

    cfg = load_config(args.config)

    try:
        if args.discover:
            discover(cfg, args.discover)
        elif args.get:
            if not args.urn:
                print("--get 需要同时提供 --urn", file=sys.stderr)
                sys.exit(2)
            get_metric(cfg, args.get, args.urn)
        elif args.send:
            send_trapper(cfg)
        else:
            p.print_help()
            sys.exit(1)
    except FCError as e:
        print("FC_ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
