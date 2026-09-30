# PRODUCT_REQUIREMENTS(每节均源自DEMAND原话，不幻想)

## 1 Product Vision
- > 交付 truthgate 命令行工具，Python 编写，运行于 CPython 标准库环境，交付 README 使用说明与验收清单两个文件，行为限于本机文件系统
  > DEMAND: > 交付 truthgate 命令行工具，Python 编写，运行于 CPython 标准库环境，交付 README 使用说明与验收清单两个文件，行为限于本机文件系统。

## 2 Target User
- 需求方(见DEMAND原话)

## 3 User Problem
- > 交付 truthgate 命令行工具，Python 编写，运行于 CPython 标准库环境，交付 README 使用说明与验收清单两个文件，行为限于本机文件系统

## 4 Core Scenario(前三核心场景)
- FLOW-CORE: CORE 相关核心流程 [src: > 交付 truthgate 命令行工具，Python 编写，运行于 CPython 标准库环境，交付 README 使]

## 5 User Journey
- start -> FLOW-CORE -> result (动态证明以trust_verify行为探针为准)

## 6 MVP Scope(KEEP)
- KEEP C-1:content: 核心流内约束，有可验证用户问题
- KEEP C-2:deliverable: 核心流内约束，有可验证用户问题

## 7 Non Goal(明确不做)
- 无

## 8 Success Metric
- trust_verify行为探针PASS + trust_ship SHIP_OK(唯一DONE判据)
- DEFER功能需用户YES才进入SPEC，否则永不BUILD

## 追溯
- 本文档每条决策见FEATURE_DECISIONS.json source_requirement
- 未说明: 无
