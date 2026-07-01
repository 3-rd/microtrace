# Sample PetClinic Logs — microtrace 开发/测试用公开数据

> 这些日志来自 Spring PetClinic 示例应用（Apache 2.0 License）。
> 公司内部使用时替换为 VNFM 实际日志。

## 日志来源
- 项目: Spring PetClinic (https://github.com/spring-projects/spring-petclinic)
- License: Apache 2.0
- 用途: microtrace 框架开发、测试、演示

## 配置说明
在家开发时，将 config.yaml 的 tools.log_dirs 指向此目录：
```yaml
tools:
  log_dirs:
    - ./data/logs/
  java_source_roots:
    - ./data/src/
```

公司验证时，改为 VNFM 实际日志路径：
```yaml
tools:
  log_dirs:
    - /var/log/vnfm/
    - C:/ProgramData/VNFM/logs/
  java_source_roots:
    - /home/vnfm/project/src/
```
