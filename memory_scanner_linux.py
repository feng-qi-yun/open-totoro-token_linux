#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 本地进程内存扫描（Linux 版）：从「龙猫体育锻炼」微信小程序进程里读出登录 token。
#
# 这是 memory_scanner.py（Windows 版）的 Linux 移植，原理完全一致：
#   微信小程序运行在独立的 WeChatAppEx 进程里（一个小程序一个进程）。Linux 版微信同样带
#   这套运行时，宿主程序位于 <微信安装目录>/RadiumWMPF/runtime/WeChatAppEx（ELF 可执行文件，
#   没有 .exe 后缀）。登录后 token 以明文形式存在于它自己的内存中——小程序沙箱本身不提供
#   给用户查看的方式。这里用 Linux 标准的 /proc 接口把它读出来，纯只读、不写内存、不注入
#   代码、不 hook。
#
# Windows 版 → Linux 版的接口对应关系：
#   OpenProcess / ReadProcessMemory      →  /proc/<pid>/mem（os.pread 按段读）
#   VirtualQueryEx 遍历内存区域          →  解析 /proc/<pid>/maps 里的可读段
#   CreateToolhelp32Snapshot 枚举进程     →  遍历 /proc/<pid>/{exe,comm}
#   QueryFullProcessImageNameW 取进程名   →  readlink /proc/<pid>/exe
#   EnumWindows 按窗口标题匹配            →  可选：xdotool（装了才有，没装就跳过）
#   clip.exe 复制剪贴板                   →  wl-copy / xclip / xsel（装了哪个用哪个）
#
# ⚠ 权限：Linux 上读 /proc/<pid>/mem 属于 ptrace 操作。在启用 Yama 限制的发行版上
#   （/proc/sys/kernel/yama/ptrace_scope = 1，Ubuntu/Debian/Fedora 默认），只有目标进程的
#   祖先进程或 root 才能读取——WeChatAppEx 是微信主进程 fork 出来的，本脚本既不是它的祖先
#   也不是 root，因此**通常需要 sudo 运行**（或临时放宽 ptrace_scope）。Windows 版不需要
#   管理员权限，这是两个平台在权限模型上的本质差别。

import argparse
import os
import shutil
import subprocess
import sys
import time

# ── 常量 ─────────────────────────────────────────────────────────────
PROCESS_NAME = "wechatappex"                      # 进程名（小写比较，不含 .exe 后缀）
RUNTIME_SUFFIX = os.path.join("RadiumWMPF", "runtime", "WeChatAppEx")
TOKEN_PREFIX = b"WXXCX"
TOKEN_MIN_LEN = 80
TOKEN_MAX_LEN = 4096      # 真实 token 约 100~500 字符；比这更长的连续 base64 串是噪声，不是 token
CHUNK_SIZE = 1024 * 1024
WINDOW_TITLE_KEYWORD = "龙猫"

# token 尾部允许的 base64 字符（'+' '/' '=' 与字母数字）
_B64_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")

# 这些伪段在 /proc/<pid>/mem 里读不到，直接跳过（[stack] [heap] 等正常段要保留）
_UNREADABLE_MAPS = ("[vvar]", "[vsyscall]")


# ── 进程发现 ─────────────────────────────────────────────────────────
def _iter_pids():
    """遍历当前 PID 命名空间里所有进程号。"""
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            yield int(entry)


def _proc_exe(pid):
    """返回 pid 的可执行文件路径，失败返回空串。"""
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def _proc_comm(pid):
    """返回 pid 的 comm（内核记录的进程名），失败返回空串。"""
    try:
        with open(f"/proc/{pid}/comm", "rb") as f:
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _is_target_process(pid):
    """判断 pid 是不是承载小程序的那类进程（WeChatAppEx）。"""
    exe = _proc_exe(pid)
    if exe:
        if os.path.basename(exe).lower() == PROCESS_NAME:
            return True
        # 微信升级换目录 / 进程路径被改写时，按运行时相对路径兜底匹配
        if exe.endswith(RUNTIME_SUFFIX):
            return True
    if _proc_comm(pid).lower() == PROCESS_NAME:
        return True
    return False


