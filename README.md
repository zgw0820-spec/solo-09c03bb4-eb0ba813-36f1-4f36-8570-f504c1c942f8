# 采集代理 · 双插件装入顺序推演器

发布员在网页上从挂载目录选择两个 x86-64 ELF 插件，系统**不执行任何插件**，
而是从“尚未装入任何私有库”的空状态出发，分别推演两种装入顺序在同一
Linux 进程语义下的结果。

## 推演规则（与需求对应）

| 规则 | 实现位置 |
| --- | --- |
| 不执行插件，只读 ELF 元数据 | `app/elfparse.py`（纯解析，无 dlopen） |
| 两种顺序各自从空状态推演 | `resolver.simulate_both_orders` |
| 按 DT_NEEDED 顺序逐层（广度优先）找库 | `resolver.simulate_order` 中的队列 |
| 引用方用自己的 DT_RUNPATH 找首个同名文件 | `resolver._find_in_runpath` |
| 同名私有库一旦装入，后续引用只能复用 | `simulate_order` 的 `loaded` 表 |
| 只支持 `$ORIGIN` 相对目录（含 `${ORIGIN}`） | `resolver._expand_origin` |
| 只支持文件名与 SONAME 一致的私有库 | `resolver._load_private` |
| 固定名单系统库跳过核查 | `resolver.SYSTEM_LIBS` |
| 每条私有依赖核对 GNU 版本化强符号 | `resolver._check_versioned_symbols` + `ElfFile.versioned_requirements` / `strong_provided_versions` |
| 可行顺序列出实际库路径 | 结果 `loads`（页面与 CLI 均展示） |
| 阻断时给出依赖链、原因、已命中库路径 | 结果 `block`（chain / reason / hit_path） |

版本化强符号的核对口径：对每条私有依赖边，取引用方 `.gnu.version_r` 中
`vn_file` 等于该库名的条目，找出引用方未定义的强符号（bind=GLOBAL）及其
要求的版本；提供方必须存在同名、已定义、GLOBAL 且版本名一致的符号
（`.gnu.version_d`），否则阻断。

## 运行

```bash
docker compose up --build
# 打开 http://localhost:8080
```

compose 把 `./samples` 只读挂载到容器 `/data/plugins`。换成自己的插件目录：
修改 `docker-compose.yml` 的 volume，或 `docker run -v /path:/data/plugins:ro`。

无 Docker 时可用 CLI（纯标准库，无需 Flask）：

```bash
python3 app/cli.py --dir samples plugin_alpha.so plugin_beta.so
python3 app/cli.py --dir samples --list     # 只列出可选插件
```

## 自带样例（两种不同结果）

`samples/` 下是一对菱形依赖（源码与构建脚本在 `samples/src/`，
`sh samples/src/build.sh` 可重建）：

- `plugin_alpha.so`：`RUNPATH=$ORIGIN/vendor_alpha`，要求 `foo@FOO_1.0`
- `plugin_beta.so`：`RUNPATH=$ORIGIN/vendor_beta`，要求 `foo@FOO_2.0`
- `vendor_alpha/libfoo.so.1`：只提供 `foo@@FOO_1.0`
- `vendor_beta/libfoo.so.1`：同时提供 `foo@FOO_1.0` 与 `foo@@FOO_2.0`

结果：

- **alpha → beta：阻断**。alpha 的 `vendor_alpha/libfoo.so.1` 先装入，
  beta 只能复用这份，而它缺少 `foo@FOO_2.0`。页面给出依赖链
  `plugin_beta.so → libfoo.so.1`、原因与已命中库路径。
- **beta → alpha：可行**。`vendor_beta/libfoo.so.1` 先装入，同时满足两个
  插件，页面列出实际装入的库路径。

两个插件都还依赖 `libc.so.6`，用于演示固定名单系统库的跳过。

## 交付状态

### 已做

- 纯 Python ELF64 解析：DT_NEEDED / DT_RUNPATH / DT_SONAME、.dynsym、
  GNU 版本三节（versym / verdef / verneed）。
- 上表全部推演规则；阻断报告含依赖链、原因、命中路径。
- Flask 网页（选择两个插件 → 同页展示两种顺序结果）+ 无依赖 CLI。
- 自带 ELF 样例（源码可重建）、Dockerfile、docker-compose.yml。
- 安全约束：插件选择与 `$ORIGIN` 展开均限制在挂载目录内；解析有大小上限。

### 已验证（本机实跑）

- `tests/test_core.py` 16 项单元测试全部通过：动态标签解析、版本需求 /
  提供提取、两种顺序结果、系统库跳过、同名复用、SONAME 不一致阻断、
  非 `$ORIGIN` RUNPATH 阻断、路径越界拒绝等。
- CLI 对自带样例实跑，两种顺序得到预期的“一阻断、一可行”。
- 样例的 DT_NEEDED / RUNPATH / SONAME / 版本符号经 `readelf` 交叉核对。
- 全部 Python 模块通过 `py_compile`。

### 未完成 / 未验证

- 本机没有 docker 与 flask，`docker compose up` 的端到端流程与网页实际
  渲染未在本环境跑过（模板经人工复查；路由层仅做了语法编译检查）。
- 未版本化的未定义符号不核查（需求只要求版本化强符号）；弱符号按
  “非强符号”跳过。
- 仅支持 ELF64 小端 x86-64；DT_RPATH、`$LIB`/`$PLATFORM` 等动态标记、
  符号版本继承（verdef 多 aux）等罕见情形未支持。
- Web 用 Flask 内置服务器，仅供演示，非生产部署形态。
