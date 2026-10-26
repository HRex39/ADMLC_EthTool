# -*- coding: utf-8 -*-
"""
device_access.py —— 按配置执行「设备访问序列」，支持 SSH 跳板（多跳）。

典型场景（8255）：
    PC 网卡 = 169.254.x.x（APIPA，地址始终不变）
      └─ SSH ─► 网关 169.254.1.0（root / 密码 A）
                    └─ direct-tcpip 通道 ─► 172.16.4.26:22（root / 密码 B）

PC 不需要到 172.16.4.0/24 的路由，也不会变成 172.16.4.x：
第二跳的 TCP 连接是从第一跳的 SSH 会话里开出来的通道
（等价于 ssh -J / WinSCP 的"通过 SSH 隧道连接"）。

configs.json 片段：
    "access": [
      {"host": "169.254.1.0", "user": "root", "password": "...",
       "wait_link_local": true},
      {"host": "172.16.4.26", "user": "root", "password": "...",
       "via": "jump", "retry_seconds": 60}
    ]

字段说明：
    host / port / user / password : 连接参数（port 默认 22）
    via="jump"                    : 从上一跳的 SSH 会话开通道转发（缺省为直连）
    wait_link_local=true          : 连接前先等本机取得 169.254.x.x
    wait_link_local_timeout       : 等待 APIPA 的超时秒数（默认 45）
    retry_seconds                 : 连不上时的重试总时长（秒，0 表示不重试）
"""

import socket
import time

import nic_driver

DEFAULT_PORT = 22
LINK_LOCAL_PREFIX = "169.254."


def _quiet(message):
    """默认日志：什么都不做。"""


def _paramiko():
    """延迟导入 paramiko：让本模块在没装 paramiko 的环境里也能被导入（便于自检）。"""
    import paramiko
    return paramiko


# ----------------------------------------------------------------------------
# 配置解析
# ----------------------------------------------------------------------------

def hops_from_config(cfg):
    """从配置里取出访问序列，只保留含 host 的项；没有则返回 []。"""
    hops = (cfg or {}).get("access")
    if not isinstance(hops, list):
        return []
    result = []
    for item in hops:
        if isinstance(item, dict) and str(item.get("host") or "").strip():
            result.append(item)
    return result


# ----------------------------------------------------------------------------
# 本机地址（判断是否已取得 169.254.x.x）
# ----------------------------------------------------------------------------

def local_ipv4(iface):
    """
    取网卡当前 IPv4 地址列表。
    - 返回 []  ：网卡没有 IPv4 地址
    - 返回 None：无法判断（调用方应直接尝试连接）
    """
    script = r"""
$name = __NAME__
try {
    $items = @(Get-NetIPAddress -InterfaceAlias $name -AddressFamily IPv4 -ErrorAction Stop |
        Select-Object IPAddress, PrefixLength)
    @{ ok = $true; addresses = @($items) } | ConvertTo-Json -Depth 4 -Compress
} catch {
    @{ ok = $false; error = $_.Exception.Message; addresses = @() } | ConvertTo-Json -Depth 4 -Compress
}
""".replace("__NAME__", nic_driver._ps_str(iface))
    data = nic_driver.run_ps_json(script, timeout=20)
    if data.get("ok"):
        values = []
        for item in nic_driver._as_list(data.get("addresses")):
            if isinstance(item, dict):
                address = str(item.get("IPAddress") or "").strip()
                if address and "." in address:
                    values.append(address)
        return values
    try:  # 兜底：psutil
        import psutil
        values = [entry.address for entry in psutil.net_if_addrs().get(iface, [])
                  if entry.family == socket.AF_INET]
        return values if values else []
    except Exception:
        return None


def wait_link_local(iface, timeout=45, log=_quiet, interval=1.5):
    """
    等待网卡取得 169.254.x.x（链路本地 / APIPA）。
    成功返回地址字符串；超时返回 False；无法读取地址返回 None。
    """
    log = nic_driver.as_log(log)
    deadline = time.time() + max(float(timeout), 0.0)
    last_note = 0.0
    while True:
        addresses = local_ipv4(iface)
        if addresses is None:
            log("   ⚠️ 无法读取网卡地址，跳过 169.254.x.x 检查，直接尝试连接")
            return None
        for address in addresses:
            if address.startswith(LINK_LOCAL_PREFIX):
                log("   ✅ 网卡已取得链路本地地址 %s" % address)
                return address
        remain = int(deadline - time.time())
        if remain <= 0:
            log("   ⚠️ 等待 %.0f 秒仍未取得 169.254.x.x（当前: %s）"
                % (float(timeout), "、".join(addresses) or "无地址"))
            return False
        if time.time() - last_note >= 5:
            last_note = time.time()
            log("   等待网卡自动获取 169.254.x.x …（当前: %s，剩余约 %d 秒）"
                % ("、".join(addresses) or "无地址", remain))
        time.sleep(interval)


# ----------------------------------------------------------------------------
# 逐跳连接
# ----------------------------------------------------------------------------

