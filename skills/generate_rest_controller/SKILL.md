# Skill: generate_rest_controller

## 功能描述
在指定模块中生成 `@RestController`，通过 `@DubboReference` 注入 Dubbo 服务，
为每个 endpoint 生成对应的 HTTP handler 方法（GET/POST/PUT/DELETE）。

**幂等性**：已存在的 controller 文件不会被覆盖（除非 `overwrite=true`）。

---

## 参数说明

| 参数名          | 类型    | 必填 | 默认值      | 说明                                                |
|-----------------|---------|------|-------------|-----------------------------------------------------|
| module_name     | str     | ✅   | —           | 目标模块名                                          |
| controller_name | str     | ✅   | —           | Controller 类名，如 `OrderController`               |
| base_path       | str     | ✅   | —           | 请求根路径，如 `/api/v1/orders`                     |
| endpoints       | list    | ✅   | —           | Endpoint 列表，见下方结构                           |
| dubbo_ref       | dict    | ❌   | null        | 注入的 Dubbo 服务，见下方结构                       |
| dubbo_version   | str     | ❌   | `"1.0.0"`   | `@DubboReference` 的 version                       |
| dubbo_group     | str     | ❌   | `"default"` | `@DubboReference` 的 group                         |
| overwrite       | bool    | ❌   | false       | 是否覆盖已存在的文件                                |

### endpoints 结构
```json
[
  {
    "http_method": "Post",
    "path": "",
    "method_name": "createOrder",
    "response_type": "Order",
    "description": "创建订单",
    "params": [
      {"name": "request", "type": "CreateOrderRequest", "source": "body"}
    ]
  },
  {
    "http_method": "Get",
    "path": "/{orderId}",
    "method_name": "getOrder",
    "response_type": "Order",
    "params": [
      {"name": "orderId", "type": "Long", "source": "path"}
    ]
  }
]
```

`source` 可选值：`"path"` / `"query"` / `"body"`

### dubbo_ref 结构
```json
{
  "interface": "com.example.serviceorder.api.OrderService",
  "field_name": "orderService"
}
```

---

## 返回值

```json
{
  "status": "success",
  "file": "/path/to/OrderController.java",
  "controller_fqn": "com.example.serviceorder.controller.OrderController"
}
```

---

## 生成文件

```
service-order/src/main/java/com/example/serviceorder/controller/
  └── OrderController.java
```

---

## 副作用
- 更新 `ModuleInfo.has_rest_controller = true`
- 更新 `ModuleInfo.controller_classes` 列表
- 记录 `change_history`