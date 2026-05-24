# Microservice Builder

An AI-powered microservice scaffolding tool built with **CrewAI** that generates production-ready **Spring Boot + Dubbo + Nacos** multi-module Maven projects from natural language descriptions.

---

## Architecture

```
User Request (Natural Language)
     │
     ▼
┌─────────────────────────────────────────────────────────┐
│                      CrewAI Crew                         │
│                                                          │
│  1. Requirement Analyst  → parse intent, new vs incremental │
│  2. Architect            → module / port / dependency layout │
│  3. Code Generator       → invoke skills to generate code   │
│  4. Incremental Modifier → diff existing code, add only new │
│  5. Configuration Mgr    → yml / Nacos / Docker             │
│  6. Validator            → mvn compile verification         │
└─────────────────────────────────────────────────────────┘
     │
     ▼
output/<project>/
  ├── pom.xml                ← Parent Maven POM
  ├── docker-compose.yml     ← Nacos (singleton reuse) + all services
  ├── .project_state.db      ← SQLite state (cross-session memory)
  ├── user-service/
  │   ├── pom.xml
  │   ├── Dockerfile
  │   └── src/main/java/...
  └── order-service/
      └── ...
```

---

## Key Features

### Cross-session State Memory
All modules, interfaces, and methods are persisted to SQLite (`.project_state.db`). The next run automatically restores the full project state, so incremental modifications always know what already exists.

### Method-level Deduplication
The `method_records` table tracks every known method per class. Before any incremental modification, the system queries the DB and only adds methods that are genuinely missing — no duplicate definition errors.

### Nacos Singleton Reuse
Uses `docker inspect` to detect whether a `nacos` container is already running:
- **Running**: new `docker-compose.yml` uses `external: true` network and joins the existing `microservice-net`, without re-declaring nacos
- **Not running**: full nacos service declaration is generated; status is written to `.global_state.json` after startup

Multiple projects share the same Nacos instance automatically.

### Auto DTO Generation
Before generating any `@RestController`, the system scans all endpoint parameter types. Any custom type (non-primitive, non-String) that is missing a DTO file gets one generated automatically — compilation never fails due to a missing DTO.

### Dubbo Consumer Wiring
When a controller references a Dubbo provider interface, the skill resolves the correct RPC method by matching parameter types exactly. If no exact match is found, a clearly annotated `TODO` comment with the full method list is generated instead of broken code.

---

## Setup

### 1. Create and activate the Conda environment

```bash
conda create -n rpc python=3.10
conda activate rpc
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:
```ini
OPENAI_API_KEY=your_key_here
OPENAI_MODEL_NAME=deepseek-ai/DeepSeek-V3   # or gpt-4o, etc.
OPENAI_API_BASE=https://your-api-base/v1     # omit if using OpenAI directly
PROJECTS_ROOT=./output
NACOS_HOST=localhost
NACOS_PORT=8848
```

### 3. Start Nacos (first time only)

Nacos must be running before you start any generated service. Start it once; all subsequent projects will reuse the same container.

```bash
cd output/<any-project>
docker compose up -d nacos
```

If you have already generated a project, just run:
```bash
cd output/my-shop
docker compose up -d
```

---

## Usage

### Create a new project

```bash
conda activate rpc

python main.py create \
  --project my-shop \
  --request "Create a user-service (login, register) and an order-service (create order). Order-service must validate user via user-service before processing orders."
```

### Incremental modification (cross-session)

```bash
# Next day — add new methods to an existing service
python main.py modify \
  --project my-shop \
  --request "Add cancelOrder and refundOrder methods to OrderService"
# → Detects OrderService already exists
# → Only adds the missing methods, no overwrite
```

### Check project state

```bash
python main.py status --project my-shop
# Shows: all modules, ports, interfaces, known methods, Nacos status
```

### Compile and validate

```bash
python main.py validate --project my-shop
# Optional: validate a single module only
python main.py validate --project my-shop --module order-service
```

### Refresh docker-compose.yml

```bash
python main.py docker --project my-shop
# Regenerates docker-compose.yml with Nacos reuse handled automatically
```

### Skill management

```bash
# List available skills
python main.py skill list

# Run a skill directly (useful for debugging)
python main.py skill run create_spring_boot_module \
  --project my-shop \
  --params '{"module_name": "payment-service"}'
