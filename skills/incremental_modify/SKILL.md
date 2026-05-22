# Skill: incremental_modify

## 功能描述
安全地向已有 Java 文件中增量添加方法、字段或注解。

**核心特性（v2）：**
- **跨 Session 去重**：先查 SQLite `method_records` 表，已记录的方法名一律跳过，彻底杜绝重复定义编译错误
- **文件级去重**：即使 DB 未记录，也会用正则扫描文件内容二次确认
- **Interface 同步**：`interface_sync=true` 时，修改接口的同时自动在 Impl 类中追加 `@Override` 实现
- **Diff 预览 + 人工确认**：修改前打印彩色 diff，等待用户输入 `y` 才写盘
- **自动路径解析**：可用简名（`"UserService"`）或全限定名，技能自动定位文件

---

## 参数说明

| 参数名              | 类型   | 必填 | 默认值   | 说明                                                                      |
|---------------------|--------|------|----------|---------------------------------------------------------------------------|
| module_name         | str    | ✅   | —        | 目标模块名                                                                |
| target_class        | str    | ✅*  | —        | 目标类简名或全限定名（与 `relative_java_path` 二选一）                   |
| target_type         | str    | ❌   | `"auto"` | `"interface"` / `"impl"` / `"controller"` / `"entity"` / `"auto"`       |
| relative_java_path  | str    | ✅*  | —        | 相对于 `src/main/java` 的路径，优先于 `target_class`                     |
| operations          | list   | ✅   | —        | 操作列表，见下方结构                                                      |
| auto_approve        | bool   | ❌   | false    | `true` 时跳过交互确认（CI/测试用）                                        |
| interface_sync      | bool   | ❌   | true     | 修改接口时自动同步给 Impl 类                                              |

### operations 结构

#### add_method
```json
{
  "type": "add_method",
  "payload": {
    "name": "cancelOrder",
    "return_type": "boolean",
    "params": [
      {"name": "orderId", "type": "Long"},
      {"name": "reason",  "type": "String"}
    ],
    "body": "// TODO: implement cancel logic\nreturn false;",
    "annotations": ["@Override"],
    "access": "public",
    "throws": ["OrderNotFoundException"],
    "imports": ["com.example.serviceorder.exception.OrderNotFoundException"]
  }
}
```

#### add_field
```json
{
  "type": "add_field",
  "payload": {
    "name": "retryCount",
    "type": "int",
    "access": "private",
    "initial_value": "3",
    "annotations": ["@Value(\"${order.retry.count:3}\")"],
    "imports": ["org.springframework.beans.factory.annotation.Value"]
  }
}
```

#### add_annotation
```json
{
  "type": "add_annotation",
  "payload": {
    "annotation": "@Transactional"
  }
}
```

#### add_implements
```json
{
  "type": "add_implements",
  "payload": {
    "interface": "Serializable"
  }
}
```

---

## 返回值

```json
{
  "status": "success",           // "success" | "no_change" | "rejected" | "error"
  "file": "/path/to/OrderService.java",
  "class_fqn": "com.example.serviceorder.api.OrderService",
  "operations": [
    {"type": "add_method", "name": "cancelOrder", "result": "added"},
    {"type": "add_method", "name": "createOrder", "result": "skipped (recorded in DB)"}
  ],
  "newly_recorded_methods": ["cancelOrder"],
  "impl_sync": [
    {"method": "cancelOrder", "result": "added to impl"}
  ],
  "diff": "--- OrderService.java (original)\n+++ OrderService.java (modified)\n..."
}
```

---

## 典型工作流

```
用户: "给 OrderService 加一个 cancelOrder 和 refundOrder 方法"

Agent 流程:
1. get_project_state(class_fqn="com.example.serviceorder.api.OrderService")
   → known_methods: ["createOrder", "getOrderById"]   ← 已有

2. 需要新增: cancelOrder, refundOrder（未在 known_methods 中）

3. run_skill("incremental_modify", {
     "module_name": "service-order",
     "target_class": "OrderService",
     "target_type": "interface",
     "interface_sync": true,
     "auto_approve": false,
     "operations": [
       {"type":"add_method","payload":{"name":"cancelOrder",...}},
       {"type":"add_method","payload":{"name":"refundOrder",...}}
     ]
   })

4. 展示 diff → 用户确认 → 写入
   OrderService.java: +cancelOrder, +refundOrder
   OrderServiceImpl.java: +cancelOrder(@Override), +refundOrder(@Override)

5. SQLite method_records 更新:
   com.example.serviceorder.api.OrderService → [createOrder, getOrderById, cancelOrder, refundOrder]
   com.example.serviceorder.service.OrderServiceImpl → 同上
```

---

## 副作用
- 向 `method_records` 写入新增方法（跨 session 有效）
- 更新 `InterfaceInfo.methods` 列表并持久化
- 记录 `change_history`（change_type="add_method"，含 diff 前 500 字符）