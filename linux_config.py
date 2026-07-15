import subprocess

def apply_config_linux(iface, cfg, output_text):
    subprocess.run(["ip", "addr", "flush", "dev", iface])

    vlan_iface = None
    if cfg["vlan_id"] != "auto":
        vlan_iface = f"{iface}.{cfg['vlan_id']}"
        result = subprocess.run(["ip", "link", "show", vlan_iface], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            subprocess.run(["ip", "link", "add", "link", iface, "name", vlan_iface, "type", "vlan", "id", str(cfg["vlan_id"])])
        subprocess.run(["ip", "link", "set", vlan_iface, "up"])
    target_iface = vlan_iface if vlan_iface else iface

    if cfg["ip"] != "auto" and cfg["netmask"] != "auto":
        subprocess.run(["ip", "addr", "add", f"{cfg['ip']}/{cfg['netmask']}", "dev", target_iface])

    if cfg["gateway"] != "auto":
        subprocess.run(["ip", "route", "del", "default"], stderr=subprocess.DEVNULL)
        subprocess.run(["ip", "route", "add", "default", "via", cfg["gateway"], "dev", target_iface])

    output_text.insert("end", f"✅ Linux 配置已应用到 {target_iface}\n")
