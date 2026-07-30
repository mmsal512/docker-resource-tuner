<div align="center">

# 🐳 Docker Resource Tuner

**Derive container limits & PHP-FPM settings from one config file — no guesswork, no OOM kills.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8+-yellow.svg)](https://www.python.org/)
[![Docker Compose](https://img.shields.io/badge/Docker_Compose-v2-2496ED.svg)](https://docs.docker.com/compose/)

</div>

---

## 🌐 Language / اللغة

- [English](#english)
- [العربية](#العربية)

---

<a id="english"></a>

# English

## 📖 What is Docker Resource Tuner?

**Docker Resource Tuner** is a Python tool that reads your server's actual resources (RAM, CPU) and a single YAML configuration file (`sizing.yml`), then automatically calculates and generates:

- **Container resource limits** — CPU, memory, PIDs for every service
- **PHP-FPM tuning** — `php.ini` and `www.conf` tailored to your exact memory budget
- **PostgreSQL tuning** — connection limits, shared buffers, work_mem
- **Redis tuning** — maxmemory and eviction policy
- **Critical-path analysis** — ensures no bottleneck along the request chain

All output is written as a **Docker Compose override file** (`docker-compose.resources.yml`), so your original `docker-compose.yml` stays untouched.

---

## 🔥 The Problem

Managing Docker container resources manually leads to two dangerous outcomes:

| Scenario | What Happens | Result |
|---|---|---|
| **Over-provisioning** | Every container gets generous limits | Containers compete, OOM killer strikes, services crash randomly |
| **Under-provisioning** | Limits are too tight | CPU throttling, slow responses, queue backlogs |
| **No limits at all** | Resources left at Docker defaults | One runaway process consumes all RAM, entire server goes down |

### Specific pain points:

- 🔴 **OOM Kills** — PHP-FPM spawns too many workers → container exceeds memory limit → kernel kills it
- 🔴 **CPU Throttling** — database gets 0.25 CPU cores → every query is slow → entire app bottlenecked
- 🔴 **Config Drift** — `php.ini` says `memory_limit=256M` but container only has 192M → guaranteed OOM
- 🔴 **Guesswork** — "how many FPM workers should I run on 8GB RAM with 3 apps?" — no one knows without math

### The Solution

Docker Resource Tuner uses **Little's Law** and **memory accounting** to calculate the optimal number of PHP-FPM workers, then derives all container limits mathematically:

```
Workers = min(CPU_supply, Traffic_demand, Memory_budget)
Container_Limit = Baseline + (Workers × Worker_RSS) + Peak + CLI + Buffer
```

**One config file → one command → all resources calculated and applied.**

---

## ⚙️ How It Works

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────────────────────────┐
│                  │     │              │     │  out/                           │
│  sizing.yml      │────▶│  bin/rt.py   │────▶│  ├── docker-compose.resources.yml│
│  (your config)   │     │  (engine)    │     │  ├── php/app1.ini               │
│                  │     │              │     │  ├── php-fpm/app1.www.conf       │
└─────────────────┘     └──────────────┘     │  ├── postgres/app1.conf          │
                              │              │  ├── PLAN.md                      │
                              ▼              │  └── unmanaged-apply.sh           │
                        ┌──────────────┐     └─────────────────────────────────┘
                        │ /proc/meminfo│                    │
                        │ (auto-detect)│                    ▼
                        └──────────────┘     ┌─────────────────────────────────┐
                                             │  docker compose                 │
                                             │    -f docker-compose.yml        │
                                             │    -f out/docker-compose.       │
                                             │       resources.yml             │
                                             │    up -d                        │
                                             └─────────────────────────────────┘
```

### Workflow:

1. **Configure** → Write your `sizing.yml` describing apps, databases, and static services
2. **Detect** → The tool reads server resources from `/proc/meminfo` (or uses `assume` values)
3. **Plan** → Calculates worker counts, memory budgets, CPU limits using mathematical models
4. **Verify** → Validates totals don't exceed server capacity, checks critical path budgets
5. **Render** → Generates all config files into `out/` directory
6. **Apply** → Merge with your docker-compose via the override mechanism

---

## 🚀 Installation & Usage

### Prerequisites

- Python 3.8+ (no pip packages needed — only `PyYAML`)
- Docker & Docker Compose v2
- Linux server (for auto-detection; Windows/macOS require `server.assume`)

### Step 1: Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/docker-resource-tuner.git
cd docker-resource-tuner
```

### Step 2: Install Dependencies

```bash
# Debian / Ubuntu
sudo apt install -y python3-yaml

# Or via pip
pip install pyyaml
```

### Step 3: Create Your Configuration

```bash
# Copy the example file
cp examples/sizing.example.yml sizing.yml

# Edit with your real values
nano sizing.yml
```

> [!IMPORTANT]
> Replace all example values in `sizing.yml` with your actual service names, server specs, and traffic patterns. The service names **must match** your `docker-compose.yml` service names exactly.

### Step 4: Detect Server Resources

```bash
python3 bin/rt.py detect
```

**Output:**
```json
{
  "source": "detected",
  "vcpu": 2,
  "mem_total_mb": 8192,
  "swap_mb": 0,
  "host_reserve_mb": 1024,
  "allocatable_mb": 7168,
  "mem_overcommit": 1.35,
  "cgroup": "v2",
  "kernel": "6.1.0-18-amd64"
}
```

> [!TIP]
> On Windows or macOS, set `server.assume.mem_total_mb` and `server.assume.vcpu` in your `sizing.yml` to match your target server.

### Step 5: Preview the Plan (Dry Run)

```bash
python3 bin/rt.py plan
```

**Output:**
```
app1-web              php-fpm   cpu=1.20  mem=736   M res=256  M children=6
app1-db               postgres  cpu=0.50  mem=320   M res=128  M children=-
app1-redis             redis    cpu=0.30  mem=96    M res=48   M children=-
traefik               static    cpu=0.60  mem=192   M res=96   M children=-
...

Server (detected): RAM=8192M  vCPU=2  swap=0M  cgroup=v2
Σ limits       = 4832M   (0.67× allocatable)
Σ reservations = 1856M   (0.26× allocatable)
Result: OK
```

This shows exactly what will be generated — **no files are written**.

### Step 6: Generate Resource Files

```bash
python3 bin/rt.py render
```

This creates the following files in `out/`:

| File | Purpose |
|---|---|
| `docker-compose.resources.yml` | Resource limits overlay for Docker Compose |
| `php/<app>.ini` | Auto-tuned php.ini (memory_limit, OPcache, etc.) |
| `php-fpm/<app>.www.conf` | Auto-tuned FPM pool config (workers, spare servers) |
| `postgres/<app>.conf` | Reference PostgreSQL config |
| `PLAN.md` | Human-readable resource allocation summary |
| `unmanaged-apply.sh` | Script for containers outside docker-compose |

### Step 7: Verify Service Names (Optional)

```bash
python3 bin/rt.py plan --compose /path/to/docker-compose.yml
```

This cross-checks that every service in `sizing.yml` exists in your `docker-compose.yml` and warns about services without resource limits.

### Step 8: Health Check (Running Containers)

```bash
python3 bin/rt.py doctor
```

**Output:**
```
CONTAINER              lim    cur   peak  use%  hitmax  oom   cpu   thr%  Verdict
────────────────────────────────────────────────────────────────────────────
app1-web               736    412    680   92%       3    0  1.20   2.1%  Hitting limit → +25%
app1-db                320    180    220   69%       0    0  0.50   0.0%  OK
app1-redis              96     32     48   50%       0    0  0.30   0.0%  OK
traefik                192     64     96   50%       0    0  0.60   0.0%  OK
```

The `doctor` command reads real-time cgroup data and recommends adjustments.

### Step 9: Measure FPM Workers (Fine-tuning)

```bash
python3 bin/rt.py fpm --container app1-web
```

**Output:**
```
app1-web: active=3/6  maxActive=5  reached_max_children=0  queue=0  slow=0
  worker RSS: avg=65M  p95=72M  max=78M   → set p95 in worker_rss_mb
```

Use the `p95` value to update `defaults.php.worker_rss_mb` in your `sizing.yml`.

---

## 🚢 Deployment

Apply the generated resources by merging both compose files:

```bash
docker compose \
  -f docker-compose.yml \
  -f out/docker-compose.resources.yml \
  up -d
```

For containers **outside** docker-compose (marked `managed: false`):

```bash
sudo bash out/unmanaged-apply.sh
```

### Typical Workflow:

```bash
# 1. Edit sizing.yml
nano sizing.yml

# 2. Validate
python3 bin/rt.py plan --compose docker-compose.yml

# 3. Generate
python3 bin/rt.py render

# 4. Deploy
docker compose -f docker-compose.yml \
  -f out/docker-compose.resources.yml up -d

# 5. Monitor
python3 bin/rt.py doctor
```

---

## 📁 Project Structure

```
docker-resource-tuner/
├── bin/
│   └── rt.py                    # Main engine
├── out/                         # Generated files (gitignored)
│   ├── docker-compose.resources.yml
│   ├── php/
│   ├── php-fpm/
│   ├── postgres/
│   ├── PLAN.md
│   └── unmanaged-apply.sh
├── examples/
│   ├── sizing.example.yml       # Example configuration
│   └── docker-compose.example.yml
├── sizing.yml                   # Your real config (gitignored)
├── .gitignore
└── README.md
```

---

## 🔒 Security Notice

> [!CAUTION]
> **Do NOT commit your real configuration files to a public repository!**
>
> The following files contain sensitive information about your server infrastructure (RAM, CPU, service names, number of containers, resource budgets):
>
> - `sizing.yml` — Your real server configuration
> - `out/` directory — Generated resource files
> - `docker-compose.yml` — Your real service definitions
>
> **Always ensure these are in your `.gitignore`:**
>
> ```gitignore
> sizing.yml
> sizing.local.yml
> out/*
> !out/.gitkeep
> docker-compose.override.yml
> docker-compose.*.yml
> !docker-compose.example.yml
> ```
>
> Only commit the sanitized `examples/` files to your public repository.

---

## 📝 Command Reference

| Command | Description |
|---|---|
| `rt.py detect` | Print detected server resources (RAM, CPU, cgroup version) |
| `rt.py plan` | Calculate and display the resource plan (dry run) |
| `rt.py verify` | Validate totals, constraints, and critical path budgets |
| `rt.py render` | Generate all output files into `out/` |
| `rt.py doctor` | Read live cgroup stats and recommend adjustments |
| `rt.py fpm --container <name>` | Measure PHP-FPM worker memory inside a container |

### Flags

| Flag | Description |
|---|---|
| `-c, --config <path>` | Path to sizing.yml (default: `sizing.yml`) |
| `-o, --out <path>` | Output directory (default: `out/`) |
| `--compose <path>` | Path to docker-compose.yml for name validation |
| `--assume-mem <MB>` | Override detected RAM |
| `--assume-cpu <N>` | Override detected CPU count |
| `--container <name>` | Container name for `fpm` command |
| `--port <N>` | FPM status port (default: 8080) |

---

## 📜 License

This project is licensed under the [MIT License](LICENSE).

---

---

<a id="العربية"></a>

<div dir="rtl" align="right">

# العربية

## 📖 ما هو Docker Resource Tuner؟

**Docker Resource Tuner** هو أداة بايثون تقرأ موارد الخادم الفعلية (RAM, CPU) وملف إعدادات YAML واحد (`sizing.yml`)، ثم تحسب وتولّد تلقائياً:

- **حدود موارد الحاويات** — CPU, ذاكرة, PIDs لكل خدمة
- **ضبط PHP-FPM** — ملفات `php.ini` و `www.conf` مُعَدَّة حسب ميزانية الذاكرة الفعلية
- **ضبط PostgreSQL** — حدود الاتصالات, shared buffers, work_mem
- **ضبط Redis** — maxmemory وسياسة الإخلاء
- **تحليل المسار الحرج** — يضمن عدم وجود عنق زجاجة في سلسلة الطلب

جميع المخرجات تُكتب كملف **Docker Compose Override** ‏(`docker-compose.resources.yml`)، بحيث يبقى ملف `docker-compose.yml` الأصلي دون تعديل.

---

## 🔥 المشكلة

إدارة موارد حاويات Docker يدوياً تؤدي إلى نتيجتين خطيرتين:

| السيناريو | ماذا يحدث | النتيجة |
|---|---|---|
| **تخصيص مبالغ فيه** | كل حاوية تأخذ حدوداً سخية | الحاويات تتنافس، OOM Killer يضرب، الخدمات تنهار عشوائياً |
| **تخصيص ناقص** | الحدود ضيقة جداً | اختناق المعالج، استجابات بطيئة، تراكم الطوابير |
| **بدون حدود** | الموارد على إعدادات Docker الافتراضية | عملية واحدة شاردة تستهلك كل الرام، الخادم بالكامل ينهار |

### نقاط الألم:

- 🔴 **OOM Kills** — PHP-FPM يُنشئ عمّالاً أكثر من اللازم ← الحاوية تتجاوز حد الذاكرة ← النواة تقتلها
- 🔴 **اختناق المعالج** — قاعدة البيانات تأخذ 0.25 نواة ← كل استعلام بطيء ← التطبيق بالكامل يتأثر
- 🔴 **انحراف الإعدادات** — `php.ini` يقول `memory_limit=256M` لكن الحاوية تملك 192M فقط ← OOM مضمون
- 🔴 **التخمين** — "كم عامل FPM أحتاج على 8GB رام مع 3 تطبيقات؟" — لا أحد يعرف بدون حسابات

### الحل

Docker Resource Tuner يستخدم **قانون Little** و**محاسبة الذاكرة** لحساب العدد الأمثل لعمّال PHP-FPM، ثم يشتق جميع حدود الحاويات رياضياً:

<div dir="ltr">

```
Workers = min(CPU_supply, Traffic_demand, Memory_budget)
Container_Limit = Baseline + (Workers × Worker_RSS) + Peak + CLI + Buffer
```

</div>

**ملف إعدادات واحد → أمر واحد → جميع الموارد محسوبة ومُطبَّقة.**

---

## ⚙️ كيف تعمل الأداة

<div dir="ltr">

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────────────────────────┐
│                  │     │              │     │  out/                           │
│  sizing.yml      │────▶│  bin/rt.py   │────▶│  ├── docker-compose.resources.yml│
│  (ملف الإعدادات)  │     │  (المحرك)    │     │  ├── php/app1.ini               │
│                  │     │              │     │  ├── php-fpm/app1.www.conf       │
└─────────────────┘     └──────────────┘     │  ├── postgres/app1.conf          │
                              │              │  ├── PLAN.md                      │
                              ▼              │  └── unmanaged-apply.sh           │
                        ┌──────────────┐     └─────────────────────────────────┘
                        │ /proc/meminfo│                    │
                        │ (كشف تلقائي) │                    ▼
                        └──────────────┘     ┌─────────────────────────────────┐
                                             │  docker compose                 │
                                             │    -f docker-compose.yml        │
                                             │    -f out/docker-compose.       │
                                             │       resources.yml             │
                                             │    up -d                        │
                                             └─────────────────────────────────┘
```

</div>

### دورة العمل:

1. **الإعداد** → اكتب `sizing.yml` يصف تطبيقاتك وقواعد بياناتك والخدمات الثابتة
2. **الاكتشاف** → الأداة تقرأ موارد الخادم من `/proc/meminfo` (أو تستخدم قيم `assume`)
3. **التخطيط** → تحسب عدد العمّال وميزانيات الذاكرة وحدود المعالج باستخدام نماذج رياضية
4. **التحقق** → تتأكد أن المجاميع لا تتجاوز سعة الخادم وتفحص ميزانيات المسار الحرج
5. **التوليد** → تُنتج جميع ملفات الإعدادات في مجلد `out/`
6. **التطبيق** → ادمجها مع docker-compose عبر آلية Override

---

## 🚀 التثبيت والاستخدام

### المتطلبات

- Python 3.8+ (لا حاجة لحزم pip — فقط `PyYAML`)
- Docker و Docker Compose v2
- خادم Linux (للكشف التلقائي؛ Windows/macOS تتطلب `server.assume`)

### الخطوة 1: نسخ المستودع

<div dir="ltr">

```bash
git clone https://github.com/YOUR_USERNAME/docker-resource-tuner.git
cd docker-resource-tuner
```

</div>

### الخطوة 2: تثبيت المتطلبات

<div dir="ltr">

```bash
# Debian / Ubuntu
sudo apt install -y python3-yaml

# أو عبر pip
pip install pyyaml
```

</div>

### الخطوة 3: إنشاء ملف الإعدادات

<div dir="ltr">

```bash
# انسخ ملف المثال
cp examples/sizing.example.yml sizing.yml

# عدّل بقيمك الحقيقية
nano sizing.yml
```

</div>

> [!IMPORTANT]
> استبدل جميع القيم المثالية في `sizing.yml` بأسماء خدماتك الفعلية ومواصفات خادمك وأنماط حركة المرور. أسماء الخدمات **يجب أن تتطابق** مع أسماء الخدمات في `docker-compose.yml` بالضبط.

### الخطوة 4: اكتشاف موارد الخادم

<div dir="ltr">

```bash
python3 bin/rt.py detect
```

</div>

**المخرجات:**
<div dir="ltr">

```json
{
  "source": "detected",
  "vcpu": 2,
  "mem_total_mb": 8192,
  "swap_mb": 0,
  "host_reserve_mb": 1024,
  "allocatable_mb": 7168,
  "mem_overcommit": 1.35,
  "cgroup": "v2",
  "kernel": "6.1.0-18-amd64"
}
```

</div>

> [!TIP]
> على Windows أو macOS، عيّن `server.assume.mem_total_mb` و `server.assume.vcpu` في ملف `sizing.yml` بما يتطابق مع خادمك المستهدف.

### الخطوة 5: معاينة الخطة (تشغيل جاف)

<div dir="ltr">

```bash
python3 bin/rt.py plan
```

</div>

هذا يعرض بالضبط ما سيتم توليده — **لا تُكتب أي ملفات**.

### الخطوة 6: توليد ملفات الموارد

<div dir="ltr">

```bash
python3 bin/rt.py render
```

</div>

هذا يُنشئ الملفات التالية في مجلد `out/`:

| الملف | الوظيفة |
|---|---|
| `docker-compose.resources.yml` | ملف حدود الموارد الإضافي لـ Docker Compose |
| `php/<app>.ini` | ملف php.ini مُضبوط تلقائياً |
| `php-fpm/<app>.www.conf` | إعدادات FPM pool مُضبوطة تلقائياً |
| `postgres/<app>.conf` | إعدادات PostgreSQL مرجعية |
| `PLAN.md` | ملخص تخصيص الموارد بشكل مقروء |
| `unmanaged-apply.sh` | سكربت للحاويات خارج docker-compose |

### الخطوة 7: فحص صحة الحاويات (حاويات تعمل)

<div dir="ltr">

```bash
python3 bin/rt.py doctor
```

</div>

أمر `doctor` يقرأ بيانات cgroup في الوقت الفعلي ويوصي بالتعديلات اللازمة.

### الخطوة 8: قياس عمّال FPM (ضبط دقيق)

<div dir="ltr">

```bash
python3 bin/rt.py fpm --container app1-web
```

</div>

**المخرجات:**
<div dir="ltr">

```
app1-web: active=3/6  maxActive=5  reached_max_children=0  queue=0  slow=0
  worker RSS: avg=65M  p95=72M  max=78M   → set p95 in worker_rss_mb
```

</div>

استخدم قيمة `p95` لتحديث `defaults.php.worker_rss_mb` في ملف `sizing.yml`.

---

## 🚢 النشر والتطبيق

طبّق الموارد المُولَّدة بدمج ملفَي compose:

<div dir="ltr">

```bash
docker compose \
  -f docker-compose.yml \
  -f out/docker-compose.resources.yml \
  up -d
```

</div>

للحاويات **خارج** docker-compose (المُعلَّمة `managed: false`):

<div dir="ltr">

```bash
sudo bash out/unmanaged-apply.sh
```

</div>

### سير العمل المعتاد:

<div dir="ltr">

```bash
# 1. عدّل sizing.yml
nano sizing.yml

# 2. تحقق
python3 bin/rt.py plan --compose docker-compose.yml

# 3. ولّد
python3 bin/rt.py render

# 4. انشر
docker compose -f docker-compose.yml \
  -f out/docker-compose.resources.yml up -d

# 5. راقب
python3 bin/rt.py doctor
```

</div>

---

## 📁 هيكل المشروع

<div dir="ltr">

```
docker-resource-tuner/
├── bin/
│   └── rt.py                    # المحرك الرئيسي
├── out/                         # الملفات المُولَّدة (محجوبة بـ gitignore)
│   ├── docker-compose.resources.yml
│   ├── php/
│   ├── php-fpm/
│   ├── postgres/
│   ├── PLAN.md
│   └── unmanaged-apply.sh
├── examples/
│   ├── sizing.example.yml       # إعدادات مثالية
│   └── docker-compose.example.yml
├── sizing.yml                   # إعداداتك الحقيقية (محجوب بـ gitignore)
├── .gitignore
└── README.md
```

</div>

---

## 🔒 ملاحظة أمنية هامة

> [!CAUTION]
> **لا ترفع ملفات إعداداتك الحقيقية إلى مستودع عام!**
>
> الملفات التالية تحتوي على معلومات حساسة عن بنية خادمك (حجم الرام، عدد النوى، أسماء الخدمات، عدد الحاويات، ميزانيات الموارد):
>
> - `sizing.yml` — إعدادات خادمك الحقيقية
> - مجلد `out/` — ملفات الموارد المُولَّدة
> - `docker-compose.yml` — تعريفات خدماتك الحقيقية
>
> **تأكد دائماً من وجودها في `.gitignore`:**
>
> <div dir="ltr">
>
> ```gitignore
> sizing.yml
> sizing.local.yml
> out/*
> !out/.gitkeep
> docker-compose.override.yml
> docker-compose.*.yml
> !docker-compose.example.yml
> ```
>
> </div>
>
> ارفع فقط ملفات `examples/` المُعقَّمة إلى مستودعك العام.

---

## 📝 مرجع الأوامر

| الأمر | الوصف |
|---|---|
| `rt.py detect` | اطبع موارد الخادم المكتشفة (RAM, CPU, إصدار cgroup) |
| `rt.py plan` | احسب واعرض خطة الموارد (تشغيل جاف) |
| `rt.py verify` | تحقق من المجاميع والقيود وميزانيات المسار الحرج |
| `rt.py render` | ولّد جميع الملفات في مجلد `out/` |
| `rt.py doctor` | اقرأ إحصائيات cgroup الحية وأوصِ بتعديلات |
| `rt.py fpm --container <name>` | قِس ذاكرة عمّال PHP-FPM داخل حاوية |

### الخيارات

| الخيار | الوصف |
|---|---|
| `-c, --config <path>` | مسار ملف sizing.yml (افتراضي: `sizing.yml`) |
| `-o, --out <path>` | مجلد المخرجات (افتراضي: `out/`) |
| `--compose <path>` | مسار docker-compose.yml للتحقق من الأسماء |
| `--assume-mem <MB>` | تجاوز الرام المكتشف |
| `--assume-cpu <N>` | تجاوز عدد النوى المكتشف |
| `--container <name>` | اسم الحاوية لأمر `fpm` |
| `--port <N>` | منفذ حالة FPM (افتراضي: 8080) |

---

## 📜 الرخصة

هذا المشروع مرخّص بموجب [رخصة MIT](LICENSE).

</div>
