# Microservice Builder

An AI-powered microservice scaffolding tool built with **CrewAI** that generates production-ready **Spring Boot + Dubbo + Nacos** multi-module Maven projects from natural language descriptions.

---

## Architecture

```
User Request (NL)
     │
     ▼
┌─────────────────────────────────────────────────────────┐
│                      CrewAI Crew                         │
│                                                          │
│  1. Requirement Analyst  → 解析意图，区分新建 vs 增量    │
│  2. Architect            → 模块/端口/依赖布局            │
│  3. Code Generator       → 调用 skills 生成新代码        │
│  4. Incremental Modifier → diff 预览 + 人工确认增量修改  │
│  5. Configuration Mgr    → yml / Nacos / Docker          │
│  6. Validator            → mvn compile 验证              │
└─────────────────────────────────────────────────────────┘
     │
     ▼
output/<project>/
  ├── pom.xml                ← Parent Maven POM
  ├── docker-compose.yml     ← Nacos（单例复用）+ 所有服务
  ├── .project_state.db      ← SQLite 状态库（跨 session 记忆）
  ├── service-user/
  │   ├── pom.xml
  │   ├── Dockerfile
  │   └── src/main/java/...
  └── service-order/
      └── ...
```

---

## 核心特性

### 渐进式开发（Incremental Development）
- **跨 session 状态记忆**：所有模块、接口、方法均持久化到 SQLite（`.project_state.db`），下次运行自动恢复
- **方法级去重**：`method_records` 表记录每个类已有的方法名，增量修改前先查 DB，彻底避免重复定义导致的编译错误
- **文件扫描补录**：启动时自动扫描已有 `.java` 文件，将手动添加的方法也纳入状态管理
- **变更历史**：每次文件修改记录到 `change_history` 表，含 diff 摘要，支持审计

### Nacos 单例复用
- 通过 `docker inspect` 真实检测 `nacos` 容器是否运行
- **已运行**：新项目的 `docker-compose.yml` 使用 `external: true` 网络，直接加入已有 `microservice-net`，不重复声明 nacos service
- **未运行**：完整声明 nacos service，启动后标记全局状态（`PROJECTS_ROOT/.global_state.json`）
- 全局状态跨项目共享，多个微服务项目公用同一个 Nacos 实例

### 智能新建 vs 增量修改
- `Requirement Analyst` 先查询项目状态，自动判断哪些是全新需求、哪些是对已有服务的扩展
- 已存在的模块/接口：路由到 `Incremental Modifier`（展示 diff，等待确认）
- 全新模块/接口：路由到 `Code Generator`（直接生成）

---

## Quick Start

### 1. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 至少设置 OPENAI_API_KEY
```

`.env` 关键配置：
```ini
OPENAI_API_KEY=your_key_here
OPENAI_MODEL_NAME=gpt-4o
PROJECTS_ROOT=./output          # 生成项目的根目录
NACOS_HOST=localhost
NACOS_PORT=8848
DOCKER_REGISTRY_MIRROR=         # 可选，如 mirrors.aliyun.com
```

### 3. 新建项目

```bash
python main.py create \
  --project my-shop \
  --request "创建用户服务（注册、登录），以及订单服务（调用用户服务验证用户后创建订单）。订单服务需要高并发线程池。"
```

### 4. 增量修改（跨 session）

```bash
# 第二天再来，给订单服务加取消订单和退款功能
python main.py modify \
  --project my-shop \
  --request "给 OrderService 增加 cancelOrder 和 refundOrder 方法"
# → 自动识别 OrderService 已存在，只添加缺少的两个方法
# → 展示 diff，等待确认后写入
```

### 5. 启动所有服务

```bash
cd output/my-shop
docker-compose up -d

# Nacos 控制台:  http://localhost:8848/nacos  (nacos/nacos)
# User 服务:     http://localhost:8081/actuator/health
# Order 服务:    http://localhost:8082/actuator/health
```

### 6. 新增微服务（Nacos 已运行时）

```bash
# nacos 容器已在运行，新加一个支付服务
python main.py create \
  --project my-shop \
  --request "新增支付服务，提供 pay 和 refund 方法"

