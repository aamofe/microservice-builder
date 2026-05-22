# Skill: add_threadpool_config

## 功能描述
为已有模块添加或更新 `ThreadPoolConfig.java`，生成 `taskExecutor`（用于 `@Async`）和 `dubboExecutor`（用于 Dubbo 业务线程池）两个 Bean。

**幂等性**：默认不覆盖已存在的文件（`overwrite=false`），重复调用安全。

---

## 参数说明

| 参数名         | 类型    | 必填 | 默认值 | 说明                                              |
|----------------|---------|------|--------|---------------------------------------------------|
| module_name    | str     | ✅   | —      | 目标模块名，如 `service-order`                    |
| core_size      | int     | ❌   | 4      | 线程池核心线程数                                  |
| max_size       | int     | ❌   | 20     | 最大线程数                                        |
| queue_capacity | int     | ❌   | 500    | 等待队列容量                                      |
| overwrite      | bool    | ❌   | false  | 是否覆盖已存在的 ThreadPoolConfig.java            |

---

## 返回值

```json
{
  "status": "success",         // "success" | "skipped"
  "file": "/path/to/ThreadPoolConfig.java",
  "message": "ThreadPoolConfig written"
}
```

---

## 生成文件

```
service-order/src/main/java/com/example/serviceorder/config/
  └── ThreadPoolConfig.java
```

### 生成代码要点
- `@Configuration @EnableAsync`
- Bean `taskExecutor` → `ThreadPoolTaskExecutor`（供 `@Async` 使用）
- Bean `dubboExecutor` → `ThreadPoolExecutor`（供 Dubbo provider 注入）
- 线程名前缀：`{module_name}-async-` / `{module_name}-dubbo-`
- 拒绝策略：`CallerRunsPolicy`（背压而非丢弃）

---

## 使用场景

```
# 用户请求: "给订单服务加上高并发线程池，核心32线程，最大200"
run_skill("add_threadpool_config", {
    "module_name": "service-order",
    "core_size": 32,
    "max_size": 200,
    "queue_capacity": 1000
}, context)
```

---

## 副作用
- 在 `ModuleInfo.has_threadpool` 设为 `true` 并持久化到 SQLite
- 记录一条 `change_history`（change_type="create"）