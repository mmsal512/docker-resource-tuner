#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rt.py — Docker Resource Tuner لحِزم PHP/Laravel على VPS صغير.
detect  : يقرأ موارد السيرفر الفعلية
doctor  : يقرأ الاستهلاك الحقيقي من cgroup (peak / OOM / CPU throttling) ويوصي
plan    : يحسب children + limits + reservations من sizing.yml
render  : يولّد php.ini + www.conf + postgres.conf + compose override
verify  : يتحقق من المجاميع والثوابت (يُستخدم في CI)
"""
from __future__ import annotations
import argparse, glob, json, math, os, subprocess, sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

MB = 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"

def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()

def load_cfg(path: Path) -> dict:
    txt = path.read_text(encoding="utf-8")
    if path.suffix in (".yml", ".yaml"):
        if not yaml:
            sys.exit("PyYAML غير متوفر: pip install pyyaml أو استخدم sizing.json")
        return yaml.safe_load(txt)
    return json.loads(txt)

def deep(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

# ------------------------------ 1. DETECT ------------------------------
def detect(cfg: dict) -> dict:
    mem = swap = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        f = line.split()
        if f[0] == "MemTotal:":  mem = int(f[1]) // 1024
        if f[0] == "SwapTotal:": swap = int(f[1]) // 1024
    vcpu = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    s = cfg.get("server", {})
    reserve = s.get("host_reserve_mb") or max(768, int(mem * 0.10))
    return {
        "vcpu": vcpu, "mem_total_mb": mem, "swap_mb": swap,
        "host_reserve_mb": reserve, "allocatable_mb": mem - reserve,
        "mem_overcommit": s.get("mem_overcommit", 1.35),
        "reservation_ratio": s.get("reservation_ratio", 0.70),
        "round_mb": s.get("round_mb", 32),
        "cgroup": "v2" if Path("/sys/fs/cgroup/cgroup.controllers").exists() else "v1",
        "kernel": sh("uname -r"),
    }

def rnd(v: float, step: int = 32) -> int:
    return int(math.ceil(v / step) * step)

# ------------------------------ 2. PLAN ------------------------------
def plan_php(name: str, app: dict, d: dict, srv: dict) -> dict:
    p = {**d["php"], **app.get("overrides", {})}
    W, Wc = p["worker_rss_mb"], p["worker_rss_cli_mb"]
    base = p["baseline_mb"] + p["opcache_mb"] + p["interned_mb"]

    # الطلب: قانون Little
    rps  = deep(app, "traffic", "rps_peak", default=2)
    ms   = deep(app, "traffic", "avg_ms",   default=200)
    demand = math.ceil(rps * (ms / 1000.0) * p["safety"])

    # العرض من المعالج
    cpu_pool = p["cpu_ceiling"] * srv["vcpu"]
    n_cpu = max(1, int(cpu_pool / p["cpu_per_request"]))

    children = max(p["min_children"], min(demand, n_cpu, p["max_children_cap"]))

    # إن حُدّد سقف ذاكرة صريح فالعمّال يُشتقّون منه (الاتجاه المعاكس)
    budget = app.get("budget_mb")
    peak = p["peak_workers"] * max(0, p["memory_limit_mb"] - W)
    cli  = deep(app, "workers", "queue", default=0) * Wc
    if deep(app, "workers", "scheduler", default=False):
        cli += int(Wc * 0.5)
    if budget:
        n_mem = int((budget - base - peak - cli - 64) / W)
        children = max(1, min(children, n_mem))

    mem_limit = budget or rnd(base + children * W + peak + cli + 64, srv["round_mb"])
    min_spare = max(2, children // 3)
    mem_res   = rnd(base + min_spare * W, srv["round_mb"])
    cpu_limit = round(min(cpu_pool, children * p["cpu_per_request"]), 2)

    conns = children + deep(app, "workers", "queue", default=0) \
            + (2 if deep(app, "workers", "scheduler", default=False) else 0) \
            + d["postgres"]["reserve_conns"]

    return {
        "kind": "php-fpm", "children": children, "demand": demand,
        "n_cpu": n_cpu, "min_spare": min_spare,
        "start_servers": max(2, children // 2), "max_spare": max(3, children - 1),
        "cpu": cpu_limit, "mem": mem_limit, "cpu_res": round(cpu_limit / 3, 2),
        "mem_res": mem_res, "shares": {"high": 900, "normal": 700, "low": 400}[app.get("weight", "normal")],
        "php": p, "db_conns": conns, "pids": 120 + children * 8,
    }

def plan_pg(app_plan: dict, d: dict, srv: dict) -> dict:
    q = d["postgres"]
    conns = app_plan["db_conns"]
    conn_cost = conns * (q["work_mem_mb"] + q["conn_overhead_mb"])
    maint = 48 if conns > 12 else 32
    mem = rnd((conn_cost + maint + 8 + 64) / 0.75, srv["round_mb"])   # shared_buffers = 25%
    return {
        "kind": "postgres", "cpu": 0.50, "cpu_res": 0.10,
        "mem": mem, "mem_res": rnd(mem * 0.35, srv["round_mb"]),
        "shares": 900, "pids": 200,
        "conf": {
            "max_connections": conns, "shared_buffers": f"{rnd(mem*0.25,16)}MB",
            "effective_cache_size": f"{rnd(mem*0.60,16)}MB",
            "work_mem": f"{q['work_mem_mb']}MB", "maintenance_work_mem": f"{maint}MB",
            "wal_buffers": "8MB", "max_wal_size": "1GB", "min_wal_size": "80MB",
            "checkpoint_completion_target": "0.9", "random_page_cost": "1.1",
            "effective_io_concurrency": "200", "jit": "off",
            "log_min_duration_statement": "500", "shared_preload_libraries": "pg_stat_statements",
        },
    }

def plan_redis(spec: dict, d: dict, srv: dict) -> dict:
    r = d["redis"]
    hot = spec.get("hot_mb", 32)
    mem = rnd(hot / r["fill_ratio"] + r["overhead_mb"], srv["round_mb"])
    return {
        "kind": "redis", "cpu": 0.15, "cpu_res": 0.03,
        "mem": mem, "mem_res": rnd(mem * 0.5, srv["round_mb"]),
        "shares": 600, "pids": 60, "maxmemory_mb": hot,
        "policy": spec.get("policy", "volatile-lru"),
    }

def build_plan(cfg: dict, srv: dict) -> dict:
    d, plans = cfg["defaults"], {}
    for name, app in cfg.get("apps", {}).items():
        ap = plan_php(name, app, d, srv)
        plans[f"{name}-app"] = ap
        if app.get("db"):
            plans[app["db"]["name"]] = plan_pg(ap, d, srv)
        if app.get("cache"):
            plans[app["cache"]["name"]] = plan_redis(app["cache"], d, srv)
    for name, s in cfg.get("static", {}).items():
        plans[name] = {"kind": "static", "cpu": s["cpu"], "cpu_res": round(s["cpu"]/4, 2),
                       "mem": s["mem"], "mem_res": s["res"], "shares": s["shares"],
                       "pids": 200, "tier": s.get("tier", "app")}
    return plans

# ------------------------------ 3. VERIFY ------------------------------
def verify(plans: dict, srv: dict) -> int:
    sl = sum(p["mem"] for p in plans.values())
    sr = sum(p["mem_res"] for p in plans.values())
    alloc, bad = srv["allocatable_mb"], 0
    print(f"RAM: total={srv['mem_total_mb']}M  host_reserve={srv['host_reserve_mb']}M  "
          f"allocatable={alloc}M  vCPU={srv['vcpu']}  cgroup={srv['cgroup']}")
    print(f"Σ limits       = {sl}M  ({sl/alloc:.2f}× allocatable, max {srv['mem_overcommit']})")
    print(f"Σ reservations = {sr}M  ({sr/alloc:.2f}× allocatable, max {srv['reservation_ratio']})")
    if sl > alloc * srv["mem_overcommit"]:
        print("FAIL: إفراط في سقوف الذاكرة"); bad = 1
    if sr > alloc * srv["reservation_ratio"]:
        print("FAIL: الحجوزات تتجاوز النسبة الآمنة"); bad = 1
    for n, p in plans.items():
        if p["kind"] == "php-fpm":
            need = (p["php"]["baseline_mb"] + p["php"]["opcache_mb"] + p["php"]["interned_mb"]
                    + p["children"] * p["php"]["worker_rss_mb"]
                    + (p["php"]["memory_limit_mb"] - p["php"]["worker_rss_mb"]))
            if need > p["mem"]:
                print(f"FAIL {n}: memory_limit×peak لا يتسع داخل {p['mem']}M (يحتاج {need}M)"); bad = 1
            if p["demand"] > p["children"]:
                print(f"WARN {n}: الطلب {p['demand']} > المتاح {p['children']} → أضف موارد أو Octane")
        if p["kind"] == "redis" and p["maxmemory_mb"] > p["mem"] * 0.8:
            print(f"FAIL {n}: maxmemory قريب جداً من سقف الحاوية"); bad = 1
    return bad

# ------------------------------ 4. DOCTOR ------------------------------
def cg_dir(cid: str):
    for pat in (f"/sys/fs/cgroup/system.slice/docker-{cid}*.scope",
                f"/sys/fs/cgroup/docker/{cid}*",
                f"/sys/fs/cgroup/memory/docker/{cid}*"):
        m = glob.glob(pat)
        if m: return Path(m[0])
    return None

def rd(p: Path):
    try: return int(p.read_text().split()[0])
    except Exception: return None

def doctor():
    print(f"{'CONTAINER':<24}{'lim':>7}{'cur':>7}{'peak':>7}{'use%':>6}"
          f"{'hitmax':>8}{'oom':>5}{'cpu':>6}{'thr%':>6}  verdict")
    for cid in sh("docker ps -q --no-trunc").split():
        ins = json.loads(sh(f"docker inspect {cid}"))[0]
        name, hc = ins["Name"].lstrip("/"), ins["HostConfig"]
        lim = (hc.get("Memory") or 0) // MB
        cpu = (hc.get("NanoCpus") or 0) / 1e9
        d = cg_dir(cid)
        cur = peak = hit = oom = 0; thr = 0.0
        if d:
            cur  = (rd(d / "memory.current") or 0) // MB
            peak = (rd(d / "memory.peak") or rd(d / "memory.max_usage_in_bytes") or 0) // MB
            ev = d / "memory.events"
            if ev.exists():
                kv = dict(l.split() for l in ev.read_text().splitlines() if len(l.split()) == 2)
                hit, oom = int(kv.get("max", 0)), int(kv.get("oom_kill", 0))
            st = d / "cpu.stat"
            if st.exists():
                kv = dict(l.split() for l in st.read_text().splitlines() if len(l.split()) == 2)
                per = int(kv.get("nr_periods", 0)) or 1
                thr = 100.0 * int(kv.get("nr_throttled", 0)) / per
        use = (100.0 * peak / lim) if lim else 0
        v = []
        if oom: v.append("OOM! ارفع السقف فوراً")
        elif use > 90 or hit: v.append("قريب من السقف → +25%")
        elif lim and use < 40: v.append("مبالغ فيه → قلّصه")
        if thr > 5: v.append(f"CPU throttled → ارفع السقف")
        if not lim: v.append("بلا حدود (drift!)")
        print(f"{name:<24}{lim:>7}{cur:>7}{peak:>7}{use:>5.0f}%{hit:>8}{oom:>5}"
              f"{cpu:>6.2f}{thr:>5.1f}%  {' / '.join(v) or 'OK'}")
    print("\nملاحظة: memory.current يشمل page cache فيبدو متضخماً؛ الإشارة الحقيقية "
          "هي عمود hitmax (مرات ضرب السقف) وoom.")

def measure_fpm(container: str, path: str = "/fpm-status"):
    out = sh(f"docker exec {container} sh -c \"curl -s 'http://127.0.0.1:8080{path}?json'\"")
    try: st = json.loads(out)
    except Exception: return print(f"{container}: لا يوجد fpm-status مُفعّل")
    rss = sh("docker exec %s sh -c \"ps -o rss= -C php-fpm 2>/dev/null || "
             "ps -o rss= | tail -n +2\"" % container).split()
    vals = sorted(int(x) // 1024 for x in rss if x.isdigit())
    body = vals[1:] or vals          # استثنِ الـ master
    print(f"{container}: active={st.get('active processes')}/{st.get('total processes')} "
          f"maxActive={st.get('max active processes')} "
          f"reached_max_children={st.get('max children reached')} "
          f"queue={st.get('listen queue')} slow={st.get('slow requests')}")
    if body:
        print(f"  worker RSS: avg={sum(body)//len(body)}M p95={body[int(len(body)*0.95)-1]}M "
              f"→ ضع هذا في worker_rss_mb")

# ------------------------------ 5. RENDER ------------------------------
PHP_INI = """; مُولَّد بواسطة rt.py — لا تُعدّله يدوياً ({app})
; children={children} · worker_rss={W}M · container_limit={mem}M
memory_limit = {memory_limit}M
max_execution_time = {maxexec}
max_input_time = {maxexec}
max_input_vars = 3000
post_max_size = {post}M
upload_max_filesize = {upload}M
realpath_cache_size = 4096k
realpath_cache_ttl = 600
expose_php = Off
display_errors = Off
log_errors = On
error_log = /proc/self/fd/2
date.timezone = {tz}
zlib.output_compression = Off

