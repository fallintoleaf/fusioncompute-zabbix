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

版本号说明（重要，对应 HTTP 400 / errorCode 10000022「版本号错误」）：
    Accept 头里的 version 是「接口版本」，不是产品版本。按接口文档 1.2 的对应关系：

        接口版本 6.3  ->  FusionCompute 6.3.0 / 6.3.1
        接口版本 6.5  ->  FusionCompute 6.5.0 / 6.5.1
        接口版本 8.0  ->  FusionCompute 8.0.0 / 8.0.0.SPC1 / 8.0.1
        接口版本 8.1  ->  FusionCompute 8.1.0 或更高版本

    所以 8.6.0 的局点要传 8.1（或 8.0），传 8.6.0 会被 VRM 直接拒绝：
        HTTP 400 {"errorCode":"10000022","errorDes":"版本号错误"}

    本脚本的 version 支持 auto（推荐）：先 GET /service/versions 取服务端支持的版本列表，
    再逐个尝试登录，用第一个被接受的版本，避免手工猜。

使用方式：
    1) 一次性发现（Low-Level Discovery），供 Zabbix 自动发现主机/虚拟机/集群/存储：
        python fusioncompute_monitor.py --discover hosts
        python fusioncompute_monitor.py --discover vms
        python fusioncompute_monitor.py --discover clusters
        python fusioncompute_monitor.py --discover datastores

    2) 取单个指标（External Check，key 由 Zabbix 传入）：
        python fusioncompute_monitor.py --get host.cpu --urn "urn:sites:xxx:hosts:1"
        python fusioncompute_monitor.py --get vm.status --urn "urn:sites:xxx:vms:i-00000002"
        python fusioncompute_monitor.py --get vm.cpu --urn "urn:sites:xxx:vms:i-00000002"
        python fusioncompute_monitor.py --get cluster.mem --urn "urn:sites:xxx:clusters:1"
        python fusioncompute_monitor.py --get vm.count          # 不需要 --urn

    3) 批量采集并推送（Trapper 模式，配合 zabbix_sender）：
        python fusioncompute_monitor.py --send

    4) 排障：
        python fusioncompute_monitor.py --probe      # 打印服务端支持的版本并逐个试登录
        python fusioncompute_monitor.py --dump       # 打印将要推送给 zabbix_sender 的行

配置文件：
    默认读取同目录 fusioncompute.conf，也可用 --config 指定。
"""

import argparse
import configparser
import hashlib
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# 常量：FusionCompute REST 接口定义（依据官方接口文档）
# ---------------------------------------------------------------------------
API_PORT = 7443
API_VERSION_AUTO = "auto"

# 候选接口版本，按「新版本优先」排列。
# 依据《VRM接口文档 03》1.2 接口文档版本与产品对应关系：
#   8.1 -> FusionCompute 8.1.0 或更高版本（8.2/8.6/8.8 等都用 8.1）
#   8.0 -> 8.0.0/8.0.0.SPC1/8.0.1；6.5 -> 6.5.0/6.5.1；6.3 -> 6.3.0/6.3.1
#   1.0 -> 不携带 version 时的默认版本
API_VERSION_CANDIDATES = ["8.1", "8.0", "6.5", "6.3", "1.0"]

# 版本号错误（HTTP 400）：说明 Accept 头里的 version 服务端不支持
ERR_VERSION = "10000022"

SESSION_TIMEOUT = 600            # token 有效期 600 秒

# 全局调试开关（--debug 或配置文件 [vrm] debug = true），只影响 stderr 输出
_DEBUG = False

# URI 模板（basic_uri = /service）
URI_VERSIONS = "/service/versions"
URI_SESSION = "/service/session"
URI_SITES = "/service/sites"

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

# 虚拟机状态 -> 数字（依据接口文档 3.4「查询虚拟机」status 取值范围）
# 0 运行中，1 已停止，2 已休眠，3 已暂停，4 不明确，5 创建中，6 启动中，7 停止中，
# 8 删除中，9 迁移中，10 故障恢复中，11 休眠中，12 回收中，13 故障
VM_STATUS_MAP = {
    "running": 0,
    "stopped": 1,
    "hibernated": 2,
    "pause": 3,
    "unknown": 4,
    "creating": 5,
    "starting": 6,
    "stopping": 7,
    "shutting-down": 8,
    "migrating": 9,
    "fault-resuming": 10,
    "hibernating": 11,
    "recycling": 12,
    "fault": 13,
}

# 虚拟机数量统计：/vms/statistics 返回 total/running/stopped/fault/hibernated
VM_STAT_KEYS = {
    "vm.count": "total",
    "vm.running": "running",
    "vm.stopped": "stopped",
    "vm.fault": "fault",
    "vm.hibernated": "hibernated",
}

# 默认监控的指标 metricId（依据《监控指标列表 01.xlsx》）
DEFAULT_HOST_METRICS = ["cpu_usage", "mem_usage", "logic_disk_usage",
                        "nic_byte_in", "nic_byte_out",
                        "dom0_cpu_usage", "dom0_mem_usage"]
DEFAULT_CLUSTER_METRICS = ["cpu_usage", "logic_disk_usage",
                           "nic_byte_in_usage", "nic_byte_out_usage"]
# 注意：《监控指标列表》里没有「数据存储」这一类指标，datastores 用不了
# monitors 接口（传 disk_usage 等会给回非数字文本）。数据存储的容量/占用率
# 一律取自 GET /sites/{id}/datastores 返回的 usedSizeGB / actualCapacityGB。


# ---------------------------------------------------------------------------
# 数值处理
#
# 真实 VRM 的 metricValue 是字符串：可能是 "1.65"、"45 %"，小指标是 JSON 字符串，
# 对象不支持该指标时还可能给出 "NA" 之类的非数字文本。
# 直接原样输出会让 Zabbix 报 "value of type string is not suitable for numeric(float)"，
# 所以这里统一做一次规整，转不出来时输出 ZBX_NOTSUPPORTED 并带上原始值和原因。
# ---------------------------------------------------------------------------
def _first_number(obj):
    if isinstance(obj, dict):
        for key in ("value", "metricValue", "usage", "percent", "data"):
            if key in obj:
                return obj[key]
        for v in obj.values():
            got = _first_number(v)
            if got is not None:
                return got
        return None
    if isinstance(obj, list):
        for v in obj:
            got = _first_number(v)
            if got is not None:
                return got
        return None
    return obj


def _numeric(value):
    """把返回值转成 float；不是数字则返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    if s[0] in "{[":
        try:
            return _numeric(_first_number(json.loads(s)))
        except ValueError:
            return None
    s = s.replace("%", " ").replace(",", "").strip()
    m = re.match(r"^[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", s)
    return float(m.group(0)) if m else None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
