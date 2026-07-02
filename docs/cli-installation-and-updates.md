# Pico CLI installation and update guide

这份文档说明 `pico-cli` 应该如何安装、激活、直接使用，以及项目后续迭代时哪些改动会自动生效、哪些改动需要重新同步环境。

## 命令名选择

推荐使用：

```bash
pico-cli
```

不推荐直接使用：

```bash
pico
```

原因是 macOS 自带 `/usr/bin/pico` 编辑器。直接在 shell 里输入 `pico doctor` 时，可能会启动系统编辑器，而不是这个项目的 CLI。

可以用下面的命令确认当前 shell 会执行哪个程序：

```bash
type -a pico
type -a pico-cli
```

## 开发时使用项目虚拟环境

在开发这个仓库时，推荐使用项目自己的 `.venv`：

```bash
cd /Users/wei/Desktop/pico
uv sync
source .venv/bin/activate
```

激活后验证：

```bash
which pico-cli
pico-cli --help
```

正常情况下，`which pico-cli` 应该指向：

```bash
/Users/wei/Desktop/pico/.venv/bin/pico-cli
```

之后可以直接运行：

```bash
pico-cli doctor
pico-cli status
pico-cli run "inspect the failing tests"
```

退出当前虚拟环境：

```bash
deactivate
```

`source .venv/bin/activate` 只对当前 shell 窗口生效。新开终端后，需要重新激活。

## 不激活环境时临时运行

如果只是临时运行一次，或者不想激活虚拟环境，可以在仓库目录里使用：

```bash
uv run pico-cli doctor
uv run pico-cli status
uv run pico-cli run "inspect the failing tests"
```

这会通过当前项目环境运行 `pico-cli`，适合一次性命令和验证。

## 全局安装为 editable tool

如果希望在任何目录里都能直接输入 `pico-cli`，可以把当前源码目录安装成 uv tool：

```bash
uv tool install --editable /Users/wei/Desktop/pico --force
uv tool update-shell
exec zsh -l
```

验证：

```bash
which pico-cli
pico-cli --help
```

这种方式会让全局 `pico-cli` 指向 `/Users/wei/Desktop/pico` 这份源码。后续源码改动通常会直接反映到全局命令里。

需要注意：如果你在 `/Users/wei/Desktop/pico` 切换分支、修改未完成代码，或者把项目改坏，全局 `pico-cli` 也会受影响。

## 后续迭代时如何更新

### 源码改动

普通源码改动通常不需要重新安装：

```bash
pico-cli --help
```

只要当前 shell 已经激活 `.venv`，或者全局工具是用 `uv tool install --editable` 安装的，命令会直接运行当前源码。

### 依赖或入口改动

下面这些情况需要重新同步环境：

- 修改了 `pyproject.toml`
- 新增、删除或升级依赖
- 新增或修改 `[project.scripts]` 里的 CLI 入口
- 切换分支后依赖或入口发生变化

项目虚拟环境使用：

```bash
uv sync
source .venv/bin/activate
```

全局 editable tool 使用：

```bash
uv tool install --editable /Users/wei/Desktop/pico --force
```

如果 PATH 没有更新，再运行：

```bash
uv tool update-shell
exec zsh -l
```

## doctor 和 doctor --offline

完整诊断使用：

```bash
pico-cli doctor
```

它会检查本地配置、存储、凭证，并尝试进行 provider connectivity 检查。

只做本地诊断使用：

```bash
pico-cli doctor --offline
```

`--offline` 不会请求 provider，也不会消耗 API 额度。它适合检查 CLI 输出样式、配置来源、存储路径和本地 readiness。

## 常见问题

### pico-cli not found

说明当前 shell 没有激活项目环境，或者全局 tool 目录不在 PATH 中。

开发仓库时：

```bash
cd /Users/wei/Desktop/pico
source .venv/bin/activate
pico-cli --help
```

临时运行：

```bash
cd /Users/wei/Desktop/pico
uv run pico-cli --help
```

全局安装：

```bash
uv tool install --editable /Users/wei/Desktop/pico --force
uv tool update-shell
exec zsh -l
```

### pico 打开了编辑器

说明当前 shell 执行的是系统自带的 `/usr/bin/pico`。使用：

```bash
pico-cli doctor
```

不要用：

```bash
pico doctor
```

## 推荐工作流

开发项目时：

```bash
cd /Users/wei/Desktop/pico
source .venv/bin/activate
pico-cli status
```

偶尔运行一次：

```bash
cd /Users/wei/Desktop/pico
uv run pico-cli status
```

长期作为本机工具使用：

```bash
uv tool install --editable /Users/wei/Desktop/pico --force
pico-cli status
```