[opcache]
opcache.enable = 1
opcache.enable_cli = 0
opcache.memory_consumption = {opcache}
opcache.interned_strings_buffer = {interned}
opcache.max_accelerated_files = {files}
opcache.validate_timestamps = 0
opcache.revalidate_freq = 0
opcache.save_comments = 1
opcache.max_wasted_percentage = 10
opcache.fast_shutdown = 1
opcache.file_cache = /tmp/opcache
opcache.huge_code_pages = 0
opcache.jit = off
opcache.jit_buffer_size = 0
"""

WWW_CONF = """; مُولَّد بواسطة rt.py — {app}
[www]
user = www-data
group = www-data
listen = /run/php-fpm.sock
listen.owner = www-data
listen.group = www-data
listen.mode = 0660
listen.backlog = 256

pm = dynamic
pm.max_children = {children}
pm.start_servers = {start}
pm.min_spare_servers = {min_spare}
pm.max_spare_servers = {max_spare}
pm.max_requests = 500
pm.process_idle_timeout = 10s
pm.status_path = /fpm-status
ping.path = /fpm-ping

request_terminate_timeout = {terminate}s
request_slowlog_timeout = 5s
slowlog = /proc/self/fd/2
rlimit_files = 8192

clear_env = no
catch_workers_output = yes
decorate_workers_output = no
access.log = /proc/self/fd/2
access.format = "%R %m %r%Q%q %s %{{mili}}d %{{kilo}}M %C%%"