def _connect_hop(hop, previous_transport=None, log=_quiet, timeout=20):
    """建立一跳 SSH 连接。via=jump 时从上一跳开 direct-tcpip 通道，否则直连。"""
    paramiko = _paramiko()
    log = nic_driver.as_log(log)
    host = str(hop.get("host")).strip()
    port = int(hop.get("port") or DEFAULT_PORT)
    user = str(hop.get("user") or "root")
    password = str(hop.get("password") or "")
    use_jump = previous_transport is not None \
        and str(hop.get("via") or "").lower() == "jump"

    if use_jump:
        log("   经 SSH 隧道转发到 %s:%s …" % (host, port))
        sock = previous_transport.open_channel(
            "direct-tcpip", (host, port), ("127.0.0.1", 0), timeout=timeout)
    else:
        sock = socket.create_connection((host, port), timeout=timeout)

    transport = paramiko.Transport(sock)
    transport.banner_timeout = 20
    transport.auth_timeout = 20
    transport.connect(username=user, password=password)
    return transport


def _explain(hop, exc):
    """把底层异常翻译成用户能看懂的原因（三类常见失败必须区分开）。"""
    host = hop.get("host")
    try:
        paramiko = _paramiko()
    except Exception:
        paramiko = None
    if paramiko is not None:
        try:
            if isinstance(exc, paramiko.AuthenticationException):
                return "连接 %s 认证失败：用户名或密码不正确" % host
            if isinstance(exc, paramiko.ssh_exception.ChannelException):
                return ("网关不允许端口转发，无法跳转到 %s"
                        "（需要在网关开启 TCP 转发 AllowTcpForwarding）" % host)
            if isinstance(exc, paramiko.ssh_exception.SSHException):
                return "连接 %s 的 SSH 握手失败：%s" % (host, exc)
        except Exception:
            pass
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "连接 %s 超时：端口不通或设备尚未就绪" % host
    if isinstance(exc, OSError):
        return "无法连接 %s：%s" % (host, exc)
    return "连接 %s 失败：%s" % (host, exc)


def connect_chain(hops, iface=None, log=_quiet, on_progress=None):
    """
    逐跳建立连接，返回 (transports, sftp_client)。
    失败时抛 RuntimeError（消息里带具体原因），并已清理建好的连接。
    """
    paramiko = _paramiko()
    log = nic_driver.as_log(log)
    progress = on_progress or (lambda text: None)
    hops = list(hops or [])
    if not hops:
        raise RuntimeError("配置里没有可用的访问地址（access 为空）")

    transports = []
    total = len(hops)
    try:
        for index, hop in enumerate(hops, 1):
            host = str(hop.get("host")).strip()
            progress("第 %d/%d 跳：正在连接 %s …" % (index, total, host))
            log("\n[第 %d/%d 跳] %s" % (index, total, host))

            if index == 1 and hop.get("wait_link_local") and iface:
                timeout = float(hop.get("wait_link_local_timeout") or 45)
                state = wait_link_local(iface, timeout=timeout, log=log)
                if state is False:
                    raise RuntimeError(
                        "网卡未取得 169.254.x.x 地址：请先应用「%s」配置（DHCP）"
                        "并确认网线已连接" % (hop.get("need_config") or "该配置"))
            elif index == 1 and hop.get("wait_link_local"):
                log("   ℹ️ 未选择网口，跳过 169.254.x.x 地址检查")

            retry_seconds = int(hop.get("retry_seconds") or 0)
            deadline = time.time() + retry_seconds
            attempt = 0
            while True:
                attempt += 1
                try:
                    transport = _connect_hop(
                        hop, transports[-1] if transports else None, log=log)
                    break
                except Exception as exc:
                    reason = _explain(hop, exc)
                    if attempt == 1:
                        log("   ⚠️ %s" % reason)
                    if not retry_seconds or time.time() >= deadline:
                        raise RuntimeError(reason) from None
                    if transports and not transports[-1].is_active():
                        raise RuntimeError("网关连接已断开，无法继续转发，请重试") from None
                    progress("第 %d/%d 跳：%s 未就绪，重试中…" % (index, total, host))
                    time.sleep(2)

            transports.append(transport)
            log("   ✅ 已连接 %s@%s:%s"
                % (hop.get("user") or "root", host, hop.get("port") or DEFAULT_PORT))

        sftp = paramiko.SFTPClient.from_transport(transports[-1])
        progress("正在打开 SFTP 浏览器…")
        return transports, sftp
    except Exception:
        close_all(transports)
        raise


def close_all(transports):
    """关闭整条连接链（后建立的先关）。"""
    for transport in reversed(list(transports or [])):
        try:
            transport.close()
        except Exception:
            pass


def open_browser(master, hops, transports, sftp):
    """
    在主线程创建 SFTP 浏览器窗口（复用已建好的 sftp 连接），
    窗口关闭时把整条连接链一起释放。
    """
    from sftp_browser import SFTPBrowser

    last = (list(hops) or [{}])[-1]
    browser = SFTPBrowser(
        master,
        host=str(last.get("host") or ""),
        user=str(last.get("user") or "root"),
        password=str(last.get("password") or ""),
        port=int(last.get("port") or DEFAULT_PORT),
        sftp_client=sftp,
    )

    def _on_destroy(event):
        if event.widget is browser:
            try:
                sftp.close()
            except Exception:
                pass
            close_all(transports)

    browser.bind("<Destroy>", _on_destroy)
    return browser
