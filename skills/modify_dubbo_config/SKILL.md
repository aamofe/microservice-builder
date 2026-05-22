# Skill: modify_dubbo_config

## 功能描述
通过点分路径（dot-notation）修改或新增模块 `application.yml` 中的任意配置项。
常用于调整 Dubbo 线程数、超时、重试次数、Nacos 地址等参数。

---

## 参数说明

| 参数名          | 类型   | 必填 | 默认值           | 说明                                             |
|-----------------|--------|------|------------------|--------------------------------------------------|
| module_name     | str    | ✅   | —                | 目标模块名                                       |
| dubbo_overrides | dict   | ✅   | —                | key 为 dot-path，value 为新值                    |
| group           | str    | ❌   | `"DEFAULT_GROUP"` | Nacos config group（用于推送）                  |

### dubbo_overrides 示例
```json
{
  "dubbo.protocol.threads": 200,
  "dubbo.consumer.timeout": 3000,
  "dubbo.consumer.retries": 1,
  "dubbo.provider.timeout": 5000,
  "thread-pool.core-size": 16,
  "thread-pool.max-size": 100
}
```

支持任意 YAML 层级，不仅限于 `dubbo.*`：
```json
{
  "spring.datasource.url": "jdbc:mysql://mysql:3306/orders",
  "logging.level.com.example": "DEBUG"
}
```

---

## 返回值

```json
{
  "status": "success",
  "file": "/path/to/application.yml",
  "applied": {
    "dubbo.protocol.threads": 200,
    "dubbo.consumer.timeout": 3000
  }
}
```

---

## 工作原理

使用 PyYAML 加载 → 按 dot-path 写入嵌套字典 → 重新序列化写回文件。

例如 `dubbo.protocol.threads=200` 会变为：
```yaml
dubbo:
  protocol:
    threads: 200
```

---

## 使用场景

```
# 场景1: 调整 Dubbo 服务端线程池，应对高并发压测
run_skill("modify_dubbo_config", {
    "module_name": "service-order",
    "dubbo_overrides": {
        "dubbo.protocol.threads": 400,
        "dubbo.provider.timeout": 8000
    }
}, context)

# 场景2: 统一修改所有模块的 Nacos 地址（容器环境）
# → 对每个 module 各调一次 modify_dubbo_config
```

---

## 副作用
- 直接覆写 `application.yml`（非增量，整个文件重新序列化）
- 记录 `change_history`（change_type="modify_config"）