def _window_pids_by_title(keyword):
    """尽力用 xdotool 找出窗口标题含 keyword 的窗口所属 pid（对应 Windows 版的
    EnumWindows + "龙猫" 标题匹配）。xdotool 没装、没有 X 显示、或调用失败都返回空列表——
    这只是个加速用的前置筛选，后续还有「扫所有 WeChatAppEx 进程」的兜底。
    """
    if not shutil.which("xdotool") or not os.environ.get("DISPLAY"):
        return []
    try:
        out = subprocess.run(["xdotool", "search", "--name", keyword],
                             capture_output=True, text=True, timeout=5, check=True).stdout
    except Exception:
        return []

    pids = []
    for wid in out.split():
        try:
            res = subprocess.run(["xdotool", "getwindowpid", wid],
                                 capture_output=True, text=True, timeout=5, check=True).stdout
        except Exception:
            continue
        pid_text = res.strip().splitlines()[0] if res.strip() else ""
        if pid_text.isdigit() and int(pid_text) not in pids:
            pids.append(int(pid_text))
    return pids


def _order_candidates(window_pids, all_pids):
    """合并去重：window_pids（标题匹配到的）在前，其余 WeChatAppEx 进程在后
    ——小程序多进程架构下 token 有时在没有可见窗口的兄弟进程里，只看标题匹配会漏掉它。
    """
    ordered = list(window_pids)
    seen = set(window_pids)
    for pid in all_pids:
        if pid not in seen:
            ordered.append(pid)
            seen.add(pid)
    return ordered


def _find_candidate_pids():
    """列出可能承载「龙猫体育锻炼」小程序的 WeChatAppEx 进程。"""
    hit = []
    for pid in _window_pids_by_title(WINDOW_TITLE_KEYWORD):
        # 与 Windows 版一致：只收进程名对得上的（窗口可能属于微信主进程而不是小程序进程）
        if _is_target_process(pid):
            hit.append(pid)
    return _order_candidates(hit, [p for p in _iter_pids() if _is_target_process(p)])


# ── 内存扫描 ─────────────────────────────────────────────────────────
def _readable_regions(pid):
    """解析 /proc/<pid>/maps，返回 [(起始地址, 长度)]，只保留可读且能真读的段。"""
    regions = []
    try:
        with open(f"/proc/{pid}/maps", "r") as f:
            for line in f:
                fields = line.split(None, 5)
                if len(fields) < 2:
                    continue
                span, perms = fields[0], fields[1]
                if "r" not in perms:
                    continue
                path = fields[5].strip() if len(fields) > 5 else ""
                if path in _UNREADABLE_MAPS:
                    continue
                start_text, _, end_text = span.partition("-")
                try:
                    start, end = int(start_text, 16), int(end_text, 16)
                except ValueError:
                    continue
                if end > start:
                    regions.append((start, end - start))
    except OSError:
        pass
    return regions


def _collect_tokens(data, tokens, seen):
    """在 data 里找出所有 WXXCX 开头、长度达标的 token（去重、保持出现顺序）。"""
    idx = 0
    while True:
        idx = data.find(TOKEN_PREFIX, idx)
        if idx == -1:
            return
        end = idx + len(TOKEN_PREFIX)
        while end < len(data) and data[end] in _B64_BYTES:
            end += 1
        token = data[idx:end].decode("ascii", "replace")
        if TOKEN_MIN_LEN <= len(token) <= TOKEN_MAX_LEN and token not in seen:
            seen.add(token)
            tokens.append(token)
        idx = end


def _trailing_run_start(buf):
    """返回 buf 末尾那段「连续 base64 字符」的起点下标（整段都是 base64 时返回 0）。"""
    i = len(buf)
    while i > 0 and buf[i - 1] in _B64_BYTES:
        i -= 1
    return i


