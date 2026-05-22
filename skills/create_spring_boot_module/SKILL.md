# Skill: create_spring_boot_module

## Description
Creates a new Spring Boot module inside the current project.
Generates: pom.xml, Application.java, application.yml, package directories.
Also updates the parent pom.xml to include the new module.

## Parameters
| Name               | Type   | Required | Description                                      |
|--------------------|--------|----------|--------------------------------------------------|
| module_name        | str    | yes      | Maven module name, e.g. "service-user"           |
| base_package       | str    | no       | Java base package; defaults to group_id + module |
| port               | int    | no       | HTTP port (auto-assigned if omitted)             |
| has_async          | bool   | no       | Whether to enable @EnableAsync                   |
| has_threadpool     | bool   | no       | Whether to generate ThreadPoolConfig             |
| module_dependencies| list   | no       | Other module names this module depends on        |

## Output Example
```
output/my-project/
  service-user/
    pom.xml
    src/main/java/com/example/serviceuser/
      ServiceUserApplication.java
    src/main/resources/
      application.yml
```

## Side Effects
- Updates parent pom.xml <modules> section
- Saves ModuleInfo to ProjectContext SQLite