```

---

## Running the Generated Services

After generation is complete:

```bash
cd output/my-shop

# Build all modules (skip tests)
mvn clean package -DskipTests

# Start all services
docker compose up -d

# Check running containers
docker compose ps
```

Service endpoints (default ports):
```
Nacos console:   http://localhost:8848/nacos   (nacos / nacos)
user-service:    http://localhost:8051
order-service:   http://localhost:8052
```

---

## Docker Debug Commands

```bash
# View all running containers
docker ps

# View logs for a specific service
docker compose logs -f user-service
docker compose logs -f order-service
docker compose logs -f nacos

# Restart a single service after code change
docker compose up -d --build user-service

# Rebuild and restart everything
docker compose down
docker compose up -d --build

# Open a shell inside a container
docker exec -it user-service bash

# Check if Nacos container is running (used internally by the tool)
docker inspect --format '{{.State.Running}}' nacos

# View Nacos registered services
curl http://localhost:8848/nacos/v1/ns/instance/list?serviceName=user-service

# Remove all generated containers and volumes (full reset)
docker compose down -v

# Remove the output folder and regenerate from scratch
rm -rf output/my-shop
python main.py create --project my-shop --request "..."
```

---

## Generated Project Structure

```
output/<project>/
├── pom.xml                                      # Parent POM
├── docker-compose.yml
├── .project_state.db                            # SQLite state
└── <module-name>/
    ├── pom.xml
    ├── Dockerfile
    └── src/main/java/com/example/<module>/
        ├── <Module>Application.java             # @SpringBootApplication @EnableDubbo
        ├── api/
        │   └── <Name>Service.java               # Dubbo interface
        ├── service/
        │   └── <Name>ServiceImpl.java           # @DubboService implementation
        ├── controller/
        │   └── <Name>Controller.java            # @RestController
        ├── dto/
        │   └── <Name>DTO.java                   # Auto-generated if needed
        ├── entity/
        │   └── <Name>.java                      # Domain entity (if any)
        └── config/
            └── ThreadPoolConfig.java            # ExecutorService beans (on demand)
    └── src/main/resources/
        └── application.yml
```

---

## Skills Reference

| Skill | What it generates | Idempotent |
|---|---|---|
| `create_spring_boot_module` | pom.xml, Application.java, application.yml | ✅ returns `already_exists` |
| `generate_dubbo_interface` | Dubbo interface + `@DubboService` impl | ✅ |
| `generate_rest_controller` | `@RestController` with Dubbo wiring + auto DTO guard | ✅ |
| `generate_entity` | JPA / POJO entity with getters/setters | ✅ |
| `generate_dto` | DTO class with getters/setters, `Serializable` | ✅ kv-table dedup |
| `add_threadpool_config` | `ThreadPoolConfig.java` (async + dubbo pools) | ✅ |
| `modify_dubbo_config` | Patch any key in `application.yml` via dot-path | overwrite |
| `update_nacos_config` | Push config to running Nacos Config Center | overwrite |
| `update_docker_compose` | Generate/update `docker-compose.yml`, Nacos reuse | ✅ merge |
| `incremental_modify` | Append methods/fields to existing `.java` files | ✅ DB dedup |

---

## State Management

```
output/
├── .global_state.json           ← Global Nacos status (shared across all projects)
└── my-shop/
    ├── .project_state.db        ← Project SQLite state
    │   ├── modules              module info (port, base package, feature flags)
    │   ├── interfaces           Dubbo interfaces and method signatures
    │   ├── entities             entity classes and fields
    │   ├── method_records       known methods per class (dedup core)
    │   ├── change_history       change log (timestamp, type, diff summary)
    │   └── kv                   project-level key-value pairs (e.g. dto registry)
    └── ...
```

---

## Tech Stack

| Component | Version |
|---|---|
| Spring Boot | 2.7.18 |
| Apache Dubbo | 3.2.7 |
| Nacos Client | 2.3.0 |
| Java | 17 |
| Maven | 3.9+ |
| Python | 3.10+ |
| CrewAI | ≥ 0.28.0 |

---

## Prerequisites

- Python 3.10+ with Conda
- Java 17+ and Maven 3.9+

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y maven
```

- Docker and Docker Compose
- An OpenAI-compatible API key