def _scan_region(fd, start, size, tokens, seen):
    """按 CHUNK_SIZE 分块读取 [start, start+size)。

    跨块边界处理：不重复读重叠区，而是把「末尾还没闭合的连续 base64 串」留到下一块前面
    一起匹配（carry），等它真正被非 base64 字节或区域末尾终结时再判定。这样既能读出跨块
    的完整 token，也不会像「重叠读」那样把一个被截断的半截 token 也当成命中——半截串长度
    往往仍 ≥ TOKEN_MIN_LEN，一旦先入列，tokens[0] 就会返回错的 token。
    """
    carry = b""
    offset = 0
    while offset < size:
        read_size = min(CHUNK_SIZE, size - offset)
        try:
            data = os.pread(fd, read_size, start + offset)
        except OSError:
            break  # 该段实际读不了（如被内核特殊处理），跳过
        if not data:
            break
        offset += len(data)

        buf = carry + data
        carry = b""
        if offset < size:                       # 还没到区域末尾：末尾可能是半截 token，先留着
            run_start = _trailing_run_start(buf)
            if len(buf) - run_start <= TOKEN_MAX_LEN:
                carry = buf[run_start:]
                buf = buf[:run_start]
            # 超过 TOKEN_MAX_LEN 的连续 base64 串直接丢弃：它不可能是 token
        if buf:
            _collect_tokens(buf, tokens, seen)

    if carry:                                   # 读取被提前中断，末尾半截要按「到此为止」判定一次
        _collect_tokens(carry, tokens, seen)


def _scan_pid(pid):
    """扫描指定进程内存，返回 (tokens, error)。error 仅在「完全没法读」时非 None。"""
    try:
        fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    except OSError as exc:
        return [], f"{exc.strerror or exc}"

    tokens, seen = [], set()
    regions = _readable_regions(pid)
    try:
        for start, size in regions:
            _scan_region(fd, start, size, tokens, seen)
    finally:
        os.close(fd)

    if not tokens and not regions:
        return [], "没有读到任何可访问的内存区域"
    return tokens, None


def extract_tokens_from_pid(pid, verbose=False):
    """扫描指定进程内存，返回其中出现过的所有 WXXCX token（去重，原始出现顺序）。"""
    tokens, err = _scan_pid(pid)
    if err and verbose:
        print(f"[跳过] pid {pid}：{err}", file=sys.stderr)
    return tokens


def _permission_hint(pid):
    """拼一条能直接照着做的报错：为什么读不了 + 三种解法。"""
    lines = [f"没有权限读取进程（pid {pid}）的内存：ptrace 权限不足。"]

    scope = None
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope", "r") as f:
            scope = f.read().strip()
    except OSError:
        pass

    if os.geteuid() != 0:
        lines.append("・用 sudo 重新运行：sudo python3 memory_scanner_linux.py")
    if scope is not None:
        lines.append(f"・当前 Yama 限制：/proc/sys/kernel/yama/ptrace_scope = {scope}"
                     + ("（1 = 只允许读取自己子进程的内存，读别的进程需要 root）"
                        if scope == "1" else ""))
        if scope != "0":
            lines.append("・或临时放宽：sudo sysctl -w kernel.yama.ptrace_scope=0"
                         "（用完建议改回原来值）")
    else:
        lines.append("・本机没有 Yama 限制，仍失败说明是 SELinux/AppArmor 或容器隔离所致，"
                     "可尝试在宿主机的终端里以 root 运行本脚本")
    lines.append("・若微信跑在 bwrap/容器里（如 AUR 的 wechat-universal-bwrap），"
                 "小程序进程在独立 PID 命名空间里，外面看不到，需改用官方 deb 包或从命名空间内运行")
    return "\n".join(lines)


def extract_token(verbose=False):
    """扫描本机的龙猫体育锻炼小程序进程，取出登录 token；返回 {"ok", "token", "error"}。"""
    pids = _find_candidate_pids()
    if not pids:
        return {"ok": False, "token": None,
                "error": "未找到龙猫体育锻炼小程序进程（WeChatAppEx），"
                         "请先在微信中打开并登录龙猫体育锻炼小程序。"}

    if verbose:
        print(f"候选进程：{pids}", file=sys.stderr)

    perm_error = None
    for pid in pids:
        tokens, err = _scan_pid(pid)
        if tokens:
            return {"ok": True, "token": tokens[0], "error": None}
        if err and perm_error is None:
            perm_error = _permission_hint(pid)

    if perm_error:
        return {"ok": False, "token": None, "error": perm_error}

    return {"ok": False, "token": None,
            "error": "未读取到登录信息，请确认已在小程序内登录后重试。"}


