# Skill: generate_entity

## 功能描述
在指定模块中生成一个 Java 实体类（支持普通 POJO 或 JPA `@Entity`）。
包含私有字段、无参构造、所有 getter/setter、`toString()`，并实现 `Serializable`。

**幂等性**：若 SQLite 中已记录该实体 FQN，返回 `already_exists`，不重复写文件。

---

## 参数说明

| 参数名      | 类型    | 必填 | 默认值          | 说明                                            |
|-------------|---------|------|------------------|-------------------------------------------------|
| module_name | str     | ✅   | —                | 目标模块名                                      |
| class_name  | str     | ✅   | —                | 实体类名，如 `Order`                             |
| fields      | list    | ✅   | —                | 字段列表，见下方结构                            |
| use_jpa     | bool    | ❌   | false            | 是否添加 `@Entity @Table @Id @GeneratedValue`   |
| table_name  | str     | ❌   | `{class_name}s`  | JPA 表名（仅 use_jpa=true 时有效）              |

### fields 结构
```json
[
  {
    "name": "userId",
    "type": "Long",
    "annotations": ["@Column(name = \"user_id\")"]
  },
  {
    "name": "createdAt",
    "type": "LocalDateTime",
    "annotations": []
  }
]
```

---

## 返回值

```json
{
  "status": "success",      // "success" | "already_exists" | "error"
  "file": "/path/to/Order.java",
  "fqn": "com.example.serviceorder.entity.Order"
}
```

---

## 生成文件

```
service-order/src/main/java/com/example/serviceorder/entity/
  └── Order.java
```

---

## 使用场景

```python
run_skill("generate_entity", {
    "module_name": "service-order",
    "class_name": "Order",
    "use_jpa": True,
    "fields": [
        {"name": "orderId",    "type": "Long",          "annotations": []},
        {"name": "userId",     "type": "Long",          "annotations": []},
        {"name": "amount",     "type": "BigDecimal",    "annotations": []},
        {"name": "status",     "type": "String",        "annotations": []},
        {"name": "createdAt",  "type": "LocalDateTime", "annotations": []}
    ]
}, context)
```

---

## 副作用
- 保存 `EntityInfo` 到 SQLite
- 更新 `ModuleInfo.entity_fqns` 列表
- 记录 `change_history`