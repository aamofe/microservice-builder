# Skill: update_docker_compose

## 功能描述
生成或增量更新项目的 `docker-compose.yml`，并为每个 module 生成 `Dockerfile`。

**核心特性（v2 — Nacos 单例）：**
- **Nacos 复用**：通过 `docker inspect` 真实检测 nacos 容器状态，已运行则不重复声明 service，
  改为 external 网络引用，让新服务能自动加入同一个 `microservice-net`
- **服务幂等**：已存在的 service 块只更新 `environment`/`ports`，不整体覆盖（保留用户手动改动）
- **Dockerfile 幂等**：已存在的 Dockerfile 默认跳过，`force_dockerfile=true` 才覆盖

---

## 参数说明

| 参数名           | 类型   | 必填 | 默认值 | 说明                                               |
|------------------|--------|------|--------|----------------------------------------------------|
| registry_mirror  | str    | ❌   | 环境变量 `DOCKER_REGISTRY_MIRROR` | 镜像仓库前缀，如 `mirrors.aliyun.com` |
| force_dockerfile | bool   | ❌   | false  | 强制覆写已存在的 Dockerfile                        |

---

## 返回值

```json
{
  "status": "success",
  "compose_file": "/path/to/docker-compose.yml",
  "nacos_action": "existing",   // "existing" = 复用已有 | "added" = 本次新建
  "services": {
    "service-user":  "unchanged",   // "added" | "updated" | "unchanged"
    "service-order": "added"
  },
  "message": "启动命令: docker-compose up -d ..."
}
```

---

## Nacos 复用逻辑详解

```
首次运行（nacos 未启动）:
  docker-compose.yml:
    services:
      nacos:          ← 完整声明（image, ports, healthcheck）
        ...
      service-user:
        depends_on:
          nacos: {condition: service_healthy}
  GlobalNacosState → running=true, compose_file=<当前 compose 路径>

第二次运行（nacos 容器已在运行）:
  docker-compose.yml:
    networks:
      microservice-net:
        external: true   ← 共享已有网络
    services:            ← 无 nacos 声明
      service-order:
        depends_on:
          nacos: {condition: service_started}
```

---

## Dockerfile 生成（模块内）

```dockerfile
FROM eclipse-temurin:17-jre-alpine
WORKDIR /app
COPY target/*.jar app.jar
EXPOSE {port}
ENTRYPOINT ["java", "-jar", "app.jar"]
```

---

## 使用场景

```
# 场景1: 首次项目，nacos + 所有服务一起启动
run_skill("update_docker_compose", {}, context)
→ docker-compose up -d
→ 启动 nacos + service-user + service-order

# 场景2: 在已有 nacos 环境下新增 service-payment
# （nacos 已在运行，用 GlobalNacosState 标记）
run_skill("update_docker_compose", {}, context)
→ docker-compose up -d service-payment
→ service-payment 加入 microservice-net，自动发现 nacos
```

---

## 副作用
- 写入/更新 `docker-compose.yml`
- 写入各模块 `Dockerfile`（幂等）
- 若本次新增 nacos service：调用 `GlobalNacosState.mark_running()`
- 记录多条 `change_history`