# 重新生成 docker-compose（自动复用已有 nacos）
python main.py docker --project my-shop
cd output/my-shop
docker-compose up -d service-payment   # 只启动新服务，nacos 继续原来的
```

---

## CLI Reference

```bash
# 新建项目 / 首次生成
python main.py create   --project NAME --request "..."

# 增量修改已有项目
python main.py modify   --project NAME --request "..."

# 查看项目状态（模块、接口、已知方法、Nacos 状态）
python main.py status   --project NAME

# 编译验证
python main.py validate --project NAME [--module MODULE_NAME]

# 刷新 docker-compose.yml（自动处理 Nacos 复用）
python main.py docker   --project NAME

# Skill 管理
python main.py skill list
python main.py skill run SKILL_NAME --project NAME --params '{...}'
```

---

## 生成的项目结构

```
output/<project>/
├── pom.xml                                    # Parent POM (Spring Boot 3.2, Dubbo 3.2.6)
├── docker-compose.yml                         # nacos + 所有模块（nacos 可为 external）
├── .project_state.db                          # SQLite 状态（跨 session 记忆）
└── service-<name>/
    ├── pom.xml
    ├── Dockerfile
    └── src/main/java/com/example/<name>/
        ├── <Name>Application.java             # @SpringBootApplication @EnableDubbo
        ├── api/
        │   └── <Name>Service.java             # Dubbo 接口
        ├── service/
        │   └── <Name>ServiceImpl.java         # @DubboService 实现
        ├── controller/
        │   └── <Name>Controller.java          # @RestController
        ├── entity/
        │   └── <Name>.java                    # 领域实体
        └── config/
            └── ThreadPoolConfig.java          # ExecutorService beans（按需生成）
    └── src/main/resources/
        └── application.yml
```

---

## Skills

| Skill | 描述 | 幂等 |
|-------|------|------|
| `create_spring_boot_module` | 新建模块（pom、Application、yml） | ✅ 已存在返回 `already_exists` |
| `generate_dubbo_interface` | 生成 Dubbo 接口 + `@DubboService` 实现 | ✅ |
| `generate_rest_controller` | 生成 `@RestController`，注入 Dubbo 引用 | ✅ |
| `generate_entity` | 生成 JPA / POJO 实体（含 getter/setter） | ✅ |
| `add_threadpool_config` | 生成 `ThreadPoolConfig.java`（双池：async + dubbo） | ✅ |
| `modify_dubbo_config` | 通过 dot-path 修改 `application.yml` 任意配置 | ➕ 覆写 |
| `update_nacos_config` | 推送配置到运行中的 Nacos Config Center | ➕ 覆写 |
| `update_docker_compose` | 生成/更新 `docker-compose.yml`，自动处理 Nacos 单例 | ✅ 服务幂等合并 |
| `incremental_modify` | 增量添加方法/字段/注解，跨 session 去重，diff 确认 | ✅ DB 级去重 |

---

## 状态管理说明

```
output/
├── .global_state.json          ← 全局 Nacos 状态（所有项目共享）
└── my-shop/
    ├── .project_state.db       ← 项目状态（SQLite）
    │   ├── modules             模块信息（端口、包名、flags）
    │   ├── interfaces          Dubbo 接口及方法签名
    │   ├── entities            实体类及字段
    │   ├── method_records      每个类已有方法（跨 session 去重核心）
    │   ├── change_history      变更记录（时间、类型、diff 摘要）
    │   └── kv                  project-level 键值对
    └── ...
```

---

## 技术栈

| 组件 | 版本 |
|------|------|
| Spring Boot | 3.2.0 |
| Apache Dubbo | 3.2.6 |
| Nacos Client | 2.3.0 |
| Spring Cloud Alibaba | 2022.0.0.0 |
| Java | 17 |
| Maven | 3.9+ |
| Python | 3.10+ |
| CrewAI | ≥ 0.28.0 |

---

## 环境要求

- Python 3.10+
- Java 17+ & Maven 3.9+（用于 `validate` 命令）
- Docker & Docker Compose（用于运行生成的服务）
- OpenAI API Key（或通过 `OPENAI_API_BASE` 配置兼容接口）

sudo apt update
sudo apt install -y maven