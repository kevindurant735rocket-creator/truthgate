# DEMAND（客户原话，一句话）

> 交付 truthgate 命令行工具，Python 编写，运行于 CPython 标准库环境，交付 README 使用说明与验收清单两个文件，行为限于本机文件系统。
(1) 三态判定：运行 truthgate verify 读取本地 YAML 判据文件逐条求值，输出 verified、failed、unverified 三态之一，其中 unverified 表示求值无法完成(判据引用的本机命令不在 PATH 中)。系统必须禁止把 unverified 归并进 verified 或 failed。进程返回码 0 当且仅当全部判据为 verified；返回码 2 当存在 failed；返回码 3 当存在 unverified 且无 failed。
(2) 恒真检测：判据若为恒真式(断言某路径文件存在，而变异算子把该路径改写为必然不存在的路径)，truthgate 必须自动派生该变异样例并判为 FAIL_CONSTANT，返回码 4，禁止静默给出 verified。
(3) 校准报告：运行 truthgate calibrate 读取本地 JSON 样例集(每条含 known_pass 与 known_fail 布尔标签)，向标准输出写 4 行数值：Brier 分数、ECE 分箱表、假阳性率、恒真判据计数。
(4) 证据回执：每次 verify 把逐条判据结果以 JSON Lines 追加写入本地回执文件，每行 4 个字段：判据名、进程返回码、UTC 时间戳、判定结果。
(5) 交付测试报告：tests/ 目录下必须有覆盖三态判定与恒真检测的测试文件，测试全部通过才算完成。
附加 2 项行为：truthgate --version 向标准输出写 1 行版本号字符串；truthgate --help 向标准输出写用法文本且该文本必须含 verify 与 calibrate 2 个子命令名。
范围边界：仅命令行程序，无 GUI 层、无数据库层、无账户体系。