php_admin_value[memory_limit] = {memory_limit}M
php_admin_value[error_log] = /proc/self/fd/2
php_admin_flag[log_errors] = on
"""

def render(cfg: dict, plans: dict, srv: dict):
    for sub in ("php", "php-fpm", "postgres"):
        (OUT / sub).mkdir(parents=True, exist_ok=True)
    svc = []
    for name, p in sorted(plans.items()):
        blk = [f"  {name}:",
               "    deploy:", "      resources:", "        limits:",
               f"          cpus: '{p['cpu']:.2f}'", f"          memory: {p['mem']}M",
               f"          pids: {p['pids']}", "        reservations:",
               f"          cpus: '{p['cpu_res']:.2f}'", f"          memory: {p['mem_res']}M",
               f"    cpu_shares: {p['shares']}"]
        if p["kind"] in ("postgres", "redis"):
            blk.append("    oom_score_adj: -500")
        if p.get("tier") in ("batch", "ops", "obs"):
            blk.append("    oom_score_adj: 500")
        if p["kind"] == "redis":
            blk.append(f"    command: redis-server --maxmemory {p['maxmemory_mb']}mb "
                       f"--maxmemory-policy {p['policy']} --save \"\" --appendonly no "
                       f"--tcp-backlog 128")
        svc.append("\n".join(blk))
    (OUT / "docker-compose.resources.yml").write_text(
        "# مُولَّد بواسطة rt.py — طبّقه فوق ملفك الأصلي:\n"
        "#   docker compose -f docker-compose.yml -f out/docker-compose.resources.yml up -d\n"
        "services:\n" + "\n".join(svc) + "\n", encoding="utf-8")

    for name, app in cfg.get("apps", {}).items():
        p = plans[f"{name}-app"]; ph = p["php"]
        (OUT / "php" / f"{name}.ini").write_text(PHP_INI.format(
            app=name, children=p["children"], W=ph["worker_rss_mb"], mem=p["mem"],
            memory_limit=ph["memory_limit_mb"], maxexec=ph["max_execution_time"],
            post=ph.get("post_max_size_mb", 12), upload=ph.get("upload_max_filesize_mb", 10),
            tz=app.get("tz", "Asia/Riyadh"), opcache=ph["opcache_mb"],
            interned=ph["interned_mb"], files=ph.get("max_accelerated_files", 16229)),
            encoding="utf-8")
        (OUT / "php-fpm" / f"{name}.www.conf").write_text(WWW_CONF.format(
            app=name, children=p["children"], start=p["start_servers"],
            min_spare=p["min_spare"], max_spare=p["max_spare"],
            terminate=ph["max_execution_time"] + 5,
            memory_limit=ph["memory_limit_mb"]), encoding="utf-8")
        if app.get("db"):
            pg = plans[app["db"]["name"]]
            (OUT / "postgres" / f"{name}.conf").write_text(
                f"# مُولَّد بواسطة rt.py — سقف الحاوية {pg['mem']}M\n" +
                "\n".join(f"{k} = {v}" for k, v in pg["conf"].items()) + "\n",
                encoding="utf-8")

    lines = ["# خطة الموارد", "",
             f"- السيرفر: {srv['mem_total_mb']}M / {srv['vcpu']} vCPU / متاح {srv['allocatable_mb']}M",
             "", "| حاوية | نوع | CPU lim | shares | MEM lim | MEM res | children |",
             "|---|---|---|---|---|---|---|"]
    for n, p in sorted(plans.items()):
        lines.append(f"| {n} | {p['kind']} | {p['cpu']:.2f} | {p['shares']} | "
                     f"{p['mem']}M | {p['mem_res']}M | {p.get('children','-')} |")
    (OUT / "PLAN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"تم التوليد في {OUT}")

# ------------------------------ main ------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["detect", "plan", "render", "verify", "doctor", "fpm"])
    ap.add_argument("-c", "--config", default=str(ROOT / "sizing.yml"))
    ap.add_argument("--container")
    a = ap.parse_args()
    if a.cmd == "doctor": return doctor()
    if a.cmd == "fpm":    return measure_fpm(a.container)
    cfg = load_cfg(Path(a.config)); srv = detect(cfg)
    if a.cmd == "detect": return print(json.dumps(srv, indent=2, ensure_ascii=False))
    plans = build_plan(cfg, srv)
    if a.cmd == "plan":
        for n, p in sorted(plans.items()):
            print(f"{n:<24}{p['kind']:<10}cpu={p['cpu']:<6.2f}mem={p['mem']:<6}M "
                  f"res={p['mem_res']:<5}M children={p.get('children','-')}")
        return sys.exit(verify(plans, srv))
    if a.cmd == "verify": return sys.exit(verify(plans, srv))
    render(cfg, plans, srv); sys.exit(verify(plans, srv))

if __name__ == "__main__":
    main()
