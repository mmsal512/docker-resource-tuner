#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rt.py — Docker Resource Tuner
يشتقّ حدود الحاويات وإعدادات php.ini / www.conf / postgres / redis من مصدر واحد.

  detect  : اطبع موارد السيرفر كما يراها
  plan    : احسب واطبع الخطة (لا يكتب أي ملف)
  verify  : تحقق من المجاميع والثوابت وميزانية المسار الحرج
  render  : ولّد الملفات في out/
  doctor  : اقرأ الاستهلاك الحقيقي من cgroup وأوصِ بتعديلات
  fpm     : قِس عمّال php-fpm داخل حاوية محددة
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

MB = 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "out"
SHARES = {"high": 900, "normal": 700, "low": 400}


# ----------------------------------------------------------------- helpers
def sh(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""


def write(path: Path, text: str, mode: int | None = None) -> None:
    """كتابة بنهايات أسطر LF دائماً (الملفات تذهب إلى حاويات لينكس)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if mode is not None:
        try:
            os.chmod(path, mode)
        except Exception:
            pass


def load_cfg(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"لم أجد ملف الإعداد: {path}")
    txt = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yml", ".yaml"):
        if not yaml:
            sys.exit("PyYAML غير مثبت. الحل: sudo apt install -y python3-yaml  (أو pip install pyyaml)")
        return yaml.safe_load(txt)
    return json.loads(txt)


def deep(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur or cur[k] is None:
            return default
        cur = cur[k]
    return cur


def rnd(v: float, step: int = 32) -> int:
    step = max(1, int(step))
    return int(math.ceil(float(v) / step) * step)


# ----------------------------------------------------------------- 1. detect
def detect(cfg: dict, assume_mem=None, assume_cpu=None) -> dict:
    s = cfg.get("server") or {}
    a = s.get("assume") or {}
    mem = assume_mem or a.get("mem_total_mb")
    vcpu = assume_cpu or a.get("vcpu")

    det_mem = det_cpu = None
    swap = 0
    mi = Path("/proc/meminfo")
    if mi.exists():
        det_mem = 0
        for line in mi.read_text().splitlines():
            f = line.split()
            if f[0] == "MemTotal:":
                det_mem = int(f[1]) // 1024
            elif f[0] == "SwapTotal:":
                swap = int(f[1]) // 1024
        det_cpu = (len(os.sched_getaffinity(0))
                   if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1))

    src = "assumed" if (a or assume_mem or assume_cpu) else "detected"
    if mem is None:
        mem, src = det_mem, "detected"
    if vcpu is None:
        vcpu, src = det_cpu, "detected"
    if not mem or not vcpu:
        sys.exit("تعذّرت قراءة موارد السيرفر (ويندوز؟) — عرّف server.assume في sizing.yml")

    if det_mem and (abs(det_mem - mem) > 256 or det_cpu != vcpu):
        print(f"WARN: القيم المفترضة ({mem}M / {vcpu} نواة) تخالف الحقيقية "
              f"({det_mem}M / {det_cpu} نواة) — حدّث server.assume أو احذفه")

    reserve = s.get("host_reserve_mb") or max(768, int(mem * 0.10))
    return {
        "source": src,
        "vcpu": int(vcpu),
        "mem_total_mb": int(mem),
        "swap_mb": int(swap),
        "host_reserve_mb": int(reserve),
        "allocatable_mb": int(mem) - int(reserve),
        "mem_overcommit": s.get("mem_overcommit", 1.35),
        "reservation_ratio": s.get("reservation_ratio", 0.70),
        "round_mb": s.get("round_mb", 32),
        "cgroup": "v2" if Path("/sys/fs/cgroup/cgroup.controllers").exists() else "v1",
        "kernel": sh("uname -r"),
    }


# ----------------------------------------------------------------- 2. plan
def plan_php(name: str, app: dict, d: dict, srv: dict) -> dict:
    p = dict(d["php"])
    p.update(app.get("overrides") or {})

    W, Wc = p["worker_rss_mb"], p["worker_rss_cli_mb"]
    base = p["baseline_mb"] + p["opcache_mb"] + p["interned_mb"]

    # الطلب: قانون Little
    rps = deep(app, "traffic", "rps_peak", default=2)
    ms = deep(app, "traffic", "avg_ms", default=200)
    demand = max(1, math.ceil(rps * (ms / 1000.0) * p["safety"]))

    # العرض من المعالج
    cpu_pool = p["cpu_ceiling"] * srv["vcpu"]
    n_cpu = max(1, int(cpu_pool / p["cpu_per_request"]))

    children = max(p["min_children"], min(demand, n_cpu, p["max_children_cap"]))

    q = deep(app, "workers", "queue", default=0)
    sched = bool(deep(app, "workers", "scheduler", default=False))
    cli = q * Wc + (int(Wc * 0.5) if sched else 0)
    peak = p["peak_workers"] * max(0, p["memory_limit_mb"] - W)

    budget = app.get("budget_mb")
    if budget:
        n_mem = int((budget - base - peak - cli - 64) / W)
        if n_mem < 1:
            sys.exit(f"[{name}] budget_mb={budget}M صغير جداً — لا يتسع لعامل واحد "
                     f"(الأساس {base}M + قمة {peak}M + CLI {cli}M)")
        children = max(1, min(children, n_mem))
        mem_limit = int(budget)
    else:
        mem_limit = rnd(base + children * W + peak + cli + 64, srv["round_mb"])

    min_spare = max(1, children // 3)
    max_spare = max(min_spare, min(children, max(2, (2 * children) // 3)))
    start = min(max_spare, max(min_spare, children // 2))

    mem_res = min(mem_limit, rnd(base + max(min_spare, 2) * W, srv["round_mb"]))
    cpu_limit = round(max(0.25, min(cpu_pool, children * p["cpu_per_request"])), 2)
    conns = children + q + (2 if sched else 0) + d["postgres"]["reserve_conns"]

    return {
        "kind": "php-fpm", "tier": "app", "app": name, "managed": True,
        "container": app.get("container", f"{name}-app"),
        "children": children, "demand": demand, "n_cpu": n_cpu,
        "start_servers": start, "min_spare": min_spare, "max_spare": max_spare,
        "cpu": cpu_limit, "cpu_res": round(cpu_limit / 3, 2),
        "mem": mem_limit, "mem_res": mem_res,
        "shares": SHARES.get(app.get("weight", "normal"), 700),
        "pids": 120 + children * 8,
        "db_conns": conns, "php": p,
    }


def plan_pg(ap: dict, spec: dict, d: dict, srv: dict) -> dict:
    q = d["postgres"]
    conns = int(spec.get("max_connections") or ap["db_conns"])
    work = q["work_mem_mb"]
    maint = 48 if conns > 12 else 32
    conn_cost = conns * (work + q["conn_overhead_mb"])
    mem = int(spec.get("mem_mb") or rnd((conn_cost + maint + 8 + 64) / 0.75, srv["round_mb"]))
    cpu = float(spec.get("cpu", q["cpu"]))

    conf = {
        "max_connections": str(conns),
        "shared_buffers": f"{rnd(mem * 0.25, 16)}MB",
        "effective_cache_size": f"{rnd(mem * 0.60, 16)}MB",
        "work_mem": f"{work}MB",
        "maintenance_work_mem": f"{maint}MB",
        "wal_buffers": "8MB",
        "max_wal_size": "1GB",
        "min_wal_size": "80MB",
        "checkpoint_completion_target": "0.9",
        "random_page_cost": "1.1",
        "effective_io_concurrency": "200",
        "default_statistics_target": "100",
        "jit": "off",
        "log_min_duration_statement": "500",
    }
    if q.get("pg_stat_statements"):
        conf["shared_preload_libraries"] = "pg_stat_statements"

    return {
        "kind": "postgres", "tier": "data", "managed": True,
        "cpu": cpu, "cpu_res": round(cpu / 4, 2),
        "mem": mem, "mem_res": rnd(mem * 0.35, srv["round_mb"]),
        "shares": int(spec.get("shares", q["shares"])), "pids": 200,
        "conf": conf,
    }


def plan_redis(spec: dict, d: dict, srv: dict) -> dict:
    r = d["redis"]
    hot = int(spec.get("hot_mb", 32))
    mem = int(spec.get("mem_mb") or rnd(hot / r["fill_ratio"] + r["overhead_mb"], srv["round_mb"]))
    cpu = float(spec.get("cpu", r["cpu"]))
    return {
        "kind": "redis", "tier": "data", "managed": True,
        "cpu": cpu, "cpu_res": round(cpu / 4, 2),
        "mem": mem, "mem_res": rnd(mem * 0.5, srv["round_mb"]),
        "shares": int(spec.get("shares", r["shares"])), "pids": 60,
        "maxmemory_mb": hot, "policy": spec.get("policy", "volatile-lru"),
    }


def build_plan(cfg: dict, srv: dict) -> dict:
    d = cfg["defaults"]
    plans = {}
    for name, app in (cfg.get("apps") or {}).items():
        ap = plan_php(name, app, d, srv)
        plans[ap["container"]] = ap
        if app.get("db"):
            plans[app["db"]["name"]] = plan_pg(ap, app["db"], d, srv)
        if app.get("cache"):
            plans[app["cache"]["name"]] = plan_redis(app["cache"], d, srv)
    for name, s in (cfg.get("static") or {}).items():
        cpu = float(s["cpu"])
        plans[name] = {
            "kind": "static", "tier": s.get("tier", "app"),
            "managed": bool(s.get("managed", True)),
            "cpu": cpu, "cpu_res": round(cpu / 4, 2),
            "mem": int(s["mem"]), "mem_res": int(s["res"]),
            "shares": int(s.get("shares", 512)), "pids": int(s.get("pids", 200)),
        }
    return plans


# ----------------------------------------------------------------- 3. verify
def verify_critical_path(cfg: dict, plans: dict, srv: dict) -> int:
    cp = cfg.get("critical_path") or {}
    chains = cp.get("chains") or []
    if not chains:
        print("\nWARN: لا توجد سلاسل مسار حرج معرّفة — تخطّي الفحص")
        return 0
    lo = cp.get("min_headroom", 1.0) * srv["vcpu"]
    hi = cp.get("max_headroom", 1.8) * srv["vcpu"]
    mst = cp.get("min_stage_cpu", 0.25)
    edge_min = cp.get("edge_min_share", 0.10)
    bad = 0
    print("\n--- ميزانية المسار الحرج ---")
    for ch in chains:
        svcs = ch.get("services") or []
        missing = [s for s in svcs if s not in plans]
        if missing:
            print(f"FAIL {ch.get('name')}: خدمات غير معرّفة في الخطة: {missing}")
            bad = 1
            continue
        total = sum(plans[s]["cpu"] for s in svcs)
        weak = min(svcs, key=lambda s: plans[s]["cpu"])
        print(f"{ch['name']:<20} Σcpu={total:>5.2f}  ({total / srv['vcpu']:.2f}× vCPU)  "
              f"أضيق حلقة: {weak}={plans[weak]['cpu']:.2f}")
        if total < lo:
            print(f"  FAIL: السلسلة لا تبلغ نواة كاملة ({total:.2f} < {lo:.2f}) → "
                  f"كل طلب يُخنق حتى لو كان السيرفر خاملاً")
            bad = 1
        elif total > hi:
            print(f"  WARN: قد تبتلع {total / srv['vcpu']:.1f}× السيرفر عند الانفجار")
        for s in svcs:
            c = plans[s]["cpu"]
            if c < mst:
                print(f"  FAIL: {s} سقفه {c:.2f} < {mst} → throttling على المسار الحرج")
                bad = 1
            if plans[s].get("tier") == "edge" and total and c < total * edge_min:
                print(f"  FAIL: {s} (حدّية) حصته {c / total:.0%} < {edge_min:.0%} → عنق زجاجة للإدخال")
                bad = 1
    return bad


def verify(cfg: dict, plans: dict, srv: dict) -> int:
    sl = sum(p["mem"] for p in plans.values())
    sr = sum(p["mem_res"] for p in plans.values())
    sc = sum(p["cpu"] for p in plans.values())
    unmanaged = [n for n, p in plans.items() if not p.get("managed", True)]
    alloc = srv["allocatable_mb"]
    bad = 0

    print(f"\nالسيرفر ({srv['source']}): RAM={srv['mem_total_mb']}M  vCPU={srv['vcpu']}  "
          f"swap={srv['swap_mb']}M  cgroup={srv['cgroup']}")
    print(f"احتياطي المضيف={srv['host_reserve_mb']}M  المتاح={alloc}M  "
          f"حاويات={len(plans)} (منها {len(unmanaged)} خارج compose)")
    print(f"Σ limits       = {sl}M   ({sl / alloc:.2f}× المتاح، الحد {srv['mem_overcommit']})")
    print(f"Σ reservations = {sr}M   ({sr / alloc:.2f}× المتاح، الحد {srv['reservation_ratio']})")
    print(f"Σ cpu limits   = {sc:.2f} ({sc / srv['vcpu']:.1f}× vCPU — سقوف لا حجوزات)")
    if sl > alloc * srv["mem_overcommit"]:
        print("FAIL: إفراط في سقوف الذاكرة")
        bad = 1
    if sr > alloc * srv["reservation_ratio"]:
        print("FAIL: الحجوزات تتجاوز النسبة الآمنة")
        bad = 1

    for n, p in sorted(plans.items()):
        if p["kind"] == "php-fpm":
            ph = p["php"]
            need = (ph["baseline_mb"] + ph["opcache_mb"] + ph["interned_mb"]
                    + p["children"] * ph["worker_rss_mb"]
                    + max(0, ph["memory_limit_mb"] - ph["worker_rss_mb"]))
            if need > p["mem"]:
                print(f"FAIL {n}: قمة الطلبات لا تتسع في {p['mem']}M (تحتاج {need}M)")
                bad = 1
            if p["demand"] > p["children"]:
                print(f"WARN {n}: الطلب {p['demand']} > المتاح {p['children']} عاملاً "
                      f"→ وسّع الموارد أو انتقل إلى Octane")
        if p["kind"] == "redis" and p["maxmemory_mb"] > p["mem"] * 0.8:
            print(f"FAIL {n}: maxmemory={p['maxmemory_mb']}M قريب جداً من السقف {p['mem']}M")
            bad = 1
        if p["mem_res"] > p["mem"]:
            print(f"FAIL {n}: الحجز أكبر من السقف")
            bad = 1

    bad |= verify_critical_path(cfg, plans, srv)
    print("\nالنتيجة: " + ("فشل — عالج ما سبق" if bad else "سليم"))
    return bad


# ----------------------------------------------------------------- 4. doctor
def cg_dir(cid: str):
    for pat in (f"/sys/fs/cgroup/system.slice/docker-{cid}*.scope",
                f"/sys/fs/cgroup/docker/{cid}*",
                f"/sys/fs/cgroup/memory/docker/{cid}*"):
        m = glob.glob(pat)
        if m:
            return Path(m[0])
    return None


def rd(p: Path):
    try:
        return int(p.read_text().split()[0])
    except Exception:
        return None


def doctor():
    ids = sh("docker ps -q --no-trunc").split()
    if not ids:
        return print("لا توجد حاويات تعمل (أو تحتاج sudo)")
    print(f"{'CONTAINER':<24}{'lim':>7}{'cur':>7}{'peak':>7}{'use%':>6}"
          f"{'hitmax':>8}{'oom':>5}{'cpu':>6}{'thr%':>7}  الحكم")
    print("-" * 112)
    for cid in ids:
        try:
            ins = json.loads(sh(f"docker inspect {cid}"))[0]
        except Exception:
            continue
        name = ins["Name"].lstrip("/")
        hc = ins.get("HostConfig", {})
        lim = (hc.get("Memory") or 0) // MB
        cpu = (hc.get("NanoCpus") or 0) / 1e9
        d = cg_dir(cid)
        cur = peak = hit = oom = 0
        thr = 0.0
        if d:
            cur = (rd(d / "memory.current") or 0) // MB
            peak = ((rd(d / "memory.peak") or rd(d / "memory.max_usage_in_bytes") or 0) // MB) or cur
            ev = d / "memory.events"
            if ev.exists():
                kv = dict(l.split() for l in ev.read_text().splitlines() if len(l.split()) == 2)
                hit, oom = int(kv.get("max", 0)), int(kv.get("oom_kill", 0))
            st = d / "cpu.stat"
            if st.exists():
                kv = dict(l.split() for l in st.read_text().splitlines() if len(l.split()) == 2)
                per = int(kv.get("nr_periods", 0)) or 1
                thr = 100.0 * int(kv.get("nr_throttled", 0)) / per
        use = (100.0 * peak / lim) if lim else 0.0
        v = []
        if not lim:
            v.append("بلا حدود (drift!)")
        if oom:
            v.append("OOM! ارفع السقف فوراً")
        elif hit or use > 90:
            v.append("يضرب السقف → +25%")
        elif lim and use < 40:
            v.append("مبالغ فيه → قلّصه")
        if thr > 5:
            v.append("CPU مخنوق → ارفع السقف")
        print(f"{name:<24}{lim:>7}{cur:>7}{peak:>7}{use:>5.0f}%{hit:>8}{oom:>5}"
              f"{cpu:>6.2f}{thr:>6.1f}%  {' / '.join(v) or 'OK'}")
    print("\nملاحظة: cur/peak يشملان page cache فيبدوان متضخمين؛ "
          "الإشارة الحقيقية هي hitmax و oom و thr%.")


def measure_fpm(container: str, port: int = 8080, path: str = "/fpm-status"):
    if not container:
        return print("استخدم: rt.py fpm --container wttms-app")
    out = sh(f"docker exec {container} sh -c \"curl -s 'http://127.0.0.1:{port}{path}?json'\"")
    try:
        st = json.loads(out)
        print(f"{container}: active={st.get('active processes')}/{st.get('total processes')}  "
              f"maxActive={st.get('max active processes')}  "
              f"reached_max_children={st.get('max children reached')}  "
              f"queue={st.get('listen queue')}  slow={st.get('slow requests')}")
    except Exception:
        print(f"{container}: fpm-status غير مفعّل (أضف pm.status_path ومرّره في nginx)")
    raw = sh(f"docker exec {container} sh -c \"ps -o rss=,comm= 2>/dev/null | grep -i php\"")
    vals = sorted(int(l.split()[0]) // 1024 for l in raw.splitlines() if l.split()[0].isdigit())
    body = vals[1:] if len(vals) > 1 else vals
    if body:
        p95 = body[max(0, int(len(body) * 0.95) - 1)]
        print(f"  worker RSS: avg={sum(body) // len(body)}M  p95={p95}M  max={body[-1]}M"
              f"   → ضع p95 في worker_rss_mb")


# ----------------------------------------------------------------- 5. render
PHP_INI = """; مُولَّد بواسطة rt.py — لا تعدّله يدوياً  [{app}]
; children={children} · worker_rss={W}M · container_limit={mem}M
memory_limit = {memory_limit}M
max_execution_time = {maxexec}
max_input_time = {maxexec}
max_input_vars = 3000
post_max_size = {post}M
upload_max_filesize = {upload}M
max_file_uploads = 20
realpath_cache_size = 4096k
realpath_cache_ttl = 600
expose_php = Off
display_errors = Off
display_startup_errors = Off
log_errors = On
error_log = /proc/self/fd/2
date.timezone = {tz}
zlib.output_compression = Off

[opcache]
opcache.enable = 1
opcache.enable_cli = {cli}
opcache.memory_consumption = {opcache}
opcache.interned_strings_buffer = {interned}
opcache.max_accelerated_files = {files}
opcache.validate_timestamps = {validate}
opcache.revalidate_freq = 0
opcache.save_comments = 1
opcache.max_wasted_percentage = 10
opcache.file_cache = /tmp
opcache.jit = off
opcache.jit_buffer_size = 0
"""

WWW_CONF = """; مُولَّد بواسطة rt.py — لا تعدّله يدوياً  [{app}]
; container_limit={mem}M · memory_limit={memory_limit}M · worker_rss={W}M
[www]
{listen_block}listen.backlog = 256

pm = {pm_mode}
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


def render(cfg: dict, plans: dict, srv: dict, out: Path) -> None:
    prefix = (deep(cfg, "render", "mount_prefix") or out.as_posix()).rstrip("/")
    blocks, unmanaged_cmds = [], []

    for name, p in sorted(plans.items()):
        if not p.get("managed", True):
            unmanaged_cmds.append(
                f"docker update --cpus {p['cpu']:.2f} --cpu-shares {p['shares']} "
                f"--memory {p['mem']}m --memory-swap {p['mem']}m "
                f"--memory-reservation {p['mem_res']}m --pids-limit {p['pids']} {name}")
            continue

        b = [f"  {name}:",
             "    deploy:",
             "      resources:",
             "        limits:",
             f"          cpus: '{p['cpu']:.2f}'",
             f"          memory: {p['mem']}M",
             "        reservations:",
             f"          cpus: '{p['cpu_res']:.2f}'",
             f"          memory: {p['mem_res']}M",
             f"    cpu_shares: {p['shares']}",
             f"    pids_limit: {p['pids']}"]
        if p["tier"] == "data":
            b.append("    oom_score_adj: -500")
        elif p["tier"] in ("obs", "ops", "batch"):
            b.append("    oom_score_adj: 500")

        if p["kind"] == "php-fpm":
            ph = p["php"]
            b += ["    volumes:",
                  f"      - {prefix}/php/{p['app']}.ini:{ph['php_conf_dir']}/zz-rt.ini:ro",
                  f"      - {prefix}/php-fpm/{p['app']}.www.conf:{ph['fpm_conf_dir']}/zz-rt.conf:ro"]
        elif p["kind"] == "redis":
            args = ["redis-server", "--maxmemory", f"{p['maxmemory_mb']}mb",
                    "--maxmemory-policy", p["policy"], "--save", "",
                    "--appendonly", "no", "--tcp-backlog", "128"]
            b.append("    command: " + json.dumps(args))
        elif p["kind"] == "postgres":
            args = ["postgres"]
            for k, v in p["conf"].items():
                args += ["-c", f"{k}={v}"]
            b.append("    command: " + json.dumps(args))
        blocks.append("\n".join(b))

    write(out / "docker-compose.resources.yml",
          "# مُولَّد بواسطة rt.py — لا تعدّله يدوياً.\n"
          "# التطبيق:\n"
          "#   docker compose -f docker-compose.yml \\\n"
          f"#     -f {prefix}/docker-compose.resources.yml up -d\n"
          "services:\n" + "\n".join(blocks) + "\n")

    if unmanaged_cmds:
        write(out / "unmanaged-apply.sh",
              "#!/usr/bin/env bash\n"
              "# حاويات تعمل خارج docker-compose — تُضبط مباشرة.\n"
              "# تنبيه: هذه الإعدادات تضيع إذا أُعيد إنشاء الحاوية (docker rm/run).\n"
              "set -euo pipefail\n" + "\n".join(unmanaged_cmds) + "\n", mode=0o755)

    for name, app in (cfg.get("apps") or {}).items():
        p = plans[app.get("container", f"{name}-app")]
        ph = p["php"]
        listen_block = "" if ph["fpm_listen"] == "keep" else (
            f"listen = {ph['fpm_listen']}\n"
            "listen.owner = www-data\nlisten.group = www-data\nlisten.mode = 0660\n")
        write(out / "php" / f"{name}.ini", PHP_INI.format(
            app=name, children=p["children"], W=ph["worker_rss_mb"], mem=p["mem"],
            memory_limit=ph["memory_limit_mb"], maxexec=ph["max_execution_time"],
            post=ph["post_max_size_mb"], upload=ph["upload_max_filesize_mb"],
            tz=ph["timezone"], cli=ph["opcache_enable_cli"], opcache=ph["opcache_mb"],
            interned=ph["interned_mb"], files=ph["max_accelerated_files"],
            validate=ph["validate_timestamps"]))
        write(out / "php-fpm" / f"{name}.www.conf", WWW_CONF.format(
            app=name, mem=p["mem"], memory_limit=ph["memory_limit_mb"], W=ph["worker_rss_mb"],
            listen_block=listen_block, pm_mode=ph["pm_mode"], children=p["children"],
            start=p["start_servers"], min_spare=p["min_spare"], max_spare=p["max_spare"],
            terminate=ph["max_execution_time"] + 5))
        if app.get("db"):
            pg = plans[app["db"]["name"]]
            write(out / "postgres" / f"{name}.conf",
                  "# مرجعي فقط — القيم تُطبَّق عبر command في ملف الـ override\n"
                  f"# سقف الحاوية {pg['mem']}M\n"
                  + "\n".join(f"{k} = {v}" for k, v in pg["conf"].items()) + "\n")

    rows = ["# خطة الموارد", "",
            f"- المصدر: `{srv['source']}` · RAM **{srv['mem_total_mb']}M** · vCPU **{srv['vcpu']}** "
            f"· المتاح **{srv['allocatable_mb']}M**",
            f"- Σ limits **{sum(p['mem'] for p in plans.values())}M** · "
            f"Σ reservations **{sum(p['mem_res'] for p in plans.values())}M**", "",
            "| حاوية | نوع | CPU | shares | MEM lim | MEM res | children | مُدارة |",
            "|---|---|---|---|---|---|---|---|"]
    for n, p in sorted(plans.items()):
        rows.append(f"| {n} | {p['kind']} | {p['cpu']:.2f} | {p['shares']} | "
                    f"{p['mem']}M | {p['mem_res']}M | {p.get('children', '-')} | "
                    f"{'نعم' if p.get('managed', True) else 'لا'} |")
    write(out / "PLAN.md", "\n".join(rows) + "\n")
    print(f"تم التوليد في: {out}")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="rt.py", description="Docker Resource Tuner")
    ap.add_argument("cmd", choices=["detect", "plan", "verify", "render", "doctor", "fpm"])
    ap.add_argument("-c", "--config", default=str(ROOT / "sizing.yml"))
    ap.add_argument("-o", "--out", default=str(DEFAULT_OUT))
    ap.add_argument("--assume-mem", type=int, help="تجاوز الرام المكتشفة (MB)")
    ap.add_argument("--assume-cpu", type=int, help="تجاوز عدد النوى المكتشف")
    ap.add_argument("--container", help="لأمر fpm")
    ap.add_argument("--port", type=int, default=8080, help="منفذ fpm-status داخل الحاوية")
    a = ap.parse_args()

    if a.cmd == "doctor":
        return doctor()
    if a.cmd == "fpm":
        return measure_fpm(a.container, a.port)

    cfg = load_cfg(Path(a.config))
    srv = detect(cfg, a.assume_mem, a.assume_cpu)
    if a.cmd == "detect":
        return print(json.dumps(srv, indent=2, ensure_ascii=False))

    plans = build_plan(cfg, srv)
    if a.cmd == "plan":
        for n, p in sorted(plans.items()):
            flag = "" if p.get("managed", True) else "  [خارج compose]"
            print(f"{n:<24}{p['kind']:<10}cpu={p['cpu']:<6.2f}mem={p['mem']:<6}M "
                  f"res={p['mem_res']:<5}M children={p.get('children', '-')}{flag}")
        return sys.exit(verify(cfg, plans, srv))
    if a.cmd == "verify":
        return sys.exit(verify(cfg, plans, srv))

    render(cfg, plans, srv, Path(a.out))
    sys.exit(verify(cfg, plans, srv))


if __name__ == "__main__":
    main()
