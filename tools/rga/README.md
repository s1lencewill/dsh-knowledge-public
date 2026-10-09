# 受控构建：rga 的缓存/配置目录回退补丁

## 为什么需要补丁

ripgrep-all 0.10.9 在 clap 解析参数时就求值默认缓存路径：

```rust
// src/config.rs:55 (upstream)
impl Default for CachePath {
    fn default() -> Self {
        let pd = project_dirs().expect("could not get cache path");
        ...
    }
}
```

`project_dirs()` 走 `directories-next 2.0.0` → Windows `known_folder_local_app_data()` /
`known_folder_roaming_app_data()`。在 AppContainer 里这两个查询失败，于是**任何** rga 调用——包括
`rga --version`——都在参数生效之前 panic：

```
thread 'main' panicked at src\config.rs:55:33:
could not get cache path: no home directory found! :(
```

已实验排除的替代做法（全部无效）：

| 尝试 | 结果 |
|---|---|
| `--rga-cache-path=<可写目录>` | 仍然 panic：默认值在参数应用前求值 |
| `--rga-no-cache` | 同上 |
| `XDG_CACHE_DIR` / `XDG_CACHE_HOME` / `RGA_CACHE_DIR`（上游 0.10.9） | 帮助文本里的默认值不变，未被读取 |
| 恢复 `HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` | 无效；`directories-next` 用 known-folder API，不读这些变量 |
| `ProjectDirs::from_path(temp_dir())` | **无效**：`from_path` 内部仍查询 known folder，容器里同样返回 `None` |

给容器授予用户目录访问权限是明确不接受的方案。

## 补丁内容（`cache-path-fallback.patch`，对 v0.10.9 的 `src/config.rs`，两处）

1. `CachePath::default()`：
   优先 `RGA_CACHE_DIR`（显式）→ 能解析时用项目目录 → 否则 `temp_dir()/ripgrep-all-cache`，**不再 panic**。
2. `read_config_file()`：项目目录不可用时不再 `?` 失败，跳过默认配置写入并使用内置默认值。

容器里 `TEMP` 已被指向授权可写的 state 目录，因此兜底路径自动落在隔离环境内；探针同时显式设置
`RGA_CACHE_DIR`。

## 重建与校验

```powershell
.\.venv\Scripts\python.exe scripts\build-rga-patched.py          # 首次：克隆 + 构建，约 2 分钟
.\.venv\Scripts\python.exe scripts\build-rga-patched.py --skip-build
```

脚本按固定 tag `v0.10.9` 取源码、幂等应用补丁、在 VS2022 环境里 `cargo build --release --locked`、
复制产物到 `.local/tools/rga-patched/rga.exe` 并把 SHA256 写入同目录 `manifest.json`。

- 依赖锁：仓库自带 `Cargo.lock`，`--locked` 保证依赖不漂移。
- 本轮产物：`sha256 8e3843608bce682830e3c1178c17044d5f882279b732efcaff23ca251fe80c63`，6.2 MB，
  `ripgrep-all 0.10.9`。
- **二进制不入库**；仓库只固定补丁、构建脚本与校验方式。
- 引擎侧另有 `--rga-cache-path` 注入（`engine/sirchmunk_compat.py`）：补丁保证 rga 能在容器里启动，
  注入保证缓存落在作业 state 目录而不是用户 profile。