class FCError(Exception):
    """FusionCompute 接口错误。"""
    pass


def sha256_hex(text):
    """按文档要求对密码做 SHA-256 加密，返回十六进制小写密文。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def log(msg):
    """诊断信息一律走 stderr，保证 stdout 只有 Zabbix 需要的数据。"""
    print(msg, file=sys.stderr)


def _version_forms(v):
    """把版本号的各种写法都补全：8.1 / v8.1 都试一遍。"""
    if v is None:
        return []
    v = str(v).strip()
    if not v:
        return []
    forms = [v]
    if v[:1] in ("v", "V"):
        forms.append(v[1:])
    else:
        forms.append("v" + v)
    return forms


class FusionComputeClient:
    """FusionCompute VRM REST 客户端，负责登录、鉴权与请求。"""

    def __init__(self, host, username, password, port=API_PORT, version=API_VERSION_AUTO,
                 timeout=10, verify=False, scheme="https", debug=False, token_cache=True):
        self.base = "%s://%s:%d" % (scheme, host, port)
        self.username = username
        self.password = password
        # None 表示不带 version；"auto" 表示尚未协商
        self.version = None if version in (None, "", "none") else str(version)
        self.configured_version = self.version
        self.timeout = timeout
        self.token = None
        self.scheme = scheme
        self.debug = debug
        # token 缓存：VM 监控会让外部检查次数随虚机数量放大，
        # 复用有效 token 可把「每次调用都登录」降为「10 分钟内登录一次」
        self.token_cache = token_cache
        self._cache_file = None
        self._used_cached_token = False
        self._login_retry = False
        self._validity = SESSION_TIMEOUT

        # VRM 默认使用自签名证书，默认关闭校验；verify 也可填 CA 证书路径
        self.ctx = ssl.create_default_context()
        if not verify:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        elif isinstance(verify, str) and verify.lower() not in ("true", "yes", "1"):
            self.ctx.load_verify_locations(cafile=verify)

    # -- token 缓存 --------------------------------------------------------
    def _cache_path(self):
        if self._cache_file:
            return self._cache_file
        digest = hashlib.sha1(("%s|%s" % (self.base, self.username)).encode("utf-8")).hexdigest()[:16]
        self._cache_file = os.path.join(tempfile.gettempdir(), "fc_token_%s.json" % digest)
        return self._cache_file

    def _cache_load(self):
        if not self.token_cache:
            return None
        try:
            with open(self._cache_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if data.get("base") != self.base or not data.get("token"):
            return None
        try:
            if float(data.get("expires", 0)) - time.time() < 60:   # 留 60 秒余量
                return None
        except (TypeError, ValueError):
            return None
        return data

    def _cache_save(self):
        if not self.token_cache or not self.token:
            return
        data = {"base": self.base, "version": self.version, "token": self.token,
                "expires": time.time() + max(self._validity - 30, 60)}
        path = self._cache_path()
        tmp = "%s.%d.tmp" % (path, os.getpid())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        except OSError as e:
            if self.debug:
                log("DEBUG token 缓存写入失败：%s" % e)

    def _cache_drop(self):
        if not self.token_cache:
            return
        try:
            os.remove(self._cache_path())
        except OSError:
            pass

    # -- 底层请求 ----------------------------------------------------------
    def request(self, method, path, body=None, headers=None, with_version=True):
        url = self.base + path
        h = {"Content-Type": "application/json;charset=UTF-8"}
        if with_version and self.version:
            h["Accept"] = "application/json;version=%s;charset=UTF-8" % self.version
        else:
            h["Accept"] = "application/json;charset=UTF-8"
        if headers:
            h.update(headers)
        if self.token:
            h["X-Auth-Token"] = self.token

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        if self.debug:
            log("DEBUG -> %s %s | Accept: %s | token: %s | body: %s"
                % (method, path, h.get("Accept"), bool(self.token),
                   json.dumps(body, ensure_ascii=False) if body is not None else "-"))

        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            if self.debug:
                log("DEBUG <- HTTP %d %s" % (e.code, detail.strip()[:2000]))
            # token 过期/失效时自动重登一次（缓存 token 被 VRM 吊销、VRM 重启等场景）
            if (self.token and not self._login_retry
                    and (e.code == 401 or "10100112" in detail)):
                self._login_retry = True
                self._cache_drop()
                self.token = None
                if self.configured_version not in (None, API_VERSION_AUTO):
                    self.version = self.configured_version
                self.login()
                return self.request(method, path, body=body, headers=headers,
                                    with_version=with_version)
            raise FCError("HTTP %d %s %s: %s" % (e.code, method, path, _pretty_err(detail)))
        except urllib.error.URLError as e:
            raise FCError("连接失败 %s: %s" % (url, e.reason))
        except Exception as e:  # noqa  ssl/socket 层的异常也归到这里
            raise FCError("请求异常 %s: %s" % (url, e))

        resp_headers = dict(resp.headers)
        raw = resp.read().decode("utf-8", errors="ignore")
        if self.debug:
            log("DEBUG <- %s %s | %s" % (resp.status, path, raw.strip()[:2000]))
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = {"_raw": raw}
        return resp_headers, parsed

    # -- 版本与登录 --------------------------------------------------------
    def get_versions(self):
        """GET /service/versions：查询当前系统支持的接口版本列表。

        按文档该接口不带 version 参数（Accept: application/json;charset=UTF-8）。
        """
        _, body = self.request("GET", URI_VERSIONS, with_version=False)
        return body.get("versions", []) or []

    def login(self):
        h = {
            "X-Auth-User": self.username,
            "X-Auth-Key": sha256_hex(self.password),
            "X-Auth-UserType": "0",          # 0: 本地账户 1: 域账户 2: 对接账户
            "X-Auth-AuthType": "0",          # 0: 本地认证 1: 级联域认证
            "X-ENCRYPT-ALGORITHM": "0",      # 0: SHA-256 加密 1: 明文
            "Accept-Language": "zh_CN",
        }
        headers, body = self.request("POST", URI_SESSION, headers=h)
        token = headers.get("X-Auth-Token") or headers.get("x-auth-token")
        if not token:
            raise FCError("登录失败：响应头缺少 X-Auth-Token，body=%s" % body)
        self.token = token
        self._used_cached_token = False
        try:
            self._validity = float(body.get("validity") or SESSION_TIMEOUT * 1000) / 1000.0
        except (TypeError, ValueError):
            self._validity = SESSION_TIMEOUT
        self._cache_save()
        return body

    def logout(self):
        if not self.token:
            return
        if self.token_cache:
            # 复用 token 时不能主动退出，否则会把缓存里给下一次调用用的 token 弄失效；
            # token 会在 600 秒后由 VRM 自动过期。
            self.token = None
            return
        try:
            self.request("DELETE", URI_SESSION)
        except FCError:
            pass
        self.token = None

    def negotiated_candidates(self, skip=()):
        """生成协商用的版本序列：服务端返回的版本优先，其次内置候选。"""
        cands = []

        def add(v):
            for form in _version_forms(v):
                if form and form not in cands and form not in skip:
                    cands.append(form)

        # 服务端版本列表形如 [{"version":"v8.1","loginUri":"/service/session"}, ...]
        server_versions = []
        try:
            server_versions = self.get_versions()
        except FCError as e:
            log("WARN 查询 /service/versions 失败（继续按内置候选尝试）：%s" % e)

        names = []
        for item in server_versions:
            if isinstance(item, dict):
                names.append(item.get("version"))
            elif isinstance(item, str):
                names.append(item)
        # 版本高的排前面：8.1 > 8.0 > 6.5 > 6.3 > 1.0
        for n in sorted([n for n in names if n], key=_version_key, reverse=True):
            add(n)

        for v in API_VERSION_CANDIDATES:
            add(v)
        return cands

    def login_negotiated(self, quiet=False):
        """自动协商接口版本并登录，返回实际使用的版本。

        顺序：token 缓存（10 分钟内复用）→ 配置里的版本 → 逐个候选版本协商。
        """
        cached = self._cache_load()
        if cached:
            self.version = cached.get("version") or self.version
            self.token = cached["token"]
            self._used_cached_token = True
            if self.debug:
                log("DEBUG 复用 token 缓存 %s（version=%s）"
                    % (self._cache_path(), self.version))
            return self.version

        tried = []
        conf = self.configured_version
        if conf and conf != API_VERSION_AUTO:
            self.version = conf
            self.token = None
            try:
                self.login()
                return conf
            except FCError as e:
                if ERR_VERSION not in str(e):
                    # 不是版本问题（例如密码错误、网络不通），直接失败，避免无意义重试
                    raise
                tried.extend(_version_forms(conf))

        for v in self.negotiated_candidates(skip=tuple(tried)):
            self.version = v
            self.token = None
            try:
                self.login()
            except FCError as e:
                if ERR_VERSION in str(e):
                    tried.append(v)
                    continue
                raise
            if not quiet:
                log("WARN 接口版本 %s 被 VRM 拒绝（10000022 版本号错误），已自动改用 %s。"
                    "建议把 fusioncompute.conf 里 version 固定为 %s。"
                    % ("/".join(tried) or "auto", v, v))
            return v
        raise FCError("所有候选接口版本均被 VRM 拒绝（%s）。请用 --probe 查看 "
                      "/service/versions 返回的支持版本。" % ", ".join(tried))

    # -- 业务接口 ----------------------------------------------------------
    def get_sites(self):
        _, body = self.request("GET", URI_SITES)
        return body.get("sites", []) or []

    def get_hosts(self, site_path):
        _, body = self.request("GET", site_path + "/hosts")
        return body.get("hosts", []) or []

    def get_clusters(self, site_path):
        _, body = self.request("GET", site_path + "/clusters")
        return body.get("clusters", []) or []

    def get_datastores(self, site_path):
        _, body = self.request("GET", site_path + "/datastores")
        return body.get("datastores", []) or []

    def get_active_alarms(self, site_path):
        _, body = self.request("POST", site_path + "/alarms/activeAlarms", body={})
        return body

    def get_realtime_metrics(self, site_path, objects):
        """批量查询对象实时指标。

        objects: [{"urn": "...", "metricId": ["cpu_usage", ...]}, ...]
        返回 {urn: {metricId: metricValue}}
        """
        _, body = self.request(
            "POST", site_path + "/monitors/objectmetric-realtimedata", body=objects)
        result = {}
        for item in body.get("items", []) or []:
            urn = item.get("urn")
            vals = {}
            for v in item.get("value", []) or []:
                vals[v.get("metricId")] = v.get("metricValue")
            result[urn] = vals
        return result

    # -- 虚拟机 ------------------------------------------------------------
    def get_vms(self, site_path, detail=1, is_template=False):
        """分页查询虚拟机列表（默认只返回虚拟机，不含模板）。

        文档：limit 单页最大 100，筛出数量超过 100 时按 limit=100、offset=0 处理，
        所以必须自己翻页，否则只会拿到前 100 台。
        detail=0 只回 uri/urn/uuid/name/status；
        detail=1 回常用属性（含 hostName/clusterName/vmConfig.nics.ip）；
        detail=2 回全量（含 cpuUsage/memUsage 等）。
        """
        out, offset, page = [], 0, 100
        while True:
            query = "%s/vms?limit=%d&offset=%d&detail=%d&isTemplate=%s" % (
                site_path, page, offset, detail, "true" if is_template else "false")
            _, body = self.request("GET", query)
            items = body.get("vms") or []
            out.extend(items)
            offset += page
            total = body.get("total")
            if len(items) < page:
                break
            if isinstance(total, int) and offset >= total:
                break
        return out

    def get_vm(self, site_path, urn):
        """查询单台虚拟机（urn 末段就是 vm id，如 i-00000002）。"""
        vid = (urn or "").strip().rstrip(":").rsplit(":", 1)[-1]
        if not vid:
            raise FCError("虚拟机 urn 不合法：%r" % (urn or ""))
        _, body = self.request("GET", "%s/vms/%s" % (site_path, vid))
        return body

    def get_vm_statistics(self, site_path, scope=None):
        """虚拟机数量统计：total/running/stopped/fault/hibernated。"""
        query = "%s/vms/statistics" % site_path
        if scope:
            query += "?scope=%s" % scope
        _, body = self.request("GET", query)
        return body

    def get_vm_info(self, site_path, urn):
        """一次取回一台虚机的全部常用信息，返回可直接 json.dumps 的字典。

        Zabbix 侧用「主监控项 + 依赖项」的方式拆分：本函数只被调用一次，
        名称/IP/状态/CPU/内存/磁盘等都由依赖项从这段 JSON 里取，不再各自发起检查。

        字段：
            name/urn/uuid/status/status_code/ip/host/cluster   —— 对象属性
            vcpu/mem_mb/disk_total_gb/disk_count              —— 配置(vmConfig)
            cpu_usage/mem_usage/disk_usage                    —— 实时指标(%)，取不到为 -1
        """
        vm = self.get_vm(site_path, urn)
        vcfg = vm.get("vmConfig") or vm.get("vmconfig") or {}
        cpu_cfg = vcfg.get("cpu") or {}
        mem_cfg = vcfg.get("memory") or {}
        disks = vcfg.get("disks") or []

        disk_total = 0.0
        for d in disks:
            q = _numeric(d.get("quantityGB"))
            if q:
                disk_total += q

        usage = {}
        try:
            usage = self.get_realtime_metrics(
                site_path,
                [{"urn": urn, "metricId": ["cpu_usage", "mem_usage", "disk_usage"]}],
            ).get(urn, {})
        except FCError as e:
            log("WARN 虚机 %s 的实时指标获取失败（按 -1 处理）：%s" % (vm.get("name"), e))

        def metric(name):
            num = _numeric(usage.get(name))
            return -1 if num is None else num

        status = vm.get("status", "")
        return {
            "name": vm.get("name", ""),
            "urn": vm.get("urn", urn),
            "uuid": vm.get("uuid", ""),
            "status": status,
            "status_code": VM_STATUS_MAP.get(status, -1),
            "ip": _first_vm_ip(vm),
            "host": vm.get("hostName", ""),
            "cluster": vm.get("clusterName", ""),
            "vcpu": int(_numeric(cpu_cfg.get("quantity")) or 0),
            "mem_mb": int(_numeric(mem_cfg.get("quantityMB")) or 0),
            "disk_total_gb": round(disk_total, 2),
            "disk_count": len(disks),
            "cpu_usage": round(metric("cpu_usage"), 2),
            "mem_usage": round(metric("mem_usage"), 2),
            "disk_usage": round(metric("disk_usage"), 2),
        }


def _version_key(v):
    """把 8.1 / v8.1 / 1.0 之类的版本号转成可排序的元组。"""
    s = str(v).strip().lstrip("vV")
    parts = []
    for p in s.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _pretty_err(detail):
    """把 VRM 的错误响应整理成人能看的样子。"""
    try:
        d = json.loads(detail)
    except ValueError:
        return detail.strip()[:500]
    if isinstance(d, dict):
        code = d.get("errorCode") or d.get("errCode")
        des = d.get("errorDes") or d.get("message")
        if code or des:
            return "errorCode=%s errorDes=%s" % (code, des)
    return json.dumps(d, ensure_ascii=False)[:500]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config(path):
    cfg = configparser.ConfigParser()
    if not os.path.exists(path):
        print("配置文件不存在：%s" % path, file=sys.stderr)
        sys.exit(2)
    try:
        # utf-8-sig：兼容记事本等工具写入的 BOM，避免 MissingSectionHeaderError
        cfg.read(path, encoding="utf-8-sig")
    except configparser.Error as e:
        print("配置文件格式错误（%s）：%s" % (path, e), file=sys.stderr)
        sys.exit(2)
    if not cfg.has_section("vrm"):
        print("配置文件缺少 [vrm] 段：%s" % path, file=sys.stderr)
        sys.exit(2)
    return cfg


def build_client(cfg, version=None):
    conf_version = version if version is not None else cfg.get(
        "vrm", "version", fallback=API_VERSION_AUTO)

    for key in ("host", "username", "password"):
        if not cfg.get("vrm", key, fallback="").strip():
            print("配置文件 [vrm] 段缺少 %s" % key, file=sys.stderr)
            sys.exit(2)

    verify_raw = cfg.get("vrm", "verify", fallback="false").strip()
    if verify_raw.lower() in ("false", "no", "0", ""):
        verify = False
    elif verify_raw.lower() in ("true", "yes", "1"):
        verify = True
    else:
        verify = verify_raw          # 当作 CA 证书路径
    debug = _DEBUG
    try:
        debug = debug or cfg.getboolean("vrm", "debug", fallback=False)
    except ValueError:
        pass
    try:
        token_cache = cfg.getboolean("vrm", "token_cache", fallback=True)
    except ValueError:
        token_cache = True
    return FusionComputeClient(
        host=cfg.get("vrm", "host").strip(),
        username=cfg.get("vrm", "username"),
        password=cfg.get("vrm", "password"),
        port=cfg.getint("vrm", "port", fallback=API_PORT),
        version=conf_version,
        timeout=cfg.getint("vrm", "timeout", fallback=10),
        verify=verify,
        scheme=cfg.get("vrm", "scheme", fallback="https").strip() or "https",
        debug=debug,
        token_cache=token_cache,
    )


# ---------------------------------------------------------------------------
# 站点与站点路径解析
#
# 现场常见两种「能登录但发现为空」的原因，这里都做了自适应：
#   1. 站点选错：多站点/联邦场景下 sites[0] 可能不是本端站点，或 isSelf 返回的是
#      字符串 "false"（字符串是 truthy，直接 if 判断会选错）；
#   2. 站点路径拼法：站点对象同时有 urn（urn:sites:XXXX）和 uri（/service/sites/XXXX），
#      不同版本对「把 urn 当路径段」的容忍度不同，可能返回 200 + 空列表。
# ---------------------------------------------------------------------------
def _is_true(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def ordered_sites(client):
    """本端站点（isSelf=true）排前面，其余站点按返回顺序跟在后面。"""
    sites = client.get_sites()
    if not sites:
        raise FCError("未找到可用站点")
    local = [s for s in sites if _is_true(s.get("isSelf"))]
    others = [s for s in sites if not _is_true(s.get("isSelf"))]
    return local + others


def site_path_candidates(site):
    """一个站点可能的 URL 路径写法，按可靠程度排序。"""
    cands = []

    def add(p):
        p = (p or "").strip().rstrip("/")
        if p and p not in cands:
            cands.append(p)

    uri = (site.get("uri") or "").strip()
    if uri.startswith("/"):
        add(uri)

    urn = (site.get("urn") or "").strip()
    if urn:
        # urn:sites:XXXX -> /service/sites/XXXX
        add(URI_SITES + "/" + urn.rsplit(":", 1)[-1])
        # 少数版本直接接受完整 urn
        add(URI_SITES + "/" + urn)
    if not cands:
        raise FCError("站点对象缺少 uri/urn：%s" % site)
    return cands


def _site_label(site):
    return "%s(%s)" % (site.get("name") or "?", site.get("urn") or site.get("uri") or "?")


def resolve_site(client, probe="hosts"):
    """定位「能取到数据」的站点与路径。

    依次尝试：本端站点优先 → 每个站点的各种路径写法 → 以 probe 类接口是否有数据判定。
    返回 (site, path, items)；items 是 probe 类型的数据（可能为空）。
    """
    tried = []
    fallback = None
    for site in ordered_sites(client):
        for path in site_path_candidates(site):
            if fallback is None:
                fallback = (site, path, [])
            loader = getattr(client, "get_" + probe)
            try:
                items = loader(path)
            except FCError as e:
                tried.append("%s %s -> %s" % (_site_label(site), path, e))
                continue
            if items:
                if tried:
                    log("NOTE 站点 %s 用路径 %s 取到 %d 条 %s（其他组合为：%s）"
                        % (_site_label(site), path, len(items), probe, "；".join(tried)))
                return site, path, items
            tried.append("%s %s -> 空列表" % (_site_label(site), path))

    log("WARN 未发现任何 %s。已尝试：%s" % (probe, "；".join(tried)))
    return fallback


def fetch_first(loader, paths, label):
    """按路径候选依次调用接口，返回第一个非空结果（全部为空则返回最后一个结果）。"""
    last = []
    for path in paths:
        try:
            items = loader(path)
        except FCError as e:
            log("WARN %s 在 %s 失败：%s" % (label, path, e))
            continue
        if items:
            return items
        last = items
    return last


def _first_vm_ip(vm):
    """从虚拟机对象里取第一个非空 IP（detail>=1 时 vmConfig.nics 才有有效值）。"""
    vcfg = vm.get("vmConfig") or vm.get("vmconfig") or {}
    for nic in vcfg.get("nics") or []:
        ip = (nic.get("ip") or "").strip()
        if ip:
            return ip
        for cand in nic.get("ipList") or []:
            if cand:
                return cand
    return ""


# ---------------------------------------------------------------------------
# 发现（Low-Level Discovery）
# ---------------------------------------------------------------------------
def discover(cfg, dtype):
    client = build_client(cfg)
    client.login_negotiated()
    try:
        # 以本次要发现的类型做探测，避免多打一次相同请求
        probe = dtype if dtype in ("hosts", "clusters", "datastores", "vms") else "hosts"
        site, spath, items = resolve_site(client, probe=probe)

        data = []
        if dtype == "hosts":
            for h in items:
                data.append({
                    "{#HOST.NAME}": h.get("name", ""),
                    "{#HOST.URN}": h.get("urn", ""),
                    "{#HOST.IP}": h.get("ip", ""),
                    "{#HOST.CLUSTER}": h.get("clusterName", ""),
                    "{#HOST.STATUS}": h.get("status", ""),
                    "{#HOST.STATUS_CODE}": HOST_STATUS_MAP.get(h.get("status"), -1),
                })
        elif dtype == "vms":
            for v in items:
                status = v.get("status", "")
                data.append({
                    "{#VM.NAME}": v.get("name", ""),
                    "{#VM.URN}": v.get("urn", ""),
                    "{#VM.UUID}": v.get("uuid", ""),
                    "{#VM.STATUS}": status,
                    "{#VM.STATUS_CODE}": VM_STATUS_MAP.get(status, -1),
                    "{#VM.HOST}": v.get("hostName", ""),
                    "{#VM.CLUSTER}": v.get("clusterName", ""),
                    "{#VM.IP}": _first_vm_ip(v),
                })
        elif dtype == "clusters":
            for c in items:
                data.append({
                    "{#CLUSTER.NAME}": c.get("name", ""),
                    "{#CLUSTER.URN}": c.get("urn", ""),
                })
        elif dtype == "datastores":
            for d in items:
                data.append({
                    "{#DATASTORE.NAME}": d.get("name", ""),
                    "{#DATASTORE.URN}": d.get("urn", ""),
                })
        else:
            raise FCError("未知发现类型：%s" % dtype)

        if not data:
            log("NOTE %s 发现结果为空；站点=%s，使用路径=%s。可用 --probe / --debug 排查。"
                % (dtype, _site_label(site) if site else "?", spath or "?"))
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
        # 数据存储没有监控指标（见文件顶部说明），ds.* 在 get_metric 里单独处理
        # 虚拟机动态指标（依据《监控指标列表》：主机虚拟机信息）
        "vm": {
            "cpu": "cpu_usage", "mem": "mem_usage", "disk": "disk_usage",
        },
    }
    if obj not in mapping or name not in mapping[obj]:
        raise FCError("未知指标类型：%s" % kind)
    return obj, mapping[obj][name]


def get_metric(cfg, kind, urn):
    """取单个值，供 Zabbix External Check 使用。

    支持的 kind：
        站点/告警（不需要 --urn）：site.status / site.name / alarm.total / alarm.level.1..4
        虚拟机汇总（不需要 --urn）：vm.count / vm.running / vm.stopped / vm.fault / vm.hibernated
        主机对象字段（需要 --urn）：host.status / host.vmcount
        虚拟机对象字段（需要 --urn）：vm.status / vm.info
        （vm.info 返回带名称/IP/状态/CPU/内存/磁盘等字段的 JSON，供 Zabbix 依赖项拆分）
        数据存储容量字段（需要 --urn）：ds.disk / ds.used_gb / ds.total_gb / ds.free_gb
        实时指标（需要 --urn）：host.cpu|mem|disk|net_in|net_out|dom0_cpu|dom0_mem
                                cluster.cpu|disk|net_in|net_out
                                vm.cpu|mem|disk
    """
    client = build_client(cfg)
    client.login_negotiated()
    try:
        site, spath, hosts = resolve_site(client)

        if kind.startswith("site."):
            val = site.get(kind.split(".", 1)[1])
        elif kind.startswith("alarm."):
            _, cond, *rest = kind.split(".")
            alarms = client.get_active_alarms(spath)
            if cond == "total":
                val = alarms.get("total", 0)
            elif cond == "level" and rest:
                want = str(rest[0])
                val = len([a for a in (alarms.get("items") or [])
                           if str(a.get("iAlarmLevel", "")) == want])
            else:
                raise FCError("未知告警类型：%s" % kind)
        elif kind in VM_STAT_KEYS:
            stats = client.get_vm_statistics(spath)
            val = stats.get(VM_STAT_KEYS[kind], 0)
        elif kind == "vm.info":
            # 主监控项：返回整段 JSON，Zabbix 侧由依赖项拆成名称/IP/CPU/内存/磁盘等
            print(json.dumps(client.get_vm_info(spath, urn), ensure_ascii=False))
            return
        elif kind == "vm.status":
            vm = client.get_vm(spath, urn)
            val = VM_STATUS_MAP.get(vm.get("status"), -1)
        elif kind.startswith("ds."):
            i64 = _numeric
            target = None
            for d in client.get_datastores(spath):
                if d.get("urn") == urn:
                    target = d
                    break
            if target is None:
                raise FCError("站点内找不到数据存储 urn=%s" % urn)
            total = i64(target.get("actualCapacityGB")) or i64(target.get("capacityGB"))
            used = i64(target.get("usedSizeGB"))
            free = i64(target.get("actualFreeSizeGB"))
            if free is None:
                free = i64(target.get("freeSizeGB"))
            if kind == "ds.disk":
                if not total or used is None:
                    raise FCError("数据存储 %s 缺少容量字段（used=%r total=%r）"
                                  % (target.get("name"), used, total))
                val = round(used * 100.0 / total, 2)
            elif kind == "ds.used_gb":
                val = used
            elif kind == "ds.total_gb":
                val = total
            elif kind == "ds.free_gb":
                val = free
            else:
                raise FCError("未知数据存储指标：%s（数据存储只提供容量字段："
                              "ds.disk / ds.used_gb / ds.total_gb / ds.free_gb）" % kind)
        elif kind in ("host.status", "host.vmcount"):
            target = None
            for h in hosts:
                if h.get("urn") == urn:
                    target = h
                    break
            if target is None:
                raise FCError("站点内找不到主机 urn=%s" % urn)
            if kind == "host.status":
                val = HOST_STATUS_MAP.get(target.get("status"), -1)
            else:
                val = target.get("vmCount", 0)
        else:
            _, metric_id = _resolve_metric(kind)
            result = client.get_realtime_metrics(
                spath, [{"urn": urn, "metricId": [metric_id]}])
            val = result.get(urn, {}).get(metric_id)

        if val is None:
            # Zabbix 外部检查约定：无数据输出空行（或 ZBX_NOTSUPPORTED）
            print("")
        elif kind.startswith("site."):
            # 站点名称/状态是文本，原样输出
            print(val)
        else:
            num = _numeric(val)
            if num is None:
                print("ZBX_NOTSUPPORTED 返回值不是数字：%r（该对象的指标可能不存在，"
                      "可用 --debug 查看 VRM 原始响应）" % (str(val)[:120],))
            else:
                print(int(num) if float(num).is_integer() else num)
    finally:
        client.logout()


# ---------------------------------------------------------------------------
# Trapper 批量采集
# ---------------------------------------------------------------------------
def collect_and_format(cfg, host_name):
    """采集并返回 [(zbx_host, zbx_key, value), ...]，供 zabbix_sender 使用。"""
    client = build_client(cfg)
    client.login_negotiated()
    rows = []
    try:
        site, spath, hosts = resolve_site(client)
        sites = client.get_sites()

        # 站点状态
        for s in sites:
            rows.append((host_name, "fc.site.status", s.get("status", "")))
            rows.append((host_name, "fc.site.name", s.get("name", "")))

        # 主机
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
            try:
                metrics = client.get_realtime_metrics(
                    spath, [{"urn": u, "metricId": DEFAULT_HOST_METRICS} for u in host_urns])
                name_map = {h.get("urn"): h.get("name") for h in hosts}
                for urn, vals in metrics.items():
                    nm = name_map.get(urn, urn)
                    for mid, mval in vals.items():
                        if mval is not None:
                            rows.append((host_name, "fc.host.%s[%s]" % (mid, nm), mval))
            except FCError as e:
                log("WARN 主机实时指标采集失败（已跳过）：%s" % e)

        # 集群
        clusters = fetch_first(client.get_clusters, site_path_candidates(site), "集群列表")
        cluster_urns = [c.get("urn") for c in clusters]
        if cluster_urns:
            try:
                metrics = client.get_realtime_metrics(
                    spath, [{"urn": u, "metricId": DEFAULT_CLUSTER_METRICS}
                            for u in cluster_urns])
                name_map = {c.get("urn"): c.get("name") for c in clusters}
                for urn, vals in metrics.items():
                    nm = name_map.get(urn, urn)
                    for mid, mval in vals.items():
                        if mval is not None:
                            rows.append((host_name, "fc.cluster.%s[%s]" % (mid, nm), mval))
            except FCError as e:
                log("WARN 集群实时指标采集失败（已跳过）：%s" % e)

        # 数据存储：文档里没有数据存储的监控指标，占用率/容量取自列表接口的容量字段
        try:
            for d in fetch_first(client.get_datastores, site_path_candidates(site),
                                 "数据存储列表"):
                nm = d.get("name")
                total = _numeric(d.get("actualCapacityGB")) or _numeric(d.get("capacityGB"))
                used = _numeric(d.get("usedSizeGB"))
                if total and used is not None:
                    rows.append((host_name, "fc.datastore.disk_usage[%s]" % nm,
                                 round(used * 100.0 / total, 2)))
                if used is not None:
                    rows.append((host_name, "fc.datastore.used_gb[%s]" % nm, used))
                if total is not None:
                    rows.append((host_name, "fc.datastore.total_gb[%s]" % nm, total))
        except FCError as e:
            log("WARN 数据存储采集失败（已跳过）：%s" % e)

        # 虚拟机（数量统计 + 逐台状态/IP/CPU/内存/磁盘）
        try:
            stats = client.get_vm_statistics(spath)
            for key, field in VM_STAT_KEYS.items():
                if key == "vm.count":
                    rows.append((host_name, "fc.vm.total", stats.get(field, 0)))
                else:
                    rows.append((host_name, "fc.vm.%s.total" % key.split(".", 1)[1],
                                 stats.get(field, 0)))
            vms = client.get_vms(spath, detail=1)
            for v in vms:
                nm = v.get("name")
                rows.append((host_name, "fc.vm.status[%s]" % nm,
                             VM_STATUS_MAP.get(v.get("status"), -1)))
                rows.append((host_name, "fc.vm.ip[%s]" % nm, _first_vm_ip(v)))
            if vms:
                metrics = client.get_realtime_metrics(
                    spath, [{"urn": v.get("urn"),
                             "metricId": ["cpu_usage", "mem_usage", "disk_usage"]}
                            for v in vms])
                name_map = {v.get("urn"): v.get("name") for v in vms}
                for urn, vals in metrics.items():
                    nm = name_map.get(urn, urn)
                    for key, mid in (("cpu", "cpu_usage"), ("mem", "mem_usage"),
                                     ("disk", "disk_usage")):
                        num = _numeric(vals.get(mid))
                        if num is not None:
                            rows.append((host_name, "fc.vm.%s[%s]" % (key, nm), num))
        except FCError as e:
            log("WARN 虚拟机采集失败（已跳过）：%s" % e)

        # 活动告警汇总
        try:
            alarms = client.get_active_alarms(spath)
            rows.append((host_name, "fc.alarm.total", alarms.get("total", 0)))
            # 按级别统计（1紧急 2重要 3次要 4提示）
            level_count = {"1": 0, "2": 0, "3": 0, "4": 0}
            for a in alarms.get("items", []) or []:
                lv = str(a.get("iAlarmLevel", ""))
                if lv in level_count:
                    level_count[lv] += 1
            for lv, cnt in level_count.items():
                rows.append((host_name, "fc.alarm.level[%s]" % lv, cnt))
        except FCError as e:
            log("WARN 活动告警采集失败（已跳过）：%s" % e)

    finally:
        client.logout()
    return rows


def send_trapper(cfg):
    """生成 zabbix_sender 输入并调用 zabbix_sender 推送。"""
    zbx_server = cfg.get("zabbix", "server", fallback="") or ""
    zbx_host = cfg.get("zabbix", "host", fallback="")
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
    if proc.returncode != 0:
        log("zabbix_sender 退出码 %d" % proc.returncode)


# ---------------------------------------------------------------------------
# 排障
# ---------------------------------------------------------------------------
def probe(cfg):
    """打印服务端版本列表，并逐个版本试登录 + 试核心接口，定位可用版本。"""
    client = build_client(cfg, version=API_VERSION_AUTO)

    print("== VRM: %s ==" % client.base)
    try:
        versions = client.get_versions()
        print("GET /service/versions -> %s" % json.dumps(versions, ensure_ascii=False))
    except FCError as e:
        versions = []
        print("GET /service/versions 失败：%s" % e)

    names = [v.get("version") for v in versions if isinstance(v, dict) and v.get("version")]
    candidates = []
    for n in sorted(names, key=_version_key, reverse=True):
        for f in _version_forms(n):
            if f not in candidates:
                candidates.append(f)
    for v in API_VERSION_CANDIDATES:
        if v not in candidates:
            candidates.append(v)

    print("\n== 逐个接口版本试登录 ==")
    usable = []
    for v in candidates:
        client.version = v
        client.token = None
        try:
            client.login()
        except FCError as e:
            if ERR_VERSION in str(e):
                print("  version=%-6s 拒绝（版本号错误 10000022）" % v)
            else:
                print("  version=%-6s 失败：%s" % (v, e))
            continue

        print("  version=%-6s 登录成功，继续试业务接口 ..." % v)
        try:
            for s in client.get_sites():
                print("        站点: name=%s urn=%s uri=%s isSelf=%r status=%s"
                      % (s.get("name"), s.get("urn"), s.get("uri"),
                         s.get("isSelf"), s.get("status")))
            site, spath, hosts = resolve_site(client)
            print("        采用站点=%s 路径=%s 主机数=%d"
                  % (_site_label(site) if site else "?", spath or "?", len(hosts)))
            if hosts:
                m = client.get_realtime_metrics(
                    spath, [{"urn": hosts[0].get("urn"), "metricId": ["cpu_usage"]}])
                print("        主机实时指标: %s" % ("有数据" if m else "空"))
            try:
                a = client.get_active_alarms(spath)
                print("        活动告警: total=%s" % a.get("total"))
            except FCError as e:
                print("        活动告警失败：%s" % e)
            print("        集群数=%d 数据存储数=%d"
                  % (len(fetch_first(client.get_clusters,
                                     site_path_candidates(site), "集群列表")),
                     len(fetch_first(client.get_datastores,
                                     site_path_candidates(site), "数据存储列表"))))
            try:
                dss = client.get_datastores(spath)
                if dss:
                    d0 = dss[0]
                    total = _numeric(d0.get("actualCapacityGB")) or _numeric(d0.get("capacityGB"))
                    used = _numeric(d0.get("usedSizeGB"))
                    pct = round(used * 100.0 / total, 2) if total and used is not None else "无法计算"
                    print("        数据存储示例: %s used=%sGB total=%sGB 占用率=%s"
                          % (d0.get("name"), used, total,
                             ("%s%%" % pct) if pct != "无法计算" else pct))
                else:
                    print("        数据存储列表为空")
            except FCError as e:
                print("        数据存储列表失败：%s" % e)
            try:
                stats = client.get_vm_statistics(spath)
                print("        虚拟机: 总数=%s 运行中=%s 已停止=%s 故障=%s 已休眠=%s"
                      % (stats.get("total"), stats.get("running"), stats.get("stopped"),
                         stats.get("fault"), stats.get("hibernated")))
            except FCError as e:
                print("        虚拟机统计失败：%s" % e)
            try:
                vms = client.get_vms(spath, detail=0)
                print("        虚拟机列表条数=%d（已排除模板）" % len(vms))
            except FCError as e:
                print("        虚拟机列表失败：%s" % e)
            usable.append(v)
        except FCError as e:
            print("        -> 业务接口失败：%s" % e)
        finally:
            client.logout()

    print("\n== 结论 ==")
    if usable:
        print("可用接口版本：%s" % ", ".join(usable))
        print("建议在 fusioncompute.conf 的 [vrm] 段写：version = %s" % usable[0])
    else:
        print("没有可用版本：请检查账号密码、网络与 VRM 端口 7443。")


def dump_rows(cfg):
    """打印 Trapper 模式将要推送的内容，便于离线核对（不调用 zabbix_sender）。"""
    zbx_host = cfg.get("zabbix", "host", fallback="FusionCompute-VRM")
    rows = collect_and_format(cfg, zbx_host)
    for h, k, v in rows:
        print("%s %s %s" % (h, k, v))
    log("共 %d 行" % len(rows))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="FusionCompute Zabbix 监控采集")
    p.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fusioncompute.conf"))
    p.add_argument("--discover", choices=["hosts", "vms", "clusters", "datastores"],
                   help="输出 LLD JSON")
    p.add_argument("--get", help="取单个指标，如 host.cpu")
    p.add_argument("--urn", help="配合 --get 使用，对象 urn")
    p.add_argument("--send", action="store_true", help="Trapper 批量采集并推送")
    p.add_argument("--dump", action="store_true", help="打印 Trapper 待推送内容，不发送")
    p.add_argument("--probe", action="store_true",
                   help="排障：列出支持版本、站点与主机情况并逐个试登录")
    p.add_argument("--version", help="覆盖配置里的接口版本，如 8.1 / 8.0 / auto")
    p.add_argument("--debug", action="store_true",
                   help="排障：把每个请求/响应的原始内容打到 stderr")
    args = p.parse_args()

    global _DEBUG
    _DEBUG = bool(args.debug)

    cfg = load_config(args.config)
    if args.version:
        if not cfg.has_section("vrm"):
            cfg.add_section("vrm")
        cfg.set("vrm", "version", args.version)

    try:
        if args.probe:
            probe(cfg)
        elif args.discover:
            discover(cfg, args.discover)
        elif args.get:
            no_urn = (args.get.startswith("site.") or args.get.startswith("alarm.")
                      or args.get in VM_STAT_KEYS)
            if not no_urn and not args.urn:
                print("--get %s 需要同时提供 --urn" % args.get, file=sys.stderr)
                sys.exit(2)
            get_metric(cfg, args.get, args.urn)
        elif args.dump:
            dump_rows(cfg)
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