# ── 剪贴板 ───────────────────────────────────────────────────────────
def _copy_to_clipboard(text):
    """尽力把 text 写进系统剪贴板，用系统里已有的 wl-copy / xclip / xsel，不引入第三方
    依赖。失败（都不存在/被拦截等）静默返回 False——token 已经打印在终端里了，复制只是
    锦上添花，不能因为这一步失败就让整个工具报错退出。
    """
    payload = text.encode("ascii", "replace")
    candidates = (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    )
    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=payload, check=True, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


# ── 命令行 ───────────────────────────────────────────────────────────
def _selftest():
    """自检：fork 一个子进程，把伪造 token 放进它内存，再按真实扫描路径读回来。

    父进程读自己的子进程在 Yama scope=1 下也是允许的，所以这个自检**不需要 root**，
    可以用来确认「/proc/<pid>/maps 解析 + /proc/<pid>/mem 分块扫描 + token 匹配」这条
    链路在本机是否正常（不需要真的开小程序）。
    """
    child_code = (
        "import sys, time\n"
        "holder = 'WXXCX' + 'A1b2C3d4' * 12\n"   # 挂在全局，保证留在堆里
        "sys.stdout.write(holder + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", child_code],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        time.sleep(1.0)
        expected = child.stdout.readline().strip()
        print(f"子进程 pid={child.pid}，伪造 token 长度 {len(expected)}")
        tokens, err = _scan_pid(child.pid)
        if err:
            print(f"[失败] 扫描自检子进程出错：{err}")
            return 1
        if expected in tokens:
            print(f"[通过] 在子进程内存里读到了伪造 token（共命中 {len(tokens)} 个候选）")
            return 0
        print("[失败] 没读到伪造 token"
              f"（命中 {len(tokens)} 个候选），分块/去重逻辑或 maps 解析有问题")
        return 1
    finally:
        child.kill()
        child.wait()
        if child.stdout:
            child.stdout.close()


def main():
    parser = argparse.ArgumentParser(
        description="从「龙猫体育锻炼」微信小程序进程内存中读出登录 token（Linux 版）")
    parser.add_argument("--pid", type=int, default=None,
                        help="只扫描指定的 pid（调试用，不做进程发现）")
    parser.add_argument("--list", action="store_true",
                        help="只列出候选的 WeChatAppEx 进程，不扫内存")
    parser.add_argument("--verbose", action="store_true", help="打印候选进程等调试信息")
    parser.add_argument("--selftest", action="store_true",
                        help="自检：扫一个自己 fork 的、放着伪造 token 的子进程（不需要 root）")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()

    if args.list:
        for pid in _find_candidate_pids():
            print(f"{pid}\t{_proc_exe(pid) or '(读不到 exe 路径)'}")
        return 0

    if args.pid is not None:
        tokens, err = _scan_pid(args.pid)
        if err:
            print(f"[失败] 扫描 pid {args.pid} 出错：{err}")
            print("\n" + _permission_hint(args.pid))
            return 1
        if tokens:
            print(f"pid {args.pid} 命中 {len(tokens)} 个 token，第一个：")
            print(tokens[0])
            return 0
        print(f"[失败] pid {args.pid} 里没有找到 token")
        return 1

    sys.stdout.reconfigure(encoding="utf-8")

    if os.geteuid() != 0:
        print("（提示：读别的进程内存通常需要 root，若下面报权限错误请用 sudo 重跑。）")

    print("正在扫描本机的龙猫体育锻炼小程序进程...")
    result = extract_token(verbose=args.verbose)

    if not result["ok"]:
        print(f"\n[失败] {result['error']}")
        return 1

    token = result["token"]
    print("\nToken:")
    print(token)
    if _copy_to_clipboard(token):
        print("\n已复制到剪贴板，可以直接粘贴使用。")
    else:
        print("\n（自动复制失败，请手动选中上面这行复制。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
