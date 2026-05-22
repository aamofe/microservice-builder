# Skill: generate_dubbo_interface

## Description
Generates a Dubbo service interface and its default implementation.

## Parameters
| Name          | Type   | Required | Description                                     |
|---------------|--------|----------|-------------------------------------------------|
| module_name   | str    | yes      | Target module name                              |
| interface_name| str    | yes      | Interface class name, e.g. "UserService"        |
| methods       | list   | yes      | List of method defs {name, return_type, params} |
| version       | str    | no       | Dubbo version string (default "1.0.0")          |
| group         | str    | no       | Dubbo group (default "default")                 |
| has_threadpool| bool   | no       | Inject executor in impl                         |

## Output Example
```
service-user/src/main/java/com/example/serviceuser/
  api/UserService.java
  service/UserServiceImpl.java
```