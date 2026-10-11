# open-totoro-token_linux

[open-totoro-token](https://github.com/SSSSSea6/open-totoro-token) 的 **Linux 移植 fork**：
上游脚本只用 Win32 调试 API，只能在 Windows 上跑；这个 fork 增加了 Linux 版
`memory_scanner_linux.py`，用 `/proc` 接口做同一件事，并保留上游的 Windows 脚本。

一个纯本地、零依赖的命令行小工具：从「龙猫体育锻炼」微信小程序**自己已经登录的进程**里读出
登录 token，打印出来给你复制。不联网、不上传、不提交任何东西。

| 平台 | 脚本 | 说明 |
| --- | --- | --- |
| Linux | `memory_scanner_linux.py` | 本 fork 新增，实测可用（需 root，见下方「权限」） |
| Windows | `memory_scanner.py` | 继承自上游，未改动 |

## 快速开始（Linux）

```bash
git clone git@github.com:feng-qi-yun/open-totoro-token_linux.git
cd open-totoro-token_linux

# 1) 先在微信里打开并登录「龙猫体育锻炼」小程序
# 2) 再跑（读别的进程内存需要 ptrace 权限，所以要 sudo）：
sudo python3 memory_scanner_linux.py
```

成功会打印 token 并尝试复制到剪贴板：

```
正在扫描本机的龙猫体育锻炼小程序进程...

Token:
WXXCX........（约 100 字符）

已复制到剪贴板，可以直接粘贴使用。
```

不需要 sudo 的自检（不必打开小程序，验证本机扫描链路是否正常）：

```bash
python3 memory_scanner_linux.py --selftest
```

其它选项：`--list`（只列出候选进程）、`--pid N`（只扫指定进程，便于排查）、`--verbose`。

## 原理

微信小程序运行在独立的 `WeChatAppEx` 进程里（一个小程序一个进程，这是微信自己的架构；
Windows 上是 `WeChatAppEx.exe`，Linux 版微信自带同一套运行时，宿主程序是
`<微信安装目录>/RadiumWMPF/runtime/WeChatAppEx`）。登录之后，token 以明文形式存在于这个
进程自己的内存中——小程序沙箱本身没有提供给用户查看它的方式。

这个工具用系统自带的调试/进程接口（Windows 上是 `OpenProcess` / `ReadProcessMemory` /
`VirtualQueryEx`，Linux 上是 `/proc/<pid>/maps` + `/proc/<pid>/mem`；和调试器、Cheat Engine
这类工具用的是同一套）把它读出来：

1. 找到承载「龙猫体育锻炼」小程序的 `WeChatAppEx` 进程（先按窗口标题匹配；标题匹配到的进程
   里如果没有 token，会继续尝试其它 `WeChatAppEx` 进程——小程序多进程架构下 token
   有时其实在没有可见窗口的兄弟进程里）。
2. 只读地遍历该进程已提交的内存区域，在里面找形如 `WXXCX` 开头、≥80 字符的 base64 字符串
   ——这是小程序后端的 token 格式。
3. 打印出来，并尝试复制到剪贴板（Windows 用自带的 `clip`，Linux 用已装的 `wl-copy` /
   `xclip` / `xsel`）；不发送到任何服务器。

全程只读，不写内存、不注入代码、不 hook，也不涉及破解或绕过任何加密——token 本身就是明文
存在的，这里只是把它读出来。

## 运行环境

**Linux**

- 官方 deb 版微信（`/opt/wechat`，自带 `RadiumWMPF/runtime/WeChatAppEx` 小程序运行时）
- Python 3.7+（只用标准库，`pip install` 什么都不用装）
- **需要 root**：Linux 上读 `/proc/<pid>/mem` 属于 ptrace 操作，在启用 Yama 限制的发行版上
  （`/proc/sys/kernel/yama/ptrace_scope = 1`，Ubuntu/Debian/Fedora 默认）只有 root 能读别的
  进程的内存，所以要用 `sudo` 运行（原因见下方「权限」）
- 可选：`xdotool`（装了就能像 Windows 版那样先按窗口标题定位，没装则直接扫所有
  `WeChatAppEx` 进程）；`wl-copy` / `xclip` / `xsel`（装了才能自动复制到剪贴板）

**Windows**（继承自上游）

- Windows
- Python 3.7+（只用标准库）
- 微信桌面客户端，且已经打开并登录了「龙猫体育锻炼」小程序（工具只读它自己的进程，不会帮你登录）

## 使用

### Linux

先在微信里打开并登录「龙猫体育锻炼」小程序，再跑：

```bash
sudo python3 memory_scanner_linux.py
```

失败会打印原因：没找到小程序进程、没读到 token（比如没登录），或者权限不足——权限不足时脚本
会直接给出可照做的处理办法。

### Windows（继承自上游）

```bash
python memory_scanner.py
```

## 两个平台的实现对应关系

| Windows（`memory_scanner.py`） | Linux（`memory_scanner_linux.py`） |
| --- | --- |
| `CreateToolhelp32Snapshot` + `QueryFullProcessImageNameW` | 遍历 `/proc/<pid>/exe`、`/proc/<pid>/comm` |
| `VirtualQueryEx` 遍历内存区域 | 解析 `/proc/<pid>/maps` 的可读段 |
| `OpenProcess` + `ReadProcessMemory` | `os.pread()` 读 `/proc/<pid>/mem` |
| `EnumWindows` 按窗口标题匹配 | 可选 `xdotool`（没装就跳过） |
| `clip.exe` | `wl-copy` / `xclip` / `xsel` |
| 不需要管理员权限 | **需要 root**（ptrace 权限模型不同） |

## 权限（Linux 必读）

Windows 上「同一用户会话下能打开自己的进程」就够，Linux 不是这样：读 `/proc/<pid>/mem`
要求对目标进程有 ptrace 权限，而 Yama 默认（`ptrace_scope=1`）只允许读取**自己子进程**的
内存。`WeChatAppEx` 是微信主进程 fork 出来的，不是本脚本的后代，所以：

1. 推荐：`sudo python3 memory_scanner_linux.py`（仍然是只读）
2. 或者临时放宽：`sudo sysctl -w kernel.yama.ptrace_scope=0`（用完记得改回 1）
3. 或者给一个专用解释器 `setcap cap_sys_ptrace+ep`（不推荐，等于给 python 常驻特权）

另外：如果微信是用 AUR 的 `wechat-universal-bwrap` 之类**带 bwrap 沙箱**的包装跑的，
`WeChatAppEx` 在独立的 PID 命名空间里，外面完全看不到，只能改用官方 deb 包或从命名空间内部
运行本脚本。

## 本 fork 相对上游的差异

- 新增 Linux 实现（进程发现 / 内存扫描 / 剪贴板全部换成 Linux 原生接口），Windows 脚本原样保留。
- 修掉了上游一个跨块读取的隐患：上游按 1MB 分块、块间重叠 4KB 读取，当一个 token 正好跨块、
  且前半段已 ≥80 字符时，被截断的半截串会先被当成命中，而 `extract_token()` 返回的是
  `tokens[0]`，于是可能返回一个**错的 token**。Linux 版改成把「末尾未闭合的连续 base64 串」
  留到下一块拼接后再判定，只输出完整 token；同时加上 `TOKEN_MAX_LEN`（4096）上限，避免把
  大段 base64 噪声当成 token。这段逻辑与平台无关，**同样建议回补到上游的
  `memory_scanner.py`**。
- Linux 版的进程路径匹配同时接受「可执行文件名是 `WeChatAppEx`」和「路径以
  `RadiumWMPF/runtime/WeChatAppEx` 结尾」两种形式，微信改目录或改进程名时更耐升级。

## 验证情况

- 真机跑通：微信 Linux 版 4.1.13.23（官方 deb）+ `sudo python3 memory_scanner_linux.py`，
  成功从 `WeChatAppEx` 进程内存中取出真实 token 并自动复制到剪贴板。
- `python3 memory_scanner_linux.py --selftest`：通过（自检会在子进程里放一个伪造 token，
  再走完整的 maps 解析 + 分块扫描 + token 匹配链路把它读回来）。
- 进程发现 + 内存扫描端到端：用一个进程名同为 `WeChatAppEx` 的测试程序验证通过。
- 跨块/长度阈值/去重的 12 个边界用例：全部通过。

## 许可证

MIT，见 [LICENSE](LICENSE)。上游项目同许可证，版权与署名归
[SSSSSea6/open-totoro-token](https://github.com/SSSSSea6/open-totoro-token) 原作者；本 fork
的改动同样以 MIT 发布。

## 上游推广信息（原文保留）

> #广告  nuaa.win   ，可实现扫码一键式跑步

（这行来自上游 README，非本 fork 的推广信息。）
