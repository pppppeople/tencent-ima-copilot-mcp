#!/usr/bin/env python3
"""
IMA Cookie 更新工具

用法:
  python update_cookie.py                          # 交互模式（推荐）
  python update_cookie.py --bkn 1234 --cookie "PLATFORM=..."
  python update_cookie.py --headers-file headers.txt
  pbpaste | python update_cookie.py --headers-file -
"""

import argparse
import os
import re
import sys
from pathlib import Path

ENV_FILE = Path(
    os.environ.get("IMA_ENV_FILE", Path.home() / ".claude/ima/.env")
).expanduser()


def update_env(bkn: str, cookie: str) -> None:
    content = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""

    if re.search(r"^IMA_X_IMA_BKN=", content, re.MULTILINE):
        content = re.sub(r"^IMA_X_IMA_BKN=.*", f"IMA_X_IMA_BKN={bkn}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\nIMA_X_IMA_BKN={bkn}\n"

    if re.search(r"^IMA_X_IMA_COOKIE=", content, re.MULTILINE):
        content = re.sub(r"^IMA_X_IMA_COOKIE=.*", f"IMA_X_IMA_COOKIE={cookie}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\nIMA_X_IMA_COOKIE={cookie}\n"

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ENV_FILE.write_text(content, encoding="utf-8")
    ENV_FILE.chmod(0o600)
    print(f"已更新 {ENV_FILE}")


def masked_value(value: str, head: int = 8, tail: int = 4) -> str:
    if len(value) <= head + tail + 4:
        return "***"
    return f"{value[:head]}...{value[-tail:]}"


def parse_headers_block(text: str) -> tuple[str | None, str | None]:
    """
    支持两种 DevTools 格式:
      格式一（标准 HTTP）: "x-ima-bkn: 1234"
      格式二（DevTools 复制）: 键名单独一行，值在下一行
    """
    bkn = None
    cookie = None
    lines = [line.rstrip() for line in text.splitlines()]

    i = 0
    while i < len(lines):
        line = lines[i]
        lower = line.strip().lower()

        # 格式一：key: value
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key == "x-ima-bkn":
                bkn = val
            elif key == "x-ima-cookie":
                cookie = val

        # 格式二：key 单独一行，值在下一行
        elif lower == "x-ima-bkn" and i + 1 < len(lines):
            bkn = lines[i + 1].strip()
            i += 1
        elif lower == "x-ima-cookie" and i + 1 < len(lines):
            cookie = lines[i + 1].strip()
            i += 1

        i += 1

    return bkn, cookie


def read_headers_file(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def parse_or_exit(text: str) -> tuple[str, str]:
    bkn, cookie = parse_headers_block(text)
    if not bkn or not cookie:
        missing = []
        if not bkn:
            missing.append("x-ima-bkn")
        if not cookie:
            missing.append("x-ima-cookie")
        print(f"解析失败，未找到: {', '.join(missing)}")
        print("请确认粘贴的是 IMA /cgi-bin/assistant/qa 请求里的完整 Request Headers。")
        sys.exit(1)
    return bkn, cookie


def apply_credentials(bkn: str, cookie: str, dry_run: bool = False) -> None:
    print("解析成功:")
    print(f"  x-ima-bkn   : {masked_value(bkn)}")
    print(f"  x-ima-cookie: {masked_value(cookie, 18, 8)}")
    if dry_run:
        print("dry-run 模式，未写入 .env。")
        return
    update_env(bkn, cookie)


def read_multiline_paste(prompt: str) -> str:
    """读取多行粘贴内容，以空行或 END 结束"""
    print(prompt)
    print("（粘贴完后，新起一行输入 END 回车，或直接按两次回车结束）")
    lines: list[str] = []
    empty_count = 0
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == "END":
            break
        if line.strip() == "":
            empty_count += 1
            if empty_count >= 2:
                break
            lines.append(line)
        else:
            empty_count = 0
            lines.append(line)
    return "\n".join(lines)


GUIDE = """
获取步骤（每次 token 过期后重复）:
  1. 打开 https://ima.qq.com 并登录
  2. 按 F12 打开开发者工具，切换到 Network（网络）标签
  3. 在 IMA 中随便发一条消息
  4. 在请求列表中找到 /cgi-bin/assistant/qa 的 POST 请求，点击它
  5. 切换到 Headers（标头）子标签
  6. 找到 Request Headers（请求标头）区域
  7. 复制以下两个字段的值:
       x-ima-bkn      → 一串数字，如 1010223773
       x-ima-cookie   → 很长的一串，以 PLATFORM=H5 开头
  8. 回到这里，按提示粘贴即可

提示: 选方式 1 可以直接把整块请求头粘进来，自动解析，更方便。
"""


def interactive_mode() -> None:
    print("=" * 56)
    print(" IMA Cookie 更新工具")
    print("=" * 56)
    print(GUIDE)
    print("选择输入方式:")
    print("  1  粘贴完整请求头（推荐，直接复制 DevTools 里的 Headers 区域）")
    print("  2  分别粘贴 x-ima-bkn 和 x-ima-cookie 两个值")
    print()

    choice = input("选择 [1/2，默认 1]: ").strip() or "1"

    if choice == "1":
        text = read_multiline_paste("\n请粘贴请求头内容:")
        bkn, cookie = parse_or_exit(text)
        print()
        apply_credentials(bkn, cookie)
        print()
        print("请重启 Claude Code（或 MCP 服务器）使更改生效。")
        return
    else:
        print()
        bkn = input("x-ima-bkn 值: ").strip()
        print("x-ima-cookie 值（粘贴后回车）: ")
        cookie = input().strip()

    if not bkn or not cookie:
        print("输入不完整，已取消。")
        sys.exit(1)

    print()
    apply_credentials(bkn, cookie)
    print()
    print("请重启 Claude Code（或 MCP 服务器）使更改生效。")


def main() -> None:
    parser = argparse.ArgumentParser(description="更新 IMA Cookie 认证信息")
    parser.add_argument("--bkn", help="x-ima-bkn 的值")
    parser.add_argument("--cookie", help="x-ima-cookie 的值")
    parser.add_argument("--headers-file", help="读取包含 x-ima-bkn/x-ima-cookie 的请求头文件；用 - 表示 stdin")
    parser.add_argument("--dry-run", action="store_true", help="只解析并显示结果，不写入 .env")
    args = parser.parse_args()

    if args.headers_file:
        if args.bkn or args.cookie:
            print("--headers-file 不能和 --bkn/--cookie 混用。")
            sys.exit(1)
        bkn, cookie = parse_or_exit(read_headers_file(args.headers_file))
        apply_credentials(bkn, cookie, dry_run=args.dry_run)
        if not args.dry_run:
            print("请重启 Claude Code（或 MCP 服务器）使更改生效。")
    elif args.bkn and args.cookie:
        apply_credentials(args.bkn, args.cookie, dry_run=args.dry_run)
        if not args.dry_run:
            print("请重启 Claude Code（或 MCP 服务器）使更改生效。")
    elif args.dry_run:
        print("--dry-run 需要搭配 --headers-file，或同时提供 --bkn 和 --cookie。")
        sys.exit(1)
    elif args.bkn or args.cookie:
        print("--bkn 和 --cookie 需要同时提供。")
        sys.exit(1)
    else:
        interactive_mode()


if __name__ == "__main__":
    main()
