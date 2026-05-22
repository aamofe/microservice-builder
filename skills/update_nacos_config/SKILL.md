# Skill: update_nacos_config

## 功能描述
将模块的 `application.yml` 配置推送到运行中的 Nacos 配置中心（Config Center）。
支持单模块推送和全量推送，可叠加额外的配置键值对。

---

## 参数说明

| 参数名        | 类型   | 必填 | 默认值           | 说明                                              |
|---------------|--------|------|------------------|---------------------------------------------------|
| module_name   | str    | ❌   | —                | 指定推送哪个模块；不填则推送所有 module           |
| group         | str    | ❌   | `"DEFAULT_GROUP"` | Nacos config group                               |
| extra_config  | dict   | ❌   | `{}`             | 额外追加到配置中的 key-value                      |

---

## 返回值

```json
{
  "status": "success",         // "success" | "warning"（Nacos 不可达）
  "results": {
    "service-user":  "pushed",
    "service-order": "failed"
  }
}
```

若 Nacos 不可达（`health_check()` 失败）：
```json
{
  "status": "warning",
  "message": "Nacos is not reachable; config not pushed. Start Nacos and retry."
}
```

---

## Nacos Config 规则

| 字段     | 值                                        |
|----------|-------------------------------------------|
| dataId   | `{module_name}.yaml`（如 `service-user.yaml`） |
| group    | `DEFAULT_GROUP`（可覆盖）                 |
| namespace| 环境变量 `NACOS_NAMESPACE`（默认 public） |
| type     | `yaml`                                    |

---

## 使用场景

```
# 场景1: 推送单个模块配置
run_skill("update_nacos_config", {
    "module_name": "service-order"
}, context)

# 场景2: 全量推送（部署时统一刷新）
run_skill("update_nacos_config", {}, context)

# 场景3: 追加动态配置（如开关）
run_skill("update_nacos_config", {
    "module_name": "service-user",
    "extra_config": {
        "feature.login.sms.enabled": True,
        "rate-limit.login.per-minute": 10
    }
}, context)
```

---

## 前提条件
- Nacos 服务已启动（`http://{NACOS_HOST}:{NACOS_PORT}/nacos/` 可访问）
- 环境变量配置：`NACOS_HOST`, `NACOS_PORT`, `NACOS_USERNAME`, `NACOS_PASSWORD`

---

## 副作用
- 调用 Nacos OpenAPI `POST /nacos/v1/cs/configs`
- **不修改本地文件**，仅推送到远端