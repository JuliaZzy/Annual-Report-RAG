#!/usr/bin/env python3
"""把 pdfs 目录里的 PDF 逐个上传到知识库（解析、向量化、写入 Elasticsearch）。

用法（在项目根目录或任意目录都可以）:

    python3 backend/scripts/upload_pdfs.py --username 你的用户名

密码会在终端里询问，不会写进命令历史。已经在知识库里的文件会跳过，
中断后重新运行会从剩下的文件继续。
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF_DIR = ROOT / "pdfs"
LOG_PATH = Path(__file__).resolve().parent / "upload_pdfs.log"
MAX_MB = 30


def log(message: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + message
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


# macOS 系统代理会让本机 8001 的请求被转走，代理返回 502。本地接口始终直连。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request_json(url: str, method: str, token: str | None, body: bytes | None, headers: dict, timeout: int):
    req_headers = dict(headers)
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with OPENER.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw}
        return error.code, payload
    except urllib.error.URLError as error:
        raise ConnectionError(str(error.reason)) from error


def login(api: str, username: str, password: str) -> str:
    status, payload = request_json(
        f"{api}/login",
        "POST",
        None,
        json.dumps({"username": username, "password": password}).encode("utf-8"),
        {"Content-Type": "application/json"},
        timeout=30,
    )
    if status != 200 or "access_token" not in payload:
        detail = payload.get("detail", payload)
        raise SystemExit(f"登录失败（HTTP {status}）：{detail}")
    return payload["access_token"]


def existing_files(api: str, token: str) -> set[str]:
    status, payload = request_json(f"{api}/get_files", "GET", token, None, {}, timeout=30)
    if status == 401:
        raise PermissionError("登录已失效")
    if status != 200 or not isinstance(payload, list):
        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        raise RuntimeError(f"读取已上传文件失败（HTTP {status}）：{detail}")
    return {item.get("file_name", "") for item in payload}


def upload_one(api: str, token: str, path: Path, timeout: int):
    boundary = "----SaifBoundary" + uuid.uuid4().hex
    filename = path.name.replace('"', "")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = head + path.read_bytes() + tail
    return request_json(
        f"{api}/upload_files",
        "POST",
        token,
        body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout,
    )


def failure_text(payload) -> str:
    if not isinstance(payload, dict):
        return str(payload)
    detail = payload.get("detail", payload)
    if isinstance(detail, dict):
        failed = detail.get("failed_files")
        if failed:
            return "；".join(str(item) for item in failed)
        return json.dumps(detail, ensure_ascii=False)
    return str(detail)


def main() -> None:
    parser = argparse.ArgumentParser(description="逐个把 PDF 上传到知识库")
    parser.add_argument("--username", help="登录用户名")
    parser.add_argument("--password", help="登录密码；不填则会在终端里询问")
    parser.add_argument("--api", default="http://localhost:8001", help="后端地址")
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR, help="PDF 目录")
    parser.add_argument("--max-mb", type=float, default=MAX_MB, help="单个文件大小上限，单位 MB")
    parser.add_argument("--interval", type=float, default=3, help="两个文件之间停顿的秒数")
    parser.add_argument("--timeout", type=int, default=1800, help="单个文件的最长等待秒数")
    args = parser.parse_args()

    username = args.username or input("用户名: ").strip()
    password = args.password or getpass.getpass("密码: ")
    if not username or not password:
        raise SystemExit("需要用户名和密码")

    pdf_dir = args.pdf_dir.expanduser().resolve()
    if not pdf_dir.is_dir():
        raise SystemExit(f"找不到 PDF 目录: {pdf_dir}")

    files = sorted(path for path in pdf_dir.iterdir() if path.suffix.lower() == ".pdf" and path.is_file())
    if not files:
        raise SystemExit(f"{pdf_dir} 里没有 PDF")

    api = args.api.rstrip("/")
    token = login(api, username, password)
    done = existing_files(api, token)
    limit = int(args.max_mb * 1024 * 1024)

    pending = []
    skipped_size = []
    for path in files:
        if path.name in done:
            continue
        if path.stat().st_size > limit:
            skipped_size.append(path)
            continue
        pending.append(path)

    log(
        f"共 {len(files)} 个 PDF，已在知识库 {len(done)} 个，"
        f"超过 {args.max_mb:g}MB 跳过 {len(skipped_size)} 个，待上传 {len(pending)} 个"
    )
    for path in skipped_size:
        log(f"跳过（超过 {args.max_mb:g}MB）: {path.name}")

    ok = 0
    failed = 0
    for index, path in enumerate(pending, start=1):
        size_mb = path.stat().st_size / 1024 / 1024
        log(f"[{index}/{len(pending)}] 开始上传 {path.name}（{size_mb:.1f}MB）")
        started = time.time()
        try:
            status, payload = upload_one(api, token, path, args.timeout)
        except (ConnectionError, TimeoutError, OSError) as error:
            failed += 1
            log(f"失败 {path.name}: {error}")
        else:
            if status == 401:
                log("登录失效，正在重新登录")
                token = login(api, username, password)
                status, payload = upload_one(api, token, path, args.timeout)

            message = failure_text(payload)
            successful = payload.get("successful_files", []) if isinstance(payload, dict) else []
            if status == 200 and path.name in successful:
                ok += 1
                elapsed = time.time() - started
                log(f"完成 {path.name}，用时 {elapsed:.0f} 秒")
            elif "已存在" in message:
                log(f"跳过（已存在）: {path.name}")
            else:
                failed += 1
                log(f"失败 {path.name}: HTTP {status} {message}")

        if index < len(pending):
            time.sleep(args.interval)

    log(f"结束。本次成功 {ok} 个，失败 {failed} 个。日志: {LOG_PATH}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已手动停止。再次运行会跳过已经上传成功的文件。")
        sys.exit(130)
